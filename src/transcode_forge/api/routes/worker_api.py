"""Worker-facing HTTP API.

Workers connect with a token issued from the admin UI (Authorization:
Bearer <token>). They never touch the DB or Redis directly — the
scheduler proxies all state mutation. The job loop is a poll-based
HTTP API for simplicity (no WebSocket); the same control flow as
before, just routed through the network instead of shared storage.

Token endpoint policy: these routes are PUBLIC w.r.t. the admin auth
middleware (workers don't have admin sessions) but EVERY route
requires a valid bearer token. That's enforced via the
`require_worker_token` dependency.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from transcode_forge.api.deps import get_db
from transcode_forge.db import DBConnection
from transcode_forge.models.job import Job, JobStatus
from transcode_forge.models.worker import Worker, WorkerStatus
from transcode_forge.repos import jobs as job_repo
from transcode_forge.repos import worker_tokens as token_repo
from transcode_forge.repos import workers as worker_repo

logger = logging.getLogger(__name__)
router = APIRouter(tags=["worker"])


def _progress_channel(request: Request) -> str:
    """Redis channel for progress events.

    Derived from the configurable redis_prefix so this publisher and the
    WebSocket subscriber (web/websocket.py) can never drift apart.
    """
    settings = getattr(request.app.state, "settings", None)
    prefix = settings.redis_prefix if settings is not None else "tf"
    return f"{prefix}:pub:progress"


def _require_worker_identity(token_row: dict[str, Any], worker_id: str) -> None:
    """Reject callers claiming a worker_id other than the one bound to their token."""
    if token_row.get("worker_id") != worker_id:
        raise HTTPException(
            status_code=403,
            detail="worker_id does not match this token's registered worker",
        )


async def _require_owned_job(db: DBConnection, job_id: str, token_row: dict[str, Any]) -> Job:
    """Fetch a job and verify it is assigned to the calling worker.

    Guards against a stale worker (crashed or evicted, token still valid)
    mutating a job that has since been re-queued or claimed by another
    worker — without this, a slow in-flight /complete or /progress from
    the old owner would silently corrupt the new owner's job state.
    """
    job = await job_repo.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    owner = token_row.get("worker_id")
    if owner is None or job.worker_id != owner:
        raise HTTPException(status_code=403, detail="Job is not assigned to this worker")
    return job


async def require_worker_token(
    request: Request,
    authorization: str | None = Header(default=None),
    db: DBConnection = Depends(get_db),
) -> dict[str, Any]:
    """Dependency: validates the Authorization header and returns the token row."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    row = await token_repo.find_active(db, token)
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid or revoked token")
    request.state.worker_token_row = row
    request.state.worker_token = token
    await token_repo.touch(db, token)
    return row


# ── DTOs ──────────────────────────────────────────────────────────────


class RegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    host: str = Field(min_length=1, max_length=120)
    capabilities: list[str]
    # Workers predating codec advertisement omit this — default to hevc so
    # a rolling update never hands an old worker an AV1 job.
    supported_codecs: list[str] | None = None
    ffmpeg_version: str | None = None
    max_concurrent: int = Field(default=1, ge=1, le=8)


class HeartbeatRequest(BaseModel):
    worker_id: str
    status: str = Field(pattern=r"^(online|busy|offline)$")
    current_job_id: str | None = None


class ClaimRequest(BaseModel):
    worker_id: str


class ProgressRequest(BaseModel):
    progress: float = Field(ge=0.0, le=1.0)
    speed: float | None = None


class CompleteRequest(BaseModel):
    output_size: int = Field(ge=0)
    space_saved: int = Field(ge=0)
    source_size: int = Field(ge=0)
    # Encode outcome (None for workers predating the VMAF gate).
    achieved_vmaf: float | None = Field(default=None, ge=0.0, le=100.0)
    resolved_crf: int | None = Field(default=None, ge=0, le=63)
    backend_used: str | None = Field(default=None, pattern=r"^(qsv|nvenc|cpu)$")


# Worker-reported error messages can embed ffmpeg stderr; cap what we
# persist. Truncated server-side (not rejected with 422) so a worker
# carrying a huge message can still mark its job failed.
MAX_ERROR_MESSAGE_LEN = 10_000


class FailedRequest(BaseModel):
    error_message: str
    retry_count: int = Field(default=0, ge=0)


class SkippedRequest(BaseModel):
    """A skip outcome the worker decided (VMAF gate / size regression) —
    the original file was kept; this is not a retryable failure."""

    reason: str = Field(pattern=r"^(below_vmaf_floor|size_regression)$")
    error_message: str = ""
    achieved_vmaf: float | None = Field(default=None, ge=0.0, le=100.0)


class CheckDerivativeRequest(BaseModel):
    job_id: str
    derivative_key: str = Field(min_length=1, max_length=512)


class RegisterDerivativeRequest(BaseModel):
    derivative_key: str = Field(min_length=1, max_length=512)
    output_size: int = Field(ge=0)
    # Outcome attributes for the derivative row (complete arrives after
    # register-derivative, so the job row doesn't have these yet).
    achieved_vmaf: float | None = Field(default=None, ge=0.0, le=100.0)
    resolved_crf: int | None = Field(default=None, ge=0, le=63)
    backend_used: str | None = Field(default=None, pattern=r"^(qsv|nvenc|cpu)$")


# ── Routes ────────────────────────────────────────────────────────────


@router.post("/worker/register")
async def register(
    body: RegisterRequest,
    request: Request,
    db: DBConnection = Depends(get_db),
    token_row: dict[str, Any] = Depends(require_worker_token),
) -> dict[str, Any]:
    """Register or re-register a worker. Idempotent per token.

    First call with this token creates a row in `workers` and stamps
    its UUID into `worker_tokens.worker_id`. Subsequent calls update
    the existing row (e.g., capabilities changed after a driver update).
    """
    bound_id = token_row.get("worker_id")
    if bound_id is not None:
        # Re-register: keep the same worker_id if it still exists in workers.
        existing = await worker_repo.get_worker(db, bound_id)
        if existing is None:
            # Worker row was wiped but token still references it — recreate.
            bound_id = None

    worker_id = bound_id or str(uuid4())
    worker = Worker(
        id=worker_id,
        name=body.name,
        host=body.host,
        capabilities=body.capabilities,
        supported_codecs=body.supported_codecs or ["hevc"],
        ffmpeg_version=body.ffmpeg_version,
        max_concurrent=body.max_concurrent,
        status=WorkerStatus.ONLINE,
    )
    await worker_repo.upsert_worker(db, worker)

    if bound_id is None:
        await token_repo.link_worker(db, token_hash=token_row["token_hash"], worker_id=worker.id)

    # Release any jobs this worker had claimed before its restart. The
    # worker's process is fresh — it has no in-memory pipeline state for
    # jobs it owned previously, so those jobs would otherwise sit in
    # 'transcoding' forever. Re-queue them so anyone (including this
    # worker) can pick them up cleanly.
    cur = await db.execute(
        "UPDATE jobs SET status = ?, worker_id = NULL, started_at = NULL,"
        " progress = 0, updated_at = ? WHERE worker_id = ? AND status IN (?, ?)",
        (
            JobStatus.QUEUED.value,
            datetime.now(UTC).isoformat(),
            worker.id,
            JobStatus.ASSIGNED.value,
            JobStatus.TRANSCODING.value,
        ),
    )
    if cur.rowcount > 0:
        await db.commit()
        logger.info(
            "Worker %s came online — released %d orphan job(s) back to the queue",
            worker.id[:8],
            cur.rowcount,
        )

    return {
        "worker_id": worker.id,
        "name": worker.name,
        "redis_progress_channel": _progress_channel(request),
    }


@router.post("/worker/heartbeat", status_code=204)
async def heartbeat(
    body: HeartbeatRequest,
    db: DBConnection = Depends(get_db),
    token_row: dict[str, Any] = Depends(require_worker_token),
) -> None:
    _require_worker_identity(token_row, body.worker_id)
    await worker_repo.update_worker_heartbeat(
        db,
        worker_id=body.worker_id,
        status=WorkerStatus(body.status),
        current_job_id=body.current_job_id,
    )


@router.post("/worker/claim-job")
async def claim_job(
    body: ClaimRequest,
    request: Request,
    db: DBConnection = Depends(get_db),
    token_row: dict[str, Any] = Depends(require_worker_token),
) -> dict[str, Any]:
    """Atomically claim the next pending/queued job the worker can encode,
    respecting queue pause + scheduling windows. Returns the job plus
    library backend info and the scheduler-owned VMAF floor."""
    from transcode_forge.repos import libraries as library_repo
    from transcode_forge.repos import settings as settings_repo
    from transcode_forge.repos import system as system_repo

    _require_worker_identity(token_row, body.worker_id)

    if await system_repo.is_queue_paused(db):
        return {"job": None, "reason": "queue_paused"}

    worker = await worker_repo.get_worker(db, body.worker_id)
    supported_codecs = worker.supported_codecs if worker else ["hevc"]

    job = await job_repo.claim_next_job(db, body.worker_id, supported_codecs)
    if job is not None:
        # claim_next_job returns ASSIGNED; bump it to TRANSCODING here so the
        # job doesn't sit in ASSIGNED for the whole encode (which broke any
        # widget filtering by status='transcoding').
        from transcode_forge.models.job import JobStatus

        await job_repo.update_job(db, job.id, status=JobStatus.TRANSCODING)
        job = job.model_copy(update={"status": JobStatus.TRANSCODING})

        # Fetch the library to include backend + content info in the response.
        library = await library_repo.get_library(db, job.library)
        job_dict = job.model_dump(mode="json")
        if library:
            job_dict["_backend_type"] = library.get("backend", "filesystem")
            job_dict["_s3_bucket"] = library.get("s3_bucket", "")
            job_dict["_s3_prefix"] = library.get("s3_prefix", "")
            job_dict["_media_type"] = library.get("media_type", "")
        # The VMAF floor is scheduler-owned config (DB override else env) —
        # workers get it with the job so there's one source of truth.
        settings = getattr(request.app.state, "settings", None)
        job_dict["_vmaf_min_floor"] = float(
            await settings_repo.effective(db, "vmaf_min_floor", settings)
        )
        return {"job": job_dict}

    return {"job": None}


@router.post("/worker/job/{job_id}/check-derivative")
async def check_derivative(
    job_id: str,
    body: CheckDerivativeRequest,
    db: DBConnection = Depends(get_db),
    token_row: dict[str, Any] = Depends(require_worker_token),
) -> dict[str, Any]:
    """Check if a derivative already exists for this job's parameters.

    Used by S3-library workers to detect dedup/reuse opportunities.
    If a matching derivative is found, mark the job COMPLETE and return its size.

    Args:
        job_id: The job ID (from URL path).
        body.derivative_key: The content-addressed derivative key (from body).

    Returns:
        {
            "found": bool,
            "output_size": int (if found),
            "derivative_key": str (if found)
        }
    """
    from transcode_forge.models.job import JobStatus
    from transcode_forge.repos import derivatives as deriv_repo

    await _require_owned_job(db, job_id, token_row)

    existing = await deriv_repo.lookup_by_key(db, body.derivative_key)
    if existing:
        # Mark the job COMPLETE since we found a reusable derivative.
        await job_repo.update_job(
            db,
            job_id,
            status=JobStatus.COMPLETE,
            output_size=existing.get("output_size"),
            space_saved=0,  # S3 doesn't reclaim space.
            progress=1.0,
            completed_at=datetime.now(UTC).isoformat(),
        )
        logger.info(
            "Job %s marked COMPLETE via dedup (derivative_key=%s, output_size=%d)",
            job_id,
            body.derivative_key,
            existing.get("output_size", 0),
        )
        return {
            "found": True,
            "output_size": existing.get("output_size"),
            "derivative_key": body.derivative_key,
        }
    return {"found": False}


@router.post("/worker/job/{job_id}/progress", status_code=204)
async def progress(
    job_id: str,
    body: ProgressRequest,
    request: Request,
    db: DBConnection = Depends(get_db),
    token_row: dict[str, Any] = Depends(require_worker_token),
) -> None:
    await _require_owned_job(db, job_id, token_row)
    await job_repo.update_job(db, job_id, progress=body.progress)
    redis = getattr(request.app.state, "redis", None)
    if redis is not None:
        try:
            import json

            # The /workers and /queue pages key progress updates by
            # worker_id. The token row has it (set when the worker first
            # registered) — pass it through so the frontend can target
            # the right card.
            await redis.publish(
                _progress_channel(request),
                json.dumps(
                    {
                        "job_id": job_id,
                        "worker_id": token_row.get("worker_id"),
                        "progress": body.progress,
                        "speed": body.speed,
                    }
                ),
            )
        except Exception:  # nosec — Redis publish is best-effort
            logger.warning("Redis progress publish failed (UI progress may stall)", exc_info=True)


@router.post("/worker/job/{job_id}/complete", status_code=204)
async def complete_job(
    job_id: str,
    body: CompleteRequest,
    db: DBConnection = Depends(get_db),
    token_row: dict[str, Any] = Depends(require_worker_token),
) -> None:
    await _require_owned_job(db, job_id, token_row)
    await job_repo.update_job(
        db,
        job_id,
        status=JobStatus.COMPLETE,
        output_size=body.output_size,
        space_saved=body.space_saved,
        source_size=body.source_size,
        achieved_vmaf=body.achieved_vmaf,
        resolved_crf=body.resolved_crf,
        backend_used=body.backend_used,
        progress=1.0,
        completed_at=datetime.now(UTC).isoformat(),
    )


@router.post("/worker/job/{job_id}/skipped", status_code=204)
async def skip_job(
    job_id: str,
    body: SkippedRequest,
    db: DBConnection = Depends(get_db),
    token_row: dict[str, Any] = Depends(require_worker_token),
) -> None:
    """Record a worker-decided skip outcome (VMAF gate / size regression).

    The original file was kept — the job ends SKIPPED (not FAILED) with the
    measured score, and the file lands on the skipped page with its reason.
    """
    from transcode_forge.models.skipped import SkipReason
    from transcode_forge.repos import skipped as skip_repo

    job = await _require_owned_job(db, job_id, token_row)
    await job_repo.update_job(
        db,
        job_id,
        status=JobStatus.SKIPPED,
        error_message=body.error_message[:MAX_ERROR_MESSAGE_LEN] or None,
        achieved_vmaf=body.achieved_vmaf,
        completed_at=datetime.now(UTC).isoformat(),
    )
    await skip_repo.record_skip(
        db,
        file_path=job.source_path,
        library=job.library,
        codec=job.source_codec,
        resolution=job.source_resolution,
        file_size=job.source_size,
        skip_reason=SkipReason(body.reason),
    )
    logger.info("Job %s skipped by worker: %s (%s)", job_id, body.reason, body.error_message[:120])


@router.post("/worker/job/{job_id}/failed", status_code=204)
async def fail_job(
    job_id: str,
    body: FailedRequest,
    db: DBConnection = Depends(get_db),
    token_row: dict[str, Any] = Depends(require_worker_token),
) -> None:
    await _require_owned_job(db, job_id, token_row)
    await job_repo.update_job(
        db,
        job_id,
        status=JobStatus.FAILED,
        error_message=body.error_message[:MAX_ERROR_MESSAGE_LEN],
        retry_count=body.retry_count,
        completed_at=datetime.now(UTC).isoformat(),
    )


@router.post("/worker/job/{job_id}/register-derivative", status_code=204)
async def register_derivative(
    job_id: str,
    body: RegisterDerivativeRequest,
    db: DBConnection = Depends(get_db),
    token_row: dict[str, Any] = Depends(require_worker_token),
) -> None:
    """Register a derivative after S3 upload.

    Called by the worker after uploading a transcoded file to S3.
    Idempotent: if the derivative_key already exists, treat it as a win
    (dedup race condition).

    Args:
        job_id: The job ID.
        body.derivative_key: The content-addressed derivative key.
        body.output_size: Size of the derivative in bytes.
    """
    from transcode_forge.repos import derivatives as deriv_repo

    # Fetch the job (verifying ownership) to get library_id and source metadata.
    job = await _require_owned_job(db, job_id, token_row)

    # Check if the derivative already exists (dedup race).
    existing = await deriv_repo.lookup_by_key(db, body.derivative_key)
    if existing:
        logger.info(
            "Derivative %s already registered (dedup race or retry): %s",
            body.derivative_key,
            job_id,
        )
        return

    # Extract the job fields needed for derivative registration. The key
    # is goal-keyed; backend/crf/preset are recipe attributes on the row.
    source_resolution = getattr(job, "source_resolution", "") or None
    source_audio_codec = getattr(job, "source_audio_codec", "") or None
    target_resolution = getattr(job, "target_resolution", "") or (job.source_resolution or "")
    target_audio_codec = getattr(job, "target_audio_codec", "") or "copy"
    crf = body.resolved_crf if body.resolved_crf is not None else job.quality_value
    preset = getattr(job, "preset", "") or ""

    # Register the derivative. If a UNIQUE violation occurs (another worker
    # registered the same key), catch it and treat as a benign dedup win.
    try:
        await deriv_repo.create_derivative(
            db,
            library_id=job.library,
            source_key=job.source_path,  # For S3 backend, source_path is the S3 key
            source_path=job.source_path,
            source_resolution=source_resolution,
            source_audio_codec=source_audio_codec,
            target_resolution=target_resolution,
            target_audio_codec=target_audio_codec,
            target_codec=job.target_codec,
            target_vmaf=job.target_vmaf,
            achieved_vmaf=body.achieved_vmaf,
            backend=body.backend_used or "cpu",
            crf=int(crf) if crf else 0,
            preset=preset,
            derivative_key=body.derivative_key,
            output_size=body.output_size,
        )
        logger.info("Derivative registered: %s (job %s)", body.derivative_key, job_id)
    except Exception as e:
        # If it's a UNIQUE constraint violation, treat as benign dedup.
        # Otherwise, log and re-raise.
        error_msg = str(e).lower()
        if "unique" in error_msg or "constraint" in error_msg:
            logger.info("Derivative already registered (concurrent dedup): %s", body.derivative_key)
        else:
            logger.error("Failed to register derivative for job %s: %s", job_id, e)
            raise

"""HTTP-only worker — connects to the scheduler with a bearer token.

This replaces the older DB-direct worker for shareable installs. The
worker holds no credentials beyond the scheduler URL and its token;
everything else (job claiming, progress reporting, completion) flows
through the HTTP API in api/routes/worker_api.py.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import socket
import time
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx

from transcode_forge.config import Settings
from transcode_forge.models.job import Job, JobPhase
from transcode_forge.models.library import StorageBackendType
from transcode_forge.worker.hardware import HardwareCapabilities, detect_capabilities
from transcode_forge.worker.http_client import WorkerHttpClient
from transcode_forge.worker.outbox import Outbox
from transcode_forge.worker.pipeline import (
    PipelineError,
    SizeRegressionError,
    VmafGateError,
    run_pipeline,
)
from transcode_forge.worker.reliability import Backoff, ErrorClass, classify_error
from transcode_forge.worker.storage.filesystem import (
    RECOVERY_STALE_LOCK_SECONDS,
    lock_holder,
    recover_orphaned_backups,
    recover_source_path,
)
from transcode_forge.worker.vmaf import has_libvmaf

logger = logging.getLogger(__name__)

# Claim-time lock wait. A fresh foreign .tf_lock proves some pipeline is
# still refreshing it, whatever the scheduler currently believes about
# that worker: a partitioned worker keeps encoding (incident 2026-09-02,
# 44 min past its dead-marking), and a dead worker's lock ages out within
# the stale window. Either way the claimer WAITS for the lock to clear,
# re-running recovery every poll, and gives up only after twice the stale
# window so a worker slot is never held forever.
LOCK_WAIT_POLL_SECONDS = 30.0
LOCK_WAIT_MAX_SECONDS = 2 * RECOVERY_STALE_LOCK_SECONDS
SHUTDOWN_ABORT_MESSAGE = "Aborted by worker shutdown"


# Poison parking: a report the scheduler PERSISTENTLY refuses with a
# retryable error (a 500 from a server-side bug) must never be dropped —
# but after this many attempts it stops blocking the worker's claims and
# retries on a slow tick instead. Observed 2026-07-20: an int32 overflow
# in skipped_files.file_size 500'd one skip report forever; the drain-
# before-claim backpressure held BOTH Docker workers idle for ~2 days
# (attempt 5,176) while the retry storm flapped the Loki error alert.
POISON_ATTEMPTS = 25
POISON_COOLDOWN_S = 600.0


class DrainResult(StrEnum):
    """Outcome of one outbox drain pass."""

    EMPTY = "empty"  # everything delivered or terminally settled
    BLOCKED = "blocked"  # retryable failures remain — try again later
    AUTH_BLOCKED = "auth_blocked"  # entries kept behind a refused credential


class HttpWorkerAgent:
    """Worker process that talks to the scheduler over HTTP only."""

    def __init__(self, settings: Settings, server_url: str, token: str) -> None:
        self.settings = settings
        self.server_url = server_url
        self.worker_name = settings.worker_name or f"worker-{socket.gethostname()}"
        self.host = socket.gethostname()
        self._shutting_down = False
        self._abort_requested = False
        self._pipeline_task: asyncio.Task[dict[str, Any]] | None = None
        self.worker_id: str | None = None
        self._current_job_id: str | None = None
        self._current_progress: float = 0.0
        self.capabilities: HardwareCapabilities | None = None
        self._client: WorkerHttpClient = WorkerHttpClient(server_url, token)

        # Initialize scratch manager once for the worker's lifetime.
        from transcode_forge.worker.storage.scratch import ScratchManager

        scratch_root = Path(settings.scratch_dir or "/tmp/transcode-scratch")
        self.scratch_manager = ScratchManager(scratch_root)

        # Milestone outbox (worker-resilience spec D1): undelivered
        # terminal reports live here, under a dir every scratch cleanup
        # path spares. TF_WORKER_STATE_DIR overrides for installs whose
        # scratch is ephemeral but that still want restart-proof delivery.
        state_root = (
            Path(settings.worker_state_dir) if settings.worker_state_dir else scratch_root / "state"
        )
        self.outbox = Outbox(state_root / "outbox")
        self._claim_backoff = Backoff(base=2.0, cap=60.0)

    async def start(self) -> None:
        logging.basicConfig(
            level=getattr(logging, self.settings.log_level.upper(), logging.INFO),
            format=f"%(asctime)s [{self.worker_name}] %(levelname)s %(name)s: %(message)s",
        )
        logger.info("Starting HTTP worker %s → %s", self.worker_name, self.server_url)

        # Clean up orphaned scratch directories from previous runs.
        await self.scratch_manager.cleanup_orphans()

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._handle_shutdown)
            except NotImplementedError:
                # Windows: signal handlers via add_signal_handler not supported
                signal.signal(sig, lambda *_a: self._handle_shutdown())

        self.capabilities = await detect_capabilities()
        logger.info(
            "Capabilities: pairs=%s codecs=%s (preferred backend: %s)",
            self.capabilities.pairs,
            self.capabilities.supported_codecs,
            self.settings.preferred_backend,
        )
        if not await has_libvmaf():
            logger.warning(
                "ffmpeg on this worker has no libvmaf — the VMAF quality gate "
                "cannot run here. Update the worker image to restore it."
            )

        await self._drain_before_register()
        if self._shutting_down:
            return

        registration = await self._register_with_retry()
        self.worker_id = registration["worker_id"]
        logger.info("Registered as worker_id=%s", self.worker_id)

        # Crash recovery (filesystem backend only): a power loss inside the
        # pipeline's SWAP window leaves the original hidden as .tf_bak with a
        # stale .tf_lock blocking retries. Restore before claiming any jobs.
        # Runs after registration so locks written by this worker's previous
        # life (same token → same worker_id) are recognized as our own.
        await self._recover_filesystem_state()

        try:
            await asyncio.gather(
                self._heartbeat_loop(),
                self._job_loop(),
            )
        finally:
            await self._cleanup()

    def _recovery_roots(self) -> list[Path]:
        """Local media roots to swap-recovery-scan: the local sides of
        TF_PATH_MAP plus any configured TF_LIBRARY_* paths. Only directories
        that actually exist on this machine are returned — S3 jobs work in
        scratch space and are covered by the scratch manager instead."""
        candidates = list(self.settings.path_map.values())
        candidates += [path for path, _quality in self.settings.libraries.values()]
        roots: list[Path] = []
        for candidate in candidates:
            p = Path(candidate)
            if p.is_dir() and p not in roots:
                roots.append(p)
        return roots

    async def _recover_filesystem_state(self) -> None:
        """Run the filesystem swap-recovery scan (see storage/filesystem.py)."""
        if self.worker_id is None:
            raise RuntimeError("worker_id is unset — registration must succeed before this runs")
        roots = self._recovery_roots()
        if not roots:
            logger.info(
                "No local media roots found (TF_PATH_MAP / TF_LIBRARY_*) — "
                "skipping the swap-recovery scan"
            )
            return
        logger.info("Swap-recovery scan over %s", [str(r) for r in roots])
        await asyncio.to_thread(recover_orphaned_backups, roots, worker_id=self.worker_id)

    def _handle_shutdown(self) -> None:
        """Escalating shutdown: 1st signal drains (finish the current job),
        2nd aborts the in-flight encode ORDERLY (ffmpeg killed, job reported,
        loops exit), 3rd force-exits. The old two-stage version raised
        SystemExit straight out of the signal callback, which tore the event
        loop down around a still-running ffmpeg — the 2026-07-06 orphan."""
        if self._abort_requested:
            logger.warning("Force shutdown")
            raise SystemExit(1)
        if self._shutting_down:
            logger.warning("Second shutdown signal — aborting the current encode")
            self._abort_requested = True
            if self._pipeline_task is not None and not self._pipeline_task.done():
                self._pipeline_task.cancel()
            return
        logger.info("Shutdown requested — finishing current job (signal again to abort it)")
        self._shutting_down = True

    async def _heartbeat_loop(self) -> None:
        if self.worker_id is None:
            raise RuntimeError("worker_id is unset — registration must succeed before this runs")
        # Keep beating while a job is draining after the first shutdown
        # signal — otherwise the scheduler shows "heartbeat lost" (and may
        # treat the worker as dead) for the entire tail of the encode.
        while not self._shutting_down or self._current_job_id is not None:
            # The whole iteration is fenced (spec D2) — even the outbox
            # read below can raise (a stale state-dir mount) and a disk
            # hiccup must not take the gather() and the job loop with it.
            try:
                status = "busy" if self._current_job_id else "online"
                # While a job's terminal report is still undelivered, keep
                # NAMING that job: the reconciliation sweep (PR A) requeues a
                # live worker's job when its heartbeat disowns it past grace —
                # a delivery merely delayed by an outage must not read as
                # abandonment, or the sweep would re-run finished work.
                named_job = self._current_job_id or self.outbox.oldest_pending_job_id()
                await self._client.heartbeat(
                    worker_id=self.worker_id,
                    status=status,
                    current_job_id=named_job,
                )
            except Exception as e:
                logger.warning("Heartbeat failed (will retry): %r", e)
            await asyncio.sleep(self.settings.heartbeat_interval)

    async def _job_loop(self) -> None:
        if self.worker_id is None:
            raise RuntimeError("worker_id is unset — registration must succeed before this runs")
        while not self._shutting_down:
            # The whole iteration is fenced: no network fault, malformed
            # claim payload, or unexpected bug may exit this loop. A worker
            # process exits on SIGTERM/SIGINT and nothing else (spec D2).
            try:
                # Outbox fence: drain undelivered reports BEFORE claiming
                # any new work. Finished work's report outranks new work —
                # and a pending entry for a retried job id must resolve
                # before that job could ever be re-claimed here (a stale
                # attempt-1 report landing on attempt-2 is the
                # successful-job-marked-failed lie).
                if await self._drain_outbox() is not DrainResult.EMPTY:
                    await asyncio.sleep(self._claim_backoff.next_delay())
                    continue
                job_dict = await self._client.claim_job(worker_id=self.worker_id)
                self._claim_backoff.reset()
                if not job_dict:
                    await asyncio.sleep(2)
                    continue
                job = Job.model_validate(job_dict)
                # Claim-time extras (library backend, media type, VMAF floors)
                # ride on private attrs outside the validated model.
                for extra in ("_backend_type", "_s3_bucket", "_s3_prefix", "_media_type"):
                    if extra in job_dict:
                        object.__setattr__(job, extra, job_dict[extra])
                # Scheduler-stamped safety floors; a pre-decoupling scheduler
                # doesn't send them, so fall back to this worker's env defaults.
                # The legacy _vmaf_min_floor stamp is deliberately ignored — it
                # carries the old target-coupled bar this design retired.
                await self._process_job(
                    job,
                    safety_mean=job_dict.get("_vmaf_safety_mean", self.settings.vmaf_safety_mean),
                    safety_perc5=job_dict.get(
                        "_vmaf_safety_perc5", self.settings.vmaf_safety_perc5
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Job loop iteration failed: %r — backing off", e)
                await asyncio.sleep(self._claim_backoff.next_delay())

    def _resolve_backend(self, codec: str) -> str | None:
        """Best backend for the job's codec — preferred if capable, else
        qsv > nvenc > cpu, else None (worker can't encode this codec)."""
        if self.capabilities is None:
            return None
        return self.capabilities.best_backend_for(codec, self.settings.preferred_backend)

    async def _process_job(
        self,
        job: Job,
        safety_mean: float | None = None,
        safety_perc5: float | None = None,
    ) -> None:
        if self.worker_id is None:
            raise RuntimeError("worker_id is unset — registration must succeed before this runs")
        self._current_job_id = job.id
        self._current_progress = 0.0
        self._current_phase: str | None = None
        codec = job.target_codec or "hevc"
        backend = self._resolve_backend(codec)
        if backend is None:
            # The claim filter should make this unreachable; if capability
            # drifted (driver died between register and claim), fail loudly
            # rather than encoding with the wrong codec.
            logger.error("No backend for codec %s — failing job %s", codec, job.id)
            await self._deliver(
                job.id,
                "failed",
                {
                    "error_message": f"Worker has no backend capable of encoding {codec}",
                    "retry_count": job.retry_count + 1,
                },
            )
            self._current_job_id = None
            return
        logger.info(
            "Processing job %s: %s (%s→%s via %s)",
            job.id,
            job.source_path,
            job.source_codec,
            codec,
            backend,
        )

        # Construct the STORAGE backend for this job's library (distinct from
        # `backend`, the encoder hardware axis — shadowing the two crashed
        # every AV1 job fleet-wide on 2026-07-01).
        # The claim-job response includes backend_type, s3_bucket, s3_prefix.
        backend_type_str = getattr(job, "_backend_type", "filesystem")
        is_s3 = backend_type_str == StorageBackendType.S3 or backend_type_str == "s3"
        storage = await self._get_backend_for_job(job)

        # Fetch the source via the storage backend. For filesystem, this is a
        # no-op (returns the path-mapped path). For S3, this downloads to
        # scratch. TF_PATH_MAP only applies to filesystem paths — S3 keys are
        # bucket coordinates, not mount points.
        source_ref = job.source_path if is_s3 else self._translate_path(job.source_path)

        # Claim-time swap recovery (filesystem only — S3 masters are
        # immutable and scratch owns S3-side crash cleanup): heal crash
        # leftovers on this path before touching it. The startup scan
        # can't help when the crashed worker never comes back.
        if not is_s3:
            recovery, decline = await self._recover_source_path_waiting(job, Path(source_ref))
            if recovery in ("active", "aborted", "attention", "restore_failed"):
                messages = {
                    "attention": (
                        "A .tf_bak backup and a finished-looking media file both exist "
                        "for this source — refusing to encode over the backup. Verify "
                        "the media plays, delete the backup manually, then retry."
                    ),
                    "restore_failed": (
                        "Restoring the original from its .tf_bak backup FAILED — the "
                        "backup may be the only intact copy of this file. Do NOT "
                        "delete it; check the worker log for the restore error and "
                        "reconcile manually."
                    ),
                }
                logger.warning("Declining job %s (%s): %s", job.id, recovery, source_ref)
                await self._deliver(
                    job.id,
                    "failed",
                    {
                        "error_message": decline or messages[recovery],
                        # None of these are the file's fault — never burn a retry.
                        "retry_count": job.retry_count,
                    },
                )
                self._current_job_id = None
                await storage.cleanup(job)
                return
            if recovery in ("restored", "cleaned"):
                logger.warning("Claim-time recovery on %s: %s", source_ref, recovery)

        # Registry check BEFORE the download (S3-only). The derivative key is
        # computed from job-row fields alone, so a hit costs zero bucket
        # traffic: no HEAD, no download, no egress. Checking after fetch()
        # (the original order) skipped the encode and upload but still paid
        # the download the registry exists to avoid.
        if is_s3:
            dedup_result = await self._try_dedup(job, storage)
            if dedup_result:
                # Job was marked COMPLETE via dedup. Report completion with reused output size.
                await self._deliver(
                    job.id,
                    "complete",
                    {
                        "output_size": int(dedup_result["output_size"]),
                        "space_saved": 0,  # S3 doesn't reclaim space; derivative is new.
                        "source_size": int(job.source_size or 0),
                    },
                )
                logger.info(
                    "Job %s complete via dedup (reused %s)",
                    job.id,
                    dedup_result.get("derivative_key", "unknown"),
                )
                await storage.cleanup(job)
                self._current_job_id = None
                return

        try:
            source_path_local = await storage.fetch(source_ref)
        except Exception as e:
            logger.error("Failed to fetch source for job %s: %r", job.id, e)
            await self._deliver(
                job.id,
                "failed",
                {
                    "error_message": f"Failed to fetch source: {e!r}",
                    "retry_count": job.retry_count + 1,
                },
            )
            self._current_job_id = None
            return

        async def on_progress(progress: float, speed: float | None) -> None:
            self._current_progress = progress
            try:
                await self._client.progress(
                    job_id=job.id, progress=progress, speed=speed, phase=self._current_phase
                )
            except (httpx.HTTPError, OSError):
                logger.debug("Progress update failed", exc_info=True)

        async def on_phase(phase: str) -> None:
            # Phase transitions are worth a report even between ffmpeg
            # progress ticks — the search/gauge phases emit no progress at
            # all, and they're exactly the ones that used to look "stuck".
            self._current_phase = phase
            try:
                await self._client.progress(
                    job_id=job.id, progress=self._current_progress, speed=None, phase=phase
                )
            except (httpx.HTTPError, OSError):
                logger.debug("Phase update failed", exc_info=True)

        async def on_phase_progress(pct: float | None, detail: str | None) -> None:
            # Within-phase progress for the timed stations (gauge %, search
            # probe count) — display-grade and best-effort like on_progress.
            try:
                await self._client.progress(
                    job_id=job.id,
                    progress=self._current_progress,
                    speed=None,
                    phase=self._current_phase,
                    phase_pct=pct,
                    phase_detail=detail,
                )
            except (httpx.HTTPError, OSError):
                logger.debug("Phase-progress update failed", exc_info=True)

        media_type = getattr(job, "_media_type", "") or ""
        try:
            if self._abort_requested:
                # The abort signal landed while we were fetching — don't
                # start an hours-long encode we've been asked to abandon.
                raise asyncio.CancelledError
            # The pipeline runs as its own task so the shutdown handler can
            # cancel JUST the encode (run_encode kills its ffmpeg tree on
            # cancellation) without tearing down the whole agent.
            pipeline_task = asyncio.ensure_future(
                run_pipeline(
                    source_path=str(source_path_local),
                    codec=codec,
                    backend=backend,
                    quality=job.quality_value,
                    source_duration=job.source_duration or 0,
                    job_id=job.id,
                    worker_id=self.worker_id,
                    target_vmaf=job.target_vmaf,
                    vmaf_safety_mean=(
                        safety_mean if safety_mean is not None else self.settings.vmaf_safety_mean
                    ),
                    vmaf_safety_perc5=(
                        safety_perc5
                        if safety_perc5 is not None
                        else self.settings.vmaf_safety_perc5
                    ),
                    crf_search=self.settings.crf_search_enabled,
                    content="anime" if media_type == "anime" else None,
                    target_height=job.target_height,
                    progress_callback=on_progress,
                    phase_callback=on_phase,
                    phase_progress_callback=on_phase_progress,
                )
            )
            self._pipeline_task = pipeline_task
            result = await pipeline_task

            # Commit the output via the storage backend.
            # For filesystem: swap already happened in run_pipeline; commit() validates sizes.
            # For S3: upload the transcoded file to S3 (but don't register yet).
            # For filesystem, space_saved comes from the pipeline result (bak file size).
            space_saved = 0 if is_s3 else int(result.get("space_saved", 0))
            commit_result = await storage.commit(
                local_output=source_path_local,
                source=job.source_path,
                job=job,
                space_saved=space_saved,
            )

            # The outcome is DECIDED here — everything below is delivery,
            # and delivery goes through the outbox (journal first, then
            # attempt): a lost POST delays the report, never changes it.
            # For S3 the register_derivative → complete order is a
            # contract; appending both preserves it — the drain stops a
            # job's chain on a retryable failure and never reorders.
            if is_s3:
                derivative_key = self._derivative_key_for(job, source_path_local)
                await self._deliver(
                    job.id,
                    "register_derivative",
                    {
                        "derivative_key": derivative_key,
                        "output_size": int(commit_result.output_size),
                        "achieved_vmaf": result.get("vmaf_mean"),
                        "resolved_crf": result.get("resolved_crf"),
                        "backend_used": result.get("backend", backend),
                    },
                )

            await self._deliver(
                job.id,
                "complete",
                {
                    "output_size": int(commit_result.output_size),
                    "space_saved": int(commit_result.space_saved),
                    "source_size": int(result["source_size"]),
                    "achieved_vmaf": result.get("vmaf_mean"),
                    "achieved_vmaf_perc5": result.get("vmaf_perc5"),
                    "predicted_vmaf_mean": result.get("predicted_vmaf_mean"),
                    "predicted_vmaf_perc5": result.get("predicted_vmaf_perc5"),
                    "resolved_crf": result.get("resolved_crf"),
                    "backend_used": result.get("backend", backend),
                },
            )
            logger.info(
                "Job %s complete — saved %d bytes",
                job.id,
                int(commit_result.space_saved),
            )
        except asyncio.CancelledError:
            # Deliberate shutdown abort (second signal): the encoder's
            # managed subprocess has already killed its ffmpeg tree — report
            # the job so it doesn't strand in 'transcoding', then return so
            # the job loop can exit orderly. Cancellation we did NOT request
            # (event-loop teardown) must keep propagating.
            if not self._abort_requested:
                if self._pipeline_task is not None and not self._pipeline_task.done():
                    self._pipeline_task.cancel()
                raise
            logger.warning("Job %s aborted by shutdown", job.id)
            # The entry is journaled either way; the timeout only bounds
            # the opportunistic send so shutdown stays snappy — an
            # unsent abort report survives to the next boot's drain.
            try:
                await asyncio.wait_for(
                    self._deliver(
                        job.id,
                        "failed",
                        {
                            "error_message": SHUTDOWN_ABORT_MESSAGE,
                            # A shutdown abort is not the file's fault —
                            # never burn a retry on it.
                            "retry_count": job.retry_count,
                        },
                    ),
                    timeout=10.0,
                )
            except TimeoutError:
                logger.warning("Abort report for job %s journaled but not yet sent", job.id)
        except VmafGateError as e:
            await self._deliver(
                job.id,
                "skipped",
                {
                    "reason": "below_vmaf_floor",
                    "error_message": str(e),
                    "achieved_vmaf": e.vmaf_mean,
                    "achieved_vmaf_perc5": e.vmaf_perc5,
                    "predicted_vmaf_mean": e.predicted_vmaf_mean,
                    "predicted_vmaf_perc5": e.predicted_vmaf_perc5,
                    "resolved_crf": e.resolved_crf,
                    "backend_used": e.backend,
                },
            )
            logger.info("Job %s skipped (below VMAF floor): %s", job.id, e)
        except SizeRegressionError as e:
            await self._deliver(
                job.id,
                "skipped",
                {
                    "reason": "size_regression",
                    "error_message": str(e),
                    "predicted_vmaf_mean": e.predicted_vmaf_mean,
                    "predicted_vmaf_perc5": e.predicted_vmaf_perc5,
                    "resolved_crf": e.resolved_crf,
                    "backend_used": e.backend,
                },
            )
            logger.info("Job %s skipped (size regression)", job.id)
        except PipelineError as e:
            new_retry = job.retry_count + 1
            await self._deliver(
                job.id,
                "failed",
                {"error_message": str(e), "retry_count": new_retry},
            )
            logger.warning("Job %s failed (attempt %d): %s", job.id, new_retry, e)
        except Exception as e:
            # An unexpected bug must fail THIS JOB, not the agent — a crashing
            # agent restarts, re-registers (which releases the job), and the
            # next worker hits the same bug: a fleet-wide crash-loop.
            logger.exception("Unexpected error processing job %s", job.id)
            await self._deliver(
                job.id,
                "failed",
                {
                    "error_message": f"Unexpected worker error: {e!r}",
                    "retry_count": job.retry_count + 1,
                },
            )
        finally:
            self._pipeline_task = None
            self._current_job_id = None
            self._current_progress = 0.0
            await storage.cleanup(job)

    async def _recover_source_path_waiting(self, job: Job, source: Path) -> tuple[str, str | None]:
        """Claim-time recovery that WAITS on a fresh foreign lock instead
        of declining the job.

        The lock's freshness says a pipeline is still refreshing it; it
        says nothing about whether the scheduler can reach that worker.
        So the claimer re-runs recover_source_path every
        LOCK_WAIT_POLL_SECONDS until the lock clears (the pipeline
        finished) or ages out (its worker died), reporting what it waits
        on as job phase progress, and gives up after LOCK_WAIT_MAX_SECONDS.

        Returns (outcome, decline message). The outcome is
        recover_source_path's, with "active" meaning the cap passed, or
        "aborted" when a shutdown landed mid-wait; the message is set for
        those two outcomes and None otherwise.
        """
        if self.worker_id is None:
            raise RuntimeError("worker_id is unset: registration must succeed before this runs")
        started = time.monotonic()
        while True:
            recovery = await asyncio.to_thread(
                recover_source_path, source, worker_id=self.worker_id
            )
            if recovery != "active":
                return recovery, None
            if self._shutting_down or self._abort_requested:
                return "aborted", SHUTDOWN_ABORT_MESSAGE
            holder = await asyncio.to_thread(lock_holder, source)
            owner, age = holder if holder is not None else ("unknown", 0)
            waited = time.monotonic() - started
            if waited >= LOCK_WAIT_MAX_SECONDS:
                return "active", (
                    f"Source lock is still being refreshed by worker {owner[:8]} "
                    f"(last refresh {age} s ago) after waiting "
                    f"{int(LOCK_WAIT_MAX_SECONDS // 60)} min. That worker is still "
                    "running the pipeline on this file even if the scheduler lost "
                    "contact with it. Retry after it finishes or is stopped."
                )
            logger.info(
                "Job %s: waiting for the lock on %s held by worker %s, refreshed %d s ago "
                "(%d s waited so far)",
                job.id,
                source,
                owner[:8],
                age,
                waited,
            )
            try:
                # The scheduler caps phase_detail at 16 chars: owner id8 + age.
                await self._client.progress(
                    job_id=job.id,
                    progress=self._current_progress,
                    speed=None,
                    phase=JobPhase.WAIT,
                    phase_detail=f"{owner[:8]} {age}s",
                )
            except (httpx.HTTPError, OSError):
                logger.debug("Lock-wait progress update failed", exc_info=True)
            await asyncio.sleep(LOCK_WAIT_POLL_SECONDS)

    async def _register_with_retry(self) -> dict[str, Any]:
        """Register with the scheduler; retry transport/5xx forever.

        A scheduler restart must never kill fleet nodes, so retryable
        failures loop with capped backoff and a loud periodic ERROR. Any
        NON-retryable refusal — 401 revoked token, any other 4xx — is an
        operator problem and exits loudly; systemd Restart=always is the
        second belt, so even that never turns into a silently dead node.
        (Deliberately `is not RETRYABLE`, not an allowlist of terminal
        classes: a future ErrorClass member must not silently reopen the
        retry-forever-on-revoked-token gap the verify pass caught here.)
        """
        assert self.capabilities is not None  # start() sets it before this
        reg_backoff = Backoff(base=2.0, cap=60.0)
        attempt = 0
        while True:
            try:
                return await self._client.register(
                    name=self.worker_name,
                    host=self.host,
                    capabilities=self.capabilities.encoders,
                    supported_codecs=self.capabilities.supported_codecs,
                    # This build understands jobs.target_height (encoder scale,
                    # VERIFY height pin, gauge-at-target) — advertise it so the
                    # scheduler's claim filter hands us downscale jobs.
                    supports_downscale=True,
                    ffmpeg_version=self.capabilities.ffmpeg_version,
                    max_concurrent=self.settings.worker_max_concurrent,
                )
            except Exception as e:
                if classify_error(e) is not ErrorClass.RETRYABLE:
                    logger.error(
                        "Registration REJECTED (%r) — check TF_WORKER_TOKEN and "
                        "TF_SERVER_URL; exiting",
                        e,
                    )
                    raise
                attempt += 1
                delay = reg_backoff.next_delay()
                log = logger.error if attempt % 10 == 0 else logger.warning
                log("Registration attempt %d failed (%r) — retrying in %.1fs", attempt, e, delay)
                await asyncio.sleep(delay)

    # ── Milestone delivery (spec D1) ──────────────────────────────────

    async def _drain_before_register(self) -> None:
        """Deliver every journaled report from a previous life BEFORE
        re-registering, retrying until the outbox settles.

        Registration releases this worker's active jobs server-side, so a
        finished-but-unreported job would be requeued — and its later
        delivery refused as ownership-moved and discarded. One badly-timed
        scheduler blip at worker restart would silently lose finished work
        (the review-confirmed CRITICAL); a single best-effort pass is not
        enough. The deliberate cost: a worker with a stuck outbox stays
        UNREGISTERED (invisible) through a long scheduler outage — it
        couldn't claim anything anyway (the job loop's fence), and
        invisibility beats silent data loss.

        AUTH-blocked entries end the wait: registration will hit the same
        credential wall and exit loudly, and the entries stay journaled
        for the next boot. The token is already bound to our worker
        identity, so delivery itself needs no fresh registration.
        """
        drain_backoff = Backoff(base=1.0, cap=60.0)
        attempt = 0
        announced = False
        while not self._shutting_down:
            try:
                result = await self._drain_outbox()
            except Exception as e:
                result = DrainResult.BLOCKED
                logger.error("Outbox drain failed: %r", e)
            if result is DrainResult.EMPTY:
                return
            if result is DrainResult.AUTH_BLOCKED:
                logger.error(
                    "Outbox delivery is blocked on credentials — proceeding to "
                    "registration, which will surface the same auth failure loudly"
                )
                return
            if not announced:
                logger.info("Draining outbox from a previous run before registering")
                announced = True
            attempt += 1
            delay = drain_backoff.next_delay()
            log = logger.error if attempt % 10 == 0 else logger.warning
            log(
                "Outbox still has undelivered reports (attempt %d) — retrying in "
                "%.1fs before registering",
                attempt,
                delay,
            )
            await asyncio.sleep(delay)

    async def _deliver(self, job_id: str, kind: str, payload: dict[str, Any]) -> None:
        """Journal a milestone report, then attempt delivery now.

        Never raises: a report-path failure may DELAY delivery (the entry
        stays journaled and is drained before the next claim) but can
        never change a decided outcome or crash the job loop. The old
        inline client calls turned a lost /complete response into a
        FAILED report on a successful, already-swapped encode — that
        whole class dies at this seam.
        """
        try:
            self.outbox.append(job_id, kind, payload)
        except OSError:
            # The state disk refused the journal — degrade to one direct
            # send so reporting doesn't silently stop with the disk.
            logger.exception("Outbox append failed for job %s — attempting direct delivery", job_id)
            try:
                await self._send_report(job_id, kind, payload)
            except Exception as e:
                logger.error("Direct delivery of %s for job %s failed: %r", kind, job_id, e)
            return
        try:
            await self._drain_outbox()
        except Exception as e:
            logger.error("Outbox drain failed: %r", e)

    async def _drain_outbox(self) -> DrainResult:
        """One delivery pass over the outbox.

        Per-job chains are ordered: a RETRYABLE failure blocks the rest of
        that job's chain (an S3 complete never overtakes its
        register_derivative); other jobs' chains continue. A TERMINAL
        refusal (ownership moved to another worker, job deleted,
        conflicting outcome already recorded) discards the entry — the
        scheduler's copy of reality won — at WARN, or at ERROR for a 422,
        which would mean a version-skew/validation bug ate a real report
        and someone must hear about it. An AUTH refusal (401) KEEPS the
        entry and screams: a revoked token says nothing about the job's
        outcome, and losing a finished job's report to a credential
        rotation is worse than a loudly stuck outbox.
        """
        blocked: set[str] = set()
        auth_blocked = False
        for entry in self.outbox.entries():
            if entry.job_id in blocked:
                continue
            if (
                entry.attempts >= POISON_ATTEMPTS
                and time.time() - entry.last_attempt_at < POISON_COOLDOWN_S
            ):
                # Parked poison entry: still in its cooldown — keep the
                # per-job chain order but don't burn an attempt.
                blocked.add(entry.job_id)
                continue
            try:
                await self._send_report(entry.job_id, entry.kind, entry.payload)
            except Exception as e:
                cls = classify_error(e)
                if cls is ErrorClass.AUTH:
                    logger.error(
                        "Outbox: delivery of %s for job %s refused by AUTH (%r) — "
                        "keeping the entry; fix TF_WORKER_TOKEN",
                        entry.kind,
                        entry.job_id,
                        e,
                    )
                    self.outbox.bump_attempts(entry)
                    blocked.add(entry.job_id)
                    auth_blocked = True
                elif cls is ErrorClass.TERMINAL:
                    is_validation = (
                        isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 422
                    )
                    log = logger.error if is_validation else logger.warning
                    log(
                        "Outbox: scheduler refused %s for job %s (%r) — discarding, "
                        "its copy of reality wins",
                        entry.kind,
                        entry.job_id,
                        e,
                    )
                    self.outbox.delete(entry)
                else:
                    logger.warning(
                        "Outbox: delivery of %s for job %s failed (%r) — will retry (attempt %d)",
                        entry.kind,
                        entry.job_id,
                        e,
                        entry.attempts + 1,
                    )
                    self.outbox.bump_attempts(entry)
                    blocked.add(entry.job_id)
                continue
            self.outbox.delete(entry)
        remaining = self.outbox.entries()
        if not remaining:
            return DrainResult.EMPTY
        if auth_blocked:
            return DrainResult.AUTH_BLOCKED
        live = [
            e
            for e in remaining
            if e.attempts < POISON_ATTEMPTS or time.time() - e.last_attempt_at >= POISON_COOLDOWN_S
        ]
        if not live:
            # Only parked poison remains: report EMPTY so claims (and
            # startup registration) proceed — the entries stay journaled
            # and retry every cooldown tick. If the parked report is a
            # terminal outcome the scheduler later re-assigns, its
            # eventual delivery resolves via the idempotent-receipt
            # endpoints (duplicate → 204, conflict → 409 discard).
            logger.warning(
                "Outbox: %d poison entr%s parked (≥%d failed attempts) — "
                "retrying every %ds without blocking claims; the scheduler "
                "is persistently refusing these reports and needs a look",
                len(remaining),
                "y" if len(remaining) == 1 else "ies",
                POISON_ATTEMPTS,
                int(POISON_COOLDOWN_S),
            )
            return DrainResult.EMPTY
        return DrainResult.AUTH_BLOCKED if auth_blocked else DrainResult.BLOCKED

    async def _send_report(self, job_id: str, kind: str, payload: dict[str, Any]) -> None:
        """One delivery attempt of one milestone report (raises on failure)."""
        if kind == "complete":
            await self._client.complete(job_id=job_id, **payload)
        elif kind == "skipped":
            await self._client.skipped(job_id=job_id, **payload)
        elif kind == "failed":
            await self._client.failed(job_id=job_id, **payload)
        elif kind == "register_derivative":
            await self._client.register_derivative(job_id=job_id, **payload)
        else:
            raise ValueError(f"Unknown milestone kind: {kind!r}")

    def _translate_path(self, path: str) -> str:
        for linux_prefix, local_prefix in self.settings.path_map.items():
            if path.startswith(linux_prefix):
                return path.replace(linux_prefix, local_prefix, 1)
        return path

    def _derivative_key_for(self, job: Job, local_output: Path | str) -> str:
        """Goal-keyed derivative key for a job (D6): source identity +
        target codec/resolution/audio + target VMAF. Recipe details
        (backend/crf/preset) deliberately don't participate — any worker's
        gate-passing encode satisfies the same goal."""
        from transcode_forge.models.derivative import (
            compute_derivative_key,
            target_resolution_for,
        )

        return compute_derivative_key(
            source_path=job.source_path,
            source_resolution=job.source_resolution or "",
            source_audio_codec=getattr(job, "source_audio_codec", "") or "",
            # Height-keyed for downscale jobs (shared rule — the scheduler's
            # register-derivative row uses the same helper). Audio streams
            # are always copied.
            target_resolution=target_resolution_for(job.target_height, job.source_resolution),
            target_audio_codec=getattr(job, "target_audio_codec", "") or "copy",
            target_codec=job.target_codec or "hevc",
            target_vmaf=job.target_vmaf,
            local_output=Path(local_output),
        )

    async def _try_dedup(self, job: Job, backend: Any) -> dict[str, Any] | None:
        """Check for a reusable derivative (S3 only).

        Computes the goal-keyed derivative key and calls the scheduler API
        to check if it already exists. If found, the scheduler marks the
        job COMPLETE and returns the derivative info.

        Args:
            job: The job object.
            backend: The S3Backend (unused; kept for future extensibility).

        Returns:
            A dict with output_size and derivative_key if found, None otherwise.
        """
        derivative_key = self._derivative_key_for(job, Path(job.source_path))

        logger.debug("Checking for derivative: %s", derivative_key)

        try:
            result = await self._client.check_derivative(
                job_id=job.id, derivative_key=derivative_key
            )
            if result.get("found"):
                return result
        except (httpx.HTTPError, OSError) as e:
            logger.warning("Dedup check failed (will proceed with encode): %s", e)

        return None

    async def _get_backend_for_job(self, job: Job) -> Any:
        """Construct the storage backend for a job's library.

        The claim-job response includes backend_type, s3_bucket, s3_prefix
        in the job metadata. Construct the appropriate backend (Filesystem or S3).

        Args:
            job: The job object (from claim-job response).

        Returns:
            A StorageBackend implementation (FilesystemBackend or S3Backend).

        Raises:
            ValueError: If backend type is unsupported or required config is missing.
        """
        # Extract backend info from the job. The claim-job response includes these fields.
        backend_type_str = getattr(job, "_backend_type", "filesystem")
        s3_bucket = getattr(job, "_s3_bucket", "")
        s3_prefix = getattr(job, "_s3_prefix", "")

        if backend_type_str == StorageBackendType.S3 or backend_type_str == "s3":
            # S3 backend: reuse the per-worker scratch manager singleton.
            from transcode_forge.worker.storage.s3 import S3Backend

            if not s3_bucket:
                raise ValueError("S3 backend selected but s3_bucket not provided")

            # HTTP worker doesn't have DB access; the scheduler registers derivatives
            # via the register-derivative endpoint.
            backend = S3Backend(
                config=self.settings,
                db=None,  # type: ignore
                scratch_manager=self.scratch_manager,
                library_id=job.library,
                bucket=s3_bucket,
                prefix=s3_prefix,
            )
            return backend
        else:
            # Filesystem backend
            from transcode_forge.worker.storage.filesystem import FilesystemBackend

            return FilesystemBackend()

    async def _cleanup(self) -> None:
        logger.info("Worker shutting down")
        if self.worker_id is not None:
            try:
                await self._client.heartbeat(worker_id=self.worker_id, status="offline")
            except (httpx.HTTPError, OSError):
                pass
        await self._client.aclose()
        # Clean up scratch directories on shutdown.
        await self.scratch_manager.cleanup_on_shutdown()
        logger.info("Worker stopped")

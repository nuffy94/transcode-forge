"""Worker endpoints — monitor transcode workers."""

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from transcode_forge.api.deps import get_db
from transcode_forge.db import DBConnection
from transcode_forge.repos import workers as worker_repo

router = APIRouter(tags=["workers"])


@router.get("/workers")
async def list_workers(
    db: DBConnection = Depends(get_db),
) -> dict[str, Any]:
    """List all registered workers."""
    workers = await worker_repo.list_workers(db)
    return {
        "data": [w.model_dump(mode="json") for w in workers],
    }


@router.get("/workers/{worker_id}")
async def get_worker(
    worker_id: str,
    db: DBConnection = Depends(get_db),
) -> dict[str, Any]:
    """Get a single worker by ID."""
    worker = await worker_repo.get_worker(db, worker_id)
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")
    return {"data": worker.model_dump(mode="json")}


@router.delete("/workers/{worker_id}")
async def delete_worker(
    worker_id: str,
    db: DBConnection = Depends(get_db),
) -> dict[str, Any]:
    """Remove a worker registration.

    Safety gates (in order):
      - Heartbeat must be older than the stale threshold (30 min) — a
        worker that's heartbeating recently might still be doing work
        regardless of what its status field says.
      - No active jobs (transcoding/queued/pending) may still point at
        this worker. If there are, the orphan-job audit should re-queue
        or fail them first.

    These rules are intentionally heartbeat-driven, not status-driven —
    the status field can be stale (e.g. a 'busy' worker whose container
    crashed) but the heartbeat is the source of truth.
    """
    worker = await worker_repo.get_worker(db, worker_id)
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")

    if worker.last_heartbeat is not None:
        age_seconds = (datetime.now(UTC) - worker.last_heartbeat).total_seconds()
        if age_seconds < worker_repo.WORKER_STALE_THRESHOLD_SECONDS:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Worker heartbeated {int(age_seconds)}s ago — wait until it's "
                    f"silent for {worker_repo.WORKER_STALE_THRESHOLD_SECONDS}s, "
                    "or stop it manually first."
                ),
            )

    active = await worker_repo.count_active_jobs_for_worker(db, worker_id)
    if active > 0:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Worker still owns {active} active job(s). "
                "Re-queue or cancel them via the orphan-job audit first."
            ),
        )

    deleted = await worker_repo.delete_worker(db, worker_id)
    return {"deleted": deleted}

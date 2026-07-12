"""Job endpoints — query and manage transcode jobs."""

import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from transcode_forge.api.deps import get_db
from transcode_forge.db import DBConnection
from transcode_forge.models.job import JobStatus
from transcode_forge.repos import exclusions as excl_repo
from transcode_forge.repos import jobs as job_repo
from transcode_forge.repos import media as media_repo
from transcode_forge.repos import system as system_repo


def _toast(response: Response, message: str, kind: str = "info") -> None:
    """Set HX-Trigger header to show a toast in the UI."""
    response.headers["HX-Trigger"] = json.dumps({"showToast": {"message": message, "type": kind}})


router = APIRouter(tags=["jobs"])


@router.get("/jobs")
async def list_jobs(
    status: str | None = Query(None, description="Comma-separated job statuses to filter"),
    library: str | None = Query(None, description="Library name to filter"),
    worker: str | None = Query(None, description="Worker ID to filter"),
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    per_page: int = Query(50, ge=1, le=200, description="Results per page (max 200)"),
    db: DBConnection = Depends(get_db),
) -> dict[str, Any]:
    """List jobs with pagination and optional filters."""
    # Validate status values if provided
    if status:
        try:
            statuses = [s.strip() for s in status.split(",")]
            for s in statuses:
                if s and s not in JobStatus.__members__.values():
                    raise ValueError(s)
        except (ValueError, AttributeError) as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status. Valid values: {', '.join(JobStatus.__members__.keys())}",
            ) from exc

    offset = (page - 1) * per_page
    jobs, total = await job_repo.list_jobs(
        db, status=status, library=library, worker_id=worker, limit=per_page, offset=offset
    )
    return {
        "data": [j.model_dump(mode="json") for j in jobs],
        "meta": {"total": total, "page": page, "per_page": per_page},
    }


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    db: DBConnection = Depends(get_db),
) -> dict[str, Any]:
    """Get a single job by ID."""
    job = await job_repo.get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"data": job.model_dump(mode="json")}


@router.post("/jobs/{job_id}/retry")
async def retry_job(
    job_id: str,
    response: Response,
    db: DBConnection = Depends(get_db),
) -> dict[str, Any]:
    """Retry a failed job."""
    job = await job_repo.get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in (JobStatus.FAILED, JobStatus.CANCELLED):
        raise HTTPException(status_code=400, detail=f"Cannot retry job with status '{job.status}'")
    if await excl_repo.is_excluded(db, job.source_path):
        raise HTTPException(
            status_code=400,
            detail="This file is on the don't-try-again list. Remove the exclusion first.",
        )

    updated = await job_repo.update_job(
        db,
        job_id,
        status=JobStatus.PENDING,
        worker_id=None,
        progress=0,
        error_message=None,
        retry_count=job.retry_count + 1,
    )
    await media_repo.update_status_by_job(db, job_id, transcode_status="queued")
    _toast(response, "Job retried", "success")
    return {"data": updated.model_dump(mode="json") if updated else None}


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    response: Response,
    db: DBConnection = Depends(get_db),
) -> dict[str, Any]:
    """Cancel a pending or queued job."""
    job = await job_repo.get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in (JobStatus.PENDING, JobStatus.QUEUED):
        raise HTTPException(status_code=400, detail=f"Cannot cancel job with status '{job.status}'")

    updated = await job_repo.update_job(db, job_id, status=JobStatus.CANCELLED)
    await media_repo.update_status_by_job(db, job_id, transcode_status="needs_transcode")
    _toast(response, "Job cancelled", "warning")
    return {"data": updated.model_dump(mode="json") if updated else None}


@router.post("/queue/pause")
async def pause_queue(db: DBConnection = Depends(get_db)) -> dict[str, Any]:
    """Pause the queue — workers finish current job but don't pick up new ones."""
    await system_repo.set_queue_paused(db, True)
    return {"status": "paused"}


@router.post("/queue/resume")
async def resume_queue(db: DBConnection = Depends(get_db)) -> dict[str, Any]:
    """Resume the queue."""
    await system_repo.set_queue_paused(db, False)
    return {"status": "resumed"}


@router.get("/queue/status")
async def queue_status(db: DBConnection = Depends(get_db)) -> dict[str, Any]:
    """Get queue pause state."""
    paused = await system_repo.is_queue_paused(db)
    return {"paused": paused}


@router.post("/jobs/cancel-all")
async def cancel_all_pending(
    response: Response, db: DBConnection = Depends(get_db)
) -> dict[str, Any]:
    """Cancel all pending and queued jobs."""
    now = datetime.now(UTC).isoformat()
    # Catalog rows first, while the jobs still match the status filter.
    await db.execute(
        "UPDATE media_files SET transcode_status = 'needs_transcode',"
        " skip_reason = NULL, updated_at = ? WHERE job_id IN"
        " (SELECT id FROM jobs WHERE status IN ('pending', 'queued'))",
        (now,),
    )
    cur = await db.execute(
        "UPDATE jobs SET status = 'cancelled', updated_at = ?"
        " WHERE status IN ('pending', 'queued')",
        (now,),
    )
    await db.commit()
    _toast(response, "All pending jobs cancelled", "warning")
    return {"cancelled": cur.rowcount}


@router.post("/jobs/clear-completed")
async def clear_completed(response: Response, db: DBConnection = Depends(get_db)) -> dict[str, Any]:
    """Remove completed jobs from history."""
    cur = await db.execute("DELETE FROM jobs WHERE status IN ('complete', 'cancelled')")
    await db.commit()
    _toast(response, "Completed jobs cleared")
    return {"removed": cur.rowcount}


@router.delete("/jobs/reset")
async def reset_all_jobs(
    confirm: str = Query(..., description="Must be 'yes-delete-all' to confirm"),
    db: DBConnection = Depends(get_db),
) -> dict[str, Any]:
    """Delete ALL jobs. Requires confirmation query param."""
    if confirm != "yes-delete-all":
        raise HTTPException(
            status_code=400,
            detail="Must pass ?confirm=yes-delete-all to confirm destructive operation",
        )
    cur = await db.execute("DELETE FROM jobs")
    await db.commit()
    return {"removed": cur.rowcount}

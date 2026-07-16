"""Data-integrity audit endpoint.

Surfaces invariants the dashboard cannot — most importantly, jobs
stuck in active status whose worker disappeared. Useful for
post-deploy smoke checks, Prometheus scraping, and CI tests that hit
the live URL.
"""

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends

from transcode_forge.api.deps import get_db
from transcode_forge.db import DBConnection
from transcode_forge.repos import jobs as job_repo

router = APIRouter(tags=["audit"])


@router.get("/audit/integrity")
async def integrity_check(db: DBConnection = Depends(get_db)) -> dict[str, Any]:
    """Run integrity invariants. Empty job lists mean clean.

    Each orphan is a job claiming `transcoding`/`assigned`/`verifying`
    whose `worker_id` references a worker that is dead, offline, or
    no longer in the workers table at all. Each abandoned job's worker
    is ALIVE but has been heartbeating a different (or no) job past the
    reconciliation grace — its own heartbeat disowns it. Neither ever
    makes progress; both are auto-requeued by the scheduler sweep, so a
    non-empty list here means the sweep hasn't caught up (or is broken).
    """
    orphans = await job_repo.find_orphan_active_jobs(db)
    abandoned = await job_repo.find_abandoned_active_jobs(db)
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "orphan_active_jobs": orphans,
        "orphan_count": len(orphans),
        "abandoned_active_jobs": abandoned,
        "abandoned_count": len(abandoned),
        "healthy": len(orphans) == 0 and len(abandoned) == 0,
    }

"""Admin-facing token management.

GET    /api/worker-tokens          — list (token values are masked)
POST   /api/worker-tokens          — issue a new token (returned ONCE)
DELETE /api/worker-tokens          — revoke by token or fingerprint

These endpoints sit behind the admin auth middleware. The worker-side
endpoints are in worker_api.py and use bearer auth instead.
"""

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from transcode_forge.api.deps import get_db
from transcode_forge.db import DBConnection
from transcode_forge.repos import worker_tokens as token_repo
from transcode_forge.repos import workers as worker_repo

router = APIRouter(tags=["worker-tokens"])


class IssueRequest(BaseModel):
    label: str = Field(min_length=1, max_length=80)


class RevokeRequest(BaseModel):
    # Accept full token OR the masked fingerprint shown in the UI.
    token: str = Field(min_length=1)


@router.get("/worker-tokens")
async def list_tokens(db: DBConnection = Depends(get_db)) -> dict[str, Any]:
    rows = await token_repo.list_all(db)
    return {"data": rows, "meta": {"total": len(rows)}}


@router.post("/worker-tokens")
async def issue_token(body: IssueRequest, db: DBConnection = Depends(get_db)) -> dict[str, Any]:
    """Issue a new token. The raw value is returned ONCE — the UI shows
    it as a copy-paste prompt and never displays it again. Subsequent
    GETs only show the fingerprint."""
    token = await token_repo.create(db, label=body.label)
    return {
        "token": token,
        "label": body.label,
        "fingerprint": token[:6] + "…",
    }


@router.delete("/worker-tokens")
async def revoke_token(body: RevokeRequest, db: DBConnection = Depends(get_db)) -> dict[str, Any]:
    """Revoke a worker token, optionally cascading to the dead worker row.

    If the token was bound to a worker that's now dead and idle, also
    delete its registration. This makes 'I'm done with this machine' a
    one-click action: revoke the token, the worker row disappears too.

    Live workers (online/busy or holding a job) are left alone — their
    next API call will be 401'd by the now-revoked token, then they
    exit cleanly and become dead via heartbeat timeout.
    """
    # Revoke FIRST so the token can't be used to register a new worker
    # under the same row between our lookup and the cascade. The
    # subsequent get_worker re-reads the worker row, so even if some
    # other process re-registers, the staleness/active-job checks will
    # see the fresh state and refuse to delete a live worker.
    worker_id = await token_repo.find_worker_id_for_token(db, body.token)
    revoked = await token_repo.revoke(db, body.token)
    if not revoked:
        raise HTTPException(status_code=404, detail="Token not found or already revoked")

    worker_cleaned = False
    if worker_id:
        worker = await worker_repo.get_worker(db, worker_id)
        if worker and worker.last_heartbeat is not None:
            age = (datetime.now(UTC) - worker.last_heartbeat).total_seconds()
            stale = age >= worker_repo.WORKER_STALE_THRESHOLD_SECONDS
            no_active = await worker_repo.count_active_jobs_for_worker(db, worker_id) == 0
            if stale and no_active:
                worker_cleaned = await worker_repo.delete_worker(db, worker_id)

    return {"revoked": True, "worker_cleaned": worker_cleaned}

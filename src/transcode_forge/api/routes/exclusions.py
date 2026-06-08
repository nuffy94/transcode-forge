"""Excluded-path endpoints.

GET    /api/exclusions          — list everything that's been excluded
POST   /api/exclusions          — exclude a path
DELETE /api/exclusions          — lift an exclusion (path in body)
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from transcode_forge.api.deps import get_db
from transcode_forge.db import DBConnection
from transcode_forge.repos import exclusions as excl_repo

router = APIRouter(tags=["exclusions"])


class ExcludeRequest(BaseModel):
    path: str
    library: str | None = None
    reason: str | None = None


class UnexcludeRequest(BaseModel):
    path: str


@router.get("/exclusions")
async def list_exclusions(db: DBConnection = Depends(get_db)) -> dict[str, Any]:
    rows = await excl_repo.list_all(db)
    return {"data": rows, "meta": {"total": len(rows)}}


@router.post("/exclusions")
async def add_exclusion(body: ExcludeRequest, db: DBConnection = Depends(get_db)) -> dict[str, Any]:
    if not body.path.strip():
        raise HTTPException(status_code=400, detail="path cannot be empty")
    await excl_repo.add(db, body.path, library=body.library, reason=body.reason)
    return {"path": body.path, "excluded": True}


@router.delete("/exclusions")
async def remove_exclusion(
    body: UnexcludeRequest, db: DBConnection = Depends(get_db)
) -> dict[str, Any]:
    removed = await excl_repo.remove(db, body.path)
    return {"path": body.path, "excluded": False, "removed": removed}

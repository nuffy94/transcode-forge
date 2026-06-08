"""Skipped files endpoints — view and manage intentionally skipped files."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from transcode_forge.api.deps import get_db
from transcode_forge.db import DBConnection
from transcode_forge.models.skipped import SkipReason
from transcode_forge.repos import skipped as skip_repo

router = APIRouter(tags=["skipped"])


@router.get("/skipped")
async def list_skipped(
    library: str | None = Query(None, description="Library name to filter"),
    reason: str | None = Query(None, description="Skip reason to filter"),
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    per_page: int = Query(50, ge=1, le=200, description="Results per page (max 200)"),
    db: DBConnection = Depends(get_db),
) -> dict[str, Any]:
    """List skipped files with optional filters."""
    # Validate reason if provided
    if reason and reason not in SkipReason.__members__.values():
        raise HTTPException(
            status_code=400,
            detail=f"Invalid reason. Valid values: {', '.join(SkipReason.__members__.keys())}",
        )

    offset = (page - 1) * per_page
    files, total = await skip_repo.list_skipped(
        db, library=library, reason=reason, limit=per_page, offset=offset
    )
    return {
        "data": [f.model_dump(mode="json") for f in files],
        "meta": {"total": total, "page": page, "per_page": per_page},
    }


@router.get("/skipped/stats")
async def skipped_stats(
    library: str | None = None,
    db: DBConnection = Depends(get_db),
) -> dict[str, Any]:
    """Get skip reason breakdown counts."""
    counts = await skip_repo.skip_reason_counts(db, library=library)
    total = sum(counts.values())
    return {
        "data": counts,
        "meta": {"total": total},
    }


class UnskipRequest(BaseModel):
    file_path: str


@router.delete("/skipped")
async def unskip_file(
    body: UnskipRequest,
    db: DBConnection = Depends(get_db),
) -> dict[str, Any]:
    """Remove a file from the skipped list so it's picked up on next scan."""
    removed = await skip_repo.unskip(db, body.file_path)
    if not removed:
        raise HTTPException(status_code=404, detail="File not found in skipped list")
    return {"status": "removed", "file_path": body.file_path}

"""Stats endpoints — aggregate statistics."""

from typing import Any

from fastapi import APIRouter, Depends

from transcode_forge.api.deps import get_db
from transcode_forge.db import DBConnection

router = APIRouter(tags=["stats"])


@router.get("/stats")
async def get_stats(
    db: DBConnection = Depends(get_db),
) -> dict[str, Any]:
    """Aggregate stats: total jobs, space saved, per-library breakdown."""
    stats: dict[str, Any] = {}

    # Overall counts by status
    async with db.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status") as cursor:
        stats["jobs_by_status"] = {row[0]: row[1] for row in await cursor.fetchall()}

    # Space savings
    async with db.execute(
        "SELECT COUNT(*), COALESCE(SUM(space_saved), 0), "
        "COALESCE(SUM(source_size), 0), COALESCE(SUM(output_size), 0) "
        "FROM jobs WHERE status = 'complete'"
    ) as cursor:
        row = await cursor.fetchone()
        if row:
            stats["completed"] = row[0]
            stats["total_space_saved_bytes"] = row[1]
            stats["total_source_bytes"] = row[2]
            stats["total_output_bytes"] = row[3]

    # Per-library breakdown
    async with db.execute(
        "SELECT library, COUNT(*), COALESCE(SUM(space_saved), 0) "
        "FROM jobs WHERE status = 'complete' GROUP BY library"
    ) as cursor:
        stats["by_library"] = {
            row[0]: {"completed": row[1], "space_saved_bytes": row[2]}
            for row in await cursor.fetchall()
        }

    # Skipped file counts
    async with db.execute(
        "SELECT skip_reason, COUNT(*) FROM skipped_files GROUP BY skip_reason"
    ) as cursor:
        stats["skipped_by_reason"] = {row[0]: row[1] for row in await cursor.fetchall()}

    # Worker count
    async with db.execute("SELECT status, COUNT(*) FROM workers GROUP BY status") as cursor:
        stats["workers_by_status"] = {row[0]: row[1] for row in await cursor.fetchall()}

    return {"data": stats}

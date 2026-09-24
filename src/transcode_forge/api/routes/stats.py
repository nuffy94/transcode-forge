"""Stats endpoints — aggregate statistics."""

from typing import Any

from fastapi import APIRouter, Depends

from transcode_forge.api.deps import get_db
from transcode_forge.db import DBConnection
from transcode_forge.repos import jobs as job_repo
from transcode_forge.repos import libraries as lib_repo

router = APIRouter(tags=["stats"])


def format_size(num_bytes: int | None) -> dict[str, str]:
    """A byte total as display parts: GiB below 1 TiB, TiB from there.

    The one size formatter: web/routes.py registers it as the `size`
    Jinja filter, and /api/stats ships its output so the Activity strip
    shows the same string without a JS copy.
    """
    gib = (num_bytes or 0) / 1024**3
    if gib >= 1024:
        return {"value": f"{gib / 1024:.1f}", "unit": "TiB"}
    return {"value": f"{gib:.1f}", "unit": "GiB"}


def avg_savings_pct(source_bytes: int | None, output_bytes: int | None) -> int:
    """Whole-percent size reduction, rounded once here. The Stats page
    renders it and /api/stats ships it to the Activity strip, so the two
    can never round the same ratio differently."""
    if not source_bytes:
        return 0
    return max(0, round((1 - (output_bytes or 0) / source_bytes) * 100))


async def _by_library(db: DBConnection) -> dict[str, dict[str, Any]]:
    """Completed count and bytes saved per library, keyed by library id.
    The label is the library's current name, or the name the jobs were
    written under when the library is gone. Rows with no id are keyed
    "name:<snapshot>" so an old name can never shadow an id.
    """
    names = {lib["id"]: lib["name"] for lib in await lib_repo.list_libraries(db)}
    return {
        (lib_id or f"name:{snapshot}"): {
            "name": names.get(lib_id or "", snapshot),
            "completed": completed,
            "space_saved_bytes": saved,
        }
        for lib_id, snapshot, completed, saved in await job_repo.completed_by_library(db)
    }


@router.get("/stats")
async def get_stats(
    db: DBConnection = Depends(get_db),
) -> dict[str, Any]:
    """Aggregate stats: total jobs, space saved, per-library breakdown."""
    stats: dict[str, Any] = {}

    # Overall counts by status
    async with db.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status") as cursor:
        stats["jobs_by_status"] = {row[0]: row[1] for row in await cursor.fetchall()}

    # Space savings. CAST the sums to BIGINT: SUM() over a BIGINT column
    # returns `numeric` on Postgres, which asyncpg hands back as a Decimal
    # that serializes to a string like "1.0E+9" — the byte totals must stay
    # integers. (SQLite's BIGINT has integer affinity, so this is a no-op there.)
    async with db.execute(
        "SELECT COUNT(*), CAST(COALESCE(SUM(space_saved), 0) AS BIGINT), "
        "CAST(COALESCE(SUM(source_size), 0) AS BIGINT), "
        "CAST(COALESCE(SUM(output_size), 0) AS BIGINT) "
        "FROM jobs WHERE status = 'complete'"
    ) as cursor:
        row = await cursor.fetchone()
        if row:
            stats["completed"] = row[0]
            stats["total_space_saved_bytes"] = row[1]
            stats["total_space_saved_display"] = format_size(row[1])
            stats["total_source_bytes"] = row[2]
            stats["total_output_bytes"] = row[3]
            stats["avg_savings_pct"] = avg_savings_pct(row[2], row[3])

    # Per-library breakdown, keyed by library id (the name is the label).
    stats["by_library"] = await _by_library(db)

    # Skipped file counts
    async with db.execute(
        "SELECT skip_reason, COUNT(*) FROM skipped_files GROUP BY skip_reason"
    ) as cursor:
        stats["skipped_by_reason"] = {row[0]: row[1] for row in await cursor.fetchall()}

    # Worker count
    async with db.execute("SELECT status, COUNT(*) FROM workers GROUP BY status") as cursor:
        stats["workers_by_status"] = {row[0]: row[1] for row in await cursor.fetchall()}

    return {"data": stats}

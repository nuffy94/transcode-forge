"""Skipped files repository — tracks files intentionally not transcoded."""

from datetime import UTC, datetime
from uuid import uuid4

import aiosqlite

from transcode_forge.db import DBConnection
from transcode_forge.models.skipped import SkippedFile, SkipReason

# Valid skip reasons for filtering (whitelist validation)
_VALID_SKIP_REASONS = frozenset(reason.value for reason in SkipReason)

# Whitelist of sortable columns -> SQL column, so user input never reaches the
# ORDER BY clause directly.
_VALID_SKIP_SORTS = {
    "file_path": "file_path",
    "library": "library",
    "skip_reason": "skip_reason",
    "codec": "codec",
    "file_size": "file_size",
    "updated_at": "updated_at",
}


def _row_to_skipped(row: aiosqlite.Row) -> SkippedFile:
    """Convert a database row to a SkippedFile model."""
    data = dict(row)
    for field in ("created_at", "updated_at"):
        if data.get(field):
            data[field] = datetime.fromisoformat(data[field])
    return SkippedFile.model_validate(data)


async def record_skip(
    db: DBConnection,
    *,
    file_path: str,
    library: str,
    codec: str,
    resolution: str | None = None,
    file_size: int | None = None,
    skip_reason: SkipReason,
    scan_id: str | None = None,
) -> None:
    """Record a skipped file. Updates reason if file already tracked."""
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """INSERT INTO skipped_files (id, file_path, library, codec, resolution,
            file_size, skip_reason, scan_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(file_path) DO UPDATE SET
            codec = excluded.codec,
            resolution = excluded.resolution,
            file_size = excluded.file_size,
            skip_reason = excluded.skip_reason,
            scan_id = excluded.scan_id,
            updated_at = excluded.updated_at
        """,
        (
            str(uuid4()),
            file_path,
            library,
            codec,
            resolution,
            file_size,
            skip_reason.value,
            scan_id,
            now,
            now,
        ),
    )
    await db.commit()


async def list_skipped(
    db: DBConnection,
    *,
    library: str | None = None,
    reason: str | None = None,
    sort_by: str = "updated_at",
    sort_dir: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[SkippedFile], int]:
    """List skipped files with optional filters. Returns (files, total_count).

    Args:
        db: Database connection.
        library: Library name to filter by.
        reason: Skip reason to filter by (validated against SkipReason enum).
        sort_by: Column to sort by (validated against _VALID_SKIP_SORTS).
        sort_dir: Sort direction, 'asc' or 'desc'.
        limit: Maximum number of results to return.
        offset: Number of results to skip.

    Returns:
        Tuple of (list of SkippedFile objects, total count of matching files).

    Raises:
        ValueError: If an invalid skip reason is provided.
    """
    conditions: list[str] = []
    params: list[str | int] = []

    if library:
        conditions.append("library = ?")
        params.append(library)

    if reason:
        # Validate reason against the enum to prevent SQL injection
        if reason not in _VALID_SKIP_REASONS:
            raise ValueError(f"Invalid skip reason: {reason}. Valid reasons: {_VALID_SKIP_REASONS}")
        conditions.append("skip_reason = ?")
        params.append(reason)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    async with db.execute(f"SELECT COUNT(*) FROM skipped_files {where}", params) as cursor:
        row = await cursor.fetchone()
        total = row[0] if row else 0

    sort_col = _VALID_SKIP_SORTS.get(sort_by, "updated_at")
    direction = "DESC" if sort_dir.lower() != "asc" else "ASC"
    query = f"SELECT * FROM skipped_files {where} ORDER BY {sort_col} {direction} LIMIT ? OFFSET ?"
    async with db.execute(query, [*params, limit, offset]) as cursor:
        rows = await cursor.fetchall()
        return [_row_to_skipped(r) for r in rows], total


async def skip_reason_counts(db: DBConnection, *, library: str | None = None) -> dict[str, int]:
    """Get count of skipped files grouped by reason."""
    if library:
        query = (
            "SELECT skip_reason, COUNT(*) FROM skipped_files WHERE library = ? GROUP BY skip_reason"
        )
        params: tuple[str, ...] = (library,)
    else:
        query = "SELECT skip_reason, COUNT(*) FROM skipped_files GROUP BY skip_reason"
        params = ()

    async with db.execute(query, params) as cursor:
        rows = await cursor.fetchall()
        return {row[0]: row[1] for row in rows}


async def unskip(db: DBConnection, file_path: str) -> bool:
    """Remove a file from the skipped list. Returns True if found and removed."""
    cursor = await db.execute("DELETE FROM skipped_files WHERE file_path = ?", (file_path,))
    await db.commit()
    result: bool = cursor.rowcount > 0
    return result

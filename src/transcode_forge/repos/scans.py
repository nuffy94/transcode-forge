"""Scan repository — CRUD operations for library scans."""

from datetime import UTC, datetime

import aiosqlite

from transcode_forge.db import DBConnection
from transcode_forge.models.scan import Scan, ScanStatus


def _row_to_scan(row: aiosqlite.Row) -> Scan:
    """Convert a database row to a Scan model."""
    data = dict(row)
    for field in ("started_at", "completed_at"):
        if data.get(field):
            data[field] = datetime.fromisoformat(data[field])
    return Scan.model_validate(data)


async def create_scan(db: DBConnection, scan: Scan) -> str:
    """Insert a new scan record. Returns scan ID."""
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """INSERT INTO scans (id, library, files_found, files_new, files_updated,
            files_skipped, started_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (scan.id, scan.library, 0, 0, 0, 0, now, scan.status.value),
    )
    await db.commit()
    return scan.id


async def get_scan(db: DBConnection, scan_id: str) -> Scan | None:
    """Fetch a single scan by ID."""
    async with db.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)) as cursor:
        row = await cursor.fetchone()
        return _row_to_scan(row) if row else None


async def list_scans(
    db: DBConnection, *, limit: int = 20, offset: int = 0
) -> tuple[list[Scan], int]:
    """List scans with pagination. Returns (scans, total_count)."""
    async with db.execute("SELECT COUNT(*) FROM scans") as cursor:
        row = await cursor.fetchone()
        total = row[0] if row else 0

    async with db.execute(
        "SELECT * FROM scans ORDER BY started_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ) as cursor:
        rows = await cursor.fetchall()
        return [_row_to_scan(r) for r in rows], total


async def update_scan(
    db: DBConnection,
    scan_id: str,
    *,
    files_found: int | None = None,
    files_new: int | None = None,
    files_updated: int | None = None,
    files_skipped: int | None = None,
    status: ScanStatus | None = None,
) -> None:
    """Update scan progress fields."""
    fields: dict[str, object] = {}
    if files_found is not None:
        fields["files_found"] = files_found
    if files_new is not None:
        fields["files_new"] = files_new
    if files_updated is not None:
        fields["files_updated"] = files_updated
    if files_skipped is not None:
        fields["files_skipped"] = files_skipped
    if status is not None:
        fields["status"] = status.value
        if status in (ScanStatus.COMPLETE, ScanStatus.FAILED):
            fields["completed_at"] = datetime.now(UTC).isoformat()

    if not fields:
        return

    set_clause = ", ".join(f"{k} = ?" for k in fields)
    await db.execute(
        f"UPDATE scans SET {set_clause} WHERE id = ?",
        [*fields.values(), scan_id],
    )
    await db.commit()

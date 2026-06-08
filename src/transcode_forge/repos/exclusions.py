"""Excluded-path repository.

Operators flag a file with "don't try this again" — the path lands in
this table. Queue endpoint refuses to create jobs for it; retry refuses
to revive a job for it. The path stays excluded across DB resets, scans,
and library renames.
"""

from datetime import UTC, datetime

from transcode_forge.db import DBConnection


async def add(
    db: DBConnection,
    path: str,
    *,
    library: str | None = None,
    reason: str | None = None,
) -> None:
    """Mark a path as excluded. Idempotent — re-adding refreshes nothing."""
    now = datetime.now(UTC).isoformat()
    # ON CONFLICT DO NOTHING keeps the original created_at + reason intact.
    await db.execute(
        "INSERT INTO excluded_paths (path, library, reason, created_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(path) DO NOTHING",
        (path, library, reason, now),
    )
    await db.commit()


async def remove(db: DBConnection, path: str) -> bool:
    """Lift the exclusion. Returns True if a row was deleted."""
    cursor = await db.execute("DELETE FROM excluded_paths WHERE path = ?", (path,))
    await db.commit()
    return bool(cursor.rowcount)


async def is_excluded(db: DBConnection, path: str) -> bool:
    async with db.execute("SELECT 1 FROM excluded_paths WHERE path = ? LIMIT 1", (path,)) as cur:
        return (await cur.fetchone()) is not None


async def filter_excluded(db: DBConnection, paths: list[str]) -> set[str]:
    """Return the subset of `paths` that are excluded (one query)."""
    if not paths:
        return set()
    placeholders = ",".join("?" * len(paths))
    async with db.execute(
        f"SELECT path FROM excluded_paths WHERE path IN ({placeholders})", list(paths)
    ) as cur:
        return {row["path"] for row in await cur.fetchall()}


async def list_all(db: DBConnection) -> list[dict[str, object]]:
    """Return all excluded paths, newest first."""
    async with db.execute(
        "SELECT path, library, reason, created_at FROM excluded_paths ORDER BY created_at DESC"
    ) as cur:
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def count(db: DBConnection) -> int:
    async with db.execute("SELECT COUNT(*) FROM excluded_paths") as cur:
        row = await cur.fetchone()
        return row[0] if row else 0

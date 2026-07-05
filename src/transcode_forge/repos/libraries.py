"""Library repository — CRUD for configured media locations."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from transcode_forge.db import DBConnection


async def create_library(
    db: DBConnection,
    *,
    name: str,
    media_type: str,
    path: str,
    quality_preset: int = 21,
    enabled: bool = True,
    auto_scan: bool = False,
    scan_interval_hours: int = 24,
    backend: str = "filesystem",
    s3_bucket: str | None = None,
    s3_prefix: str | None = None,
) -> str:
    """Create a new library. Returns library ID."""
    lib_id = str(uuid4())
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """INSERT INTO libraries (id, name, media_type, path, quality_preset,
            enabled, auto_scan, scan_interval_hours, backend, s3_bucket,
            s3_prefix, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            lib_id,
            name,
            media_type,
            path,
            quality_preset,
            int(enabled),
            int(auto_scan),
            scan_interval_hours,
            backend,
            s3_bucket,
            s3_prefix,
            now,
            now,
        ),
    )
    await db.commit()
    return lib_id


async def get_library(db: DBConnection, lib_id: str) -> dict[str, Any] | None:
    async with db.execute("SELECT * FROM libraries WHERE id = ?", (lib_id,)) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def path_in_use(db: DBConnection, path: str) -> bool:
    """True if a library already exists for this path (unique constraint)."""
    async with db.execute("SELECT 1 FROM libraries WHERE path = ? LIMIT 1", (path,)) as cur:
        return (await cur.fetchone()) is not None


async def list_libraries(
    db: DBConnection, *, media_type: str | None = None, enabled_only: bool = False
) -> list[dict[str, Any]]:
    conditions: list[str] = []
    params: list[Any] = []
    if media_type:
        conditions.append("media_type = ?")
        params.append(media_type)
    if enabled_only:
        conditions.append("enabled = 1")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    async with db.execute(
        f"SELECT * FROM libraries {where} ORDER BY media_type, name", params
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


_VALID_LIBRARY_COLUMNS = frozenset(
    {
        "name",
        "media_type",
        "path",
        "quality_preset",
        "enabled",
        "auto_scan",
        "scan_interval_hours",
        "backend",
        "s3_bucket",
        "s3_prefix",
        "updated_at",
    }
)


async def update_library(db: DBConnection, lib_id: str, **fields: object) -> dict[str, Any] | None:
    """Update specific fields on a library. Only whitelisted columns allowed."""
    if not fields:
        return await get_library(db, lib_id)

    invalid_cols = set(fields.keys()) - _VALID_LIBRARY_COLUMNS - {"updated_at"}
    if invalid_cols:
        raise ValueError(f"Invalid library column names: {invalid_cols}")

    fields["updated_at"] = datetime.now(UTC).isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    await db.execute(
        f"UPDATE libraries SET {set_clause} WHERE id = ?",
        [*fields.values(), lib_id],
    )
    await db.commit()
    return await get_library(db, lib_id)


async def delete_library(db: DBConnection, lib_id: str) -> bool:
    cur = await db.execute("DELETE FROM libraries WHERE id = ?", (lib_id,))
    await db.commit()
    result: bool = cur.rowcount > 0
    return result

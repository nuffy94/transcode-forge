"""Derivative repository — CRUD for cached/reused transcoded outputs."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from transcode_forge.db import DBConnection


async def create_derivative(
    db: DBConnection,
    *,
    library_id: str,
    source_key: str,
    source_path: str,
    source_resolution: str | None,
    source_audio_codec: str | None,
    target_resolution: str,
    target_audio_codec: str,
    target_codec: str = "hevc",
    target_vmaf: float | None = None,
    achieved_vmaf: float | None = None,
    backend: str,
    crf: int,
    preset: str,
    derivative_key: str,
    output_size: int,
) -> str:
    """Create a new derivative record. Returns derivative ID.

    The derivative_key is goal-keyed (codec + quality goal); backend, crf,
    preset, and achieved_vmaf are recipe/outcome attributes on the row.

    Idempotent: if the derivative_key already exists (UNIQUE constraint),
    return the existing ID (treated as a benign dedup race win).

    Raises:
        Exception: On any error other than UNIQUE constraint violation.
    """
    deriv_id = str(uuid4())
    now = datetime.now(UTC).isoformat()
    try:
        await db.execute(
            """INSERT INTO derivatives
            (id, library_id, source_key, source_path, source_resolution,
             source_audio_codec, target_resolution, target_audio_codec,
             target_codec, target_vmaf, achieved_vmaf,
             backend, crf, preset, derivative_key, output_size, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                deriv_id,
                library_id,
                source_key,
                source_path,
                source_resolution,
                source_audio_codec,
                target_resolution,
                target_audio_codec,
                target_codec,
                target_vmaf,
                achieved_vmaf,
                backend,
                crf,
                preset,
                derivative_key,
                output_size,
                now,
            ),
        )
        await db.commit()
        return deriv_id
    except Exception as e:
        # Only a UNIQUE violation on derivative_key is a benign dedup race
        # (SQLite: 'UNIQUE constraint failed', asyncpg: 'unique constraint').
        # The bare word 'constraint' would also match FOREIGN KEY failures.
        error_msg = str(e).lower()
        if "unique" in error_msg:
            # Look up the existing row to get its ID.
            existing = await lookup_by_key(db, derivative_key)
            if existing is not None:
                return str(existing["id"])
        raise


async def lookup_by_key(db: DBConnection, derivative_key: str) -> dict[str, Any] | None:
    """Look up a derivative by its content-addressed key."""
    async with db.execute(
        "SELECT * FROM derivatives WHERE derivative_key = ?", (derivative_key,)
    ) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None

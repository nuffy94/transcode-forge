"""Schema migration runner.

Migrations are numbered SQL files (NNNN_name.sql) in this directory.
Each is applied at most once; the schema_migrations table records which
versions have run. New migrations append a higher number — never edit a
migration that's already been released.

Bootstrap: when an existing v0.4-or-earlier install starts up for the
first time after this lands, schema_migrations doesn't exist yet but
its tables do. The runner detects that and stamps every existing
migration as applied without re-running it, so the existing data is
not touched.

Dialect: migrations are written in SQLite-flavored SQL. On Postgres
the runner promotes specific INTEGER columns to BIGINT because file
sizes and bitrates routinely exceed 2 GB / 2 Gbit.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent
SCHEMA_VERSION_TABLE = "schema_migrations"

# INTEGER columns that hold byte counts or bitrates — promote to BIGINT on Postgres.
_BIGINT_COLUMNS = (
    "file_size",
    "bitrate",
    "source_size",
    "source_bitrate",
    "output_size",
    "space_saved",
)


class _Connectable(Protocol):
    """Minimal raw-connection protocol the runner uses (asyncpg or aiosqlite)."""

    async def execute(self, sql: str, *args: object) -> object: ...


def discover_migrations() -> list[tuple[int, str, str]]:
    """Return [(version, name, sql), ...] sorted by version."""
    files = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    out: list[tuple[int, str, str]] = []
    for f in files:
        match = re.match(r"(\d{4})_(.+)\.sql$", f.name)
        if not match:
            continue
        version = int(match.group(1))
        name = match.group(2)
        out.append((version, name, f.read_text(encoding="utf-8")))
    return out


def _adapt_for_postgres(sql: str) -> str:
    """Promote selected INTEGER columns to BIGINT for Postgres."""
    for col in _BIGINT_COLUMNS:
        sql = re.sub(rf"\b{col}\s+INTEGER\b", f"{col} BIGINT", sql)
    return sql


def _strip_line_comments(sql: str) -> str:
    """Drop -- single-line comments before naive ;-splitting for Postgres."""
    out: list[str] = []
    for line in sql.splitlines():
        comment = line.find("--")
        if comment >= 0:
            line = line[:comment]
        out.append(line)
    return "\n".join(out)


def _split_statements(sql: str) -> list[str]:
    """Split a migration into individual statements (by ;) for asyncpg.

    asyncpg's execute() takes one statement at a time. Strip line comments
    first so a banner comment doesn't get glued to the next CREATE.
    """
    sql = _strip_line_comments(sql)
    return [s.strip() for s in sql.split(";") if s.strip()]


async def apply_sqlite(conn: object, *, fresh: bool | None = None) -> None:
    """Apply pending migrations using an aiosqlite Connection."""
    aio = conn  # mypy convenience; aiosqlite.Connection
    await aio.execute(  # type: ignore[attr-defined]
        f"CREATE TABLE IF NOT EXISTS {SCHEMA_VERSION_TABLE} ("
        "version INTEGER PRIMARY KEY,"
        "name TEXT NOT NULL,"
        "applied_at TEXT NOT NULL)"
    )
    await aio.commit()  # type: ignore[attr-defined]

    if fresh is None:
        fresh = await _is_fresh_install_sqlite(aio)

    applied = await _applied_versions_sqlite(aio)
    migrations = discover_migrations()
    for version, name, sql in migrations:
        if version in applied:
            continue
        if not fresh and version == 1:
            # Pre-existing v0.4 install: tables already exist, just record v1 applied.
            await _record_applied_sqlite(aio, version, name)
            logger.info("Migration 0001_%s: stamped (existing install)", name)
            continue
        # aiosqlite handles multi-statement scripts + comments natively.
        await aio.executescript(sql)  # type: ignore[attr-defined]
        if name == "token_hash":
            await _backfill_token_hashes_sqlite(aio)
        await _record_applied_sqlite(aio, version, name)
        logger.info("Migration %04d_%s: applied", version, name)
    await aio.commit()  # type: ignore[attr-defined]


async def _backfill_token_hashes_sqlite(aio: object) -> None:
    """Hash any pre-existing plaintext worker tokens (migration 0004).

    Runs inside the same transaction as the ALTER, before the version is
    recorded — HMAC with the pepper can't be done in pure SQL.
    """
    # Lazy import: db -> migrations -> repos.worker_tokens -> db would cycle.
    from transcode_forge.repos.worker_tokens import fingerprint_prefix, hash_token

    cur = await aio.execute(  # type: ignore[attr-defined]
        "SELECT token FROM worker_tokens WHERE token_hash IS NULL AND token IS NOT NULL"
    )
    rows = await cur.fetchall()
    for row in rows:
        tok = row[0]
        await aio.execute(  # type: ignore[attr-defined]
            "UPDATE worker_tokens SET token_hash = ?, token_prefix = ? WHERE token = ?",
            (hash_token(tok), fingerprint_prefix(tok), tok),
        )
    if rows:
        logger.info("Backfilled %d existing worker-token hash(es)", len(rows))


async def _backfill_token_hashes_postgres(conn: object) -> None:
    """Postgres variant of the worker-token hash backfill (migration 0004)."""
    from transcode_forge.repos.worker_tokens import fingerprint_prefix, hash_token

    rows = await conn.fetch(  # type: ignore[attr-defined]
        "SELECT token FROM worker_tokens WHERE token_hash IS NULL AND token IS NOT NULL"
    )
    for row in rows:
        tok = row["token"]
        await conn.execute(  # type: ignore[attr-defined]
            "UPDATE worker_tokens SET token_hash = $1, token_prefix = $2 WHERE token = $3",
            hash_token(tok),
            fingerprint_prefix(tok),
            tok,
        )
    if rows:
        logger.info("Backfilled %d existing worker-token hash(es)", len(rows))


async def _is_fresh_install_sqlite(aio: object) -> bool:
    cur = await aio.execute(  # type: ignore[attr-defined]
        "SELECT name FROM sqlite_master WHERE type='table' AND name='jobs'"
    )
    row = await cur.fetchone()
    return row is None


async def _applied_versions_sqlite(aio: object) -> set[int]:
    cur = await aio.execute(  # type: ignore[attr-defined]
        f"SELECT version FROM {SCHEMA_VERSION_TABLE}"
    )
    rows = await cur.fetchall()
    return {row[0] for row in rows}


async def _record_applied_sqlite(aio: object, version: int, name: str) -> None:
    await aio.execute(  # type: ignore[attr-defined]
        f"INSERT INTO {SCHEMA_VERSION_TABLE} (version, name, applied_at) VALUES (?, ?, ?)",
        (version, name, datetime.now(UTC).isoformat()),
    )


async def apply_postgres(pool: object) -> None:
    """Apply pending migrations using an asyncpg pool."""
    async with pool.acquire() as conn:  # type: ignore[attr-defined]
        await conn.execute(
            f"CREATE TABLE IF NOT EXISTS {SCHEMA_VERSION_TABLE} ("
            "version INTEGER PRIMARY KEY,"
            "name TEXT NOT NULL,"
            "applied_at TEXT NOT NULL)"
        )
        fresh = await _is_fresh_install_postgres(conn)
        applied = await _applied_versions_postgres(conn)
        migrations = discover_migrations()
        for version, name, sql in migrations:
            if version in applied:
                continue
            if not fresh and version == 1:
                await _record_applied_postgres(conn, version, name)
                logger.info("Migration 0001_%s: stamped (existing install)", name)
                continue
            sql_pg = _adapt_for_postgres(sql)
            for stmt in _split_statements(sql_pg):
                await conn.execute(stmt)
            if name == "token_hash":
                await _backfill_token_hashes_postgres(conn)
            await _record_applied_postgres(conn, version, name)
            logger.info("Migration %04d_%s: applied", version, name)


async def _is_fresh_install_postgres(conn: object) -> bool:
    row = await conn.fetchrow(  # type: ignore[attr-defined]
        "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='jobs'"
    )
    return row is None


async def _applied_versions_postgres(conn: object) -> set[int]:
    rows = await conn.fetch(  # type: ignore[attr-defined]
        f"SELECT version FROM {SCHEMA_VERSION_TABLE}"
    )
    return {row["version"] for row in rows}


async def _record_applied_postgres(conn: object, version: int, name: str) -> None:
    await conn.execute(  # type: ignore[attr-defined]
        f"INSERT INTO {SCHEMA_VERSION_TABLE} (version, name, applied_at) VALUES ($1, $2, $3)",
        version,
        name,
        datetime.now(UTC).isoformat(),
    )

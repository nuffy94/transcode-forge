"""Database abstraction — supports both SQLite (dev/test) and PostgreSQL (production).

URL format:
  sqlite:///path/to/db.sqlite   (or just a plain path for backwards compat)
  postgresql://user:pass@host:5432/dbname?sslmode=require

Repos keep using ? placeholders — the PostgreSQL wrapper translates to $1,$2,...

PostgreSQL SSL: asyncpg parses the `sslmode` query parameter from the DSN
itself (require / verify-ca / verify-full / prefer / allow / disable), so the
URL is passed straight through with no manual SSL handling. Linode DBaaS
requires TLS — use `?sslmode=require`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Schema lives in src/transcode_forge/migrations/. The runner there is
# called from init_db. To change the schema, add a new numbered migration
# file — never edit a released migration.


# ── Protocol (what repos see) ─────────────────────────────────────────


@runtime_checkable
class DBConnection(Protocol):
    """Database connection interface used by all repositories."""

    dialect: str
    """'sqlite' or 'postgres' — lets repos opt into dialect-specific SQL."""

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any: ...

    async def commit(self) -> None: ...
    async def close(self) -> None: ...

    def transaction(self) -> AbstractAsyncContextManager[DBConnection]:
        """Group several statements into one atomic unit.

        Usage::

            async with db.transaction() as tx:
                await repo_a.write(tx, ...)
                await repo_b.write(tx, ...)
            # committed on success; rolled back if the block raises

        Within the block, intermediate ``commit()`` calls are deferred —
        the real commit (or rollback) happens once at block exit. On
        PostgreSQL the block pins a single pooled connection so the
        statements share one real transaction; on SQLite it defers the
        shared connection's commit.
        """
        ...


# ── SQLite implementation ─────────────────────────────────────────────


class _SqliteConnection:
    """Thin pass-through wrapper around aiosqlite.Connection."""

    dialect = "sqlite"

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        return self._conn.execute(sql, params)

    async def commit(self) -> None:
        await self._conn.commit()

    async def close(self) -> None:
        await self._conn.close()

    def transaction(self) -> AbstractAsyncContextManager[DBConnection]:
        return _SqliteTransaction(self._conn)


class _SqliteTransaction:
    """Defer the shared connection's commit until the block exits.

    SQLite (one process-wide connection) implicitly opens a transaction
    before the first write. We let writes accumulate, no-op intermediate
    ``commit()`` calls, then commit once on success or roll back on error.
    """

    dialect = "sqlite"

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> _SqliteTransaction:
        return self

    async def __aexit__(self, exc_type: Any, *_args: Any) -> None:
        if exc_type is not None:
            await self._conn.rollback()
        else:
            await self._conn.commit()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        return self._conn.execute(sql, params)

    async def commit(self) -> None:
        pass  # deferred to __aexit__

    async def close(self) -> None:
        pass  # the transaction does not own the shared connection

    def transaction(self) -> AbstractAsyncContextManager[DBConnection]:
        raise RuntimeError("nested transactions are not supported")


# ── PostgreSQL implementation ─────────────────────────────────────────


def _translate_placeholders(sql: str) -> str:
    """Convert ? placeholders to $1, $2, ... for asyncpg."""
    counter = 0

    def _replacer(_match: re.Match[str]) -> str:
        nonlocal counter
        counter += 1
        return f"${counter}"

    return re.sub(r"\?", _replacer, sql)


class _PgCursor:
    """Mimics aiosqlite cursor: both awaitable and async-context-manager.

    Runs on a pinned connection when one is supplied (inside a
    transaction); otherwise acquires a connection from the pool for the
    single statement and releases it (autocommit-per-statement).
    """

    def __init__(
        self,
        sql: str,
        params: Sequence[Any],
        *,
        pool: Any = None,
        conn: Any = None,
    ) -> None:
        self._pool = pool
        self._conn = conn
        self._sql = _translate_placeholders(sql)
        self._params = tuple(params)
        self._rows: list[Any] | None = None
        self._rowcount: int = 0
        self._executed = False

    async def _exec_on(self, conn: Any) -> None:
        upper = self._sql.strip().upper()
        if upper.startswith("SELECT") or "RETURNING" in upper:
            self._rows = await conn.fetch(self._sql, *self._params)
            self._rowcount = len(self._rows)
        else:
            status: str = await conn.execute(self._sql, *self._params)
            parts = status.split()
            if len(parts) >= 2 and parts[-1].isdigit():
                self._rowcount = int(parts[-1])

    async def _run(self) -> None:
        if self._executed:
            return
        self._executed = True
        if self._conn is not None:
            await self._exec_on(self._conn)
        else:
            async with self._pool.acquire() as conn:
                await self._exec_on(conn)

    async def __aenter__(self) -> _PgCursor:
        await self._run()
        return self

    async def __aexit__(self, *_args: object) -> None:
        pass

    def __await__(self) -> Any:
        return self._do_await().__await__()

    async def _do_await(self) -> _PgCursor:
        await self._run()
        return self

    async def fetchone(self) -> Any:
        if not self._executed:
            await self._run()
        if self._rows:
            return self._rows[0]
        return None

    async def fetchall(self) -> list[Any]:
        if not self._executed:
            await self._run()
        return self._rows or []

    @property
    def rowcount(self) -> int:
        return self._rowcount


class _PgConnection:
    """asyncpg pool wrapper that looks like aiosqlite.Connection.

    Outside a transaction, each statement runs on its own pooled
    connection and autocommits. Use ``transaction()`` to run several
    statements atomically on one pinned connection.
    """

    dialect = "postgres"

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _PgCursor:
        return _PgCursor(sql, tuple(params), pool=self._pool)

    async def commit(self) -> None:
        pass  # asyncpg auto-commits each statement outside a transaction

    async def close(self) -> None:
        await self._pool.close()

    def transaction(self) -> AbstractAsyncContextManager[DBConnection]:
        return _PgTransaction(self._pool)


class _PgTransaction:
    """Pin one pooled connection and run all statements in a single
    asyncpg transaction — commit on success, roll back on error."""

    dialect = "postgres"

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        self._conn: Any = None
        self._tx: Any = None

    async def __aenter__(self) -> _PgTransaction:
        self._conn = await self._pool.acquire()
        self._tx = self._conn.transaction()
        await self._tx.start()
        return self

    async def __aexit__(self, exc_type: Any, *_args: Any) -> None:
        try:
            if exc_type is not None:
                await self._tx.rollback()
            else:
                await self._tx.commit()
        finally:
            await self._pool.release(self._conn)
            self._conn = None
            self._tx = None

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _PgCursor:
        return _PgCursor(sql, tuple(params), conn=self._conn)

    async def commit(self) -> None:
        pass  # deferred to __aexit__

    async def close(self) -> None:
        pass  # the transaction does not own the pool

    def transaction(self) -> AbstractAsyncContextManager[DBConnection]:
        raise RuntimeError("nested transactions are not supported")


# ── Factory ───────────────────────────────────────────────────────────


def _is_postgres_url(url: str) -> bool:
    return url.startswith("postgres://") or url.startswith("postgresql://")


async def _init_sqlite(db_path: str) -> _SqliteConnection:
    import aiosqlite

    from transcode_forge.migrations import apply_sqlite

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(path))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA busy_timeout=5000")
    await apply_sqlite(conn)
    return _SqliteConnection(conn)


async def _init_postgres(url: str) -> _PgConnection:
    import asyncpg  # type: ignore[import-untyped]

    from transcode_forge.migrations import apply_postgres

    # asyncpg parses sslmode from the URL itself; pass the DSN straight through.
    pool = await asyncpg.create_pool(url, min_size=2, max_size=10)
    await apply_postgres(pool)
    logger.info("PostgreSQL schema initialized")
    return _PgConnection(pool)


async def init_db(db_url: str) -> DBConnection:
    """Create a database connection from a URL.

    Supports:
      postgresql://user:pass@host/db  -> asyncpg pool
      sqlite:///path.db               -> aiosqlite
      ./path.db  (bare path)          -> aiosqlite (backwards compat)
    """
    if _is_postgres_url(db_url):
        return await _init_postgres(db_url)
    path = db_url.removeprefix("sqlite:///").removeprefix("sqlite://")
    return await _init_sqlite(path)


async def close_db(db: DBConnection) -> None:
    """Gracefully close the database connection."""
    await db.close()


async def check_db_health(db: DBConnection) -> bool:
    """Return True if the database is accessible."""
    try:
        async with db.execute("SELECT 1") as cursor:
            row = await cursor.fetchone()
            return row is not None
    except Exception:
        return False

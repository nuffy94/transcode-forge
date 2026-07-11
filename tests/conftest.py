"""Shared test fixtures for Transcode Forge.

By default every test runs against a per-test temp SQLite database. Set
``TF_TEST_DB_URL`` to a ``postgresql://`` DSN (the CI ``test-postgres`` job
does this against a service container) and the same suite runs against real
Postgres instead — one shared database with the schema migrated once, and
every table TRUNCATEd before each test for isolation. This exercises the
asyncpg wrapper + placeholder translation + real transactions that
SQLite-only runs can't reach. Tests that genuinely can't run on Postgres
carry ``@pytest.mark.sqlite_only``.
"""

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from transcode_forge.config import Settings
from transcode_forge.db import DBConnection, init_db
from transcode_forge.main import create_app

_TEST_DB_URL = os.environ.get("TF_TEST_DB_URL", "")
USE_PG = _TEST_DB_URL.startswith("postgres")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip @pytest.mark.sqlite_only tests when targeting Postgres."""
    if not USE_PG:
        return
    skip = pytest.mark.skip(reason="sqlite_only: not applicable on Postgres")
    for item in items:
        if item.get_closest_marker("sqlite_only"):
            item.add_marker(skip)


async def _truncate_pg(conn: DBConnection) -> None:
    """Empty every app table (keeping schema_migrations) for test isolation."""
    async with conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
        "AND tablename <> 'schema_migrations'"
    ) as cur:
        rows = await cur.fetchall()
    names = [r["tablename"] for r in rows]
    if names:
        joined = ", ".join(f'"{n}"' for n in names)
        await conn.execute(f"TRUNCATE {joined} RESTART IDENTITY CASCADE")


# Defined ONLY when targeting Postgres. An autouse *async* fixture forces
# pytest-asyncio to run every test inside an event loop, which the sync
# Playwright tests in tests/qa/ (Runner.run) cannot tolerate — so under the
# default SQLite path this fixture must not exist at all, not merely no-op.
if USE_PG:

    @pytest_asyncio.fixture(autouse=True)
    async def _pg_reset() -> AsyncIterator[None]:
        """Before each Postgres test: apply migrations (idempotent — the
        first test creates the schema, the rest skip) and TRUNCATE every
        table for isolation. Autouse + function-scoped, so it runs before
        the db/app fixtures populate anything, and stays on the per-function
        event loop (no session-scoped-async-fixture loop pitfalls)."""
        conn = await init_db(_TEST_DB_URL)
        try:
            await _truncate_pg(conn)
        finally:
            await conn.close()
        yield


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> str:
    """The database URL for a test: the shared Postgres DSN under
    ``TF_TEST_DB_URL``, else a per-test temp SQLite file."""
    if USE_PG:
        return _TEST_DB_URL
    return f"sqlite:///{tmp_path / 'test.db'}"


@pytest_asyncio.fixture
async def db(tmp_db_path: str) -> Any:
    """A database connection with the schema initialized (SQLite temp file or
    the shared Postgres DB, freshly truncated by _pg_reset)."""
    conn: DBConnection = await init_db(tmp_db_path)
    yield conn
    await conn.close()


@pytest.fixture
def test_settings(tmp_db_path: str, tmp_path: Path) -> Settings:
    """Settings configured for testing (no real Redis, no real libraries)."""
    return Settings(
        redis_url="redis://localhost:6379/15",
        db_url=tmp_db_path,
        library_movies=str(tmp_path / "movies"),
        library_tv=str(tmp_path / "tv"),
        library_anime=str(tmp_path / "anime"),
    )


@pytest_asyncio.fixture
async def app(test_settings: Settings) -> Any:
    """Create a test FastAPI app with mocked Redis."""
    application = create_app(settings=test_settings)

    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock(return_value=True)
    mock_redis.aclose = AsyncMock()

    application.state.db = await init_db(test_settings.db_url)
    application.state.redis = mock_redis
    application.state.settings = test_settings

    yield application

    await application.state.db.close()


@pytest_asyncio.fixture
async def client(app: Any) -> AsyncClient:
    """Async HTTP test client. Auto-creates an admin and logs in so
    individual tests don't have to. Tests that need to verify auth
    behavior should use `unauthed_client` instead.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        # First-run setup creates the admin and logs the caller in.
        resp = await c.post("/api/auth/setup", json={"password": "test-pwd-12345"})
        if resp.status_code == 409:
            # Admin already exists in this DB — log in normally
            await c.post("/api/auth/login", json={"password": "test-pwd-12345"})
        yield c


@pytest_asyncio.fixture
async def unauthed_client(app: Any) -> AsyncClient:
    """Same client but with no admin and no session — for testing the
    auth middleware itself."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

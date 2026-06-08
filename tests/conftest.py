"""Shared test fixtures for Transcode Forge."""

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from transcode_forge.config import Settings
from transcode_forge.db import DBConnection, init_db
from transcode_forge.main import create_app


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> str:
    """Return a temporary SQLite database URL."""
    return f"sqlite:///{tmp_path / 'test.db'}"


@pytest_asyncio.fixture
async def db(tmp_db_path: str) -> Any:
    """Create a temporary SQLite database with schema initialized."""
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

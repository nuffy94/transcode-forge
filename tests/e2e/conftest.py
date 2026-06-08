"""E2E test fixtures — starts a real FastAPI server for Playwright."""

import asyncio
import threading
from collections.abc import AsyncIterator, Generator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
import uvicorn
from fastapi import FastAPI

from transcode_forge.config import Settings
from transcode_forge.db import init_db

E2E_PORT = 18765
E2E_BASE_URL = f"http://localhost:{E2E_PORT}"


@asynccontextmanager
async def _test_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Minimal lifespan for E2E tests — no Redis, no background tasks."""
    yield


def _create_test_app(settings: Settings, db) -> FastAPI:
    """Create app with test lifespan (no Redis connection)."""
    from transcode_forge.main import create_app

    # Create app but replace the lifespan
    app = create_app(settings=settings)
    app.router.lifespan_context = _test_lifespan

    # Pre-set state so routes work
    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock(return_value=True)
    mock_redis.aclose = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.set = AsyncMock(return_value=True)
    mock_redis.delete = AsyncMock(return_value=True)
    mock_redis.keys = AsyncMock(return_value=[])

    app.state.db = db
    app.state.redis = mock_redis
    app.state.settings = settings

    return app


def _run_server(app: FastAPI, port: int) -> None:
    """Run uvicorn in a background thread."""
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


@pytest.fixture(scope="session")
def base_url() -> str:
    return E2E_BASE_URL


@pytest.fixture(scope="session")
def _server(tmp_path_factory) -> Generator[None, None, None]:
    """Start the FastAPI app on a real port for the entire test session."""
    tmp_dir = tmp_path_factory.mktemp("e2e")
    db_path = f"sqlite:///{tmp_dir / 'e2e_test.db'}"

    settings = Settings(
        redis_url="redis://localhost:6379/15",
        db_url=db_path,
        library_movies=str(tmp_dir / "movies"),
        library_tv=str(tmp_dir / "tv"),
        library_anime="",
    )

    # Initialize DB synchronously
    loop = asyncio.new_event_loop()
    db = loop.run_until_complete(init_db(db_path))
    loop.close()

    app = _create_test_app(settings, db)

    # Start server in background thread
    thread = threading.Thread(target=_run_server, args=(app, E2E_PORT), daemon=True)
    thread.start()

    # Wait for server to be ready
    import time
    import urllib.request

    for _ in range(50):
        try:
            urllib.request.urlopen(f"{E2E_BASE_URL}/", timeout=2)
            break
        except Exception:
            time.sleep(0.3)
    else:
        raise RuntimeError("E2E server failed to start")

    yield


@pytest.fixture(autouse=True)
def _ensure_server(_server) -> None:
    """Ensure the server is running for every E2E test."""
    pass

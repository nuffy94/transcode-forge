"""E2E test fixtures — starts a real FastAPI server for Playwright and logs in
once so every test runs as the authenticated admin.

The app enforces auth (AuthMiddleware 302s anonymous requests to /login, or
/setup when no admin exists). So the harness mirrors the QA sweep: bring the
threaded server up, POST /api/auth/setup to create the admin (that endpoint is
CSRF-exempt — no Origin needed), then log in through the UI in a throwaway
context and reuse its storage_state for every test context.
"""

import asyncio
import json
import threading
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Generator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
import uvicorn
from fastapi import FastAPI
from playwright.sync_api import Browser

from transcode_forge.config import Settings
from transcode_forge.db import init_db

E2E_PORT = 18765
E2E_BASE_URL = f"http://localhost:{E2E_PORT}"
ADMIN_PW = "e2e-admin-password-123"


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


def _post_json(url: str, payload: bytes) -> int:
    req = urllib.request.Request(
        url, data=payload, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


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

    # Start uvicorn in a background thread with a controllable shutdown, so the
    # process exits cleanly after the session — a threaded server left with open
    # WebSocket connections otherwise lingers and stalls interpreter teardown.
    config = uvicorn.Config(app, host="127.0.0.1", port=E2E_PORT, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for server to be ready
    import time

    for _ in range(50):
        try:
            urllib.request.urlopen(f"{E2E_BASE_URL}/", timeout=2)
            break
        except Exception:
            time.sleep(0.3)
    else:
        raise RuntimeError("E2E server failed to start")

    yield

    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="session")
def _admin_created(_server) -> None:
    """First-run setup: create the admin so authenticated pages are reachable."""
    status = _post_json(
        f"{E2E_BASE_URL}/api/auth/setup", json.dumps({"password": ADMIN_PW}).encode()
    )
    assert status in (200, 409), f"admin setup failed: HTTP {status}"


@pytest.fixture(scope="session")
def _auth_state(browser: Browser, _admin_created: None, tmp_path_factory) -> str:
    """Log in once through the UI; return a storage_state path every test reuses."""
    state_path = tmp_path_factory.mktemp("e2e_auth") / "state.json"
    context = browser.new_context()
    page = context.new_page()
    page.goto(f"{E2E_BASE_URL}/login", wait_until="domcontentloaded")
    page.fill("input[type=password]", ADMIN_PW)
    page.click("button[type=submit]")
    page.wait_for_url(lambda url: "/login" not in url, timeout=10_000)
    context.storage_state(path=str(state_path))
    context.close()
    return str(state_path)


@pytest.fixture
def browser_context_args(browser_context_args: dict, _auth_state: str) -> dict:
    """Every test context starts already authenticated as the admin."""
    return {**browser_context_args, "storage_state": _auth_state}


@pytest.fixture(autouse=True)
def _ensure_server(_server) -> None:
    """Ensure the server is running for every E2E test."""
    pass

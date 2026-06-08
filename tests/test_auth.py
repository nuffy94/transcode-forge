"""Tests for first-run setup, login/logout, and the auth middleware."""

from httpx import AsyncClient

from transcode_forge.repos import users as user_repo


class TestPasswordHash:
    def test_round_trip(self):
        h = user_repo.hash_password("hunter2-please-fix-this")
        assert user_repo.verify_password("hunter2-please-fix-this", h) is True
        assert user_repo.verify_password("wrong", h) is False

    def test_verify_handles_garbage(self):
        # Corrupt hash should not raise — must return False.
        assert user_repo.verify_password("anything", "not-a-real-hash") is False


class TestSetupFlow:
    """Setup endpoint creates the admin and logs the caller in atomically.
    If an admin already exists, /setup returns 409 — used to gate the
    first-run UI."""

    async def test_setup_creates_admin(self, unauthed_client: AsyncClient):
        resp = await unauthed_client.get("/api/auth/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["setup_required"] is True
        assert body["authenticated"] is False

        resp = await unauthed_client.post("/api/auth/setup", json={"password": "good-password-123"})
        assert resp.status_code == 200

        resp = await unauthed_client.get("/api/auth/status")
        body = resp.json()
        assert body["setup_required"] is False
        assert body["authenticated"] is True

    async def test_setup_rejects_short_password(self, unauthed_client: AsyncClient):
        resp = await unauthed_client.post("/api/auth/setup", json={"password": "short"})
        assert resp.status_code == 422

    async def test_setup_409_if_admin_exists(self, unauthed_client: AsyncClient):
        await unauthed_client.post("/api/auth/setup", json={"password": "good-password-123"})
        resp = await unauthed_client.post(
            "/api/auth/setup", json={"password": "another-good-one-456"}
        )
        assert resp.status_code == 409


class TestLoginFlow:
    async def test_wrong_password_rejected(self, client: AsyncClient):
        # client fixture already created the admin + logged in. Log out
        # and then try a bad password.
        await client.post("/api/auth/logout")
        resp = await client.post("/api/auth/login", json={"password": "WRONG"})
        assert resp.status_code == 401

    async def test_login_succeeds(self, client: AsyncClient):
        await client.post("/api/auth/logout")
        resp = await client.post("/api/auth/login", json={"password": "test-pwd-12345"})
        assert resp.status_code == 200

    async def test_logout_clears_session(self, client: AsyncClient):
        resp = await client.get("/api/auth/status")
        assert resp.json()["authenticated"] is True

        await client.post("/api/auth/logout")
        resp = await client.get("/api/auth/status")
        assert resp.json()["authenticated"] is False

    async def test_login_rate_limited_after_repeated_failures(self, client: AsyncClient):
        await client.post("/api/auth/logout")
        for _ in range(5):
            r = await client.post("/api/auth/login", json={"password": "WRONG"})
            assert r.status_code == 401
        r = await client.post("/api/auth/login", json={"password": "WRONG"})
        assert r.status_code == 429


class TestMiddlewareGate:
    """Unauthenticated requests to admin endpoints get 401 (API) or
    redirect to /login (HTML)."""

    async def test_api_returns_401_unauthed(self, unauthed_client: AsyncClient):
        resp = await unauthed_client.get("/api/jobs")
        assert resp.status_code == 401

    async def test_html_redirects_to_login(self, unauthed_client: AsyncClient):
        resp = await unauthed_client.get("/", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"

    async def test_htmx_request_gets_hx_redirect_not_page(self, unauthed_client: AsyncClient):
        """An expired-session HTMX poll must return HX-Redirect (a full-page
        client redirect), NOT a 302 — the XHR follows a 302 transparently and
        swaps the whole /login page into the polled widget, leaving the app
        visible behind a login overlay."""
        resp = await unauthed_client.get(
            "/partials/jobs",
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        assert resp.headers.get("HX-Redirect") == "/login"
        assert resp.status_code != 302
        # The login page body must not come back to be swapped into a widget.
        assert "FORGE" not in resp.text

    async def test_health_is_public(self, unauthed_client: AsyncClient):
        resp = await unauthed_client.get("/api/health")
        assert resp.status_code == 200

    async def test_login_page_is_public(self, unauthed_client: AsyncClient):
        # Should redirect to /setup when no admin exists yet
        resp = await unauthed_client.get("/login", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/setup"

    async def test_setup_page_is_public(self, unauthed_client: AsyncClient):
        resp = await unauthed_client.get("/setup")
        assert resp.status_code == 200
        assert "FORGE" in resp.text

    async def test_authed_request_passes(self, client: AsyncClient):
        # The standard `client` fixture is logged in.
        resp = await client.get("/api/jobs")
        assert resp.status_code == 200

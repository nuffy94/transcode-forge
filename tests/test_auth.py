"""Tests for login/logout and the auth middleware.

First-run account creation is not here because it is not reachable over
HTTP: see tests/test_admin_bootstrap.py.
"""

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


class TestNoSetupDoor:
    """R-040: the admin is created on the machine at startup, so no request
    can create one. A fresh instance is never claimable by whoever reaches
    it first — which on a public deploy is a race against the certificate
    transparency logs. See tests/test_admin_bootstrap.py for the door that
    replaced it."""

    async def test_setup_endpoint_is_gone(self, unauthed_client: AsyncClient):
        resp = await unauthed_client.post("/api/auth/setup", json={"password": "good-password-123"})
        assert resp.status_code == 404

    async def test_setup_page_is_not_public(self, unauthed_client: AsyncClient):
        # Not in PUBLIC_PATHS any more, so the middleware turns it away
        # before routing ever gets a say.
        resp = await unauthed_client.get("/setup", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"

    async def test_setup_page_does_not_exist_even_when_logged_in(self, client: AsyncClient):
        resp = await client.get("/setup", follow_redirects=False)
        assert resp.status_code == 404

    async def test_status_advertises_no_setup_state(self, unauthed_client: AsyncClient):
        resp = await unauthed_client.get("/api/auth/status")
        assert resp.status_code == 200
        assert resp.json() == {"authenticated": False}


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
        resp = await unauthed_client.get("/login", follow_redirects=False)
        assert resp.status_code == 200
        assert "FORGE" in resp.text

    async def test_authed_request_passes(self, client: AsyncClient):
        # The standard `client` fixture is logged in.
        resp = await client.get("/api/jobs")
        assert resp.status_code == 200

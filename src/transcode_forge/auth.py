"""Auth middleware + helpers.

Public path policy:
- /api/auth/*           — login/setup/status (auth itself, obviously)
- /api/health           — for compose healthcheck and external monitoring
- /api/worker/*         — worker token-based auth (separate flow, v0.5-3)
- /metrics              — Prometheus scrape
- /static/*             — assets
- /login, /setup        — the auth pages themselves
- /partials/health      — the sidebar dot polls this; cheap + read-only

Anything else in /api/* requires an authenticated session and returns
401 if absent. HTML routes redirect to /login (or /setup if no admin
exists yet).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp

from transcode_forge.api.routes.auth import SESSION_KEY
from transcode_forge.repos import users as user_repo

PUBLIC_PREFIXES = (
    "/api/auth/",
    "/api/health",
    "/api/worker/",
    "/metrics",
    "/static/",
    "/partials/health",
)
PUBLIC_PATHS = {"/login", "/setup", "/favicon.ico"}


def _is_public(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    return any(path.startswith(p) for p in PUBLIC_PREFIXES)


def _is_api(path: str) -> bool:
    return path.startswith("/api/")


# Methods that mutate state — these need CSRF protection. Safe methods
# (GET/HEAD/OPTIONS) are exempt. Worker endpoints are also exempt
# because they use bearer-token auth, not cookies (CSRF is a
# cookie-attack class).
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Origin-prefixed paths that DO need CSRF gating but happen BEFORE login
# can establish a CSRF cookie — we whitelist these by path. Setup is the
# only one (no admin exists yet, by definition).
CSRF_EXEMPT_PATHS = frozenset({"/api/auth/setup", "/api/auth/login"})


def _csrf_check(scope: dict[str, Any]) -> Response | None:
    """Reject cross-site state-changing requests by Origin/Referer.

    Browsers attach Origin/Sec-Fetch-Site to cross-origin fetches; if a
    state-changing request arrives without a same-origin Origin header
    (or with the explicit cross-site marker), it's almost certainly CSRF
    and we deny. Returns a 403 response if the request fails the check,
    None if it passes.
    """
    method = scope.get("method", "GET")
    if method not in UNSAFE_METHODS:
        return None

    path = scope.get("path", "")
    if path.startswith("/api/worker/"):  # bearer auth, not cookie
        return None
    if path in CSRF_EXEMPT_PATHS:
        return None

    headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}

    # Modern browsers send Sec-Fetch-Site. cross-site / cross-origin = block.
    sec_fetch_site = headers.get("sec-fetch-site")
    if sec_fetch_site in ("cross-site", "cross-origin"):
        return JSONResponse(status_code=403, content={"detail": "Cross-origin request rejected"})

    # Fallback for browsers that don't send Sec-Fetch-Site: compare Origin
    # against Host. If Origin is present and doesn't match, reject.
    origin = headers.get("origin", "")
    if origin:
        host = headers.get("host", "")
        scheme = scope.get("scheme", "http")
        # Origin includes scheme: 'http://localhost:8000'. Strip and compare host portion.
        try:
            origin_host = origin.split("://", 1)[1] if "://" in origin else origin
        except IndexError:
            origin_host = origin
        if (
            origin_host != host
            and not (scheme == "https" and origin == f"https://{host}")
            and origin != f"{scheme}://{host}"
        ):
            return JSONResponse(
                status_code=403,
                content={"detail": "Origin/Host mismatch on state-changing request"},
            )

    return None


class AuthMiddleware:
    """Block unauthenticated access to admin endpoints + reject CSRF.

    Implementation note: this is a plain ASGI middleware (not BaseHTTPMiddleware)
    because we already have SessionMiddleware injecting `request.session` into
    scope; we only need to read it and short-circuit non-public requests.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # CSRF gate runs even on public paths — a CSRFed POST to /login
        # could let an attacker plant a session for accounts they control.
        csrf_resp = _csrf_check(scope)
        if csrf_resp is not None:
            await csrf_resp(scope, receive, send)
            return

        path = scope.get("path", "")
        if _is_public(path):
            await self.app(scope, receive, send)
            return

        session = scope.get("session") or {}
        if session.get(SESSION_KEY):
            await self.app(scope, receive, send)
            return

        # Not authed. HTMX requests must NOT get a 302: the XHR follows it
        # transparently and swaps the full /login page into the polled element,
        # leaving the app visible behind a login overlay. HX-Redirect tells htmx
        # to navigate the whole page to /login instead. (200 so htmx honors the
        # header on every version without swapping the empty body.)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        resp: Response
        if headers.get("hx-request") == "true":
            resp = Response(status_code=200, headers={"HX-Redirect": "/login"})
        elif _is_api(path):
            resp = JSONResponse(status_code=401, content={"detail": "Not authenticated"})
        else:
            # We can't know yet whether setup is required without DB access here;
            # the /login page itself bounces to /setup if needed.
            resp = RedirectResponse(url="/login", status_code=302)
        await resp(scope, receive, send)


async def require_admin(request: Request) -> None:
    """Dependency form — for routes that prefer an explicit guard.

    Most endpoints get coverage from the middleware; use this for
    anything routed outside the standard tree.
    """
    if not request.session.get(SESSION_KEY):
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="Not authenticated")


# Re-export for symmetric imports in main.py
__all__ = ["AuthMiddleware", "Awaitable", "Callable", "require_admin", "user_repo"]

"""Token verification and the per-request gate for /mcp.

The SDK's bearer middleware calls ``GrantTokenVerifier`` and puts the result
on the request. ``Gate`` then runs between that middleware and the transport
(docs/MCP.md, "Per-tool authorization"):

1. protocol admission: only a supported modern ``MCP-Protocol-Version``,
2. the per-grant request budget,
3. for ``tools/call``, the registry: the tool named by ``Mcp-Name`` must
   exist and the grant's scopes must cover it,
4. one audit record per tool call.

The transport afterwards proves the body names the same method and tool as
the headers, so authorizing on the headers is safe.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from mcp.server.auth.provider import AccessToken
from mcp.shared.inbound import (
    MCP_METHOD_HEADER,
    MCP_NAME_HEADER,
    MCP_PROTOCOL_VERSION_HEADER,
    decode_header_value,
)
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from transcode_forge.config import Settings
from transcode_forge.db import DBConnection
from transcode_forge.mcp.registry import required_scopes
from transcode_forge.repos import oauth as oauth_repo

audit_log = logging.getLogger("transcode_forge.mcp.audit")

RATE_WINDOW_SECONDS = 60


class GrantTokenVerifier:
    """The SDK's ``TokenVerifier`` protocol over the grant store."""

    def __init__(self, get_db: Callable[[], DBConnection], resource: str) -> None:
        self._get_db = get_db
        self._resource = resource

    async def verify_token(self, token: str) -> AccessToken | None:
        principal = await oauth_repo.verify_access_token(
            self._get_db(), token, resource=self._resource
        )
        if principal is None:
            return None
        return AccessToken(
            token=token,
            client_id=principal.client_id,
            scopes=sorted(principal.scopes),
            expires_at=principal.expires_at,
            resource=principal.resource,
            subject=principal.user_id,
            claims={"grant_id": principal.grant_id},
        )


@dataclass
class _Window:
    started: float
    count: int


class RateLimiter:
    """Fixed window per key. ``allow`` returns the seconds to wait, 0 when allowed."""

    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._windows: dict[str, _Window] = {}

    def allow(self, key: str, limit: int) -> int:
        now = self._now()
        window = self._windows.get(key)
        if window is None or now - window.started >= RATE_WINDOW_SECONDS:
            self._windows[key] = _Window(started=now, count=1)
            self._prune(now)
            return 0
        if window.count >= limit:
            return max(1, int(RATE_WINDOW_SECONDS - (now - window.started)) + 1)
        window.count += 1
        return 0

    def _prune(self, now: float) -> None:
        if len(self._windows) > 1024:
            stale = [k for k, w in self._windows.items() if now - w.started >= RATE_WINDOW_SECONDS]
            for key in stale:
                del self._windows[key]


def _header_values(scope: Scope, name: str) -> list[str]:
    wanted = name.lower().encode("latin-1")
    return [v.decode("latin-1") for k, v in scope.get("headers", []) if k.lower() == wanted]


async def _reply(
    send: Send,
    status: int,
    error: str,
    description: str,
    *,
    challenge: dict[str, str] | None = None,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    body = json.dumps({"error": error, "error_description": description}).encode()
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    if challenge is not None:
        parts = ", ".join(f'{k}="{v}"' for k, v in challenge.items())
        headers.append((b"www-authenticate", f"Bearer {parts}".encode()))
    headers.extend(extra_headers or [])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class Gate:
    """ASGI wrapper around the transport. Runs after the SDK's bearer
    middleware, so the verified token is on ``scope["user"]``."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings: Settings,
        resource_metadata_url: str,
        limiter: RateLimiter | None = None,
    ) -> None:
        self.app = app
        self._settings = settings
        self._prm_url = resource_metadata_url
        self._limiter = limiter or RateLimiter()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        token: AccessToken = scope["user"].access_token
        grant_id = str(token.claims.get("grant_id")) if token.claims else ""
        held = frozenset(token.scopes)

        versions = _header_values(scope, MCP_PROTOCOL_VERSION_HEADER)
        if len(versions) != 1 or versions[0] not in MODERN_PROTOCOL_VERSIONS:
            await _reply(
                send,
                400,
                "unsupported_protocol_version",
                f"{MCP_PROTOCOL_VERSION_HEADER} must be exactly one of "
                f"{', '.join(MODERN_PROTOCOL_VERSIONS)}",
            )
            return

        wait = self._limiter.allow(grant_id, self._settings.mcp_rate_limit_per_minute)
        if wait:
            await _reply(
                send,
                429,
                "rate_limited",
                "request budget for this grant is spent",
                extra_headers=[(b"retry-after", str(wait).encode())],
            )
            return

        methods = _header_values(scope, MCP_METHOD_HEADER)
        method = methods[0] if len(methods) == 1 else None
        if method != "tools/call":
            await self.app(scope, receive, send)
            return

        names = _header_values(scope, MCP_NAME_HEADER)
        name = decode_header_value(names[0]) if len(names) == 1 else None
        if name is None:
            await _reply(
                send,
                400,
                "invalid_request",
                f"tools/call requires exactly one {MCP_NAME_HEADER} header",
            )
            return

        correlation = uuid4().hex[:12]
        needed = required_scopes(name)
        if needed is None:
            self._audit(grant_id, token.client_id, name, "denied", "unknown tool", correlation)
            await _reply(
                send,
                403,
                "insufficient_scope",
                "unknown tool",
                challenge={"error": "insufficient_scope", "resource_metadata": self._prm_url},
            )
            return
        missing = needed - held
        if missing:
            scope_text = " ".join(sorted(missing))
            self._audit(
                grant_id,
                token.client_id,
                name,
                "denied",
                f"missing scope {scope_text}",
                correlation,
            )
            await _reply(
                send,
                403,
                "insufficient_scope",
                f"this tool needs scope {scope_text}",
                challenge={
                    "error": "insufficient_scope",
                    "scope": scope_text,
                    "resource_metadata": self._prm_url,
                },
            )
            return

        status = {"code": 0}
        started = time.perf_counter()

        async def observing_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, observing_send)
        finally:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            self._audit(
                grant_id,
                token.client_id,
                name,
                "allowed",
                f"http {status['code']} in {elapsed_ms}ms",
                correlation,
            )

    @staticmethod
    def _audit(
        grant_id: str, client_id: str, tool: str, decision: str, detail: str, correlation: str
    ) -> None:
        audit_log.info(
            "mcp tool=%s decision=%s grant=%s client=%s detail=%r correlation=%s",
            tool,
            decision,
            grant_id,
            client_id,
            detail,
            correlation,
        )


def challenge_for_scope(scope_text: str, resource_metadata_url: str) -> dict[str, Any]:
    """The parts of a 403 challenge, for callers that build one elsewhere."""
    return {
        "error": "insufficient_scope",
        "scope": scope_text,
        "resource_metadata": resource_metadata_url,
    }

"""The embedded OAuth 2.1 authorization server (docs/MCP.md, "Authorization
server").

Every endpoint of the authorization server is in this module, as plain
Starlette routes mounted next to ``/mcp`` by the same switch
(``TF_PUBLIC_URL``):

    GET  /.well-known/oauth-authorization-server   RFC 8414 metadata
    GET  /oauth/authorize                           validate, park the request
    GET  /oauth/consent?txn=                        the consent page (admin session)
    POST /oauth/consent                             the decision (admin session + nonce)
    POST /oauth/token                               code exchange, refresh
    POST /oauth/revoke                              RFC 7009

The SDK's own authorization handlers are deliberately not mounted: they
drop the token request's ``resource``, split loading a code from redeeming
it, match redirect URIs exactly and build metadata without the flags the
Claude clients need. This module borrows the SDK's request and metadata
models and its redirect builder and owns every check itself, so the
threat-model rows 8 to 11 are proven against code in this repository.

Authentication classes: ``/oauth/authorize`` is public (it validates and
stores the request; nothing is granted), the consent page and its POST
require the admin session, ``/oauth/token`` and ``/oauth/revoke`` follow the
OAuth protocol rules (a registered public client, PKCE) and never the cookie.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode, urlsplit

from mcp.server.auth.errors import stringify_pydantic_error
from mcp.server.auth.handlers.authorize import AuthorizationRequest
from mcp.server.auth.handlers.token import (
    AuthorizationCodeRequest,
    RefreshTokenRequest,
    token_request_adapter,
)
from mcp.server.auth.provider import construct_redirect_uri
from mcp.server.auth.routes import cors_middleware
from mcp.server.transport_security import RequestBodyLimitMiddleware
from mcp.shared.auth import OAuthMetadata, OAuthToken
from mcp.shared.inbound import MCP_PROTOCOL_VERSION_HEADER
from pydantic import ValidationError
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import BaseRoute, Route, request_response
from starlette.types import ASGIApp

from transcode_forge.config import Settings
from transcode_forge.db import DBConnection
from transcode_forge.mcp import cimd
from transcode_forge.mcp.auth import RateLimiter
from transcode_forge.repos import oauth as oauth_repo
from transcode_forge.repos import users as user_repo
from transcode_forge.web.routes import templates

audit_log = logging.getLogger("transcode_forge.mcp.audit")

METADATA_PATH = "/.well-known/oauth-authorization-server"
AUTHORIZE_PATH = "/oauth/authorize"
CONSENT_PATH = "/oauth/consent"
TOKEN_PATH = "/oauth/token"
REVOKE_PATH = "/oauth/revoke"

MAX_FORM_BYTES = 16 * 1024

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
"""Hosts whose registered redirect URI matches any port (RFC 8252 section 7.3,
plus ``localhost`` because Claude Code declares it)."""

SEEDED_CLIENT_DOCUMENTS: dict[str, str] = {
    "https://claude.ai/oauth/claude-code-client-metadata": "Claude Code",
}
"""Client documents allowed on every installation that turns the endpoint on."""

_VERIFIER = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")

EXPIRED_MESSAGE = (
    "This sign-in request has expired or was already answered. Start again from the client."
)

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
CONSENT_HEADERS = {
    **NO_STORE,
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
}


# ── metadata ─────────────────────────────────────────────────────────


def build_metadata(settings: Settings) -> dict[str, Any]:
    """The RFC 8414 document. The two flags Claude checks before it uses a
    client metadata document are both here."""
    issuer = settings.public_url.rstrip("/")
    metadata = OAuthMetadata(
        issuer=issuer,  # type: ignore[arg-type]
        authorization_endpoint=issuer + AUTHORIZE_PATH,  # type: ignore[arg-type]
        token_endpoint=issuer + TOKEN_PATH,  # type: ignore[arg-type]
        revocation_endpoint=issuer + REVOKE_PATH,  # type: ignore[arg-type]
        scopes_supported=sorted(oauth_repo.SCOPES),
        response_types_supported=["code"],
        response_modes_supported=["query"],
        grant_types_supported=["authorization_code", "refresh_token"],
        token_endpoint_auth_methods_supported=["none"],
        revocation_endpoint_auth_methods_supported=["none"],
        code_challenge_methods_supported=["S256"],
        client_id_metadata_document_supported=True,
        authorization_response_iss_parameter_supported=True,
    )
    return metadata.model_dump(mode="json", exclude_none=True)


# ── helpers ──────────────────────────────────────────────────────────


def redirect_matches(registered: str, requested: str) -> bool:
    """Exact match, except that a registered loopback URI matches any port."""
    reg, req = urlsplit(registered), urlsplit(requested)
    if reg.fragment or req.fragment:
        return False
    if (reg.scheme, reg.hostname, reg.path, reg.query) != (
        req.scheme,
        req.hostname,
        req.path,
        req.query,
    ):
        return False
    if reg.scheme == "http" and reg.hostname in LOOPBACK_HOSTS:
        return True
    return reg.port == req.port


def is_loopback(uri: str) -> bool:
    return urlsplit(uri).hostname in LOOPBACK_HOSTS


def verifier_matches(code_verifier: str, code_challenge: str) -> bool:
    """PKCE S256: BASE64URL(SHA256(verifier)) without padding equals the challenge."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    derived = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(derived, code_challenge)


def _error_json(error: str, description: str, *, status: int = 400) -> Response:
    return JSONResponse(
        {"error": error, "error_description": description}, status_code=status, headers=NO_STORE
    )


def _redirect(url: str) -> Response:
    return RedirectResponse(url, status_code=302, headers=NO_STORE)


def _audit(event: str, **fields: Any) -> None:
    """One JSON record per authorization decision. Never carries a token,
    a code or a verifier."""
    clipped = {k: (v[:256] if isinstance(v, str) else v) for k, v in fields.items()}
    record = {"event": event, **clipped}
    audit_log.info("%s", json.dumps(record, separators=(",", ":")))


def _client_label(client: dict[str, Any]) -> str:
    """What the consent page calls the client. A document is self-asserted,
    so its host comes first and its name second."""
    if client["kind"] == "cimd":
        host = urlsplit(client["id"]).hostname or client["id"]
        return f"{host} ({client['name']})" if client["name"] else host
    return str(client["name"])


async def seed_clients(db: DBConnection) -> None:
    """Allow the Claude client documents on first boot. Runs at every startup
    when the endpoint is on, but only creates rows that do not exist: an
    operator who disabled one keeps it disabled across restarts."""
    for url, name in SEEDED_CLIENT_DOCUMENTS.items():
        await oauth_repo.ensure_client(db, url, kind="cimd", name=name, metadata_url=url)


# ── the server ───────────────────────────────────────────────────────


class AuthorizationServer:
    def __init__(self, settings: Settings, *, get_db: Callable[[], DBConnection]) -> None:
        self._settings = settings
        self._get_db = get_db
        self._issuer = settings.public_url.rstrip("/")
        self._resource = settings.mcp_resource_url
        self._metadata = build_metadata(settings)
        self._limiter = RateLimiter()

    def _throttled(self, request: Request) -> Response | None:
        """The public endpoints share the per-minute budget /mcp applies per
        grant, keyed by the caller's address: an authorize request creates a
        row, a token request costs a lookup, and neither has a grant yet."""
        key = request.client.host if request.client else "unknown"
        wait = self._limiter.allow(key, self._settings.mcp_rate_limit_per_minute)
        if not wait:
            return None
        return JSONResponse(
            {"error": "rate_limited", "error_description": "too many requests from this address"},
            status_code=429,
            headers={**NO_STORE, "Retry-After": str(wait)},
        )

    # -- client resolution --

    async def _resolve_client(self, client_id: str) -> dict[str, Any] | None:
        """The client row, with a fresh copy of its document when it is one.
        None when the client is unknown or disabled."""
        db = self._get_db()
        client = await oauth_repo.get_client(db, client_id)
        if client is None or client["disabled_at"] is not None:
            return None
        if client["kind"] != "cimd":
            return client
        if oauth_repo.document_is_fresh(client, max_age_seconds=cimd.CACHE_SECONDS):
            return client
        try:
            document = await cimd.fetch_document(client_id)
        except cimd.DocumentError as exc:
            _audit("mcp_client_document_refused", client=client_id, reason=str(exc))
            return None
        await oauth_repo.cache_client_document(
            db, client_id, name=document.client_name, redirect_uris=document.redirect_uris
        )
        return await oauth_repo.get_client(db, client_id)

    def _check_resource(self, value: str | None) -> str | None:
        """The error description when ``value`` is not the canonical resource."""
        if not value:
            return "resource is required"
        if value.rstrip("/") != self._resource:
            return "resource does not name this server"
        return None

    # -- endpoints --

    async def metadata(self, request: Request) -> Response:
        return JSONResponse(self._metadata)

    async def authorize(self, request: Request) -> Response:
        if throttled := self._throttled(request):
            return throttled
        params = dict(request.query_params)
        state = params.get("state")
        try:
            req = AuthorizationRequest.model_validate(params)
        except ValidationError as exc:
            error = "invalid_request"
            if any(e["loc"] == ("response_type",) for e in exc.errors()):
                error = "unsupported_response_type"
            return _error_json(error, stringify_pydantic_error(exc))

        client = await self._resolve_client(req.client_id)
        if client is None:
            return _error_json(
                "invalid_request",
                f"client {req.client_id!r} is not allowed here. An operator can allow it "
                "under Settings, Connected apps.",
            )
        requested_redirect = params.get("redirect_uri", "")
        if not requested_redirect or not any(
            redirect_matches(registered, requested_redirect)
            for registered in client["redirect_uris"]
        ):
            return _error_json("invalid_request", "redirect_uri is not registered for this client")

        def fail(error: str, description: str) -> Response:
            return _redirect(
                construct_redirect_uri(
                    requested_redirect,
                    error=error,
                    error_description=description,
                    state=state,
                    iss=self._issuer,
                )
            )

        scopes = set(req.scope.split()) if req.scope else set(oauth_repo.INITIAL_SCOPES)
        unknown = scopes - oauth_repo.SCOPES
        if unknown:
            return fail("invalid_scope", f"unknown scope {' '.join(sorted(unknown))}")
        problem = self._check_resource(req.resource)
        if problem:
            return fail("invalid_target", problem)

        db = self._get_db()
        await oauth_repo.purge_expired_transactions(db)
        txn_id = await oauth_repo.create_transaction(
            db,
            client_id=client["id"],
            redirect_uri=requested_redirect,
            code_challenge=req.code_challenge,
            scopes=scopes,
            resource=self._resource,
            state=state,
        )
        return _redirect(f"{CONSENT_PATH}?{urlencode({'txn': txn_id})}")

    async def consent_page(self, request: Request) -> Response:
        txn_id = request.query_params.get("txn", "")
        db = self._get_db()
        txn = await oauth_repo.get_transaction(db, txn_id) if txn_id else None
        nonce = await oauth_repo.arm_consent(db, txn_id) if txn else None
        if txn is None or nonce is None:
            return self._consent_error(request, EXPIRED_MESSAGE)
        client = await oauth_repo.get_client(db, txn["client_id"])
        if client is None or client["disabled_at"] is not None:
            return self._consent_error(
                request, "The client behind this request is no longer allowed."
            )
        context = {
            "txn_id": txn["id"],
            "nonce": nonce,
            "client_label": _client_label(client),
            "resource": txn["resource"],
            "scopes": [
                {"name": s, "description": oauth_repo.SCOPE_DESCRIPTIONS[s]} for s in txn["scopes"]
            ],
            "redirect_host": urlsplit(txn["redirect_uri"]).netloc,
            "loopback": is_loopback(txn["redirect_uri"]),
        }
        return templates.TemplateResponse(
            request, "oauth_consent.html", context, headers=CONSENT_HEADERS
        )

    async def consent_decision(self, request: Request) -> Response:
        form = await request.form()
        txn_id = str(form.get("txn") or "")
        nonce = str(form.get("nonce") or "")
        decision = str(form.get("decision") or "")
        db = self._get_db()
        txn = await oauth_repo.get_transaction(db, txn_id) if txn_id else None
        if txn is None:
            return self._consent_error(request, EXPIRED_MESSAGE)
        redirect_uri, state, client_id = txn["redirect_uri"], txn["state"], txn["client_id"]

        def back(**params: str | None) -> Response:
            return _redirect(
                construct_redirect_uri(redirect_uri, state=state, iss=self._issuer, **params)
            )

        if decision == "allow":
            user_id = await user_repo.get_admin_id(db)
            code = (
                await oauth_repo.approve_transaction(db, txn_id, nonce=nonce, user_id=user_id)
                if user_id
                else None
            )
            if code is None:
                return self._consent_error(request, EXPIRED_MESSAGE)
            _audit(
                "mcp_consent", decision="allow", client=client_id, scopes=" ".join(txn["scopes"])
            )
            return back(code=code)

        if not await oauth_repo.deny_transaction(db, txn_id, nonce=nonce):
            return self._consent_error(request, EXPIRED_MESSAGE)
        _audit("mcp_consent", decision="deny", client=client_id)
        return back(error="access_denied", error_description="the administrator declined")

    def _consent_error(self, request: Request, message: str) -> Response:
        return templates.TemplateResponse(
            request,
            "oauth_error.html",
            {"message": message},
            status_code=400,
            headers=CONSENT_HEADERS,
        )

    async def token(self, request: Request) -> Response:
        if throttled := self._throttled(request):
            return throttled
        form = dict(await request.form())
        client_id = str(form.get("client_id") or "")
        client = await oauth_repo.get_client(self._get_db(), client_id) if client_id else None
        if client is None or client["disabled_at"] is not None:
            return _error_json("invalid_client", "unknown client", status=401)
        try:
            req = token_request_adapter.validate_python(form)
        except ValidationError as exc:
            return _error_json("invalid_request", stringify_pydantic_error(exc))
        if isinstance(req, AuthorizationCodeRequest):
            # The redirect URI is compared as the client spelled it, never as a
            # parsed URL re-serializes it (default ports, case, a bare origin's
            # trailing slash all change under normalization).
            raw_redirect = form.get("redirect_uri")
            return await self._exchange_code(
                client, req, raw_redirect if isinstance(raw_redirect, str) else None
            )
        if isinstance(req, RefreshTokenRequest):
            return await self._refresh(client, req)
        return _error_json("unsupported_grant_type", "only authorization_code and refresh_token")

    async def _exchange_code(
        self, client: dict[str, Any], req: AuthorizationCodeRequest, redirect_uri: str | None
    ) -> Response:
        problem = self._check_resource(req.resource)
        if problem:
            return _error_json("invalid_target", problem)
        if not _VERIFIER.match(req.code_verifier):
            return _error_json("invalid_request", "code_verifier is malformed")
        db = self._get_db()
        try:
            txn = await oauth_repo.redeem_code(db, req.code)
        except oauth_repo.ReplayError:
            _audit("mcp_code_replayed", client=client["id"])
            return _error_json("invalid_grant", "authorization code was already used")
        if txn is None:
            return _error_json("invalid_grant", "authorization code is unknown or expired")
        if txn["client_id"] != client["id"]:
            return _error_json("invalid_grant", "authorization code belongs to another client")
        if redirect_uri is not None and redirect_uri != txn["redirect_uri"]:
            return _error_json(
                "invalid_grant", "redirect_uri differs from the authorization request"
            )
        if not verifier_matches(req.code_verifier, txn["code_challenge"]):
            return _error_json("invalid_grant", "code_verifier does not match")
        issued = await oauth_repo.issue_tokens(db, txn["grant_id"])
        _audit("mcp_tokens_issued", client=client["id"], grant=txn["grant_id"], via="code")
        return self._token_response(issued)

    async def _refresh(self, client: dict[str, Any], req: RefreshTokenRequest) -> Response:
        if req.resource is not None:
            problem = self._check_resource(req.resource)
            if problem:
                return _error_json("invalid_target", problem)
        db = self._get_db()
        requested = set(req.scope.split()) if req.scope else None
        try:
            grant = await oauth_repo.redeem_refresh_token(
                db, req.refresh_token, requested_scopes=requested
            )
        except oauth_repo.ReplayError:
            _audit("mcp_refresh_replayed", client=client["id"])
            return _error_json("invalid_grant", "refresh token was already used. Grant revoked")
        except oauth_repo.ScopeError as exc:
            return _error_json("invalid_scope", str(exc))
        if grant is None or grant["client_id"] != client["id"]:
            return _error_json("invalid_grant", "refresh token is unknown, expired or revoked")
        issued = await oauth_repo.issue_tokens(db, grant["id"])
        _audit("mcp_tokens_issued", client=client["id"], grant=grant["id"], via="refresh")
        return self._token_response(issued)

    @staticmethod
    def _token_response(issued: oauth_repo.IssuedTokens) -> Response:
        body = OAuthToken(
            access_token=issued.access_token,
            token_type="Bearer",
            expires_in=issued.expires_in,
            scope=issued.scope,
            refresh_token=issued.refresh_token,
        )
        return JSONResponse(body.model_dump(mode="json", exclude_none=True), headers=NO_STORE)

    async def revoke(self, request: Request) -> Response:
        if throttled := self._throttled(request):
            return throttled
        form = await request.form()
        client_id = str(form.get("client_id") or "")
        token = str(form.get("token") or "")
        db = self._get_db()
        client = await oauth_repo.get_client(db, client_id) if client_id else None
        if client is None or client["disabled_at"] is not None:
            return _error_json("invalid_client", "unknown client", status=401)
        if not token:
            return _error_json("invalid_request", "token is required")
        if await oauth_repo.revoke_by_token(db, token, client_id=client["id"]):
            _audit("mcp_grant_revoked", client=client["id"], via="revocation endpoint")
        return Response(status_code=200, headers=NO_STORE)


def _protocol_endpoint(handler: Callable[[Request], Any], methods: list[str]) -> ASGIApp:
    """A token-style endpoint: body capped, CORS open so a browser-based
    client (MCP Inspector) can call it. The same stack the SDK builds for its
    own token route; CORS is outermost so an over-limit 413 carries it too."""
    limited = RequestBodyLimitMiddleware(request_response(handler), MAX_FORM_BYTES)
    return CORSMiddleware(
        limited,
        allow_origins="*",
        allow_methods=methods,
        allow_headers=[MCP_PROTOCOL_VERSION_HEADER],
    )


def build_authz_routes(
    settings: Settings, *, get_db: Callable[[], DBConnection]
) -> list[BaseRoute]:
    """The authorization server's routes, to mount beside ``/mcp``."""
    server = AuthorizationServer(settings, get_db=get_db)
    return [
        Route(
            METADATA_PATH,
            endpoint=cors_middleware(server.metadata, ["GET", "OPTIONS"]),
            methods=["GET", "OPTIONS"],
        ),
        Route(AUTHORIZE_PATH, endpoint=server.authorize, methods=["GET"]),
        Route(CONSENT_PATH, endpoint=server.consent_page, methods=["GET"]),
        Route(
            CONSENT_PATH,
            endpoint=RequestBodyLimitMiddleware(
                request_response(server.consent_decision), MAX_FORM_BYTES
            ),
            methods=["POST"],
        ),
        Route(
            TOKEN_PATH,
            endpoint=_protocol_endpoint(server.token, ["POST", "OPTIONS"]),
            methods=["POST", "OPTIONS"],
        ),
        Route(
            REVOKE_PATH,
            endpoint=_protocol_endpoint(server.revoke, ["POST", "OPTIONS"]),
            methods=["POST", "OPTIONS"],
        ),
    ]

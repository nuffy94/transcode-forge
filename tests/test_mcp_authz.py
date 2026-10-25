"""The embedded authorization server on the wire (docs/MCP.md, "Authorization
server"; docs/MCP-THREAT-MODEL.md rows 8 to 11).

Every test speaks HTTP: query strings, redirects, form posts and the JSON a
client sees. The Claude Code client document is served from a stub, so no
test touches the network. ``client`` (conftest) is the administrator's
browser, logged in; ``anon`` is the OAuth client, with no cookie.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import secrets
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from transcode_forge.config import Settings
from transcode_forge.main import create_app
from transcode_forge.mcp import authz, cimd
from transcode_forge.models.job import Job, JobStatus
from transcode_forge.repos import jobs as job_repo
from transcode_forge.repos import oauth as oauth_repo
from transcode_forge.web.routes import safe_next

ISSUER = "http://test"
RESOURCE = "http://test/mcp"
PV = "2026-07-28"
META = {
    "io.modelcontextprotocol/protocolVersion": PV,
    "io.modelcontextprotocol/clientCapabilities": {},
}
CLAUDE_CODE = "https://claude.ai/oauth/claude-code-client-metadata"
CALLBACK = "http://localhost:3118/callback"
READ_SCOPES = "catalog:read jobs:read"
ALL_SCOPES = "catalog:read jobs:read jobs:write"
NONCE = re.compile(r'name="nonce" value="([^"]+)"')


# ── helpers ──────────────────────────────────────────────────────────


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _query(location: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


async def authorize(c: AsyncClient, **overrides: Any) -> tuple[Any, str]:
    verifier, challenge = _pkce()
    params: dict[str, Any] = {
        "response_type": "code",
        "client_id": CLAUDE_CODE,
        "redirect_uri": CALLBACK,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": READ_SCOPES,
        "resource": RESOURCE,
        "state": "s-1",
    }
    params.update(overrides)
    params = {k: v for k, v in params.items() if v is not None}
    resp = await c.get("/oauth/authorize", params=params, follow_redirects=False)
    return resp, verifier


def txn_of(resp: Any) -> str:
    assert resp.status_code == 302, resp.text
    location = resp.headers["location"]
    assert location.startswith("/oauth/consent?txn="), location
    return _query(location)["txn"]


async def consent(admin: AsyncClient, txn_id: str) -> tuple[Any, str]:
    page = await admin.get("/oauth/consent", params={"txn": txn_id})
    match = NONCE.search(page.text)
    return page, (match.group(1) if match else "")


async def decide(
    admin: AsyncClient, txn_id: str, nonce: str, decision: str = "allow", **headers: str
) -> Any:
    return await admin.post(
        "/oauth/consent",
        data={"txn": txn_id, "nonce": nonce, "decision": decision},
        follow_redirects=False,
        headers=headers,
    )


async def obtain_code(
    anon: AsyncClient, admin: AsyncClient, **overrides: Any
) -> tuple[str, str, dict[str, str]]:
    """authorize, consent, allow. Returns (code, verifier, the redirect's query)."""
    resp, verifier = await authorize(anon, **overrides)
    txn_id = txn_of(resp)
    _, nonce = await consent(admin, txn_id)
    done = await decide(admin, txn_id, nonce)
    assert done.status_code == 302, done.text
    query = _query(done.headers["location"])
    assert "code" in query, query
    return query["code"], verifier, query


async def exchange(anon: AsyncClient, code: str, verifier: str, **overrides: Any) -> Any:
    form: dict[str, Any] = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "client_id": CLAUDE_CODE,
        "redirect_uri": CALLBACK,
        "resource": RESOURCE,
    }
    form.update(overrides)
    return await anon.post("/oauth/token", data={k: v for k, v in form.items() if v is not None})


async def refresh(anon: AsyncClient, token: str, **overrides: Any) -> Any:
    form: dict[str, Any] = {
        "grant_type": "refresh_token",
        "refresh_token": token,
        "client_id": CLAUDE_CODE,
        "resource": RESOURCE,
    }
    form.update(overrides)
    return await anon.post("/oauth/token", data={k: v for k, v in form.items() if v is not None})


async def obtain_tokens(anon: AsyncClient, admin: AsyncClient, **overrides: Any) -> dict[str, Any]:
    code, verifier, _ = await obtain_code(anon, admin, **overrides)
    resp = await exchange(
        anon,
        code,
        verifier,
        client_id=overrides.get("client_id", CLAUDE_CODE),
        redirect_uri=overrides.get("redirect_uri", CALLBACK),
    )
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def mcp_headers(token: str, method: str, name: str | None = None) -> dict[str, str]:
    h = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": PV,
        "Mcp-Method": method,
        "Authorization": f"Bearer {token}",
    }
    if name is not None:
        h["Mcp-Name"] = name
    return h


async def mcp_list(c: AsyncClient, token: str) -> Any:
    return await c.post(
        "/mcp",
        headers=mcp_headers(token, "tools/list"),
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": META}},
    )


async def mcp_call(c: AsyncClient, token: str, tool: str, arguments: dict[str, Any]) -> Any:
    return await c.post(
        "/mcp",
        headers=mcp_headers(token, "tools/call", tool),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments, "_meta": META},
        },
    )


# ── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def claude_document(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Serve the Claude Code client document without the network; count fetches."""
    calls = {"n": 0}

    async def fake_fetch(url: str, **_: Any) -> cimd.ClientDocument:
        calls["n"] += 1
        if url != CLAUDE_CODE:
            raise cimd.DocumentError("not served")
        return cimd.ClientDocument(
            client_id=url,
            client_name="Claude Code",
            redirect_uris=("http://localhost/callback", "http://127.0.0.1/callback"),
        )

    monkeypatch.setattr(cimd, "fetch_document", fake_fetch)
    return calls


@pytest_asyncio.fixture
async def anon(app: Any, claude_document: dict[str, int]) -> Any:
    """The OAuth client: no cookie. The allowed clients are seeded as startup does."""
    await authz.seed_clients(app.state.db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ISSUER) as c:
        yield c


# ── metadata and the off state ───────────────────────────────────────


async def test_metadata_advertises_what_claude_checks(anon: AsyncClient) -> None:
    resp = await anon.get(
        "/.well-known/oauth-authorization-server", headers={"Origin": "https://i.example"}
    )
    assert resp.status_code == 200
    meta = resp.json()
    assert meta["issuer"] == ISSUER
    assert meta["authorization_endpoint"] == f"{ISSUER}/oauth/authorize"
    assert meta["token_endpoint"] == f"{ISSUER}/oauth/token"
    assert meta["revocation_endpoint"] == f"{ISSUER}/oauth/revoke"
    assert meta["client_id_metadata_document_supported"] is True
    assert meta["token_endpoint_auth_methods_supported"] == ["none"]
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["authorization_response_iss_parameter_supported"] is True
    assert meta["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert "registration_endpoint" not in meta
    assert "offline_access" not in meta["scopes_supported"]
    assert resp.headers["access-control-allow-origin"] == "*"


async def test_nothing_is_mounted_without_public_url(tmp_db_path: str, tmp_path: Any) -> None:
    """The one switch: an empty TF_PUBLIC_URL leaves no MCP or OAuth route
    registered at all, whatever this module adds in the future."""
    settings = Settings(db_url=tmp_db_path, library_movies=str(tmp_path / "m"), public_url="")
    application = create_app(settings=settings)
    paths = [getattr(route, "path", "") for route in application.routes]
    offenders = [p for p in paths if p.startswith(("/mcp", "/oauth", "/.well-known"))]
    assert offenders == []


# ── authorize (row 9) ────────────────────────────────────────────────


async def test_authorize_parks_the_request_and_sends_to_consent(anon: AsyncClient) -> None:
    resp, _ = await authorize(anon)
    txn_id = txn_of(resp)
    assert resp.headers["cache-control"] == "no-store"
    assert len(txn_id) == 32


async def test_authorize_unknown_client_is_a_direct_error(anon: AsyncClient) -> None:
    resp, _ = await authorize(
        anon, client_id="https://evil.example/client", redirect_uri="https://evil.example/cb"
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"
    assert "Settings" in resp.json()["error_description"]


async def test_authorize_unregistered_redirect_is_a_direct_error(anon: AsyncClient) -> None:
    """A changed path, host or scheme on a loopback URI never gets a redirect."""
    for bad in (
        "http://localhost:3118/other",
        "http://localhost.evil.example:3118/callback",
        "https://localhost:3118/callback",
        "http://localhost:3118/callback#frag",
        "http://evil.example/callback",
    ):
        resp, _ = await authorize(anon, redirect_uri=bad)
        assert resp.status_code == 400, bad
        assert resp.json()["error"] == "invalid_request"
    resp, _ = await authorize(anon, redirect_uri=None)
    assert resp.status_code == 400


async def test_authorize_loopback_port_may_vary(anon: AsyncClient) -> None:
    for uri in (
        "http://localhost:3118/callback",
        "http://localhost:4000/callback",
        "http://127.0.0.1:51234/callback",
    ):
        resp, _ = await authorize(anon, redirect_uri=uri)
        txn_of(resp)


async def test_authorize_requires_s256_pkce(anon: AsyncClient) -> None:
    resp, _ = await authorize(anon, code_challenge=None)
    assert resp.status_code == 400
    resp, _ = await authorize(anon, code_challenge_method="plain")
    assert resp.status_code == 400
    resp, _ = await authorize(anon, response_type="token")
    assert resp.status_code == 400
    assert resp.json()["error"] == "unsupported_response_type"


async def test_authorize_resource_is_required_and_checked(anon: AsyncClient) -> None:
    """Errors after the redirect URI is validated go back to the client with
    the state and the issuer (RFC 9207)."""
    for resource in (None, "http://other.example/mcp", "http://test/other"):
        resp, _ = await authorize(anon, resource=resource)
        assert resp.status_code == 302, resource
        query = _query(resp.headers["location"])
        assert resp.headers["location"].startswith(CALLBACK)
        assert query["error"] == "invalid_target"
        assert query["state"] == "s-1"
        assert query["iss"] == ISSUER


async def test_authorize_unknown_scope_redirects_with_iss(anon: AsyncClient) -> None:
    resp, _ = await authorize(anon, scope="jobs:read admin:*")
    query = _query(resp.headers["location"])
    assert query["error"] == "invalid_scope"
    assert query["iss"] == ISSUER


async def test_authorize_defaults_to_the_initial_scopes(
    anon: AsyncClient, client: AsyncClient
) -> None:
    resp, _ = await authorize(anon, scope=None)
    txn_id = txn_of(resp)
    page, _ = await consent(client, txn_id)
    assert "catalog:read" in page.text and "jobs:read" in page.text
    assert "jobs:write" not in page.text


async def test_expired_transaction_cannot_be_consented(
    anon: AsyncClient, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(oauth_repo, "TRANSACTION_TTL_SECONDS", -1)
    resp, _ = await authorize(anon)
    txn_id = txn_of(resp)
    page, nonce = await consent(client, txn_id)
    assert page.status_code == 400
    assert nonce == ""


# ── consent (row 10) ─────────────────────────────────────────────────


async def test_consent_requires_the_admin_session(anon: AsyncClient) -> None:
    resp, _ = await authorize(anon)
    txn_id = txn_of(resp)
    page = await anon.get("/oauth/consent", params={"txn": txn_id}, follow_redirects=False)
    assert page.status_code == 302
    assert page.headers["location"] == f"/login?next=%2Foauth%2Fconsent%3Ftxn%3D{txn_id}"
    post = await anon.post(
        "/oauth/consent",
        data={"txn": txn_id, "nonce": "x", "decision": "allow"},
        follow_redirects=False,
    )
    assert post.status_code == 302
    assert post.headers["location"].startswith("/login")


async def test_login_returns_to_a_local_page_only(client: AsyncClient) -> None:
    page = await client.get("/login", params={"next": "/oauth/consent?txn=abc"})
    assert '"/oauth/consent?txn=abc"' in page.text
    for hostile in (
        "//evil.example/x",
        "https://evil.example",
        "/\\evil.example",
        "javascript:alert(1)",
    ):
        assert safe_next(hostile) == "/", hostile
        page = await client.get("/login", params={"next": hostile})
        assert "evil" not in page.text and "javascript" not in page.text


async def test_consent_page_names_the_parties_and_cannot_be_framed(
    anon: AsyncClient, client: AsyncClient
) -> None:
    resp, _ = await authorize(anon)
    txn_id = txn_of(resp)
    page, nonce = await consent(client, txn_id)
    assert page.status_code == 200
    assert nonce
    assert "claude.ai" in page.text  # the document's host, not only its self-asserted name
    assert "localhost:3118" in page.text  # where the browser goes afterwards
    assert "program here could be listening" in page.text  # loopback warning
    assert "Read jobs, the queue state and statistics" in page.text
    assert page.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["cache-control"] == "no-store"


async def test_login_is_not_consent(anon: AsyncClient, client: AsyncClient, app: Any) -> None:
    """A crafted authorize link opened by a logged-in administrator grants
    nothing until the form is posted with the page's own nonce."""
    resp, _ = await authorize(anon)
    txn_id = txn_of(resp)
    await consent(client, txn_id)
    assert await oauth_repo.list_grants(app.state.db) == []


async def test_approval_needs_the_nonce_of_its_own_page(
    anon: AsyncClient, client: AsyncClient, app: Any
) -> None:
    resp, _ = await authorize(anon)
    txn_id = txn_of(resp)
    other, _ = await authorize(anon)
    other_id = txn_of(other)
    _, nonce = await consent(client, txn_id)
    _, other_nonce = await consent(client, other_id)

    assert (await decide(client, txn_id, "")).status_code == 400
    assert (await decide(client, txn_id, "not-the-nonce")).status_code == 400
    assert (await decide(client, txn_id, other_nonce)).status_code == 400
    cross = await decide(client, txn_id, nonce, **{"Sec-Fetch-Site": "cross-site"})
    assert cross.status_code == 403
    assert await oauth_repo.list_grants(app.state.db) == []

    done = await decide(client, txn_id, nonce)
    assert done.status_code == 302
    query = _query(done.headers["location"])
    assert done.headers["location"].startswith(CALLBACK)
    assert query["code"] and query["state"] == "s-1" and query["iss"] == ISSUER
    # The same nonce cannot approve twice.
    assert (await decide(client, txn_id, nonce)).status_code == 400
    grants = await oauth_repo.list_grants(app.state.db)
    assert len(grants) == 1
    assert grants[0]["client_id"] == CLAUDE_CODE
    assert grants[0]["scopes"] == ["catalog:read", "jobs:read"]


async def test_deny_sends_access_denied_and_grants_nothing(
    anon: AsyncClient, client: AsyncClient, app: Any
) -> None:
    resp, _ = await authorize(anon)
    txn_id = txn_of(resp)
    _, nonce = await consent(client, txn_id)
    done = await decide(client, txn_id, nonce, decision="deny")
    assert done.status_code == 302
    query = _query(done.headers["location"])
    assert query["error"] == "access_denied"
    assert query["state"] == "s-1" and query["iss"] == ISSUER
    assert await oauth_repo.list_grants(app.state.db) == []
    # The transaction is closed: no second decision.
    assert (await decide(client, txn_id, nonce)).status_code == 400


# ── token endpoint (rows 8, 9) ───────────────────────────────────────


async def test_code_exchange_issues_tokens_that_work(
    anon: AsyncClient, client: AsyncClient, mcp_running: Any, app: Any
) -> None:
    tokens = await obtain_tokens(anon, client)
    assert tokens["token_type"] == "Bearer"
    assert 0 < tokens["expires_in"] <= oauth_repo.ACCESS_TTL_SECONDS
    assert tokens["scope"] == READ_SCOPES
    assert tokens["refresh_token"]
    assert (await mcp_list(anon, tokens["access_token"])).status_code == 200
    rows = (await client.get("/api/mcp/grants")).json()["data"]
    assert len(rows) == 1
    assert rows[0]["client_name"] == "Claude Code"
    assert rows[0]["status"] == "active"
    assert tokens["access_token"] not in str(rows)


async def test_token_response_is_not_cacheable(anon: AsyncClient, client: AsyncClient) -> None:
    code, verifier, _ = await obtain_code(anon, client)
    anon.headers["Origin"] = "https://i.example"  # a browser-based client
    resp = await exchange(anon, code, verifier)
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["access-control-allow-origin"] == "*"


async def test_code_exchange_rejections(anon: AsyncClient, client: AsyncClient) -> None:
    code, verifier, _ = await obtain_code(anon, client)
    # Unknown client: 401 per RFC 6749 section 5.2, before anything is consumed.
    resp = await exchange(anon, code, verifier, client_id="https://evil.example/client")
    assert resp.status_code == 401 and resp.json()["error"] == "invalid_client"
    # Resource missing or foreign: invalid_target, before the code is consumed.
    for resource in (None, "http://other.example/mcp"):
        resp = await exchange(anon, code, verifier, resource=resource)
        assert resp.status_code == 400 and resp.json()["error"] == "invalid_target", resource
    # A malformed verifier never reaches the code.
    resp = await exchange(anon, code, "short")
    assert resp.json()["error"] == "invalid_request"
    # The code is still good: nothing above consumed it.
    resp = await exchange(anon, code, verifier)
    assert resp.status_code == 200, resp.text

    # A wrong verifier consumes the code it was tried against.
    code, verifier, _ = await obtain_code(anon, client)
    resp = await exchange(anon, code, verifier + "x")
    assert resp.json()["error"] == "invalid_grant"
    resp = await exchange(anon, code, verifier)
    assert resp.json()["error"] == "invalid_grant"

    # A redirect_uri that differs from the authorization request is refused.
    code, verifier, _ = await obtain_code(anon, client)
    resp = await exchange(anon, code, verifier, redirect_uri="http://localhost:3118/other")
    assert resp.json()["error"] == "invalid_grant"

    # Unknown code, unknown grant type.
    resp = await exchange(anon, "no-such-code", verifier)
    assert resp.json()["error"] == "invalid_grant"
    resp = await anon.post(
        "/oauth/token", data={"grant_type": "password", "client_id": CLAUDE_CODE}
    )
    assert resp.status_code == 400


async def test_code_replay_revokes_the_grant(
    anon: AsyncClient, client: AsyncClient, mcp_running: Any
) -> None:
    code, verifier, _ = await obtain_code(anon, client)
    first = await exchange(anon, code, verifier)
    assert first.status_code == 200
    token = first.json()["access_token"]
    assert (await mcp_list(anon, token)).status_code == 200
    second = await exchange(anon, code, verifier)
    assert second.status_code == 400
    assert second.json()["error"] == "invalid_grant"
    # The tokens the first exchange issued are dead: the code leaked.
    assert (await mcp_list(anon, token)).status_code == 401


async def test_concurrent_code_exchange_has_one_winner(
    anon: AsyncClient, client: AsyncClient
) -> None:
    code, verifier, _ = await obtain_code(anon, client)
    results = await asyncio.gather(*(exchange(anon, code, verifier) for _ in range(4)))
    statuses = sorted(r.status_code for r in results)
    assert statuses.count(200) == 1, statuses


async def test_expired_code_is_refused(
    anon: AsyncClient, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(oauth_repo, "CODE_TTL_SECONDS", -1)
    code, verifier, _ = await obtain_code(anon, client)
    resp = await exchange(anon, code, verifier)
    assert resp.json()["error"] == "invalid_grant"


async def test_refresh_rotates_and_a_replay_revokes(
    anon: AsyncClient, client: AsyncClient, mcp_running: Any
) -> None:
    tokens = await obtain_tokens(anon, client)
    rotated = await refresh(anon, tokens["refresh_token"])
    assert rotated.status_code == 200, rotated.text
    new = rotated.json()
    assert new["refresh_token"] != tokens["refresh_token"]
    assert new["access_token"] != tokens["access_token"]
    assert (await mcp_list(anon, new["access_token"])).status_code == 200
    # The consumed refresh token presented again: the whole grant goes.
    replay = await refresh(anon, tokens["refresh_token"])
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"
    assert (await mcp_list(anon, new["access_token"])).status_code == 401
    assert (await refresh(anon, new["refresh_token"])).json()["error"] == "invalid_grant"


async def test_concurrent_refresh_has_one_winner(anon: AsyncClient, client: AsyncClient) -> None:
    tokens = await obtain_tokens(anon, client)
    results = await asyncio.gather(*(refresh(anon, tokens["refresh_token"]) for _ in range(4)))
    assert sorted(r.status_code for r in results).count(200) == 1


async def test_refresh_scope_and_resource_rules(anon: AsyncClient, client: AsyncClient) -> None:
    tokens = await obtain_tokens(anon, client)
    wider = await refresh(anon, tokens["refresh_token"], scope="catalog:read jobs:write")
    assert wider.json()["error"] == "invalid_scope"
    foreign = await refresh(anon, tokens["refresh_token"], resource="http://other.example/mcp")
    assert foreign.json()["error"] == "invalid_target"
    # Neither refusal consumed the token; a narrower scope and no resource are fine.
    ok = await refresh(anon, tokens["refresh_token"], scope="jobs:read", resource=None)
    assert ok.status_code == 200, ok.text
    other_client = await refresh(
        anon, ok.json()["refresh_token"], client_id="https://evil.example/c"
    )
    assert other_client.status_code == 401


async def test_revoked_grant_ends_refresh_too(anon: AsyncClient, client: AsyncClient) -> None:
    tokens = await obtain_tokens(anon, client)
    (grant,) = (await client.get("/api/mcp/grants")).json()["data"]
    assert (await client.delete(f"/api/mcp/grants/{grant['id']}")).status_code == 200
    assert (await refresh(anon, tokens["refresh_token"])).json()["error"] == "invalid_grant"


# ── revocation endpoint ──────────────────────────────────────────────


async def test_revocation_endpoint(
    anon: AsyncClient, client: AsyncClient, mcp_running: Any, app: Any
) -> None:
    tokens = await obtain_tokens(anon, client)
    unknown_client = await anon.post(
        "/oauth/revoke",
        data={"token": tokens["refresh_token"], "client_id": "https://evil.example/c"},
    )
    assert unknown_client.status_code == 401
    # Another registered client cannot revoke this client's grant; the answer is still 200.
    other = (
        await client.post(
            "/api/mcp/clients",
            json={"kind": "admin", "name": "Other", "redirect_uris": ["https://other.example/cb"]},
        )
    ).json()["id"]
    resp = await anon.post(
        "/oauth/revoke", data={"token": tokens["access_token"], "client_id": other}
    )
    assert resp.status_code == 200
    assert (await mcp_list(anon, tokens["access_token"])).status_code == 200
    # The owning client revokes with either token; the grant ends.
    resp = await anon.post(
        "/oauth/revoke", data={"token": tokens["refresh_token"], "client_id": CLAUDE_CODE}
    )
    assert resp.status_code == 200
    assert (await mcp_list(anon, tokens["access_token"])).status_code == 401
    (grant,) = await oauth_repo.list_grants(app.state.db)
    assert grant["status"] == "revoked"
    # Unknown tokens are not an error (RFC 7009 section 2.2).
    resp = await anon.post("/oauth/revoke", data={"token": "nope", "client_id": CLAUDE_CODE})
    assert resp.status_code == 200


# ── clients: documents, admin clients, disabling ─────────────────────


async def test_client_document_is_fetched_once_and_cached(
    anon: AsyncClient, claude_document: dict[str, int]
) -> None:
    for _ in range(3):
        txn_of((await authorize(anon))[0])
    assert claude_document["n"] == 1


async def test_refused_document_means_unknown_client(
    anon: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refusing(url: str, **_: Any) -> cimd.ClientDocument:
        raise cimd.DocumentError("resolves to a non-public address")

    monkeypatch.setattr(cimd, "fetch_document", refusing)
    resp, _ = await authorize(anon)
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


async def test_admin_client_flow_matches_redirects_exactly(
    anon: AsyncClient, client: AsyncClient, mcp_running: Any
) -> None:
    created = await client.post(
        "/api/mcp/clients",
        json={
            "kind": "admin",
            "name": "MCP Inspector",
            "redirect_uris": ["https://inspector.example:6274/cb"],
        },
    )
    assert created.status_code == 200, created.text
    client_id = created.json()["id"]
    # A non-loopback registered URI matches only with its own port.
    resp, _ = await authorize(
        anon, client_id=client_id, redirect_uri="https://inspector.example:6275/cb"
    )
    assert resp.status_code == 400
    tokens = await obtain_tokens(
        anon, client, client_id=client_id, redirect_uri="https://inspector.example:6274/cb"
    )
    assert (await mcp_list(anon, tokens["access_token"])).status_code == 200


async def test_disabled_client_is_cut_off_everywhere(
    anon: AsyncClient, client: AsyncClient, mcp_running: Any
) -> None:
    tokens = await obtain_tokens(anon, client)
    code, verifier, _ = await obtain_code(anon, client)
    from urllib.parse import quote

    assert (
        await client.delete(f"/api/mcp/clients/{quote(CLAUDE_CODE, safe='')}")
    ).status_code == 200
    resp, _ = await authorize(anon)
    assert resp.status_code == 400
    assert (await exchange(anon, code, verifier)).status_code == 401
    assert (await refresh(anon, tokens["refresh_token"])).status_code == 401
    assert (await mcp_list(anon, tokens["access_token"])).status_code == 401


# ── the step-up round trip (the Phase 2 acceptance test) ─────────────


async def test_step_up_round_trip(
    anon: AsyncClient, client: AsyncClient, mcp_running: Any, app: Any
) -> None:
    """A read grant is challenged for jobs:write, the client re-authorizes
    with the wider scope, and the retried call succeeds."""
    job = Job(
        source_path="/test/a.mkv",
        library="movies",
        source_codec="h264",
        quality_value=21,
        status=JobStatus.QUEUED,
    )
    await job_repo.create_job(app.state.db, job)

    read = await obtain_tokens(anon, client)
    challenged = await mcp_call(anon, read["access_token"], "cancel_job", {"job_id": job.id})
    assert challenged.status_code == 403
    challenge = challenged.headers["www-authenticate"]
    assert 'error="insufficient_scope"' in challenge
    assert 'scope="jobs:write"' in challenge

    write = await obtain_tokens(anon, client, scope=ALL_SCOPES)
    assert write["scope"] == ALL_SCOPES
    done = await mcp_call(anon, write["access_token"], "cancel_job", {"job_id": job.id})
    assert done.status_code == 200, done.text
    body = done.json()
    assert not body["result"].get("isError"), body
    assert "cancelled" in done.text
    refreshed = await job_repo.get_job(app.state.db, job.id)
    assert refreshed is not None and refreshed.status == JobStatus.CANCELLED
    # The read grant never gained the scope: two grants, one of each.
    rows = (await client.get("/api/mcp/grants")).json()["data"]
    assert sorted(" ".join(r["scopes"]) for r in rows) == [READ_SCOPES, ALL_SCOPES]


# ── secrets stay out of the logs (row 7) ─────────────────────────────


async def test_codes_and_tokens_never_reach_the_logs(
    anon: AsyncClient, client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    code, verifier, _ = await obtain_code(anon, client)
    tokens = (await exchange(anon, code, verifier)).json()
    rotated = (await refresh(anon, tokens["refresh_token"])).json()
    for secret in (
        code,
        verifier,
        tokens["access_token"],
        tokens["refresh_token"],
        rotated["access_token"],
    ):
        assert secret not in caplog.text
    assert '"event":"mcp_consent"' in caplog.text
    assert '"event":"mcp_tokens_issued"' in caplog.text


# ── unit: redirect matching ──────────────────────────────────────────


def test_redirect_matches_rule() -> None:
    assert authz.redirect_matches("http://localhost/callback", "http://localhost:3118/callback")
    assert authz.redirect_matches("http://127.0.0.1/callback", "http://127.0.0.1:60000/callback")
    assert authz.redirect_matches("http://[::1]/callback", "http://[::1]:9/callback")
    assert not authz.redirect_matches("http://localhost/callback", "http://localhost:3118/other")
    assert not authz.redirect_matches(
        "http://localhost/callback", "https://localhost:3118/callback"
    )
    assert not authz.redirect_matches(
        "http://localhost/callback", "http://localhost:3118/callback#x"
    )
    assert not authz.redirect_matches(
        "https://claude.ai/api/mcp/auth_callback", "https://claude.ai:8443/api/mcp/auth_callback"
    )
    assert authz.redirect_matches(
        "https://claude.ai/api/mcp/auth_callback", "https://claude.ai/api/mcp/auth_callback"
    )
    assert not authz.redirect_matches("https://a.example/cb?x=1", "https://a.example/cb?x=2")

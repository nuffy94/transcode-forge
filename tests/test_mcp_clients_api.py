"""Settings API for the OAuth clients: /api/mcp/clients (docs/MCP.md,
"Authorization server") and its Settings partial.

The allowlist of client metadata documents IS the clients table: allowing a
URL is the registration, disabling the row cuts the client off.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from httpx import AsyncClient

from transcode_forge.mcp import authz
from transcode_forge.repos import oauth as oauth_repo

CLAUDE_CODE = "https://claude.ai/oauth/claude-code-client-metadata"


def _path(client_id: str) -> str:
    return f"/api/mcp/clients/{quote(client_id, safe='')}"


async def test_requires_admin_session(unauthed_client: AsyncClient) -> None:
    assert (await unauthed_client.get("/api/mcp/clients")).status_code == 401
    assert (
        await unauthed_client.post(
            "/api/mcp/clients", json={"kind": "cimd", "url": "https://x.example/d"}
        )
    ).status_code == 401
    assert (await unauthed_client.delete(_path("x"))).status_code == 401
    partial = await unauthed_client.get("/partials/mcp-clients", follow_redirects=False)
    assert partial.status_code == 302


async def test_seeded_clients_are_listed_and_the_manual_client_is_not(
    client: AsyncClient, app: Any
) -> None:
    await authz.seed_clients(app.state.db)
    await oauth_repo.create_manual_grant(
        app.state.db, label="l", scopes={"jobs:read"}, resource="http://test/mcp", user_id="u"
    )
    listing = (await client.get("/api/mcp/clients")).json()
    ids = [row["id"] for row in listing["data"]]
    assert ids == [CLAUDE_CODE]
    assert oauth_repo.MANUAL_CLIENT_ID not in ids
    (row,) = listing["data"]
    assert row["kind"] == "cimd"
    assert row["name"] == "Claude Code"
    assert row["status"] == "active"
    assert row["redirect_uris"] == []  # read at first sign-in


async def test_allow_document_client(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/mcp/clients", json={"kind": "cimd", "url": "https://apps.example/oauth/client"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": "https://apps.example/oauth/client", "kind": "cimd"}
    for bad in ("http://apps.example/doc", "https://apps.example/doc#x", "not a url", ""):
        resp = await client.post("/api/mcp/clients", json={"kind": "cimd", "url": bad})
        assert resp.status_code == 422, bad


async def test_allow_admin_client(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/mcp/clients",
        json={"kind": "admin", "name": "Inspector", "redirect_uris": ["https://i.example/cb", " "]},
    )
    assert resp.status_code == 200, resp.text
    client_id = resp.json()["id"]
    assert client_id.startswith("tf-")
    (row,) = (await client.get("/api/mcp/clients")).json()["data"]
    assert row["name"] == "Inspector"
    assert row["redirect_uris"] == ["https://i.example/cb"]
    for bad in (
        {"kind": "admin", "name": "", "redirect_uris": ["https://i.example/cb"]},
        {"kind": "admin", "name": "x", "redirect_uris": []},
        {"kind": "admin", "name": "x", "redirect_uris": ["ftp://i.example/cb"]},
        {"kind": "admin", "name": "x", "redirect_uris": ["https://i.example/cb#f"]},
        {"kind": "admin", "name": "x", "redirect_uris": ["/relative"]},
        {"kind": "other", "name": "x", "redirect_uris": ["https://i.example/cb"]},
    ):
        assert (await client.post("/api/mcp/clients", json=bad)).status_code == 422, bad


async def test_disable_and_allow_again(client: AsyncClient, app: Any) -> None:
    await authz.seed_clients(app.state.db)
    assert (await client.delete(_path(CLAUDE_CODE))).status_code == 200
    (row,) = (await client.get("/api/mcp/clients")).json()["data"]
    assert row["status"] == "disabled"
    assert (await client.delete(_path(CLAUDE_CODE))).status_code == 404
    assert (await client.delete(_path("never-existed"))).status_code == 404
    assert (await client.delete(_path(oauth_repo.MANUAL_CLIENT_ID))).status_code == 404
    # Allowing the same URL again re-enables it.
    assert (
        await client.post("/api/mcp/clients", json={"kind": "cimd", "url": CLAUDE_CODE})
    ).status_code == 200
    (row,) = (await client.get("/api/mcp/clients")).json()["data"]
    assert row["status"] == "active"


async def test_refused_without_public_url(client: AsyncClient, app: Any) -> None:
    app.state.settings.public_url = ""
    resp = await client.post(
        "/api/mcp/clients", json={"kind": "cimd", "url": "https://x.example/d"}
    )
    assert resp.status_code == 409
    assert "TF_PUBLIC_URL" in resp.json()["detail"]


async def test_partial_lists_clients(client: AsyncClient, app: Any) -> None:
    await authz.seed_clients(app.state.db)
    html = (await client.get("/partials/mcp-clients")).text
    assert "Claude Code" in html
    assert CLAUDE_CODE in html
    assert "metadata document" in html
    assert "Active" in html
    settings_page = (await client.get("/settings")).text
    assert "Allow client" in settings_page
    assert 'hx-get="/partials/mcp-clients"' in settings_page

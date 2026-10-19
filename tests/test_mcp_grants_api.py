"""Settings API for MCP grants: /api/mcp/grants (docs/MCP.md, Phase 1).

The operator issues a manual grant here for a client that configures a
bearer header instead of running the browser flow, sees the grants that
exist, and revokes one. The token is returned once and never shown again.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest_asyncio
from httpx import AsyncClient

from transcode_forge.repos import oauth as oauth_repo

PV = "2026-07-28"
META = {
    "io.modelcontextprotocol/protocolVersion": PV,
    "io.modelcontextprotocol/clientCapabilities": {},
}


@pytest_asyncio.fixture
async def mcp_running(app: Any) -> Any:
    started, stop = asyncio.Event(), asyncio.Event()

    async def runner() -> None:
        async with app.state.mcp_session_manager.run():
            started.set()
            await stop.wait()

    task = asyncio.create_task(runner())
    await started.wait()
    yield
    stop.set()
    await task


async def _tools_list(c: AsyncClient, token: str) -> Any:
    return await c.post(
        "/mcp",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PV,
            "Mcp-Method": "tools/list",
            "Authorization": f"Bearer {token}",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": META}},
    )


async def test_requires_admin_session(unauthed_client: AsyncClient) -> None:
    assert (await unauthed_client.get("/api/mcp/grants")).status_code == 401
    assert (
        await unauthed_client.post("/api/mcp/grants", json={"label": "x", "scopes": ["jobs:read"]})
    ).status_code == 401
    assert (await unauthed_client.delete("/api/mcp/grants/some-id")).status_code == 401


async def test_issue_list_use_revoke(client: AsyncClient, app: Any, mcp_running: Any) -> None:
    resp = await client.post("/api/mcp/grants", json={"label": "laptop", "scopes": ["jobs:read"]})
    assert resp.status_code == 200, resp.text
    issued = resp.json()
    token = issued["token"]
    assert issued["label"] == "laptop"
    assert issued["scopes"] == ["jobs:read"]
    assert issued["resource"] == "http://test/mcp"
    assert issued["client_id"] == oauth_repo.MANUAL_CLIENT_ID
    assert issued["expires_at"]

    # The token works on the endpoint.
    assert (await _tools_list(client, token)).status_code == 200

    # The list shows the grant and never the token.
    listing = (await client.get("/api/mcp/grants")).json()
    assert listing["meta"]["total"] == 1
    (row,) = listing["data"]
    assert row["id"] == issued["grant_id"]
    assert row["label"] == "laptop"
    assert row["revoked_at"] is None
    assert row["status"] == "active"
    assert token not in str(listing)

    # Revoke: the token stops working at its next check.
    resp = await client.delete(f"/api/mcp/grants/{issued['grant_id']}")
    assert resp.status_code == 200
    assert (await _tools_list(client, token)).status_code == 401
    after = (await client.get("/api/mcp/grants")).json()["data"][0]
    assert after["revoked_at"] is not None
    assert after["status"] == "revoked"
    # A second revoke of the same grant is a 404, not a silent success.
    assert (await client.delete(f"/api/mcp/grants/{issued['grant_id']}")).status_code == 404


async def test_validation(client: AsyncClient) -> None:
    bad = [
        {"label": "", "scopes": ["jobs:read"]},
        {"label": "x", "scopes": []},
        {"label": "x", "scopes": ["admin:*"]},
        {"label": "x", "scopes": ["jobs:read"], "expires_in_days": 0},
        {"label": "x", "scopes": ["jobs:read"], "expires_in_days": 400},
        {"label": "x" * 81, "scopes": ["jobs:read"]},
    ]
    for body in bad:
        resp = await client.post("/api/mcp/grants", json=body)
        assert resp.status_code == 422, body
    assert (await client.delete("/api/mcp/grants/does-not-exist")).status_code == 404


async def test_expiry_follows_request(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/mcp/grants", json={"label": "short", "scopes": ["catalog:read"], "expires_in_days": 1}
    )
    assert resp.status_code == 200
    issued = resp.json()
    listing = (await client.get("/api/mcp/grants")).json()["data"]
    assert listing[0]["expires_at"] == issued["expires_at"]


async def test_issue_refused_without_public_url(client: AsyncClient, app: Any) -> None:
    app.state.settings.public_url = ""
    resp = await client.post("/api/mcp/grants", json={"label": "x", "scopes": ["jobs:read"]})
    assert resp.status_code == 409
    assert "TF_PUBLIC_URL" in resp.json()["detail"]

"""Connected apps on the Settings page (docs/MCP.md, Phase 1).

The section lists MCP grants, issues a manual one and revokes one. The
token appears only in the issue response, never in a rendered page.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from httpx import AsyncClient

from transcode_forge.repos import oauth as oauth_repo


async def test_settings_page_shows_connected_apps(client: AsyncClient) -> None:
    resp = await client.get("/settings")
    assert resp.status_code == 200
    assert "Connected apps" in resp.text
    assert "http://test/mcp" in resp.text
    assert 'id="mcp-grant-issue-btn"' in resp.text


async def test_settings_page_explains_how_to_turn_it_on(client: AsyncClient, app: Any) -> None:
    app.state.settings.public_url = ""
    resp = await client.get("/settings")
    assert resp.status_code == 200
    assert "Connected apps" in resp.text
    assert "TF_PUBLIC_URL" in resp.text
    assert 'id="mcp-grant-issue-btn"' not in resp.text


async def test_partial_empty_state(client: AsyncClient) -> None:
    resp = await client.get("/partials/mcp-grants")
    assert resp.status_code == 200
    assert "No connected apps" in resp.text


async def test_partial_lists_grant_without_token(client: AsyncClient) -> None:
    issued = (
        await client.post("/api/mcp/grants", json={"label": "desk", "scopes": ["jobs:read"]})
    ).json()
    resp = await client.get("/partials/mcp-grants")
    assert resp.status_code == 200
    assert "desk" in resp.text
    assert "jobs:read" in resp.text
    assert "Active" in resp.text
    assert issued["token"] not in resp.text
    assert f'data-grant="{issued["grant_id"]}"' in resp.text


async def test_partial_shows_revoked(client: AsyncClient) -> None:
    issued = (
        await client.post("/api/mcp/grants", json={"label": "old", "scopes": ["catalog:read"]})
    ).json()
    await client.delete(f"/api/mcp/grants/{issued['grant_id']}")
    resp = await client.get("/partials/mcp-grants")
    assert "Revoked" in resp.text
    assert f'data-grant="{issued["grant_id"]}"' not in resp.text


async def test_partial_shows_expired(client: AsyncClient, app: Any) -> None:
    yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    await oauth_repo.create_manual_grant(
        app.state.db,
        label="stale",
        scopes={"jobs:read"},
        resource="http://test/mcp",
        user_id="admin-1",
        expires_at=yesterday,
    )
    resp = await client.get("/partials/mcp-grants")
    assert "stale" in resp.text
    assert "Expired" in resp.text
    assert "Active" not in resp.text


async def test_partial_requires_session(unauthed_client: AsyncClient) -> None:
    resp = await unauthed_client.get("/partials/mcp-grants")
    assert resp.status_code in (302, 401)

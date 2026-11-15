"""Grant store for the MCP endpoint (docs/MCP.md, "Grants and tokens").

The grant is the unit of permission. Tokens are stored only as kind-prefixed
HMAC digests, so a worker token or a token of another kind can never verify
as an access token, and the raw value never reaches the database.
Threat-model rows 1 and 7.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from transcode_forge.repos import oauth as oauth_repo
from transcode_forge.repos import worker_tokens as worker_token_repo

RESOURCE = "http://test/mcp"


async def _manual(db: Any, **overrides: Any) -> tuple[str, str]:
    kwargs: dict[str, Any] = {
        "label": "laptop",
        "scopes": {"jobs:read"},
        "resource": RESOURCE,
        "user_id": "admin-1",
    }
    kwargs.update(overrides)
    return await oauth_repo.create_manual_grant(db, **kwargs)


async def test_manual_grant_round_trip(db: Any) -> None:
    grant_id, raw = await _manual(db, scopes={"jobs:read", "catalog:read"})
    principal = await oauth_repo.verify_access_token(db, raw, resource=RESOURCE)
    assert principal is not None
    assert principal.grant_id == grant_id
    assert principal.client_id == oauth_repo.MANUAL_CLIENT_ID
    assert principal.user_id == "admin-1"
    assert principal.scopes == frozenset({"jobs:read", "catalog:read"})
    assert principal.resource == RESOURCE


async def test_raw_token_is_never_stored(db: Any) -> None:
    _, raw = await _manual(db)
    async with db.execute("SELECT * FROM oauth_tokens") as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert raw not in [str(cell) for cell in rows[0]]
    assert rows[0]["token_hash"] == oauth_repo.hash_token("access", raw)


async def test_token_kinds_hash_differently() -> None:
    raw = oauth_repo.generate()
    assert oauth_repo.hash_token("access", raw) != oauth_repo.hash_token("refresh", raw)
    assert oauth_repo.hash_token("access", raw) != worker_token_repo.hash_token(raw)


async def test_worker_token_does_not_verify_as_access_token(db: Any) -> None:
    raw = await worker_token_repo.create(db, label="node-a")
    assert await oauth_repo.verify_access_token(db, raw, resource=RESOURCE) is None


async def test_random_string_does_not_verify(db: Any) -> None:
    await _manual(db)
    assert await oauth_repo.verify_access_token(db, "not-a-token", resource=RESOURCE) is None
    assert await oauth_repo.verify_access_token(db, "", resource=RESOURCE) is None


async def test_revoked_grant_rejects_its_token(db: Any) -> None:
    grant_id, raw = await _manual(db)
    assert await oauth_repo.revoke_grant(db, grant_id) is True
    assert await oauth_repo.verify_access_token(db, raw, resource=RESOURCE) is None
    # Second revoke is a no-op, not an error.
    assert await oauth_repo.revoke_grant(db, grant_id) is False


async def test_expired_access_token_rejects(db: Any) -> None:
    grant_id, _ = await _manual(db)
    raw = await oauth_repo.issue_access_token(db, grant_id, ttl_seconds=-1)
    assert await oauth_repo.verify_access_token(db, raw, resource=RESOURCE) is None


async def test_grant_past_its_deadline_rejects(db: Any) -> None:
    yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    _, raw = await _manual(db, expires_at=yesterday)
    assert await oauth_repo.verify_access_token(db, raw, resource=RESOURCE) is None


async def test_disabled_client_rejects(db: Any) -> None:
    _, raw = await _manual(db)
    await oauth_repo.disable_client(db, oauth_repo.MANUAL_CLIENT_ID)
    assert await oauth_repo.verify_access_token(db, raw, resource=RESOURCE) is None


async def test_token_for_another_resource_rejects(db: Any) -> None:
    _, raw = await _manual(db, resource="https://other.example/mcp")
    assert await oauth_repo.verify_access_token(db, raw, resource=RESOURCE) is None


async def test_unknown_scope_is_refused(db: Any) -> None:
    with pytest.raises(ValueError):
        await _manual(db, scopes={"jobs:read", "admin:*"})
    with pytest.raises(ValueError):
        await _manual(db, scopes=set())


async def test_list_grants_shows_no_secret_material(db: Any) -> None:
    grant_id, raw = await _manual(db, label="desk")
    rows = await oauth_repo.list_grants(db)
    assert [r["id"] for r in rows] == [grant_id]
    row = rows[0]
    assert row["label"] == "desk"
    assert row["client_name"]
    assert row["scopes"] == ["jobs:read"]
    assert row["revoked_at"] is None
    assert raw not in str(row)
    assert oauth_repo.hash_token("access", raw) not in str(row)


async def test_verify_touches_last_used(db: Any) -> None:
    _, raw = await _manual(db)
    before = (await oauth_repo.list_grants(db))[0]["last_used_at"]
    assert before is None
    await oauth_repo.verify_access_token(db, raw, resource=RESOURCE)
    after = (await oauth_repo.list_grants(db))[0]["last_used_at"]
    assert after is not None

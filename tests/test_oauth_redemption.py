"""Redemption on SQLite's shared connection (docs/MCP-THREAT-MODEL.md row 8).

On SQLite every request shares one connection, so another request's
rollback between a conditional update and its commit erases the update
while the cursor still reports a row changed. The consume primitive reads
its marker back after the commit; these tests stage exactly that rollback
and check that nothing is issued on an erased consumption, that it is not
mistaken for a replay, and that the retry works.
"""

from __future__ import annotations

from typing import Any

import pytest

from transcode_forge.repos import oauth as oauth_repo

pytestmark = pytest.mark.sqlite_only

RESOURCE = "http://test/mcp"
CLIENT = "https://c.example/doc"


async def _code(db: Any) -> str:
    await oauth_repo.allow_cimd_client(db, CLIENT, name="c")
    txn = await oauth_repo.create_transaction(
        db,
        client_id=CLIENT,
        redirect_uri="http://localhost/cb",
        code_challenge="x" * 43,
        scopes={"jobs:read"},
        resource=RESOURCE,
        state=None,
    )
    nonce = await oauth_repo.arm_consent(db, txn)
    assert nonce
    code = await oauth_repo.approve_transaction(db, txn, nonce=nonce, user_id="admin-1")
    assert code
    return code


def _rollback_before_next_commit(db: Any) -> None:
    """Make the next commit roll the connection back first, as a concurrent
    request's failed transaction does on the shared connection."""
    real = db.commit

    async def hostile() -> None:
        db.commit = real
        await db._conn.rollback()
        await real()

    db.commit = hostile


async def _grant_status(db: Any) -> str:
    (grant,) = await oauth_repo.list_grants(db)
    return str(grant["status"])


async def test_code_consumption_erased_by_a_rollback_is_refused_not_trusted(db: Any) -> None:
    code = await _code(db)
    _rollback_before_next_commit(db)
    assert await oauth_repo.redeem_code(db, code) is None
    assert await _grant_status(db) == "active"  # not treated as a replay
    retried = await oauth_repo.redeem_code(db, code)
    assert retried is not None and retried["grant_id"]
    with pytest.raises(oauth_repo.ReplayError):
        await oauth_repo.redeem_code(db, code)
    assert await _grant_status(db) == "revoked"


async def test_refresh_consumption_erased_by_a_rollback_is_refused_not_trusted(db: Any) -> None:
    code = await _code(db)
    txn = await oauth_repo.redeem_code(db, code)
    assert txn is not None
    issued = await oauth_repo.issue_tokens(db, txn["grant_id"])
    _rollback_before_next_commit(db)
    assert await oauth_repo.redeem_refresh_token(db, issued.refresh_token) is None
    assert await _grant_status(db) == "active"
    grant = await oauth_repo.redeem_refresh_token(db, issued.refresh_token)
    assert grant is not None
    with pytest.raises(oauth_repo.ReplayError):
        await oauth_repo.redeem_refresh_token(db, issued.refresh_token)
    assert await _grant_status(db) == "revoked"

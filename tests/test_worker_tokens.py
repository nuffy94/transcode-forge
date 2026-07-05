"""Worker-token hashing-at-rest tests (M4 Step 13).

Tokens are stored as HMAC-SHA256 hashes; auth looks them up by hash only.
These guard the migration backfill, expiry, revocation, and the invariant
that the plaintext column is never consulted for auth.
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from transcode_forge.migrations import _backfill_token_hashes_sqlite
from transcode_forge.repos import worker_tokens as token_repo


async def test_create_then_find_active_by_hash(db: Any) -> None:
    raw = await token_repo.create(db, label="node-a")
    row = await token_repo.find_active(db, raw)
    assert row is not None
    assert row["label"] == "node-a"
    assert row["token_hash"] == token_repo.hash_token(raw)


async def test_find_active_uses_hash_not_plaintext(db: Any) -> None:
    """Null the plaintext column and the token still authenticates by hash —
    proves auth never reads it, so dropping it in v0.7 is safe."""
    raw = await token_repo.create(db, label="node-b")
    await db.execute(
        "UPDATE worker_tokens SET token = NULL WHERE token_hash = ?",
        (token_repo.hash_token(raw),),
    )
    await db.commit()
    assert await token_repo.find_active(db, raw) is not None


async def test_unknown_token_not_active(db: Any) -> None:
    await token_repo.create(db, label="node-c")
    assert await token_repo.find_active(db, "not-a-real-token") is None


async def test_revoked_token_not_active(db: Any) -> None:
    raw = await token_repo.create(db, label="node-d")
    assert await token_repo.revoke(db, raw[:6] + "…") is True
    assert await token_repo.find_active(db, raw) is None


async def test_expired_token_not_active(db: Any) -> None:
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    raw = await token_repo.create(db, label="node-e", expires_at=past)
    assert await token_repo.find_active(db, raw) is None


async def test_future_expiry_still_active(db: Any) -> None:
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    raw = await token_repo.create(db, label="node-f", expires_at=future)
    assert await token_repo.find_active(db, raw) is not None


async def test_list_all_masks_token_with_fingerprint(db: Any) -> None:
    raw = await token_repo.create(db, label="node-g")
    rows = await token_repo.list_all(db)
    entry = next(r for r in rows if r["label"] == "node-g")
    assert "token" not in entry and "token_hash" not in entry
    assert entry["fingerprint"] == raw[:6] + "…"


async def test_link_worker_cas_first_bind_wins(db: Any) -> None:
    """link_worker is a compare-and-set: once bound, a second bind attempt
    (the leaked-token race) is a no-op and reports False."""
    raw = await token_repo.create(db, label="cas")
    token_hash = token_repo.hash_token(raw)
    assert await token_repo.link_worker(db, token_hash=token_hash, worker_id="w-1") is True
    assert await token_repo.link_worker(db, token_hash=token_hash, worker_id="w-2") is False
    row = await token_repo.find_active(db, raw)
    assert row is not None
    assert row["worker_id"] == "w-1"


async def test_link_worker_cas_expected_rebind(db: Any) -> None:
    """Relinking (worker row wiped, token still bound) must name the stale
    binding it expects — a mismatched expectation means someone else got
    there first and the update is a no-op."""
    raw = await token_repo.create(db, label="cas2")
    token_hash = token_repo.hash_token(raw)
    await token_repo.link_worker(db, token_hash=token_hash, worker_id="w-1")
    assert (
        await token_repo.link_worker(
            db, token_hash=token_hash, worker_id="w-2", expected_worker_id="w-1"
        )
        is True
    )
    assert (
        await token_repo.link_worker(
            db, token_hash=token_hash, worker_id="w-3", expected_worker_id="w-1"
        )
        is False
    )
    row = await token_repo.find_active(db, raw)
    assert row is not None
    assert row["worker_id"] == "w-2"


async def test_unique_worker_binding_enforced_by_schema(db: Any) -> None:
    """Migration 0010's unique index is the backstop: no code path may bind
    two tokens to the same worker identity."""
    raw_a = await token_repo.create(db, label="a")
    raw_b = await token_repo.create(db, label="b")
    await token_repo.link_worker(
        db, token_hash=token_repo.hash_token(raw_a), worker_id="same-worker"
    )
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "UPDATE worker_tokens SET worker_id = ? WHERE token_hash = ?",
            ("same-worker", token_repo.hash_token(raw_b)),
        )


async def test_backfill_hashes_legacy_plaintext_row(db: Any) -> None:
    """A row that predates 0004 (plaintext only, NULL hash) authenticates
    after the backfill hook runs."""
    legacy = token_repo.generate()
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT INTO worker_tokens (token, label, created_at) VALUES (?, ?, ?)",
        (legacy, "legacy-node", now),
    )
    await db.commit()
    assert await token_repo.find_active(db, legacy) is None  # no hash yet

    await _backfill_token_hashes_sqlite(db._conn)
    await db.commit()

    row = await token_repo.find_active(db, legacy)
    assert row is not None
    assert row["label"] == "legacy-node"
    assert row["token_prefix"] == legacy[:6]

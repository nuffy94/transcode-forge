"""Worker-token hashing-at-rest tests (M4 Step 13).

Tokens are stored as HMAC-SHA256 hashes; auth looks them up by hash only.
These guard the migration backfill, expiry, revocation, and the invariant
that the plaintext column is never consulted for auth.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

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

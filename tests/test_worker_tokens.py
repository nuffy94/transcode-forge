"""Worker-token hashing-at-rest tests (M4 Step 13).

Tokens are stored as HMAC-SHA256 hashes; auth looks them up by hash only.
These guard expiry, revocation, and the invariant that the raw token is
never written to the database (migration 0016 dropped the plaintext
column). The 0004 backfill hook is covered by the upgrade-path tests in
test_migrations.py, the only place the old column still exists.
"""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest

from transcode_forge.repos import worker_tokens as token_repo

# The unique-binding backstop raises a driver-specific integrity error —
# sqlite3.IntegrityError on SQLite, an asyncpg IntegrityConstraintViolationError
# (UniqueViolationError's base) on Postgres.
_INTEGRITY_ERRORS = (sqlite3.IntegrityError, asyncpg.exceptions.IntegrityConstraintViolationError)


async def test_create_then_find_active_by_hash(db: Any) -> None:
    raw = await token_repo.create(db, label="node-a")
    row = await token_repo.find_active(db, raw)
    assert row is not None
    assert row["label"] == "node-a"
    assert row["token_hash"] == token_repo.hash_token(raw)


async def test_raw_token_is_never_stored(db: Any) -> None:
    """Migration 0016 dropped the plaintext column: the stored row has no
    `token` column at all and no cell in it equals the raw token."""
    raw = await token_repo.create(db, label="node-b")
    async with db.execute(
        "SELECT * FROM worker_tokens WHERE token_hash = ?", (token_repo.hash_token(raw),)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    stored = dict(row)
    assert "token" not in stored
    assert raw not in stored.values()
    assert await token_repo.find_active(db, raw) is not None


async def test_unknown_token_not_active(db: Any) -> None:
    await token_repo.create(db, label="node-c")
    assert await token_repo.find_active(db, "not-a-real-token") is None


async def test_revoked_token_not_active(db: Any) -> None:
    raw = await token_repo.create(db, label="node-d")
    assert await token_repo.revoke(db, raw[:6] + "…") is True
    assert await token_repo.find_active(db, raw) is None


async def test_revoke_by_raw_token(db: Any) -> None:
    raw = await token_repo.create(db, label="node-d2")
    assert await token_repo.revoke(db, raw) is True
    assert await token_repo.find_active(db, raw) is None


async def test_touch_stamps_last_used_at(db: Any) -> None:
    raw = await token_repo.create(db, label="node-d3")
    await token_repo.touch(db, raw)
    rows = await token_repo.list_all(db)
    entry = next(r for r in rows if r["label"] == "node-d3")
    assert entry["last_used_at"] is not None


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


async def test_link_worker_concurrent_first_bind_single_winner(db: Any) -> None:
    """TRUE-concurrency CAS race (PR #49 follow-up): two workers racing to
    first-bind the same token in parallel — exactly one may win. Under the
    Postgres CI lane each UPDATE runs on its own pooled connection, so this
    exercises the real row-lock race that single-writer SQLite (where the
    sequential tests above run) can only serialize."""
    raw = await token_repo.create(db, label="race-first-bind")
    token_hash = token_repo.hash_token(raw)

    results = await asyncio.gather(
        token_repo.link_worker(db, token_hash=token_hash, worker_id="racer-a"),
        token_repo.link_worker(db, token_hash=token_hash, worker_id="racer-b"),
    )

    assert sorted(results) == [False, True]  # exactly one winner
    winner = "racer-a" if results[0] else "racer-b"
    row = await token_repo.find_active(db, raw)
    assert row is not None
    assert row["worker_id"] == winner


async def test_link_worker_concurrent_rebind_single_winner(db: Any) -> None:
    """The same race on the rebind path: two machines presenting the same
    (leaked) token and naming the same stale binding can't both steal it."""
    raw = await token_repo.create(db, label="race-rebind")
    token_hash = token_repo.hash_token(raw)
    assert await token_repo.link_worker(db, token_hash=token_hash, worker_id="original") is True

    results = await asyncio.gather(
        token_repo.link_worker(
            db, token_hash=token_hash, worker_id="thief-1", expected_worker_id="original"
        ),
        token_repo.link_worker(
            db, token_hash=token_hash, worker_id="thief-2", expected_worker_id="original"
        ),
    )

    assert sorted(results) == [False, True]
    winner = "thief-1" if results[0] else "thief-2"
    row = await token_repo.find_active(db, raw)
    assert row is not None
    assert row["worker_id"] == winner


async def test_unique_worker_binding_enforced_by_schema(db: Any) -> None:
    """Migration 0010's unique index is the backstop: no code path may bind
    two tokens to the same worker identity."""
    raw_a = await token_repo.create(db, label="a")
    raw_b = await token_repo.create(db, label="b")
    await token_repo.link_worker(
        db, token_hash=token_repo.hash_token(raw_a), worker_id="same-worker"
    )
    with pytest.raises(_INTEGRITY_ERRORS):
        await db.execute(
            "UPDATE worker_tokens SET worker_id = ? WHERE token_hash = ?",
            ("same-worker", token_repo.hash_token(raw_b)),
        )
        await db.commit()

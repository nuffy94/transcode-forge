"""Worker token repository — issuance, lookup, revocation.

Tokens are stored as an HMAC-SHA256 hash (key = pepper, see config). Auth
looks up by hash only. The raw token is never written to the database:
migration 0016 dropped the plaintext column that 0004 had left behind.
The 6-char `token_prefix` is the fingerprint shown in the UI.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime
from typing import Any

from transcode_forge.config import get_token_pepper
from transcode_forge.db import DBConnection

FINGERPRINT_LEN = 6


def generate() -> str:
    """Return a new opaque token. 32 random bytes, URL-safe encoded → 43 chars."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """HMAC-SHA256 of the token under the process pepper. Hex digest."""
    return hmac.new(get_token_pepper().encode(), token.encode(), hashlib.sha256).hexdigest()


def fingerprint_prefix(token: str) -> str:
    """First 6 chars of the raw token — the UI fingerprint (sans the '…')."""
    return token[:FINGERPRINT_LEN]


async def create(db: DBConnection, label: str, expires_at: str | None = None) -> str:
    """Issue a new token. Returns the raw token — show it once, then never
    again (only its hash + prefix are stored)."""
    token = generate()
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT INTO worker_tokens "
        "(token_hash, token_prefix, label, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (hash_token(token), fingerprint_prefix(token), label, now, expires_at),
    )
    await db.commit()
    return token


async def list_all(db: DBConnection) -> list[dict[str, Any]]:
    """Return all tokens with their bound worker_id and revocation status.

    The token value is never exposed — callers see only the fingerprint
    (prefix + '…') so they can identify a row without leaking the credential.
    """
    async with db.execute(
        "SELECT token_prefix, label, worker_id, created_at, revoked_at, "
        "last_used_at, expires_at FROM worker_tokens ORDER BY created_at DESC"
    ) as cur:
        rows = await cur.fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["fingerprint"] = (d.pop("token_prefix") or "") + "…"
        out.append(d)
    return out


async def find_active(db: DBConnection, token: str) -> dict[str, Any] | None:
    """Look up a token by its hash. Returns the row only if it has not been
    revoked and has not expired."""
    now = datetime.now(UTC).isoformat()
    async with db.execute(
        "SELECT token_hash, token_prefix, label, worker_id, created_at, expires_at "
        "FROM worker_tokens "
        "WHERE token_hash = ? AND revoked_at IS NULL "
        "AND (expires_at IS NULL OR expires_at > ?)",
        (hash_token(token), now),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def link_worker(
    db: DBConnection,
    token_hash: str,
    worker_id: str,
    *,
    expected_worker_id: str | None = None,
) -> bool:
    """Bind a worker_id to a token (called on first /register).

    Compare-and-set: the UPDATE only fires if the token's current binding
    still matches ``expected_worker_id`` (None = not yet bound). Two
    machines racing to register with the same leaked token can't both win —
    the loser's conditional UPDATE matches zero rows and this returns False,
    which the registration endpoint turns into a 409.
    """
    if expected_worker_id is None:
        cur = await db.execute(
            "UPDATE worker_tokens SET worker_id = ? WHERE token_hash = ? AND worker_id IS NULL",
            (worker_id, token_hash),
        )
    else:
        cur = await db.execute(
            "UPDATE worker_tokens SET worker_id = ? WHERE token_hash = ? AND worker_id = ?",
            (worker_id, token_hash, expected_worker_id),
        )
    await db.commit()
    return bool(cur.rowcount)


async def touch(db: DBConnection, token: str) -> None:
    """Stamp last_used_at — useful for surfacing 'last seen' in the UI."""
    await db.execute(
        "UPDATE worker_tokens SET last_used_at = ? WHERE token_hash = ?",
        (datetime.now(UTC).isoformat(), hash_token(token)),
    )
    await db.commit()


def _match_clause(token_or_fingerprint: str) -> tuple[str, str]:
    """Return (column, value) for matching by fingerprint OR raw token.

    The UI hands back the masked fingerprint ('abc123…'); a raw token (no
    '…') is matched by its hash. Never compares the plaintext column.
    """
    if "…" in token_or_fingerprint:
        return "token_prefix", token_or_fingerprint.split("…")[0]
    return "token_hash", hash_token(token_or_fingerprint)


async def find_worker_id_for_token(db: DBConnection, token_or_fingerprint: str) -> str | None:
    """Return the worker_id bound to a token, or None if no match.

    Accepts the masked fingerprint shown in the UI or a raw token. Used by
    the revoke handler to cascade-delete the dead worker row."""
    column, value = _match_clause(token_or_fingerprint)
    async with db.execute(
        f"SELECT worker_id FROM worker_tokens WHERE {column} = ?",
        (value,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    wid = dict(row).get("worker_id")
    return wid if isinstance(wid, str) else None


async def revoke(db: DBConnection, token_or_fingerprint: str) -> bool:
    """Revoke by raw token OR by the fingerprint shown in the UI.

    Returns True if a row was updated.
    """
    now = datetime.now(UTC).isoformat()
    column, value = _match_clause(token_or_fingerprint)
    cursor = await db.execute(
        f"UPDATE worker_tokens SET revoked_at = ? WHERE {column} = ? AND revoked_at IS NULL",
        (now, value),
    )
    await db.commit()
    return bool(cursor.rowcount)

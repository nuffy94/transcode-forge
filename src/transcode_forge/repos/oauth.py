"""Grant store for the MCP endpoint: clients, grants, tokens.

The grant is the unit of permission (docs/MCP.md, "Grants and tokens"). A
token is stored only as an HMAC-SHA256 digest under the token pepper, with
the token kind prefixed into the digest input, so a worker token or a token
of another kind can never verify as an access token. The raw value is
returned once at issue time and never written.

Verification is one query: token, then its grant, then the grant's client.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from transcode_forge.config import get_token_pepper
from transcode_forge.db import DBConnection

SCOPE_DESCRIPTIONS: dict[str, str] = {
    "catalog:read": "Read the libraries and the media catalog",
    "jobs:read": "Read jobs, the queue state and statistics",
    "jobs:write": "Queue files and cancel waiting jobs",
}
"""Every scope the server knows, with the words the consent page uses."""

SCOPES: frozenset[str] = frozenset(SCOPE_DESCRIPTIONS)
"""A grant may only carry scopes from this set."""

INITIAL_SCOPES: tuple[str, ...] = ("catalog:read", "jobs:read")
"""The minimal set advertised to a client that has not been challenged yet."""

MANUAL_CLIENT_ID = "manual"
"""The client every operator-issued bearer token belongs to."""

ACCESS_TTL_SECONDS = 3600
GRANT_TTL_DAYS = 30
MANUAL_GRANT_TTL_DAYS = 90
TRANSACTION_TTL_SECONDS = 600
"""How long an authorization request waits for the consent decision."""
CODE_TTL_SECONDS = 600
"""How long an issued authorization code can be redeemed."""

TOKEN_KINDS = frozenset({"access", "refresh", "code", "nonce"})
"""The kinds the digest separates. Access and refresh tokens live in
oauth_tokens; a code and a consent nonce live on their transaction row."""


class ReplayError(Exception):
    """A consumed code or refresh token was presented again. The grant it
    belongs to has been revoked (OAuth 2.1 section 4.3.1, RFC 9700 4.14)."""


class ScopeError(ValueError):
    """A refresh asked for more than its grant holds. Nothing was consumed."""


@dataclass(frozen=True)
class Principal:
    """What a verified access token stands for."""

    grant_id: str
    client_id: str
    user_id: str
    scopes: frozenset[str]
    resource: str
    expires_at: int
    """Access token expiry, seconds since the epoch."""


def generate() -> str:
    """A new opaque token: 32 random bytes, URL-safe encoded."""
    return secrets.token_urlsafe(32)


def hash_token(kind: str, token: str) -> str:
    """HMAC-SHA256 of ``mcp-<kind>:<token>`` under the token pepper."""
    if kind not in TOKEN_KINDS:
        raise ValueError(f"unknown token kind: {kind!r}")
    message = f"mcp-{kind}:{token}".encode()
    return hmac.new(get_token_pepper().encode(), message, hashlib.sha256).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _canonical(resource: str) -> str:
    return resource.rstrip("/")


def _validate_scopes(scopes: set[str] | frozenset[str]) -> str:
    unknown = set(scopes) - SCOPES
    if unknown:
        raise ValueError(f"unknown scopes: {sorted(unknown)}")
    if not scopes:
        raise ValueError("a grant needs at least one scope")
    return " ".join(sorted(scopes))


# ── Clients ──────────────────────────────────────────────────────────


async def ensure_client(
    db: DBConnection,
    client_id: str,
    *,
    kind: str,
    name: str,
    redirect_uris: tuple[str, ...] = (),
    metadata_url: str | None = None,
) -> None:
    """Create the client row if it does not exist yet. Existing rows are left as they are."""
    await db.execute(
        "INSERT INTO oauth_clients (id, kind, name, redirect_uris, metadata_url, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
        (client_id, kind, name, json.dumps(list(redirect_uris)), metadata_url, _now().isoformat()),
    )
    await db.commit()


async def disable_client(db: DBConnection, client_id: str) -> bool:
    cur = await db.execute(
        "UPDATE oauth_clients SET disabled_at = ? WHERE id = ? AND disabled_at IS NULL",
        (_now().isoformat(), client_id),
    )
    await db.commit()
    return bool(cur.rowcount)


# ── Grants ───────────────────────────────────────────────────────────


async def create_grant(
    db: DBConnection,
    *,
    client_id: str,
    user_id: str,
    scopes: set[str] | frozenset[str],
    resource: str,
    label: str = "",
    expires_at: str | None = None,
) -> str:
    """Record a consent decision. Returns the grant id.

    ``expires_at`` is the refresh deadline; it defaults to GRANT_TTL_DAYS
    from now and is never extended afterwards.
    """
    scope_text = _validate_scopes(scopes)
    now = _now()
    deadline = expires_at or (now + timedelta(days=GRANT_TTL_DAYS)).isoformat()
    grant_id = str(uuid4())
    await db.execute(
        "INSERT INTO oauth_grants "
        "(id, client_id, user_id, scopes, resource, label, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            grant_id,
            client_id,
            user_id,
            scope_text,
            _canonical(resource),
            label,
            now.isoformat(),
            deadline,
        ),
    )
    await db.commit()
    return grant_id


async def revoke_grant(db: DBConnection, grant_id: str) -> bool:
    """End a grant. Every token under it fails its next check. Returns True
    if the grant was live."""
    cur = await db.execute(
        "UPDATE oauth_grants SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
        (_now().isoformat(), grant_id),
    )
    await db.commit()
    return bool(cur.rowcount)


async def list_grants(db: DBConnection) -> list[dict[str, Any]]:
    """Grants for the Settings page. Carries no token material."""
    async with db.execute(
        "SELECT g.id, g.client_id, c.name AS client_name, g.label, g.scopes, g.resource, "
        "g.created_at, g.expires_at, g.revoked_at, g.last_used_at "
        "FROM oauth_grants g JOIN oauth_clients c ON c.id = g.client_id "
        "ORDER BY g.created_at DESC, g.id"
    ) as cur:
        rows = await cur.fetchall()
    now = _now()
    return [_grant_view(dict(row), now) for row in rows]


def _grant_view(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    """One derivation of a grant's state for every view of the list."""
    if row["revoked_at"] is not None:
        status = "revoked"
    elif _parse(row["expires_at"]) <= now:
        status = "expired"
    else:
        status = "active"
    return {**row, "scopes": row["scopes"].split(), "status": status}


# ── Tokens ───────────────────────────────────────────────────────────


async def issue_access_token(
    db: DBConnection, grant_id: str, *, ttl_seconds: int = ACCESS_TTL_SECONDS
) -> str:
    """Mint an access token under a grant. Returns the raw token once."""
    token = generate()
    now = _now()
    await db.execute(
        "INSERT INTO oauth_tokens (token_hash, grant_id, kind, created_at, expires_at) "
        "VALUES (?, ?, 'access', ?, ?)",
        (
            hash_token("access", token),
            grant_id,
            now.isoformat(),
            (now + timedelta(seconds=ttl_seconds)).isoformat(),
        ),
    )
    await db.commit()
    return token


async def create_manual_grant(
    db: DBConnection,
    *,
    label: str,
    scopes: set[str] | frozenset[str],
    resource: str,
    user_id: str,
    expires_at: str | None = None,
) -> tuple[str, str]:
    """An operator-issued bearer credential for clients that configure a
    header instead of running the browser flow. The grant and its single
    access token share one deadline (MANUAL_GRANT_TTL_DAYS by default).
    Returns (grant_id, raw_token)."""
    await ensure_client(db, MANUAL_CLIENT_ID, kind="admin", name="Manual bearer token")
    deadline = expires_at or (_now() + timedelta(days=MANUAL_GRANT_TTL_DAYS)).isoformat()
    grant_id = await create_grant(
        db,
        client_id=MANUAL_CLIENT_ID,
        user_id=user_id,
        scopes=scopes,
        resource=resource,
        label=label,
        expires_at=deadline,
    )
    ttl = int((_parse(deadline) - _now()).total_seconds())
    token = await issue_access_token(db, grant_id, ttl_seconds=ttl)
    return grant_id, token


async def verify_access_token(db: DBConnection, token: str, *, resource: str) -> Principal | None:
    """Resolve a bearer token to the principal it stands for, or None.

    One query walks token, grant and client. The token must be an unexpired,
    unconsumed access token; the grant live and inside its deadline; the
    client enabled; and the grant's resource the canonical resource given.
    """
    if not token:
        return None
    async with db.execute(
        "SELECT t.grant_id, t.expires_at AS token_expires_at, "
        "g.client_id, g.user_id, g.scopes, g.resource, "
        "g.expires_at AS grant_expires_at, g.revoked_at, c.disabled_at "
        "FROM oauth_tokens t "
        "JOIN oauth_grants g ON g.id = t.grant_id "
        "JOIN oauth_clients c ON c.id = g.client_id "
        "WHERE t.token_hash = ? AND t.kind = 'access' AND t.consumed_at IS NULL",
        (hash_token("access", token),),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    r = dict(row)
    now = _now()
    token_expiry = _parse(r["token_expires_at"])
    if token_expiry <= now or _parse(r["grant_expires_at"]) <= now:
        return None
    if r["revoked_at"] is not None or r["disabled_at"] is not None:
        return None
    if r["resource"] != _canonical(resource):
        return None
    await db.execute(
        "UPDATE oauth_grants SET last_used_at = ? WHERE id = ?",
        (now.isoformat(), r["grant_id"]),
    )
    await db.commit()
    return Principal(
        grant_id=r["grant_id"],
        client_id=r["client_id"],
        user_id=r["user_id"],
        scopes=frozenset(r["scopes"].split()),
        resource=r["resource"],
        expires_at=int(token_expiry.timestamp()),
    )


# ── Clients of the authorization server ──────────────────────────────


def _client_view(row: dict[str, Any]) -> dict[str, Any]:
    """One derivation of a client's state for every view of the list."""
    return {
        **row,
        "redirect_uris": json.loads(row["redirect_uris"] or "[]"),
        "status": "disabled" if row["disabled_at"] is not None else "active",
    }


async def get_client(db: DBConnection, client_id: str) -> dict[str, Any] | None:
    async with db.execute("SELECT * FROM oauth_clients WHERE id = ?", (client_id,)) as cur:
        row = await cur.fetchone()
    return _client_view(dict(row)) if row is not None else None


async def list_clients(db: DBConnection) -> list[dict[str, Any]]:
    """OAuth clients for the Settings page. The manual bearer-token client is
    not an OAuth client and is not listed."""
    async with db.execute(
        "SELECT * FROM oauth_clients WHERE id <> ? ORDER BY created_at DESC, id",
        (MANUAL_CLIENT_ID,),
    ) as cur:
        rows = await cur.fetchall()
    return [_client_view(dict(row)) for row in rows]


async def allow_cimd_client(db: DBConnection, url: str, *, name: str = "") -> None:
    """Allow a client identified by its metadata document URL. Allowing a
    disabled one again re-enables it and drops its cached document, so the
    next authorization fetches a fresh copy."""
    await db.execute(
        "INSERT INTO oauth_clients (id, kind, name, redirect_uris, metadata_url, created_at) "
        "VALUES (?, 'cimd', ?, '[]', ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET disabled_at = NULL, metadata_fetched_at = NULL",
        (url, name, url, _now().isoformat()),
    )
    await db.commit()


async def create_admin_client(
    db: DBConnection, *, name: str, redirect_uris: tuple[str, ...] | list[str]
) -> str:
    """A client the operator registers by hand: a name and exact redirect URIs."""
    client_id = f"tf-{uuid4().hex[:16]}"
    await ensure_client(db, client_id, kind="admin", name=name, redirect_uris=tuple(redirect_uris))
    return client_id


async def cache_client_document(
    db: DBConnection, client_id: str, *, name: str, redirect_uris: tuple[str, ...]
) -> None:
    """Store what a client's metadata document said, and when it was read."""
    await db.execute(
        "UPDATE oauth_clients SET name = ?, redirect_uris = ?, metadata_fetched_at = ? "
        "WHERE id = ?",
        (name, json.dumps(list(redirect_uris)), _now().isoformat(), client_id),
    )
    await db.commit()


def document_is_fresh(client: dict[str, Any], *, max_age_seconds: int) -> bool:
    fetched = client.get("metadata_fetched_at")
    if not fetched:
        return False
    return _now() - _parse(fetched) < timedelta(seconds=max_age_seconds)


# ── Authorization transactions ───────────────────────────────────────


def _txn_view(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "scopes": row["scopes"].split()}


async def create_transaction(
    db: DBConnection,
    *,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    scopes: set[str] | frozenset[str],
    resource: str,
    state: str | None,
) -> str:
    """Park a validated authorization request until the administrator decides."""
    scope_text = _validate_scopes(scopes)
    now = _now()
    txn_id = uuid4().hex
    await db.execute(
        "INSERT INTO oauth_transactions "
        "(id, client_id, redirect_uri, code_challenge, scopes, resource, state, "
        "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            txn_id,
            client_id,
            redirect_uri,
            code_challenge,
            scope_text,
            _canonical(resource),
            state,
            now.isoformat(),
            (now + timedelta(seconds=TRANSACTION_TTL_SECONDS)).isoformat(),
        ),
    )
    await db.commit()
    return txn_id


async def get_transaction(db: DBConnection, txn_id: str) -> dict[str, Any] | None:
    """A transaction that is still open: unexpired and not consumed."""
    async with db.execute("SELECT * FROM oauth_transactions WHERE id = ?", (txn_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    r = dict(row)
    if r["consumed_at"] is not None or _parse(r["expires_at"]) <= _now():
        return None
    return _txn_view(r)


async def arm_consent(db: DBConnection, txn_id: str) -> str | None:
    """A fresh nonce for the consent page, the one its decision must carry.
    None when the transaction is not open for a decision."""
    nonce = generate()
    cur = await db.execute(
        "UPDATE oauth_transactions SET nonce_hash = ? "
        "WHERE id = ? AND approved_at IS NULL AND consumed_at IS NULL",
        (hash_token("nonce", nonce), txn_id),
    )
    await db.commit()
    return nonce if cur.rowcount else None


async def approve_transaction(
    db: DBConnection, txn_id: str, *, nonce: str, user_id: str
) -> str | None:
    """Record consent: claim the transaction with its nonce, create the
    grant, mint the code. Returns the raw code once, or None when the
    transaction was not open for approval or the nonce is not its own."""
    if not nonce:
        return None
    now = _now()
    cur = await db.execute(
        "UPDATE oauth_transactions SET approved_at = ? "
        "WHERE id = ? AND nonce_hash = ? AND approved_at IS NULL AND consumed_at IS NULL",
        (now.isoformat(), txn_id, hash_token("nonce", nonce)),
    )
    await db.commit()
    if not cur.rowcount:
        return None
    txn = await get_transaction(db, txn_id)
    if txn is None:
        return None
    grant_id = await create_grant(
        db,
        client_id=txn["client_id"],
        user_id=user_id,
        scopes=set(txn["scopes"]),
        resource=txn["resource"],
    )
    code = generate()
    await db.execute(
        "UPDATE oauth_transactions SET grant_id = ?, code_hash = ?, expires_at = ? WHERE id = ?",
        (
            grant_id,
            hash_token("code", code),
            (now + timedelta(seconds=CODE_TTL_SECONDS)).isoformat(),
            txn_id,
        ),
    )
    await db.commit()
    return code


async def deny_transaction(db: DBConnection, txn_id: str, *, nonce: str) -> bool:
    """Close a transaction without a grant. True when it was open and the nonce was its own."""
    if not nonce:
        return False
    cur = await db.execute(
        "UPDATE oauth_transactions SET consumed_at = ? "
        "WHERE id = ? AND nonce_hash = ? AND approved_at IS NULL AND consumed_at IS NULL",
        (_now().isoformat(), txn_id, hash_token("nonce", nonce)),
    )
    await db.commit()
    return bool(cur.rowcount)


async def _consume(
    db: DBConnection, table: str, key_column: str, key: str, *, extra_where: str = ""
) -> bool:
    """The one conditional update that spends a credential.

    Stamps ``consumed_at`` and a marker only this call knows, commits, then
    reads the marker back. The read-back is what makes it safe on SQLite's
    shared connection: another request's rollback between the update and
    the commit erases the stamp while the cursor still reports a row
    changed. True only when this call spent the credential and the stamp
    persisted. The table and column names are module constants, never input.
    """
    marker = secrets.token_hex(16)
    cur = await db.execute(
        f"UPDATE {table} SET consumed_at = ?, consumed_by = ? "
        f"WHERE {key_column} = ? AND consumed_at IS NULL{extra_where}",
        (_now().isoformat(), marker, key),
    )
    await db.commit()
    if not cur.rowcount:
        return False
    async with db.execute(
        f"SELECT consumed_by FROM {table} WHERE {key_column} = ?", (key,)
    ) as check:
        row = await check.fetchone()
    return row is not None and row["consumed_by"] == marker


async def redeem_code(db: DBConnection, code: str) -> dict[str, Any] | None:
    """Consume an authorization code: one conditional update.

    Returns the transaction (client, redirect URI, PKCE challenge, grant)
    on the first redemption and None for an unknown or expired code, or
    for a consumption that did not persist (the client may retry). A
    second redemption raises ``ReplayError`` and revokes the grant the code
    created, because a reused code means it leaked.
    """
    code_hash = hash_token("code", code)
    now = _now()
    spent = await _consume(db, "oauth_transactions", "code_hash", code_hash)
    async with db.execute(
        "SELECT * FROM oauth_transactions WHERE code_hash = ?", (code_hash,)
    ) as lookup:
        row = await lookup.fetchone()
    if row is None:
        return None
    r = dict(row)
    if not spent:
        if r["consumed_at"] is None:
            return None  # our stamp was rolled back from under us: refuse, no replay
        if r["grant_id"]:
            await revoke_grant(db, r["grant_id"])
        raise ReplayError()
    if r["grant_id"] is None or _parse(r["expires_at"]) <= now:
        return None
    return _txn_view(r)


async def purge_expired_transactions(db: DBConnection) -> int:
    cur = await db.execute(
        "DELETE FROM oauth_transactions WHERE expires_at < ?", (_now().isoformat(),)
    )
    await db.commit()
    return int(cur.rowcount or 0)


# ── Token pairs and refresh ──────────────────────────────────────────


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    expires_in: int
    scope: str


async def issue_tokens(db: DBConnection, grant_id: str) -> IssuedTokens:
    """A new access token and refresh token under a grant. The refresh
    token's deadline is the grant's and is never extended; the access
    token lives ACCESS_TTL_SECONDS or until that deadline, whichever is first."""
    async with db.execute(
        "SELECT scopes, expires_at FROM oauth_grants WHERE id = ?", (grant_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise LookupError("unknown grant")
    now = _now()
    deadline = _parse(row["expires_at"])
    access_expiry = min(now + timedelta(seconds=ACCESS_TTL_SECONDS), deadline)
    access, refresh = generate(), generate()
    await db.execute(
        "INSERT INTO oauth_tokens (token_hash, grant_id, kind, created_at, expires_at) "
        "VALUES (?, ?, 'access', ?, ?)",
        (hash_token("access", access), grant_id, now.isoformat(), access_expiry.isoformat()),
    )
    await db.execute(
        "INSERT INTO oauth_tokens (token_hash, grant_id, kind, created_at, expires_at) "
        "VALUES (?, ?, 'refresh', ?, ?)",
        (hash_token("refresh", refresh), grant_id, now.isoformat(), deadline.isoformat()),
    )
    await db.commit()
    expires_in = max(0, int((access_expiry - now).total_seconds()))
    return IssuedTokens(access, refresh, expires_in, str(row["scopes"]))


async def redeem_refresh_token(
    db: DBConnection, token: str, *, requested_scopes: set[str] | None = None
) -> dict[str, Any] | None:
    """Consume a refresh token.

    Returns the live grant on the first use; None when the token is unknown
    or its grant is expired, revoked or under a disabled client. A second
    use raises ``ReplayError`` and revokes the grant (refresh token
    rotation). ``requested_scopes`` wider than the grant raise ``ScopeError``
    before anything is consumed. One conditional update is the only thing
    that consumes, so two concurrent uses have exactly one winner.
    """
    token_hash = hash_token("refresh", token)
    now = _now()
    async with db.execute(
        "SELECT t.expires_at AS token_expires_at, t.consumed_at, g.id, g.client_id, "
        "g.user_id, g.scopes, g.resource, g.expires_at, g.revoked_at, c.disabled_at "
        "FROM oauth_tokens t "
        "JOIN oauth_grants g ON g.id = t.grant_id "
        "JOIN oauth_clients c ON c.id = g.client_id "
        "WHERE t.token_hash = ? AND t.kind = 'refresh'",
        (token_hash,),
    ) as lookup:
        row = await lookup.fetchone()
    if row is None:
        return None
    r = dict(row)
    if r["consumed_at"] is not None:
        await revoke_grant(db, r["id"])
        raise ReplayError()
    if _parse(r["token_expires_at"]) <= now or _parse(r["expires_at"]) <= now:
        return None
    if r["revoked_at"] is not None or r["disabled_at"] is not None:
        return None
    scopes = r["scopes"].split()
    if requested_scopes is not None and not requested_scopes <= set(scopes):
        raise ScopeError("scope is wider than the grant")
    if not await _consume(
        db, "oauth_tokens", "token_hash", token_hash, extra_where=" AND kind = 'refresh'"
    ):
        async with db.execute(
            "SELECT consumed_at FROM oauth_tokens WHERE token_hash = ?", (token_hash,)
        ) as check:
            after = await check.fetchone()
        if after is not None and after["consumed_at"] is None:
            return None  # our stamp was rolled back from under us: refuse, no replay
        await revoke_grant(db, r["id"])
        raise ReplayError()
    return {**r, "scopes": scopes}


async def revoke_by_token(db: DBConnection, token: str, *, client_id: str) -> bool:
    """RFC 7009: end the grant behind an access or refresh token, when the
    token belongs to the client asking. An unknown token is not an error."""
    hashes = (hash_token("access", token), hash_token("refresh", token))
    async with db.execute(
        "SELECT g.id, g.client_id FROM oauth_tokens t "
        "JOIN oauth_grants g ON g.id = t.grant_id WHERE t.token_hash IN (?, ?)",
        hashes,
    ) as cur:
        row = await cur.fetchone()
    if row is None or row["client_id"] != client_id:
        return False
    return await revoke_grant(db, row["id"])

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

SCOPES: frozenset[str] = frozenset({"catalog:read", "jobs:read", "jobs:write"})
"""Every scope the server knows. A grant may only carry scopes from this set."""

INITIAL_SCOPES: tuple[str, ...] = ("catalog:read", "jobs:read")
"""The minimal set advertised to a client that has not been challenged yet."""

MANUAL_CLIENT_ID = "manual"
"""The client every operator-issued bearer token belongs to."""

ACCESS_TTL_SECONDS = 3600
GRANT_TTL_DAYS = 30
MANUAL_GRANT_TTL_DAYS = 90

TOKEN_KINDS = frozenset({"access", "refresh", "code"})


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

"""Single-admin user repository.

The only field that actually matters here is `password_hash` — username
is fixed at 'admin' for v0.5. Future versions may add multi-user, but
the schema is already shaped for it.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from uuid import uuid4

import bcrypt

from transcode_forge.db import DBConnection
from transcode_forge.repos import system as system_repo

ADMIN_USERNAME = "admin"
_AUTH_SECRET_KEY = "auth_secret"


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except (ValueError, TypeError):
        return False


async def has_admin(db: DBConnection) -> bool:
    async with db.execute(
        "SELECT 1 FROM users WHERE username = ? LIMIT 1", (ADMIN_USERNAME,)
    ) as cur:
        return (await cur.fetchone()) is not None


async def create_admin(db: DBConnection, password: str) -> str:
    """Create the admin user. Errors if one already exists."""
    if await has_admin(db):
        raise ValueError("admin already exists")
    user_id = str(uuid4())
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT INTO users (id, username, password_hash, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, ADMIN_USERNAME, hash_password(password), now, now),
    )
    await db.commit()
    return user_id


async def update_admin_password(db: DBConnection, new_password: str) -> bool:
    if not await has_admin(db):
        return False
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "UPDATE users SET password_hash = ?, updated_at = ? WHERE username = ?",
        (hash_password(new_password), now, ADMIN_USERNAME),
    )
    await db.commit()
    return True


async def authenticate(db: DBConnection, password: str) -> bool:
    """Return True iff the supplied password matches the stored hash."""
    async with db.execute(
        "SELECT password_hash FROM users WHERE username = ?", (ADMIN_USERNAME,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return False
    return verify_password(password, row["password_hash"])


async def get_or_create_auth_secret(db: DBConnection) -> str:
    """Return the cookie-signing secret, generating + persisting one on
    first call. The secret rotates only if explicitly cleared from the
    DB — keeping it stable means existing sessions survive a restart.
    """
    existing = await system_repo.get_state(db, _AUTH_SECRET_KEY, "")
    if existing:
        return existing
    secret = secrets.token_urlsafe(48)
    await system_repo.set_state(db, _AUTH_SECRET_KEY, secret)
    return secret

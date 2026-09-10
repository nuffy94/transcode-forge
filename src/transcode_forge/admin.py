"""The admin account door — and there is only one of it.

If you can run commands on the host, you are the admin. That is the whole
model, the same way Nextcloud's `occ user:resetpassword` or Django's
`manage.py changepassword` work. Nothing reachable over the network can
create or reset the admin: `ensure_admin()` runs at startup so an instance is
owned before it serves its first request, and this CLI covers recovery.

There used to be a second door, `POST /api/auth/setup`, which handed the
account to whoever loaded it first. On a public deploy Caddy publishes the
hostname to the certificate transparency logs seconds before the operator has
set a password, so that door was a race against the internet (R-040). It is
gone.

    # In a Docker deploy:
    docker compose exec scheduler python -m transcode_forge.admin reset-password

    # Or non-interactively (-T = no TTY):
    docker compose exec -T scheduler python -m transcode_forge.admin reset-password --password PW

Resets the admin password if an admin exists, or creates the admin if one
doesn't (so it doubles as a headless first-run). Touches only the login — the
catalog, jobs, workers, and schedules are untouched.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import secrets
import sys
from typing import NamedTuple

from transcode_forge.config import get_settings
from transcode_forge.db import DBConnection, close_db, init_db
from transcode_forge.repos import users as user_repo

MIN_LEN = 8
MAX_LEN = 200

# 12 random bytes -> 16 URL-safe characters: far past guessing, still short
# enough to read off a terminal and type once.
_GENERATED_PASSWORD_BYTES = 12


class AdminBootstrap(NamedTuple):
    """What startup did about the admin account.

    `generated_password` is set only when this call invented the password,
    which is the only case where anything should be printed. An operator who
    supplied TF_ADMIN_PASSWORD already knows it, so it stays out of the log.
    """

    created: bool
    generated_password: str | None


async def ensure_admin(db: DBConnection, password: str | None = None) -> AdminBootstrap:
    """Give this instance an owner before it can serve a request.

    Called once at startup. If an admin already exists this is a no-op, so a
    restart never rotates the credential out from under the operator.
    """
    if await user_repo.has_admin(db):
        return AdminBootstrap(created=False, generated_password=None)

    if password is not None and not (MIN_LEN <= len(password) <= MAX_LEN):
        # Refuse to boot rather than boot unowned — the whole point of this
        # function is that an unowned instance never gets to serve traffic.
        raise ValueError(f"TF_ADMIN_PASSWORD must be {MIN_LEN}-{MAX_LEN} characters.")

    generated = password is None
    secret = password if password is not None else secrets.token_urlsafe(_GENERATED_PASSWORD_BYTES)

    try:
        await user_repo.create_admin(db, secret)
    except Exception:
        # Another scheduler replica can boot against the same database.
        # users.username is UNIQUE, so the loser's INSERT raises: whoever won
        # owns the instance, and its password is the live one.
        if await user_repo.has_admin(db):
            return AdminBootstrap(created=False, generated_password=None)
        raise

    return AdminBootstrap(created=True, generated_password=secret if generated else None)


async def reset_admin_password(password: str) -> str:
    """Set the admin password, creating the admin if absent. Returns the
    action taken ('updated' or 'created')."""
    settings = get_settings()
    db = await init_db(settings.db_url)
    try:
        if await user_repo.has_admin(db):
            await user_repo.update_admin_password(db, password)
            return "updated"
        await user_repo.create_admin(db, password)
        return "created"
    finally:
        await close_db(db)


def _read_password(arg: str | None) -> str:
    if arg is not None:
        return arg
    # Allow piping: `echo newpw | python -m transcode_forge.admin reset-password`
    if not sys.stdin.isatty():
        piped = sys.stdin.readline().rstrip("\n")
        if piped:
            return piped
        sys.exit("No password provided (pass --password or run interactively).")
    pw = getpass.getpass("New admin password: ")
    if pw != getpass.getpass("Confirm password: "):
        sys.exit("Passwords do not match.")
    return pw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m transcode_forge.admin")
    sub = parser.add_subparsers(dest="command", required=True)
    rp = sub.add_parser("reset-password", help="Reset (or create) the admin login")
    rp.add_argument("--password", help="New password; omit to be prompted or read from stdin")
    args = parser.parse_args(argv)

    if args.command == "reset-password":
        password = _read_password(args.password)
        if not (MIN_LEN <= len(password) <= MAX_LEN):
            sys.exit(f"Password must be {MIN_LEN}-{MAX_LEN} characters.")
        action = asyncio.run(reset_admin_password(password))
        verb = "reset" if action == "updated" else "created"
        print(f"Admin password {verb}. Log in at your Transcode Forge URL.")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

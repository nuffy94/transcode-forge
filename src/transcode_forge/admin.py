"""Admin maintenance CLI — run on the server (where TF_DB_URL is set).

The single-admin login has no email reset (none configured, one user), so
recovery is server-side, the same way Nextcloud's `occ user:resetpassword` or
Django's `manage.py changepassword` work: if you can run commands on the host,
you are the admin.

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
import sys

from transcode_forge.config import get_settings
from transcode_forge.db import close_db, init_db
from transcode_forge.repos import users as user_repo

MIN_LEN = 8  # matches the /setup form (SetupRequest)
MAX_LEN = 200


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

"""Tests for the admin maintenance CLI (`python -m transcode_forge.admin`)."""

import pytest

from transcode_forge.admin import main, reset_admin_password
from transcode_forge.db import close_db, init_db
from transcode_forge.repos import users as user_repo


async def test_reset_creates_then_updates(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path / 'admin.db'}"
    monkeypatch.setenv("TF_DB_URL", db_url)

    # No admin yet → created.
    assert await reset_admin_password("first-password-123") == "created"
    db = await init_db(db_url)
    try:
        assert await user_repo.authenticate(db, "first-password-123")
    finally:
        await close_db(db)

    # Admin exists → updated; old password no longer works.
    assert await reset_admin_password("second-password-456") == "updated"
    db = await init_db(db_url)
    try:
        assert await user_repo.authenticate(db, "second-password-456")
        assert not await user_repo.authenticate(db, "first-password-123")
    finally:
        await close_db(db)


def test_cli_rejects_too_short_password(tmp_path, monkeypatch):
    monkeypatch.setenv("TF_DB_URL", f"sqlite:///{tmp_path / 'a.db'}")
    with pytest.raises(SystemExit):
        main(["reset-password", "--password", "short"])

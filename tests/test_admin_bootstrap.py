"""The admin is born on the machine, never over the network.

R-040: a fresh instance used to be claimable by whoever loaded /setup
first, and on the Linode deploy Caddy publishes the hostname to the
public certificate transparency logs seconds before the operator has set
a password. There is no longer an HTTP door to create the admin: startup
mints it, so the instance is owned before it can serve a request.
"""

import pytest

from transcode_forge.admin import MIN_LEN, ensure_admin
from transcode_forge.db import DBConnection
from transcode_forge.repos import users as user_repo


class TestEnsureAdmin:
    async def test_generates_a_password_when_the_operator_supplied_none(self, db: DBConnection):
        result = await ensure_admin(db)

        assert result.created is True
        assert result.generated_password is not None
        assert len(result.generated_password) >= MIN_LEN
        # The printed password is the one that actually works.
        assert await user_repo.authenticate(db, result.generated_password) is True

    async def test_uses_the_operators_password_and_does_not_echo_it(self, db: DBConnection):
        result = await ensure_admin(db, "operator-chosen-password")

        assert result.created is True
        # Nothing to print: they already know it, so it stays out of the log.
        assert result.generated_password is None
        assert await user_repo.authenticate(db, "operator-chosen-password") is True

    async def test_second_boot_is_a_no_op(self, db: DBConnection):
        first = await ensure_admin(db)
        assert first.generated_password is not None

        second = await ensure_admin(db)

        assert second.created is False
        assert second.generated_password is None
        # The first boot's password still works — a restart must not
        # silently rotate the credential out from under the operator.
        assert await user_repo.authenticate(db, first.generated_password) is True

    async def test_refuses_to_boot_unowned_on_a_too_short_password(self, db: DBConnection):
        with pytest.raises(ValueError, match="TF_ADMIN_PASSWORD"):
            await ensure_admin(db, "short")

        assert await user_repo.has_admin(db) is False

    async def test_race_loser_defers_to_the_replica_that_won(self, db: DBConnection, monkeypatch):
        """Two scheduler replicas can boot against one database. users.username
        is UNIQUE, so the loser's INSERT raises; it must report 'already owned'
        rather than crash the process."""
        await ensure_admin(db, "the-winners-password")

        calls = {"n": 0}
        real_has_admin = user_repo.has_admin

        async def has_admin_racing(conn: DBConnection) -> bool:
            calls["n"] += 1
            if calls["n"] == 1:
                return False  # the winner had not committed yet when we looked
            return await real_has_admin(conn)

        monkeypatch.setattr(user_repo, "has_admin", has_admin_racing)

        result = await ensure_admin(db)

        assert result.created is False
        assert result.generated_password is None
        assert await user_repo.authenticate(db, "the-winners-password") is True

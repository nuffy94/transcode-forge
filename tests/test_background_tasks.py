"""Background loops are started through one supervisor that reports any
exit that is not a cancellation (ledger R-007: the scan loop died silently
on one DB error, and shutdown's gather(return_exceptions=True) swallowed
the exception)."""

import asyncio
import logging

import pytest

from transcode_forge import main


async def test_a_loop_that_dies_is_reported(caplog):
    async def dies() -> None:
        raise RuntimeError("db blip")

    with caplog.at_level(logging.ERROR):
        task = main._supervised(dies(), "scans")
        with pytest.raises(RuntimeError):
            await task
        await asyncio.sleep(0)  # done callbacks run on the next loop step
    assert "scans" in caplog.text and "died" in caplog.text


async def test_a_loop_that_returns_is_reported(caplog):
    async def returns() -> None:
        return None

    with caplog.at_level(logging.ERROR):
        task = main._supervised(returns(), "orphans")
        await task
        await asyncio.sleep(0)
    assert "orphans" in caplog.text and "exited" in caplog.text


async def test_cancellation_is_quiet(caplog):
    async def forever() -> None:
        await asyncio.sleep(3600)

    with caplog.at_level(logging.ERROR):
        task = main._supervised(forever(), "stale-workers")
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
    assert caplog.text == ""


@pytest.fixture
async def stranded_scan(db):
    """A scan row left 'running' by a process that died mid-scan. Listed
    before ``app`` in a test's arguments, so it exists when the app boots."""
    from transcode_forge.models.scan import Scan
    from transcode_forge.repos import scans as scan_repo

    return await scan_repo.create_scan(db, Scan(library="movies"))


async def test_the_app_fixture_boots_through_the_real_lifespan(stranded_scan, app):
    """Every test's app starts the way the server does, so a startup step
    that goes missing from lifespan fails the suite."""
    from transcode_forge.models.scan import ScanStatus
    from transcode_forge.repos import scans as scan_repo
    from transcode_forge.repos import users as user_repo

    live = {t.get_name() for t in asyncio.all_tasks() if not t.done()}
    assert {"scheduled-scans", "stale-workers", "orphan-requeue"} <= live

    assert app.state.redis is not None  # the mock the fixture patched in
    assert await user_repo.has_admin(app.state.db)

    scan = await scan_repo.get_scan(app.state.db, stranded_scan)
    assert scan is not None and scan.status == ScanStatus.FAILED

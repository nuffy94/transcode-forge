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

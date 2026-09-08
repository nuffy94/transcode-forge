"""The scheduled-scan loop (ledger R-007).

It survives a DB error, it takes "when did this library last scan" from
the scans table so a restart does not rescan everything, and it skips a
library whose scan is already running. Scans themselves go through the
one door in scanner/runner.py, which is patched here.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from transcode_forge import scheduler_cron
from transcode_forge.models.scan import Scan, ScanStatus
from transcode_forge.repos import scans as scan_repo
from transcode_forge.scheduler_cron import run_scheduled_scans


def _lib(lib_id: str = "lib1", name: str = "Movies", *, auto_scan: bool = True, hours: int = 24):
    return {
        "id": lib_id,
        "name": name,
        "auto_scan": auto_scan,
        "scan_interval_hours": hours,
        "path": f"/{name.lower()}",
        "media_type": "movies",
    }


def _finished_task() -> asyncio.Task[None]:
    """What start_scan returns for a scan that runs and completes."""
    return asyncio.create_task(asyncio.sleep(0))


async def _seed_scan(db, library: str, *, hours_ago: float, status=ScanStatus.COMPLETE) -> None:
    scan = Scan(library=library)
    await scan_repo.create_scan(db, scan)
    started = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
    await db.execute(
        "UPDATE scans SET started_at = ?, status = ? WHERE id = ?",
        (started, status.value, scan.id),
    )
    await db.commit()


async def _run_briefly(settings, db, *, seconds: float = 0.1) -> None:
    task = asyncio.create_task(run_scheduled_scans(settings, db, tick_seconds=0.01))
    await asyncio.sleep(seconds)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class TestSchedulerCron:
    async def test_no_libraries(self, db):
        with patch("transcode_forge.scheduler_cron.lib_repo.list_libraries", return_value=[]):
            await _run_briefly(MagicMock(), db)

    async def test_auto_scan_off_is_never_scanned(self, db):
        with (
            patch(
                "transcode_forge.scheduler_cron.lib_repo.list_libraries",
                return_value=[_lib(auto_scan=False)],
            ),
            patch("transcode_forge.scheduler_cron.runner.start_scan") as start,
        ):
            await _run_briefly(MagicMock(), db)
        start.assert_not_called()

    async def test_due_library_goes_through_the_door(self, db):
        settings = MagicMock()
        with (
            patch(
                "transcode_forge.scheduler_cron.lib_repo.list_libraries",
                return_value=[_lib(hours=0)],
            ),
            patch(
                "transcode_forge.scheduler_cron.runner.start_scan",
                side_effect=lambda **kw: _finished_task(),
            ) as start,
        ):
            await _run_briefly(settings, db)
        assert start.call_count >= 1
        start.assert_called_with(
            library_id="lib1",
            library_name="Movies",
            library_path="/movies",
            media_type="movies",
            limit=0,
            db=db,
            settings=settings,
        )

    async def test_db_error_does_not_kill_the_loop(self, db, caplog):
        """One transient DB error used to end the task for good with no log
        line (list_libraries sat outside the try). The next tick must scan."""
        calls = {"n": 0}

        async def flaky_list(_db, **_kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("db blip")
            return [_lib(hours=0)]

        with (
            patch("transcode_forge.scheduler_cron.lib_repo.list_libraries", side_effect=flaky_list),
            patch(
                "transcode_forge.scheduler_cron.runner.start_scan",
                side_effect=lambda **kw: _finished_task(),
            ) as start,
            caplog.at_level(logging.ERROR),
        ):
            await _run_briefly(MagicMock(), db)
        assert calls["n"] >= 2, "the loop stopped after the error"
        assert start.call_count >= 1
        assert "db blip" in caplog.text

    async def test_recent_scan_row_is_not_rescanned_on_the_first_pass(self, db):
        """last_scan lived in process memory, so every restart scanned every
        library within seconds of boot. The scans table is the memory now:
        a library scanned an hour ago (interval 24 h) waits, one scanned
        30 h ago runs, one never scanned runs."""
        await _seed_scan(db, "Fresh", hours_ago=1)
        await _seed_scan(db, "Stale", hours_ago=30)
        libs = [_lib("a", "Fresh"), _lib("b", "Stale"), _lib("c", "Never")]
        with (
            patch("transcode_forge.scheduler_cron.lib_repo.list_libraries", return_value=libs),
            patch(
                "transcode_forge.scheduler_cron.runner.start_scan",
                side_effect=lambda **kw: _finished_task(),
            ) as start,
        ):
            await scheduler_cron._tick(MagicMock(), db)
        scanned = sorted(call.kwargs["library_name"] for call in start.call_args_list)
        assert scanned == ["Never", "Stale"]

    async def test_failed_attempt_still_counts_as_the_last_attempt(self, db):
        """A library whose path is missing fails its scan; it is retried on
        the next interval, not every tick (that was the old behavior too)."""
        await _seed_scan(db, "Broken", hours_ago=1, status=ScanStatus.FAILED)
        with (
            patch(
                "transcode_forge.scheduler_cron.lib_repo.list_libraries",
                return_value=[_lib("a", "Broken")],
            ),
            patch("transcode_forge.scheduler_cron.runner.start_scan") as start,
        ):
            await scheduler_cron._tick(MagicMock(), db)
        start.assert_not_called()

    async def test_busy_library_is_skipped_with_a_log_line(self, db, caplog):
        """The docstring promised a running-scan skip; the door provides it:
        start_scan returns None while a scan is alive."""
        with (
            patch(
                "transcode_forge.scheduler_cron.lib_repo.list_libraries",
                return_value=[_lib(hours=0)],
            ),
            patch("transcode_forge.scheduler_cron.runner.start_scan", return_value=None),
            caplog.at_level(logging.INFO),
        ):
            await scheduler_cron._tick(MagicMock(), db)
        assert "already running" in caplog.text

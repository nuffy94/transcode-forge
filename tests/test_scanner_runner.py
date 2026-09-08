"""scanner/runner.py: the one door every library scan goes through (R-007).

A scan is an asyncio task and a library has at most one live one. The
reservation is synchronous, so two triggers in the same tick cannot both
win, and a scan that crashes frees its library because a finished task is
not alive; there is no running flag to keep in step.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from transcode_forge.repos import libraries as lib_repo
from transcode_forge.scanner import runner


async def _library(db, name: str = "Movies") -> str:
    return await lib_repo.create_library(db, name=name, media_type="movies", path=f"/{name}")


def _start(lib_id: str, db, name: str = "Movies") -> asyncio.Task[None] | None:
    return runner.start_scan(
        library_id=lib_id,
        library_name=name,
        library_path=f"/{name}",
        media_type="movies",
        limit=0,
        db=db,
        settings=MagicMock(demo_mode=False),
    )


async def test_second_start_for_a_running_library_is_refused(db):
    lib_id = await _library(db)
    gate = asyncio.Event()

    async def blocked_scan(**kwargs):
        await gate.wait()

    with patch("transcode_forge.scanner.scanner.scan_library", side_effect=blocked_scan):
        first = _start(lib_id, db)
        assert first is not None
        await asyncio.sleep(0)  # let the scan reach its await
        assert _start(lib_id, db) is None, "a live scan must refuse a second one"
        assert runner.live_scans() == [first]

        gate.set()
        await first
        assert runner.live_scans() == []

        again = _start(lib_id, db)
        assert again is not None, "a finished scan frees the library"
        await again


async def test_different_libraries_scan_concurrently(db):
    movies = await _library(db, "Movies")
    tv = await _library(db, "TV")
    gate = asyncio.Event()

    async def blocked_scan(**kwargs):
        await gate.wait()

    with patch("transcode_forge.scanner.scanner.scan_library", side_effect=blocked_scan):
        a = _start(movies, db, "Movies")
        b = _start(tv, db, "TV")
        assert a is not None and b is not None
        assert set(runner.live_scans()) == {a, b}
        gate.set()
        await asyncio.gather(a, b)


async def test_a_crashed_scan_frees_its_library(db, caplog):
    lib_id = await _library(db)

    async def broken_scan(**kwargs):
        raise RuntimeError("disk fell off")

    with patch("transcode_forge.scanner.scanner.scan_library", side_effect=broken_scan):
        task = _start(lib_id, db)
        assert task is not None
        await task  # run_scan logs the failure; the task itself does not raise
        assert "disk fell off" in caplog.text
        assert runner.live_scans() == []
        assert _start(lib_id, db) is not None


async def test_demo_mode_scans_through_the_door_too(db):
    """A demo scan is a scan: it reserves the library like any other."""
    with patch("transcode_forge.demo.simulator.simulate_scan", new=AsyncMock()) as simulate:
        task = runner.start_scan(
            library_id="demo-lib",
            library_name="Movies",
            library_path="/movies",
            media_type="movies",
            limit=5,
            db=db,
            settings=MagicMock(demo_mode=True),
        )
        assert task is not None
        await task
    simulate.assert_awaited_once_with("demo-lib", "Movies", "movies", 5, db)


async def test_cancel_all_stops_live_scans(db):
    lib_id = await _library(db)
    gate = asyncio.Event()

    async def blocked_scan(**kwargs):
        await gate.wait()

    with patch("transcode_forge.scanner.scanner.scan_library", side_effect=blocked_scan):
        task = _start(lib_id, db)
        assert task is not None
        await asyncio.sleep(0)
        await runner.cancel_all()
        assert task.cancelled()
        assert runner.live_scans() == []

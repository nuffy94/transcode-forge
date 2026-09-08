"""Scheduled scan support: runs each library's scan on its own interval."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from transcode_forge.config import Settings
from transcode_forge.db import DBConnection
from transcode_forge.metrics import tf_errors_total
from transcode_forge.repos import libraries as lib_repo
from transcode_forge.repos import scans as scan_repo
from transcode_forge.scanner import runner

logger = logging.getLogger(__name__)

SCAN_TICK_SECONDS = 300.0


async def run_scheduled_scans(
    settings: Settings,
    db: DBConnection,
    *,
    tick_seconds: float = SCAN_TICK_SECONDS,
) -> None:
    """Background task that runs library scans on schedule.

    Runs as a long-lived asyncio task started in the FastAPI lifespan.
    Every tick, each enabled auto_scan library whose scan_interval_hours
    has passed since its last scan attempt is scanned, one library at a
    time. "Last attempt" is read from the scans table, so a restart does
    not rescan everything within seconds of boot. A library whose scan is
    already running (manual or scheduled) is skipped; the door in
    scanner/runner.py is what knows. The whole tick sits inside the try:
    one DB error costs one tick, never the loop (ledger R-007).
    """
    logger.info("Scheduled scan loop started")
    while True:
        try:
            await _tick(settings, db)
        except Exception:
            logger.exception("Scheduled scan pass failed")
            tf_errors_total.labels(error_type="scheduled_scan").inc()
        await asyncio.sleep(tick_seconds)


async def _tick(settings: Settings, db: DBConnection) -> None:
    now = datetime.now(UTC)
    for lib in await lib_repo.list_libraries(db, enabled_only=True):
        if not lib.get("auto_scan"):
            continue
        interval = timedelta(hours=lib.get("scan_interval_hours", 24))
        last = await scan_repo.latest_started_at(db, lib["name"])
        if last is not None and now - last < interval:
            continue

        task = runner.start_scan(
            library_id=lib["id"],
            library_name=lib["name"],
            library_path=lib["path"],
            media_type=lib["media_type"],
            limit=0,
            db=db,
            settings=settings,
        )
        if task is None:
            logger.info("Scheduled scan of '%s' skipped: a scan is already running", lib["name"])
            continue
        logger.info("Scheduled scan triggered for '%s'", lib["name"])
        await task  # one library at a time; run_scan logs its own failures

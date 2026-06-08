"""Scheduled scan support — runs library scans on a cron-like schedule."""

import asyncio
import logging
from datetime import UTC, datetime

from transcode_forge.config import Settings
from transcode_forge.db import DBConnection
from transcode_forge.metrics import tf_errors_total
from transcode_forge.models.library import StorageBackendType
from transcode_forge.repos import libraries as lib_repo
from transcode_forge.scanner.s3_scanner import scan_s3_library
from transcode_forge.scanner.scanner import scan_library

logger = logging.getLogger(__name__)


async def run_scheduled_scans(
    settings: Settings,
    db: DBConnection,
) -> None:
    """Background task that runs library scans on schedule.

    Runs as a long-lived asyncio task started in the FastAPI lifespan.
    Each library has its own scan_interval_hours. Skips if a scan is already running.
    """
    last_scan: dict[str, float] = {}

    logger.info("Scheduled scan loop started")

    while True:
        now = datetime.now(UTC)

        # Get enabled libraries from DB (auto_scan must be enabled)
        libs = await lib_repo.list_libraries(db, enabled_only=True)

        for lib in libs:
            if not lib.get("auto_scan"):
                continue

            interval = lib.get("scan_interval_hours", 24) * 3600
            last = last_scan.get(lib["id"], 0)
            if (now.timestamp() - last) < interval:
                continue

            logger.info("Scheduled scan triggered for '%s'", lib["name"])
            try:
                backend = lib.get("backend", "filesystem")
                if backend == StorageBackendType.S3:
                    # S3 library: use S3 scanner
                    await scan_s3_library(
                        library_id=lib["id"],
                        library_name=lib["name"],
                        bucket=lib.get("s3_bucket", ""),
                        prefix=lib.get("s3_prefix", ""),
                        config=settings,
                        db=db,
                    )
                else:
                    # Filesystem library: use FS scanner
                    await scan_library(
                        library_id=lib["id"],
                        library_name=lib["name"],
                        library_path=lib["path"],
                        media_type=lib["media_type"],
                        db=db,
                    )
                last_scan[lib["id"]] = now.timestamp()
            except Exception:
                logger.exception("Scheduled scan failed for '%s'", lib["name"])
                tf_errors_total.labels(error_type="scheduled_scan").inc()

        # Check every 5 minutes
        await asyncio.sleep(300)

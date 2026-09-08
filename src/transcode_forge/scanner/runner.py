"""The one door every library scan goes through.

A scan is an asyncio task, and a library has at most one live one. Both
API routes and the scheduled-scan loop call start_scan(); it reserves the
library synchronously (no await between the check and the reservation, so
two triggers in the same tick cannot both win), runs the single backend
dispatch that used to be copied in api/routes/scan.py and
scheduler_cron.py, and never needs a "running" flag kept in step: a
library is being scanned exactly when its task is alive, however the last
one ended (ledger R-007).
"""

import asyncio
import logging

from transcode_forge.config import Settings
from transcode_forge.db import DBConnection
from transcode_forge.metrics import tf_errors_total
from transcode_forge.models.library import StorageBackendType
from transcode_forge.repos import libraries as lib_repo
from transcode_forge.scanner import s3_scanner
from transcode_forge.scanner import scanner as fs_scanner

logger = logging.getLogger(__name__)

# library id -> its most recent scan task. A finished task is not live.
_scans: dict[str, asyncio.Task[None]] = {}


def start_scan(
    *,
    library_id: str,
    library_name: str,
    library_path: str,
    media_type: str,
    limit: int,
    db: DBConnection,
    settings: Settings,
) -> asyncio.Task[None] | None:
    """Start a scan of one library. Returns its task, or None when a scan
    of that library is already alive (the caller answers 409 or skips)."""
    current = _scans.get(library_id)
    if current is not None and not current.done():
        return None
    task = asyncio.create_task(
        run_scan(
            library_id=library_id,
            library_name=library_name,
            library_path=library_path,
            media_type=media_type,
            limit=limit,
            db=db,
            settings=settings,
        ),
        name=f"scan:{library_name}",
    )
    _scans[library_id] = task
    return task


def live_scans() -> list[asyncio.Task[None]]:
    """Every scan task that is still running."""
    return [task for task in _scans.values() if not task.done()]


async def cancel_all() -> None:
    """Shutdown: no scan may outlive the DB it writes to."""
    tasks = live_scans()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_scan(
    *,
    library_id: str,
    library_name: str,
    library_path: str,
    media_type: str,
    limit: int,
    db: DBConnection,
    settings: Settings,
) -> None:
    """One scan, dispatched on the library's backend. Failures are logged
    and counted here, never raised out of the task."""
    try:
        lib = await lib_repo.get_library(db, library_id)
        if not lib:
            logger.error("Library %s not found", library_id)
            return
        if lib.get("backend", "filesystem") == StorageBackendType.S3:
            await s3_scanner.scan_s3_library(
                library_id=library_id,
                library_name=library_name,
                bucket=lib.get("s3_bucket", ""),
                prefix=lib.get("s3_prefix", ""),
                config=settings,
                db=db,
                max_files=limit,
            )
        else:
            await fs_scanner.scan_library(
                library_id=library_id,
                library_name=library_name,
                library_path=library_path,
                media_type=media_type,
                db=db,
                max_files=limit,
            )
    except Exception as e:
        logger.exception("Scan failed for library '%s': %s", library_name, e)
        tf_errors_total.labels(error_type="scan").inc()

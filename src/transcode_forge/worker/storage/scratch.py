"""Scratch space manager for S3 backend downloads and uploads.

Manages temporary per-job directories on fast local storage. Includes:
- Disk-space guards (fail cleanly if insufficient space)
- Atomic recursive delete on completion
- Orphan cleanup (stale per-job directories)
- Shutdown hook integration
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)


class ScratchManager:
    """Manages temporary scratch directories for per-job downloads/uploads.

    Per-job directories are named `{job_id}_{uuid}` to avoid collisions
    if a job ID is reused. Disk-space checks are performed on reserve()
    to prevent silent failures mid-transcode.

    Usage:
        manager = ScratchManager(scratch_root=Path("/tmp/transcode-scratch"))
        working_path = await manager.reserve(job_id="job-123", size_bytes=10_000_000_000)
        # ... download / transcode / upload ...
        await manager.release(job_id="job-123")
    """

    # Margin: if free space < size_bytes + margin, reject the reserve.
    # This prevents us from filling the disk completely.
    DISK_MARGIN_BYTES = 100 * 1024 * 1024  # 100 MB

    # Non-job entries that must survive EVERY cleanup path: the worker's
    # durable state (milestone outbox — undelivered terminal reports)
    # defaults to <scratch_root>/state. Eating it on shutdown/orphan
    # cleanup would silently lose finished work's reports — the exact
    # failure the outbox exists to prevent (worker-resilience spec D1).
    RESERVED_DIRS = ("state",)

    def __init__(self, scratch_root: Path | str) -> None:
        """Initialize the scratch manager.

        Args:
            scratch_root: Root directory for per-job scratch spaces.
                Will be created if it doesn't exist.
        """
        self.scratch_root = Path(scratch_root)
        self.scratch_root.mkdir(parents=True, exist_ok=True)
        logger.info("Scratch manager initialized at %s", self.scratch_root)

    async def reserve(self, job_id: str, size_bytes: int) -> Path:
        """Reserve scratch space for a job.

        Creates a per-job directory and verifies sufficient disk space.
        The directory is immediately created; cleanup on failure is the
        caller's responsibility.

        Args:
            job_id: Job identifier.
            size_bytes: Estimated size of the file to download.

        Returns:
            Path to the reserved per-job scratch directory.

        Raises:
            OSError: If disk space is insufficient.
        """
        # Check disk space before creating the directory.
        free = await asyncio.to_thread(self._get_free_space)
        required = size_bytes + self.DISK_MARGIN_BYTES

        if free < required:
            raise OSError(
                f"Insufficient scratch space: have {free // (1024**3):.1f} GB, "
                f"need {required // (1024**3):.1f} GB (file {size_bytes // (1024**3):.1f} GB "
                f"+ {self.DISK_MARGIN_BYTES // (1024**3):.1f} GB margin)"
            )

        # Create a unique per-job directory.
        job_uuid = str(uuid4())[:8]
        job_dir = self.scratch_root / f"{job_id}_{job_uuid}"

        await asyncio.to_thread(job_dir.mkdir, parents=True, exist_ok=True)
        logger.info("Reserved scratch space for job %s at %s", job_id, job_dir)
        return job_dir

    async def release(self, job_id: str) -> None:
        """Release scratch space after a job completes.

        Atomically deletes the per-job directory and all contents.
        No-op if the directory doesn't exist.

        Args:
            job_id: Job identifier.
        """
        # Find and delete any directories matching {job_id}_*
        # (in case there are multiple from retries).
        for job_dir in self.scratch_root.glob(f"{job_id}_*"):
            try:
                await asyncio.to_thread(shutil.rmtree, job_dir)
                logger.info("Released scratch space: %s", job_dir)
            except FileNotFoundError:
                # Already deleted — no-op
                pass
            except OSError as e:
                logger.warning("Failed to release scratch space %s: %s", job_dir, e)

    async def cleanup_orphans(self, max_age_hours: int = 24) -> None:
        """Delete stale per-job directories older than max_age_hours.

        Runs on startup and periodically (e.g., hourly) to reclaim space
        from jobs that crashed or exited unexpectedly.

        Args:
            max_age_hours: Age threshold in hours.
        """
        cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
        deleted_count = 0

        for job_dir in self.scratch_root.glob("*_*"):
            if not job_dir.is_dir() or job_dir.name in self.RESERVED_DIRS:
                continue

            try:
                mtime = datetime.fromtimestamp(job_dir.stat().st_mtime, tz=UTC)
                if mtime < cutoff:
                    await asyncio.to_thread(shutil.rmtree, job_dir)
                    deleted_count += 1
                    logger.info("Cleaned up orphaned scratch directory: %s", job_dir)
            except FileNotFoundError:
                # Already deleted — no-op
                pass
            except OSError as e:
                logger.warning("Error removing orphan directory %s: %s", job_dir, e)

        if deleted_count > 0:
            logger.info("Orphan cleanup removed %d directories", deleted_count)

    async def cleanup_on_shutdown(self) -> None:
        """Clean up per-job scratch directories on worker shutdown.

        Called from http_agent._cleanup() to ensure no stale files remain
        after an unclean shutdown. Deliberately deletes JOB directories,
        never the whole root: the durable state dir (outbox) lives under
        the root by default and must outlive the process — an undelivered
        completion report is the one thing a restart exists to save.
        """
        logger.info("Scratch cleanup on shutdown...")
        for job_dir in self.scratch_root.glob("*_*"):
            if not job_dir.is_dir() or job_dir.name in self.RESERVED_DIRS:
                continue
            try:
                await asyncio.to_thread(shutil.rmtree, job_dir)
            except FileNotFoundError:
                pass
            except OSError as e:
                logger.warning("Error removing scratch directory %s: %s", job_dir, e)

    def _get_free_space(self) -> int:
        """Get free disk space in bytes at the scratch root.

        On error, returns a generous estimate (10 GB) to avoid permanent
        deadlock. The system will likely fail later if space is actually
        insufficient, but this avoids the worker getting stuck.
        """
        try:
            stat = shutil.disk_usage(self.scratch_root)
            return stat.free
        except OSError as e:
            logger.warning(
                "Could not determine free disk space at %s: %s — assuming 10 GB available",
                self.scratch_root,
                e,
            )
            # Return a generous estimate instead of 0 to avoid deadlock.
            # If the system actually runs out, the upload/transcode will fail
            # with a real error at that point.
            return 10 * 1024**3  # 10 GB

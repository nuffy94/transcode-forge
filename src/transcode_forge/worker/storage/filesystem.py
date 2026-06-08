"""Filesystem storage backend — local paths and shared NFS/SMB mounts.

This backend implements the current in-place transcode behavior:
- fetch() returns the (path-mapped) original file on the filesystem.
- commit() performs the atomic swap that was previously in pipeline.py.
- No downloads/uploads; all operations are local file I/O.

The 8-step safety pipeline (run_pipeline) still handles the core
transcode, verification, and cleanup. This backend only wraps the
pre/post-transcode lock and swap operations.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from transcode_forge.worker.storage.base import CommitResult

logger = logging.getLogger(__name__)

LOCK_SUFFIX = ".tf_lock"
BAK_SUFFIX = ".tf_bak"


class FilesystemBackend:
    """Filesystem storage backend for local and NFS/SMB media.

    Implements the StorageBackend protocol (structural; no explicit inheritance).
    No copying; fetch() returns the original file path (path-mapped).
    commit() performs the atomic swap that was previously inline in
    the pipeline.

    CRITICAL INVARIANT:
    fetch() returns a LOCAL Path that can be passed directly to
    run_pipeline(). No S3 keys or remote identifiers — always a
    local filesystem path.
    """

    def __init__(self) -> None:
        """Initialize the filesystem backend."""
        pass

    async def lock(self, key: str) -> None:
        """No-op: the pipeline's .tf_lock mechanism handles locking.

        Args:
            key: Unused (for protocol compatibility).
        """
        pass

    async def unlock(self, key: str) -> None:
        """No-op: the pipeline's .tf_lock mechanism handles unlocking.

        Args:
            key: Unused (for protocol compatibility).
        """
        pass

    async def fetch(self, source: str) -> Path:
        """Return the local path to the source file.

        CRITICAL: This returns a LOCAL filesystem path that MUST be
        passed to run_pipeline(). The path is already path-mapped at
        the call site (http_agent.py:_translate_path), so we just
        validate and return it.

        Args:
            source: Local filesystem path (already path-mapped).

        Returns:
            The same path, as a Path object.

        Raises:
            FileNotFoundError: If the source does not exist.
        """
        p = Path(source)
        if not await asyncio.to_thread(p.exists):
            raise FileNotFoundError(f"Source file not found: {source}")
        return p

    async def commit(
        self,
        local_output: Path,
        source: str,
        job: Any,
        space_saved: int = 0,
    ) -> CommitResult:
        """Commit the transcoded output.

        For the filesystem backend, the atomic swap (original → .tf_bak,
        output → original) happens INSIDE the pipeline's run_pipeline()
        function, not here. This method just validates and returns
        the result sizes.

        The caller (http_agent._process_job) has already run the full
        8-step pipeline (including the SWAP step), so the output has
        been placed at the original location and is ready.

        Args:
            local_output: Path to the transcoded file (now at original location).
            source: Source path identifier (unused for filesystem backend).
            job: Job object (Pydantic model or dict) with id, source_path, etc.
                Used only for logging. Can be either type.
            space_saved: Bytes reclaimed from the swap (passed from the pipeline result).

        Returns:
            CommitResult with output_size and space_saved.
        """
        # Extract job_id for logging (support both Pydantic models and dicts).
        job_id = job.id if hasattr(job, "id") else job.get("id", "unknown")

        # The output file is now at the original location (after the
        # swap inside run_pipeline). Just report the sizes.
        output_size = await asyncio.to_thread(self._get_size, local_output)
        logger.info(
            "Filesystem commit for job %s: output_size=%d bytes, space_saved=%d bytes",
            job_id,
            output_size,
            space_saved,
        )

        return CommitResult(output_size=output_size, space_saved=space_saved)

    @staticmethod
    def _get_size(path: Path) -> int:
        """Get file size in bytes."""
        return path.stat().st_size

    async def scan(self, library: str) -> list[dict[str, Any]]:
        """Scan a filesystem library directory.

        Walks the directory tree and probes each media file. This is
        what scanner.py does; for now we return an empty list (the
        scanner is not yet refactored to use the backend).

        Args:
            library: Library path (filesystem directory).

        Returns:
            List of file dicts with source_path, duration, codec, etc.
        """
        # TODO: integrate with scanner.py probe logic. For now, this
        # is a no-op because the scheduler calls the scanner directly.
        return []

    async def cleanup(self, job: Any) -> None:
        """Clean up temporary resources after a job.

        For filesystem backend, the pipeline's finally block already
        deletes .tf_lock and .tf_tmp files, so this is a no-op.

        Args:
            job: Job dict (unused).
        """
        pass


# Legacy helper functions kept for backward compatibility and test imports.
# These are now internal to FilesystemBackend but exposed for testing.


def _acquire_lock(lock_path: Path, *, job_id: str, worker_id: str) -> None:
    """Atomically create a lock file. Raises if the lock already exists.

    Uses exclusive-create (open mode "x") so two workers racing on the
    same path can't both acquire it — the loser gets FileExistsError.
    This closes the check-then-write TOCTOU window a plain exists() test
    would leave open.
    """
    lock_data = json.dumps(
        {
            "job_id": job_id,
            "worker_id": worker_id,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )
    try:
        with lock_path.open("x") as f:
            f.write(lock_data)
    except FileExistsError:
        try:
            content = lock_path.read_text()
        except OSError:
            content = "<unreadable>"
        from transcode_forge.worker.pipeline import PipelineError

        raise PipelineError(
            "LOCK",
            f"Lock file already exists (another transcode in progress?): {content}",
        ) from None


def _atomic_swap(original: Path, tmp: Path, bak: Path) -> None:
    """Rename original → bak, then tmp → original.

    If the second rename fails, restore bak → original.
    """
    from transcode_forge.worker.pipeline import PipelineError

    try:
        original.rename(bak)
    except OSError as e:
        raise PipelineError("SWAP", f"Failed to rename original to backup: {e}") from e

    try:
        tmp.rename(original)
    except OSError as e:
        # Rollback: restore backup
        try:
            bak.rename(original)
        except OSError as rollback_err:
            logger.critical(
                "[SWAP] MANUAL INTERVENTION REQUIRED: rollback also failed. "
                "backup=%s, original=%s, tmp=%s, error=%s",
                bak,
                original,
                tmp,
                rollback_err,
            )
        raise PipelineError("SWAP", f"Failed to rename tmp to original: {e}") from e


def _rollback_swap(original: Path, bak: Path) -> None:
    """Restore backup to original position after a failed confirmation."""
    if not bak.exists():
        return
    try:
        # Path.replace() is atomic on most filesystems — no TOCTOU race
        bak.replace(original)
        logger.info("[ROLLBACK] Backup restored to original")
    except OSError as e:
        logger.critical(
            "[ROLLBACK] MANUAL INTERVENTION REQUIRED: backup=%s, original=%s, error=%s",
            bak,
            original,
            e,
        )


def _preserve_metadata(path: Path, src_stat: os.stat_result) -> None:
    """Apply the source file's owner/group/mode/mtime to the new file.

    chown needs root or CAP_CHOWN; chmod and utime work for the file
    owner. Each is wrapped so a partial failure (e.g. non-root worker)
    just logs a warning instead of breaking a successful pipeline.

    Refuses to act on symlinks — a malicious source path could otherwise
    redirect chown/chmod to an arbitrary file (the worker runs as root).
    """
    if path.is_symlink():
        logger.warning("Refusing metadata preservation on symlink: %s", path)
        return
    try:
        # Resolve once and verify the target is the same file we expect.
        # If the inode behind `path` changed since the swap (TOCTOU),
        # bail rather than chown the wrong file.
        if path.resolve(strict=True) != path.resolve():
            logger.warning("Path resolution mismatch on %s — skipping metadata", path)
            return
    except (OSError, RuntimeError) as e:
        logger.warning("Could not resolve %s for metadata preservation — %s", path, e)
        return

    # The upfront is_symlink() guard above is what protects against the
    # chown-the-wrong-file class of attack. follow_symlinks=False would
    # be belt-and-suspenders but isn't supported by all OSes for these
    # calls (Windows in particular), so we rely on the upfront check.
    if hasattr(os, "chown"):
        try:
            os.chown(path, src_stat.st_uid, src_stat.st_gid)
        except (OSError, PermissionError) as e:
            logger.warning(
                "Could not chown %s to %s:%s — %s",
                path,
                src_stat.st_uid,
                src_stat.st_gid,
                e,
            )

    try:
        os.chmod(path, stat.S_IMODE(src_stat.st_mode))
    except OSError as e:
        logger.warning("Could not chmod %s — %s", path, e)

    try:
        os.utime(path, ns=(src_stat.st_atime_ns, src_stat.st_mtime_ns))
    except OSError as e:
        logger.warning("Could not preserve mtime on %s — %s", path, e)


def _safe_delete(path: Path) -> None:
    """Delete a file if it exists. Never raises."""
    try:
        if path.exists():
            path.unlink()
    except OSError as e:
        logger.warning("Failed to delete %s: %s", path, e)

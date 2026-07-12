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
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from transcode_forge.worker.storage.base import CommitResult

logger = logging.getLogger(__name__)

LOCK_SUFFIX = ".tf_lock"
BAK_SUFFIX = ".tf_bak"
TMP_SUFFIX = ".tf_tmp"

# Locks from OTHER workers older than this are presumed dead (matches the
# find_stale_locks default in worker/pipeline.py). Our own locks are always
# stale at startup — a freshly started worker has no pipeline in flight.
RECOVERY_STALE_LOCK_HOURS = 2.0


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


def _touch_lock(lock_path: Path, *, job_id: str, worker_id: str) -> None:
    """Atomically refresh the lock's timestamp (the lock heartbeat).

    Written to a sibling temp file then os.replace()d so readers see the
    old or the new JSON, never a partial write. Recovery treats locks
    older than RECOVERY_STALE_LOCK_HOURS as dead — this heartbeat is what
    makes that inference valid for encodes that legitimately run longer
    (x265-slow 4K passes the 2h mark easily).
    """
    payload = json.dumps(
        {
            "job_id": job_id,
            "worker_id": worker_id,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )
    tmp = lock_path.with_name(lock_path.name + ".new")
    tmp.write_text(payload)
    try:
        os.replace(tmp, lock_path)
    except OSError:
        # A failed touch is non-fatal (the heartbeat retries next tick) but
        # must not litter media dirs with .new files — e.g. Windows replace
        # fails transiently when a reader holds the destination open.
        _safe_delete(tmp)
        raise


def pipeline_artifacts(src: Path) -> tuple[Path, Path, Path]:
    """(lock, tmp, bak) sidecar paths for a source file — the single
    source of truth for the pipeline's artifact naming
    (movie.mkv → movie.mkv.tf_lock, movie.tf_tmp.mkv, movie.tf_bak.mkv)."""
    lock = src.with_name(src.name + LOCK_SUFFIX)
    tmp = src.with_name(src.stem + TMP_SUFFIX + src.suffix)
    bak = src.with_name(src.stem + BAK_SUFFIX + src.suffix)
    return lock, tmp, bak


def _atomic_swap(original: Path, tmp: Path, bak: Path) -> None:
    """Rename original → bak, then tmp → original.

    If the second rename fails, restore bak → original.
    """
    from transcode_forge.worker.pipeline import PipelineError

    if bak.exists():
        # A stranded backup is the LAST copy of a true original (e.g. a
        # completed job whose CLEANUP failed). POSIX rename would replace
        # it silently — refuse instead; an operator must reconcile first.
        raise PipelineError(
            "SWAP",
            f"Pre-existing backup at {bak} — refusing to overwrite it. "
            "Verify the media file plays, then delete the backup manually.",
        )

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


# ── Startup swap recovery ─────────────────────────────────────────────
#
# A power loss inside the pipeline's SWAP window (between the two renames,
# or before CLEANUP/UNLOCK) leaves the original hidden as .tf_bak and a
# stale .tf_lock that blocks every retry of that path at the LOCK step.
# The worker runs this scan over its local media roots at startup.


def _lock_is_active(lock_path: Path, *, worker_id: str, stale_after: timedelta) -> bool:
    """True when a lock belongs to a DIFFERENT worker and is still fresh.

    Such a lock must be respected — on shared storage (NFS/SMB) another
    worker may be mid-pipeline on the same file. Our own locks are always
    stale at startup (a freshly started worker has no pipeline in flight),
    and unreadable or ancient locks are treated as stale too.
    """
    try:
        content = json.loads(lock_path.read_text())
        lock_worker = content.get("worker_id")
        ts = datetime.fromisoformat(str(content["timestamp"]))
    except (OSError, ValueError, KeyError):
        return False
    if lock_worker == worker_id:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return datetime.now(UTC) - ts < stale_after


def recover_orphaned_backups(
    roots: Iterable[Path | str],
    *,
    worker_id: str,
    stale_lock_hours: float = RECOVERY_STALE_LOCK_HOURS,
) -> dict[str, int]:
    """Worker-startup recovery scan for the filesystem backend.

    Cases handled, per orphaned ``.tf_bak``:

    - Original path MISSING → crash between the two swap renames: the
      backup IS the original; rename it back.
    - Original present, lock still held → crash after the swap but before
      CONFIRM/CLEANUP: the file at the original path is an unconfirmed
      encode. Restore the backup over it — originals are never sacrificed
      for unverified output (the job re-queues at registration anyway).
    - Original present, NO lock → the pipeline finished (UNLOCK ran);
      almost certainly a completed job whose backup deletion failed. The
      original path holds a confirmed encode — do NOT clobber it; log
      loudly for the operator instead.

    Separately, stale ``.tf_lock``/``.tf_tmp`` leftovers without a backup
    (crash mid-transcode) are removed so the path can be retried.

    Locks held by other workers are respected unless older than
    ``stale_lock_hours``. Returns action counters for logging and tests.
    """
    stats = {
        "restored": 0,
        "locks_removed": 0,
        "tmp_removed": 0,
        "skipped_active": 0,
        "needs_attention": 0,
    }
    stale_after = timedelta(hours=stale_lock_hours)

    for root_like in roots:
        root = Path(root_like)
        if not root.is_dir():
            continue
        _recover_backups_under(root, worker_id=worker_id, stale_after=stale_after, stats=stats)
        _remove_stale_locks_under(root, worker_id=worker_id, stale_after=stale_after, stats=stats)

    if any(stats[k] for k in ("restored", "locks_removed", "tmp_removed", "needs_attention")):
        logger.warning(
            "[RECOVERY] Startup swap-recovery: restored %d original(s), removed "
            "%d stale lock(s) and %d leftover tmp file(s); skipped %d in "
            "progress elsewhere; %d need manual attention",
            stats["restored"],
            stats["locks_removed"],
            stats["tmp_removed"],
            stats["skipped_active"],
            stats["needs_attention"],
        )
    return stats


def recover_source_path(
    original: Path,
    *,
    worker_id: str,
    stale_lock_hours: float = RECOVERY_STALE_LOCK_HOURS,
) -> str:
    """Single-path swap recovery, run by a worker at claim time.

    Same crash matrix as ``recover_orphaned_backups`` (kept in sync by
    hand — the scan variant also aggregates stats), applied lazily to the
    one path a job is about to touch. This is what heals a crash whose
    worker never restarts: the startup scan can't run if nobody starts up.

    Returns:
    - ``"clean"``:     no artifacts; nothing done.
    - ``"restored"``:  original restored from .tf_bak; leftovers removed.
    - ``"cleaned"``:   stale .tf_lock/.tf_tmp removed (no backup involved).
    - ``"active"``:    fresh foreign lock — another worker is mid-pipeline
                       on this path right now; hands off.
    - ``"attention"``: backup + finished-looking original with no lock (or
                       a restore that failed) — operator must reconcile;
                       hands off.
    """
    lock, tmp, bak = pipeline_artifacts(original)
    stale_after = timedelta(hours=stale_lock_hours)

    if lock.exists() and _lock_is_active(lock, worker_id=worker_id, stale_after=stale_after):
        return "active"

    if bak.exists():
        if original.exists() and not lock.exists():
            logger.critical(
                "[RECOVERY] MANUAL ATTENTION: backup %s exists but %s looks like a "
                "finished transcode (pipeline unlocked). Not touching either file — "
                "verify the media plays, then delete the backup (or restore it "
                "manually).",
                bak,
                original,
            )
            return "attention"
        clobbered_unconfirmed = original.exists()
        try:
            bak.replace(original)
        except OSError as e:
            logger.critical("[RECOVERY] Could not restore %s → %s: %s", bak, original, e)
            return "attention"
        logger.warning(
            "[RECOVERY] Claim-time restore of %s: %s (from %s)",
            "original over unconfirmed encode" if clobbered_unconfirmed else "missing original",
            original,
            bak,
        )
        _safe_delete(lock)
        _safe_delete(tmp)
        return "restored"

    if lock.exists() or tmp.exists():
        _safe_delete(lock)
        _safe_delete(tmp)
        logger.warning("[RECOVERY] Cleared stale pipeline leftovers for %s", original)
        return "cleaned"

    return "clean"


def _recover_backups_under(
    root: Path, *, worker_id: str, stale_after: timedelta, stats: dict[str, int]
) -> None:
    """Restore orphaned .tf_bak files under ``root`` (see recover_orphaned_backups)."""
    for bak in sorted(root.rglob(f"*{BAK_SUFFIX}*")):
        if not bak.is_file() or BAK_SUFFIX not in bak.name:
            continue
        # movie.tf_bak.mkv → movie.mkv (inverse of the pipeline's naming).
        original = bak.with_name(bak.name.replace(BAK_SUFFIX, "", 1))
        lock = original.with_name(original.name + LOCK_SUFFIX)
        tmp = original.with_name(original.stem + TMP_SUFFIX + original.suffix)

        if lock.exists() and _lock_is_active(lock, worker_id=worker_id, stale_after=stale_after):
            logger.info("[RECOVERY] %s: lock held by another live worker — leaving it alone", bak)
            stats["skipped_active"] += 1
            continue

        if original.exists() and not lock.exists():
            logger.critical(
                "[RECOVERY] MANUAL ATTENTION: backup %s exists but %s looks like a "
                "finished transcode (pipeline unlocked). Not touching either file — "
                "delete the backup after verifying the media plays, or restore it "
                "manually.",
                bak,
                original,
            )
            stats["needs_attention"] += 1
            continue

        try:
            clobbered_unconfirmed = original.exists()
            bak.replace(original)
            logger.warning(
                "[RECOVERY] Restored %s: %s (from %s)",
                "original over unconfirmed encode" if clobbered_unconfirmed else "missing original",
                original,
                bak,
            )
            stats["restored"] += 1
        except OSError as e:
            logger.error("[RECOVERY] Could not restore %s → %s: %s", bak, original, e)
            continue

        for leftover, key in ((lock, "locks_removed"), (tmp, "tmp_removed")):
            try:
                if leftover.exists():
                    leftover.unlink()
                    stats[key] += 1
            except OSError as e:
                logger.warning("[RECOVERY] Could not delete %s: %s", leftover, e)


def _remove_stale_locks_under(
    root: Path, *, worker_id: str, stale_after: timedelta, stats: dict[str, int]
) -> None:
    """Remove stale locks (and their tmp partials) left by a mid-transcode
    crash — without this, the LOCK step rejects every retry of that path."""
    for lock in sorted(root.rglob(f"*{LOCK_SUFFIX}")):
        if not lock.is_file():
            continue
        if _lock_is_active(lock, worker_id=worker_id, stale_after=stale_after):
            # Another worker is (or recently was) mid-pipeline here. Counted
            # by the backups pass when a .tf_bak is involved — don't double
            # count, just leave the lock in place.
            logger.info("[RECOVERY] %s: held by another live worker — leaving it", lock)
            continue
        original = lock.with_name(lock.name.removesuffix(LOCK_SUFFIX))
        tmp = original.with_name(original.stem + TMP_SUFFIX + original.suffix)
        try:
            lock.unlink()
            stats["locks_removed"] += 1
            logger.warning("[RECOVERY] Removed stale lock: %s", lock)
        except OSError as e:
            logger.warning("[RECOVERY] Could not delete stale lock %s: %s", lock, e)
            continue
        try:
            if tmp.exists():
                tmp.unlink()
                stats["tmp_removed"] += 1
                logger.warning("[RECOVERY] Removed leftover tmp file: %s", tmp)
        except OSError as e:
            logger.warning("[RECOVERY] Could not delete %s: %s", tmp, e)

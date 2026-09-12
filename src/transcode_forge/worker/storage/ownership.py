"""The owned source transaction: one handle from acquisition to release.

LOCK through UNLOCK is a single transaction over one source path and its
sidecars, and this module is that transaction. Acquiring the lock is what
creates it, so the capability to delete the lock or the temporary output
exists nowhere else: an invocation that loses the race never gets a
handle and therefore has nothing to delete. That is the difference
between a check a future cleanup path might skip and a capability it
never receives.

Two properties make the release trustworthy.

- **A token, not a fact about the past.** Every write to the lock carries
  this job and this worker, and is skipped when the lock on disk no
  longer carries them. Having acquired a lock once is not the same as
  still holding it: a recovery scan elsewhere may revoke a lock it judges
  stale and a successor may take the path.
- **The exit is one settled unit.** ``asyncio.to_thread`` cancellation
  does not wait for the OS thread, so a mutation that has started must
  not be abandoned. :func:`settled` runs it to completion and re-raises
  the cancellation afterwards. A cancel delivered while the swap thread
  renames still rolls back, and the lock outlives every thread that is
  still touching these paths.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Coroutine
from pathlib import Path
from types import TracebackType
from typing import Any

from transcode_forge.worker.storage.filesystem import (
    LOCK_TOUCH_INTERVAL,
    _acquire_lock,
    _atomic_swap,
    _lock_matches,
    _preserve_metadata,
    _rollback_swap,
    _safe_delete,
    _touch_lock,
    pipeline_artifacts,
)

logger = logging.getLogger(__name__)


async def settled[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run ``coro`` to completion even when the caller is cancelled.

    Cancellation then means "abort once this mutation has settled" rather
    than "return while it runs". A cancellation delivered during the wait
    is re-raised after the coroutine finishes. An exception raised by the
    coroutine wins over a pending cancellation, so the failure reason is
    never lost.

    Only ever wrap something short. These regions are renames and
    unlinks; the decode samples in CONFIRM stay cancellable so the second
    shutdown signal still aborts promptly.
    """
    task = asyncio.ensure_future(coro)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class SourceOwnership:
    """One owned transaction over a source path and its sidecars.

    Entered only by a successful exclusive create of the lock. Use it as
    an async context manager around the whole pipeline body::

        async with SourceOwnership(src, job_id=job_id, worker_id=wid) as own:
            ...                        # encode, verify, compare
            await own.swap()           # settled
            ...                        # confirm the swapped file
            await own.confirm(src_stat)

    Anything the body raises, cancellation included, exits through the
    single settled release below.
    """

    def __init__(self, source: Path, *, job_id: str, worker_id: str) -> None:
        self.source = source
        self.job_id = job_id
        self.worker_id = worker_id
        self.lock_path, self.tmp_path, self.bak_path = pipeline_artifacts(source)
        self._heartbeat: asyncio.Task[None] | None = None
        # Set inside the swap thread, so a cancel delivered while the
        # rename runs still knows the source path is mid-mutation. A flag
        # set after the await would miss exactly that case.
        self._mutating = threading.Event()
        self._confirmed = False

    async def __aenter__(self) -> SourceOwnership:
        try:
            await settled(
                asyncio.to_thread(
                    _acquire_lock, self.lock_path, job_id=self.job_id, worker_id=self.worker_id
                )
            )
        except asyncio.CancelledError:
            # The exclusive create may have completed just before the
            # cancel landed. There is no scope to release it from, so
            # release it here rather than strand a lock nobody owns.
            await settled(asyncio.to_thread(self._release_lock))
            raise
        self._heartbeat = asyncio.create_task(self._beat())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # One settled unit, in order: stop the heartbeat, roll back an
        # unconfirmed mutation, delete the temp output, release the lock
        # last. Returning None, never True: whatever the body raised
        # still leaves this scope. A hard event-loop teardown abandons the
        # threads under this and no guard here can change that. That case
        # is a crash, and recover_source_path is what heals a crash.
        await settled(self._settle())

    async def swap(self) -> None:
        """SWAP as a settled unit: original to .tf_bak, temp to original."""
        await settled(
            asyncio.to_thread(
                _atomic_swap,
                self.source,
                self.tmp_path,
                self.bak_path,
                on_mutating=self._mutating.set,
            )
        )

    async def confirm(self, src_stat: os.stat_result) -> None:
        """Close the mutation as a settled unit: restore the original's
        metadata, then drop the backup. Past this the exit has nothing to
        roll back."""
        await settled(asyncio.to_thread(self._finish, src_stat))

    # ── internals ─────────────────────────────────────────────────────

    def _finish(self, src_stat: os.stat_result) -> None:
        _preserve_metadata(self.source, src_stat)
        _safe_delete(self.bak_path)
        self._confirmed = True

    async def _settle(self) -> None:
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            try:
                await self._heartbeat
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Lock heartbeat task died unexpectedly")

        if self._mutating.is_set() and not self._confirmed:
            restored = await asyncio.to_thread(_rollback_swap, self.source, self.bak_path)
            if not restored:
                # The transaction is unresolved: the backup may be the
                # only intact copy. Keeping the lock is the hands-off
                # signal recover_source_path already understands.
                logger.critical(
                    "[UNLOCK] Restoring %s from %s failed, keeping the lock so nothing "
                    "else touches this path until an operator has reconciled it",
                    self.source,
                    self.bak_path,
                )
                return

        await asyncio.to_thread(self._release_artifacts)
        logger.info("[UNLOCK] Lock released")

    def _release_artifacts(self) -> None:
        """Delete the temp output, then the lock, and only while the lock
        still carries this transaction's token.

        Order: a lock released first lets a successor acquire the path and
        start writing its own .tf_tmp, which this delete would then
        remove. Token: if our lock was revoked and re-acquired, both paths
        belong to the successor now, so leave both alone. Its own release,
        or recovery, clears them.
        """
        if not _lock_matches(self.lock_path, job_id=self.job_id, worker_id=self.worker_id):
            logger.warning(
                "Lock %s no longer carries this job's token, leaving it and %s to "
                "whoever holds the path now",
                self.lock_path,
                self.tmp_path,
            )
            return
        _safe_delete(self.tmp_path)
        _safe_delete(self.lock_path)

    def _release_lock(self) -> None:
        """Delete the lock if it is still ours (cancelled acquisition)."""
        if _lock_matches(self.lock_path, job_id=self.job_id, worker_id=self.worker_id):
            _safe_delete(self.lock_path)

    def _refresh_lock(self) -> None:
        """One heartbeat write, skipped when the lock is no longer ours."""
        if not _lock_matches(self.lock_path, job_id=self.job_id, worker_id=self.worker_id):
            logger.warning(
                "Lock %s no longer carries this job's token, not refreshing it",
                self.lock_path,
            )
            return
        _touch_lock(self.lock_path, job_id=self.job_id, worker_id=self.worker_id)

    async def _beat(self) -> None:
        """Refresh the lock's timestamp every LOCK_TOUCH_INTERVAL seconds
        until the transaction releases, so "stale" means dead rather than
        old across multi-hour encodes. A failed touch is logged, never
        fatal: the transaction owns the lock either way, the heartbeat
        only keeps its liveness visible. Each touch is a settled unit, so
        cancelling this task at release waits for an in-flight write."""
        while True:
            await asyncio.sleep(LOCK_TOUCH_INTERVAL)
            try:
                await settled(asyncio.to_thread(self._refresh_lock))
            except OSError as e:
                logger.warning("Could not refresh lock %s: %s", self.lock_path, e)

"""Cancellation-safe child processes — no ffmpeg may outlive the worker.

The fleet incident behind this module (2026-07-06, CTs 202 + 205): a
worker shutdown exited the agent while its x265 child kept encoding as
an orphan. Three layered defenses:

1. managed_subprocess() — an async context manager guaranteeing the
   child (and its whole process group) is terminated whenever the block
   is left while the process is still running: task cancellation
   (shutdown abort, event-loop teardown), timeouts, exceptions, early
   returns. SIGTERM first (ffmpeg finalizes and exits fast), SIGKILL
   after a grace period.
2. On Linux, every child gets PR_SET_PDEATHSIG=SIGKILL: if the worker
   process itself is killed hard (docker stop grace timeout → SIGKILL),
   the kernel reaps the child anyway — the case no Python code can
   handle.
3. Every child has a deadline. managed_subprocess() requires `timeout`
   and runs the caller's block under it, so an unbounded child cannot be
   written: a wedged ffmpeg (GPU driver, dead NFS mount) dies after the
   window instead of parking the worker forever (ledger R-001). A long
   child that reports progress calls Child.extend() on every sign of
   life, which turns the deadline into a stall watchdog.

This is the only place a child process is created; the scheduler's
ffprobe uses it too, and tests/test_worker_shutdown.py refuses any other
spawn site (ledger R-025 found two).

POSIX children start in their own session (start_new_session=True) so a
group signal also sweeps anything the child itself spawned. On Windows
(dev only) both mechanisms degrade to plain terminate()/kill().

Fork-safety note: asyncio always spawns with fork()+exec() (its Popen
call keeps close_fds=True, which rules out the posix_spawn path), and
this process is multithreaded (asyncio's to_thread pool). Without a
preexec_fn the child runs only C code between fork and exec; WITH one
it runs Python, so a lock held by another thread at fork() time can
deadlock the child before exec, and the parent, which waits for the
exec synchronously on the loop thread, hangs with it: the whole event
loop, not one spawn, and no deadline can cover it. The preexec_fn here
is deliberately minimal (one ctypes call, bound at import, no
imports/logging) to shrink that window; it cannot be closed entirely.
If a spawn ever hangs (create_subprocess_exec never returns, no child
process appears), suspect this before anything else.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections.abc import AsyncIterator
from typing import Any

logger = logging.getLogger(__name__)

KILL_GRACE_SECONDS = 5.0

if sys.platform == "win32":
    _SPAWN_KWARGS: dict[str, Any] = {}

    def _term_tree(proc: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            proc.terminate()

    def _kill_tree(proc: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            proc.kill()

else:
    # Annotated in BOTH platform branches: mypy narrows sys.platform and
    # only checks the branch matching its --platform, so the win32
    # annotation is invisible when CI's Linux mypy checks this branch.
    _SPAWN_KWARGS: dict[str, Any] = {"start_new_session": True}

    if sys.platform == "linux":
        import ctypes

        _PR_SET_PDEATHSIG = 1
        try:
            _libc: Any = ctypes.CDLL("libc.so.6", use_errno=True)
        except OSError:  # pragma: no cover — musl/exotic libc: skip pdeathsig
            _libc = None

        if _libc is not None:
            # Bound in the parent so the child's pre-exec window does not
            # pay for ctypes' first-call symbol lookup and _FuncPtr build.
            _prctl: Any = _libc.prctl

            def _set_pdeathsig() -> None:  # pragma: no cover — runs in the forked child
                _prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)

            _SPAWN_KWARGS["preexec_fn"] = _set_pdeathsig

    def _term_tree(proc: asyncio.subprocess.Process) -> None:
        # start_new_session makes the child a session/group leader, so its
        # pgid is its pid — the group signal sweeps grandchildren too.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGTERM)

    def _kill_tree(proc: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)


async def terminate_process_tree(
    proc: asyncio.subprocess.Process, *, grace: float = KILL_GRACE_SECONDS
) -> None:
    """Terminate a still-running child: TERM → grace → KILL, then reap.

    No-op if the process has already exited."""
    if proc.returncode is not None:
        return
    _term_tree(proc)
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
    except TimeoutError:
        logger.warning("Child pid=%s ignored SIGTERM for %.0fs — killing", proc.pid, grace)
        _kill_tree(proc)
        await proc.wait()
    except asyncio.CancelledError:
        # Torn down mid-grace: make the kill unconditional (synchronous)
        # before propagating — never trade cleanliness for an orphan.
        _kill_tree(proc)
        raise


class Child:
    """What managed_subprocess yields: the asyncio Process, the deadline its
    block runs under, and the window that deadline was set to."""

    __slots__ = ("_deadline", "proc", "timeout")

    def __init__(
        self, proc: asyncio.subprocess.Process, deadline: asyncio.Timeout, timeout: float
    ) -> None:
        self.proc = proc
        self.timeout = timeout
        self._deadline = deadline

    def extend(self, seconds: float | None = None) -> None:
        """Move the deadline to `seconds` from now (default: the window the
        child was started with).

        For a long child that reports progress (an encode): call this on
        every line it emits and the deadline becomes a stall watchdog. A
        child that goes quiet for the window is killed; a slow one that
        keeps talking runs as long as it needs to. A no-op once the
        deadline has fired or the block has exited."""
        window = self.timeout if seconds is None else seconds
        # asyncio refuses to reschedule an expiring, expired or exited
        # Timeout; all three mean there is nothing left to extend.
        with contextlib.suppress(RuntimeError):
            self._deadline.reschedule(asyncio.get_running_loop().time() + window)


@contextlib.asynccontextmanager
async def managed_subprocess(
    *cmd: str, timeout: float, grace: float = KILL_GRACE_SECONDS, **kwargs: Any
) -> AsyncIterator[Child]:
    """create_subprocess_exec that cannot leak the child and cannot run unbounded.

    `timeout` bounds the whole block: when it expires, the await inside the
    block is cancelled, the child is terminated, and the block raises
    TimeoutError (catch it OUTSIDE the block, as with asyncio.timeout).
    Leaving the block while the process is still running for any other
    reason (cancellation, exception, early return) terminates its whole
    process tree too."""
    # typeshed names create_subprocess_exec's first param `program`, so any
    # dict unpack looks like it could collide with it — it never does here
    # (spawn kwargs are start_new_session/preexec_fn, callers pass pipes).
    proc = await asyncio.create_subprocess_exec(*cmd, **_SPAWN_KWARGS, **kwargs)  # type: ignore[misc]
    try:
        async with asyncio.timeout(timeout) as deadline:
            yield Child(proc, deadline, timeout)
    finally:
        if proc.returncode is None:
            await terminate_process_tree(proc, grace=grace)

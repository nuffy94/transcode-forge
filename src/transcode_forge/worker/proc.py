"""Cancellation-safe child processes — no ffmpeg may outlive the worker.

The fleet incident behind this module (2026-07-06, CTs 202 + 205): a
worker shutdown exited the agent while its x265 child kept encoding as
an orphan. Two layered defenses:

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

POSIX children start in their own session (start_new_session=True) so a
group signal also sweeps anything the child itself spawned. On Windows
(dev only) both mechanisms degrade to plain terminate()/kill().

Fork-safety note: preexec_fn forces subprocess off the posix_spawn fast
path onto fork()+exec(), and this process is multithreaded (asyncio's
to_thread pool) — a lock held by another thread at fork() time would
deadlock the child before exec. The preexec_fn here is deliberately
minimal (one pre-bound ctypes call, no imports/logging/allocation) to
shrink that window; it cannot be closed entirely. If an encode ever
hangs INSIDE subprocess creation (spawn never returns, no ffmpeg
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

            def _set_pdeathsig() -> None:  # pragma: no cover — runs in the forked child
                _libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)

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


@contextlib.asynccontextmanager
async def managed_subprocess(
    *cmd: str, grace: float = KILL_GRACE_SECONDS, **kwargs: Any
) -> AsyncIterator[asyncio.subprocess.Process]:
    """create_subprocess_exec that cannot leak the child.

    Leaving the block while the process is still running — cancellation,
    exception, early return — terminates its whole process tree."""
    # typeshed names create_subprocess_exec's first param `program`, so any
    # dict unpack looks like it could collide with it — it never does here
    # (spawn kwargs are start_new_session/preexec_fn, callers pass pipes).
    proc = await asyncio.create_subprocess_exec(*cmd, **_SPAWN_KWARGS, **kwargs)  # type: ignore[misc]
    try:
        yield proc
    finally:
        if proc.returncode is None:
            await terminate_process_tree(proc, grace=grace)

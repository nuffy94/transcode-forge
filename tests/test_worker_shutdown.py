"""Shutdown discipline: no ffmpeg child may outlive the worker.

Fleet incident 2026-07-06 (CTs 202 + 205): a graceful SIGTERM shutdown
exited the worker while its x265 child kept encoding as an orphan. These
tests pin the fix on both layers:

- worker/proc.py managed_subprocess(): leaving the block while the child
  still runs (task cancellation, exception, early return) terminates it.
- HttpWorkerAgent shutdown semantics: 1st signal drains (heartbeats keep
  flowing), 2nd signal aborts the in-flight encode and reports the job,
  3rd signal force-exits.
"""

import asyncio
import contextlib
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from transcode_forge.models.job import Job
from transcode_forge.worker.encoder import run_encode
from transcode_forge.worker.hardware import HardwareCapabilities
from transcode_forge.worker.http_agent import HttpWorkerAgent, fence_after_seconds
from transcode_forge.worker.proc import Child, managed_subprocess

# A child that runs "forever" (far longer than any test timeout).
SLEEPER = [sys.executable, "-c", "import time; time.sleep(120)"]
# A child that prints a line every 0.1 s for ~1.5 s, then exits 0: slow but alive.
TALKER = [
    sys.executable,
    "-c",
    "import time\nfor i in range(15):\n    print('tick', i, flush=True)\n    time.sleep(0.1)",
]


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    """Poll until predicate() is true (or fail the test)."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


class TestManagedSubprocess:
    async def test_child_killed_on_cancel(self):
        """Cancelling the task that awaits the child must reap the child —
        THE orphan bug: cancellation used to abandon a running ffmpeg."""
        holder: dict = {}

        async def consume() -> None:
            async with managed_subprocess(*SLEEPER, timeout=30.0, grace=2.0) as child:
                holder["proc"] = child.proc
                await child.proc.wait()

        task = asyncio.create_task(consume())
        await _wait_for(lambda: "proc" in holder)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert holder["proc"].returncode is not None  # reaped, not orphaned

    async def test_child_killed_on_exception(self):
        holder: dict = {}
        with pytest.raises(RuntimeError, match="boom"):
            async with managed_subprocess(*SLEEPER, timeout=30.0, grace=2.0) as child:
                holder["proc"] = child.proc
                raise RuntimeError("boom")
        assert holder["proc"].returncode is not None

    async def test_child_killed_on_early_return(self):
        async with managed_subprocess(*SLEEPER, timeout=30.0, grace=2.0) as child:
            pass  # leave the block with the child still running
        assert child.proc.returncode is not None

    async def test_completed_child_is_untouched(self):
        async with managed_subprocess(
            sys.executable, "-c", "print('ok')", timeout=30.0, stdout=asyncio.subprocess.PIPE
        ) as child:
            stdout, _ = await child.proc.communicate()
        assert child.proc.returncode == 0
        assert b"ok" in stdout

    async def test_missing_binary_raises_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            async with managed_subprocess("definitely-not-a-real-binary-xyz", timeout=30.0):
                pass  # pragma: no cover

    @pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
    async def test_child_runs_in_its_own_process_group(self):
        """start_new_session must isolate the child as a group leader so a
        group kill sweeps anything the child itself spawned."""
        async with managed_subprocess(*SLEEPER, timeout=30.0, grace=2.0) as child:
            assert os.getpgid(child.proc.pid) == child.proc.pid

    def test_a_child_cannot_be_started_without_a_deadline(self):
        """The deadline is part of the door, not a courtesy at the call
        site: managed_subprocess refuses to spawn without one (ledger
        R-001: the two unbounded ffmpeg calls were the ones that parked a
        worker forever)."""
        with pytest.raises(TypeError, match="timeout"):
            managed_subprocess(*SLEEPER)  # type: ignore[call-arg]

    async def test_deadline_kills_a_silent_child(self):
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            async with managed_subprocess(*SLEEPER, timeout=0.5, grace=2.0) as child:
                await child.proc.wait()
        assert child.proc.returncode is not None  # reaped, not orphaned
        assert time.monotonic() - started < 10.0

    async def test_extend_turns_the_deadline_into_a_stall_watchdog(self):
        """A child that keeps talking outlives its window; the same child
        without extend() does not. This is how an hours-long encode gets a
        bound without a wall-clock guess: it dies after N seconds of
        silence, not after N seconds."""
        started = time.monotonic()
        async with managed_subprocess(
            *TALKER, timeout=0.5, grace=2.0, stdout=asyncio.subprocess.PIPE
        ) as child:
            assert child.proc.stdout is not None
            while await child.proc.stdout.readline():
                child.extend(0.5)
            await child.proc.wait()
        assert child.proc.returncode == 0
        assert time.monotonic() - started > 1.0  # ran well past the 0.5 s window

    async def test_without_extend_a_talking_child_still_hits_the_deadline(self):
        with pytest.raises(TimeoutError):
            async with managed_subprocess(
                *TALKER, timeout=0.5, grace=2.0, stdout=asyncio.subprocess.PIPE
            ) as child:
                await child.proc.wait()
        assert child.proc.returncode is not None

    def test_extend_after_expiry_is_a_no_op(self):
        """Race: the deadline fires while the caller is between an await
        and its extend() call. asyncio refuses to reschedule an expired
        Timeout; the caller must not blow up with RuntimeError on its way
        to the TimeoutError the door is about to raise."""

        class Expired:
            def expired(self) -> bool:
                return True

            def reschedule(self, when: float) -> None:
                raise RuntimeError("Cannot change state of expiring Timeout")

        Child(MagicMock(), Expired()).extend(5.0)  # type: ignore[arg-type]

    def test_only_proc_py_starts_child_processes(self):
        """Every child is created in worker/proc.py: that is where the
        lifetime tie and the deadline live, so a spawn anywhere else is a
        child that can outlive the worker or run unbounded (R-025 found
        two such doors: the hardware probes and the scanner's ffprobe)."""
        src = Path(__file__).resolve().parent.parent / "src" / "transcode_forge"
        spawners = (
            "create_subprocess_exec",
            "create_subprocess_shell",
            "subprocess.run(",
            "subprocess.Popen(",
            "subprocess.call(",
            "subprocess.check_output(",
            "os.system(",
            "os.spawn",
            "os.exec",
        )
        offenders = [
            f"{path.relative_to(src)}: {needle}"
            for path in sorted(src.rglob("*.py"))
            if path.name != "proc.py"
            for needle in spawners
            if needle in path.read_text(encoding="utf-8")
        ]
        assert offenders == [], offenders


class TestRunEncodeCancellation:
    async def test_run_encode_kills_ffmpeg_on_cancel(self, tmp_path):
        """run_encode must spawn through managed_subprocess so a cancelled
        encode (shutdown abort / teardown) kills its ffmpeg."""
        holder: dict = {}
        real_cm = managed_subprocess

        @contextlib.asynccontextmanager
        async def recording(*cmd, **kwargs):
            async with real_cm(*cmd, grace=2.0, **kwargs) as child:
                holder["proc"] = child.proc
                yield child

        # A fake "ffmpeg": emits nothing on stderr, never exits — run_encode
        # blocks in its readline loop, exactly like a mid-encode ffmpeg.
        cmd = [
            sys.executable,
            "-c",
            "import time; time.sleep(120)",
            str(tmp_path / "out.mkv"),  # run_encode treats cmd[-1] as output
        ]
        with patch("transcode_forge.worker.encoder.managed_subprocess", recording):
            task = asyncio.create_task(run_encode(cmd, total_duration=60.0))
            await _wait_for(lambda: "proc" in holder)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert holder["proc"].returncode is not None

    async def test_measure_vmaf_kills_ffmpeg_on_cancel(self):
        """measure_vmaf blocks for up to hours on full-file scoring — its
        child must die on cancellation too."""
        from transcode_forge.worker import vmaf as vmaf_mod

        holder: dict = {}

        @contextlib.asynccontextmanager
        async def sleeper_cm(*cmd, **kwargs):
            # Swap the (nonexistent-libvmaf) command for a long sleeper; the
            # point is that measure_vmaf routes through managed_subprocess
            # and its child dies with the cancelled task.
            async with managed_subprocess(*SLEEPER, grace=2.0, **kwargs) as child:
                holder["proc"] = child.proc
                yield child

        with patch.object(vmaf_mod, "managed_subprocess", sleeper_cm):
            task = asyncio.create_task(vmaf_mod.measure_vmaf("/src.mkv", "/enc.mkv"))
            await _wait_for(lambda: "proc" in holder)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert holder["proc"].returncode is not None


def _cpu_caps() -> HardwareCapabilities:
    return HardwareCapabilities(
        encoders=["cpu"],
        pairs=[("av1", "cpu"), ("hevc", "cpu")],
        ffmpeg_version="ffmpeg 7.0",
        os_platform="Linux",
    )


def _agent(test_settings) -> HttpWorkerAgent:
    agent = HttpWorkerAgent(test_settings, "http://scheduler", "test-token")
    agent.worker_id = "worker-1"
    agent.capabilities = _cpu_caps()
    agent._client = AsyncMock()
    return agent


def _job() -> Job:
    job = Job(
        source_path="/media/movies/test.mkv",
        library="movies",
        source_codec="h264",
        quality_value=21,
    )
    object.__setattr__(job, "_backend_type", "filesystem")
    return job


def _mock_storage(tmp_path):
    output_file = tmp_path / "out.mkv"
    output_file.write_bytes(b"fake video data")
    storage = AsyncMock()
    storage.fetch = AsyncMock(return_value=output_file)
    storage.commit = AsyncMock(return_value=MagicMock(output_size=5, space_saved=5))
    storage.cleanup = AsyncMock()
    return storage


class TestAgentShutdownSignals:
    async def test_second_signal_aborts_running_encode(self, test_settings, tmp_path):
        """2nd SIGTERM: cancel the in-flight pipeline, report the job as
        failed (so it doesn't strand in 'transcoding'), return orderly."""
        agent = _agent(test_settings)
        job = _job()
        started = asyncio.Event()

        async def never_finishes(**kwargs):
            started.set()
            await asyncio.sleep(120)

        with (
            patch("transcode_forge.worker.http_agent.run_pipeline", new=never_finishes),
            patch.object(agent, "_get_backend_for_job", return_value=_mock_storage(tmp_path)),
        ):
            task = asyncio.create_task(agent._process_job(job))
            await started.wait()
            agent._handle_shutdown()  # 1st: drain
            agent._handle_shutdown()  # 2nd: abort the encode
            await asyncio.wait_for(task, timeout=5.0)  # completes, no raise

        agent._client.failed.assert_awaited_once()
        kwargs = agent._client.failed.call_args.kwargs
        assert "shutdown" in kwargs["error_message"].lower()
        # A shutdown abort is not the file's fault — never burn a retry.
        assert kwargs["retry_count"] == job.retry_count
        assert agent._current_job_id is None

    async def test_abort_during_fetch_never_starts_the_encode(self, test_settings, tmp_path):
        """2nd signal while fetching (S3 download can take minutes): the
        hours-long encode must not start afterwards."""
        agent = _agent(test_settings)
        job = _job()
        fetch_entered = asyncio.Event()
        fetch_gate = asyncio.Event()
        storage = _mock_storage(tmp_path)
        output_file = tmp_path / "out.mkv"

        async def slow_fetch(ref):
            fetch_entered.set()
            await fetch_gate.wait()
            return output_file

        storage.fetch = AsyncMock(side_effect=slow_fetch)

        with (
            patch("transcode_forge.worker.http_agent.run_pipeline") as mock_pipeline,
            patch.object(agent, "_get_backend_for_job", return_value=storage),
        ):
            task = asyncio.create_task(agent._process_job(job))
            await fetch_entered.wait()
            agent._handle_shutdown()
            agent._handle_shutdown()
            fetch_gate.set()
            await asyncio.wait_for(task, timeout=5.0)

        mock_pipeline.assert_not_called()
        agent._client.failed.assert_awaited_once()

    async def test_third_signal_forces_exit(self, test_settings):
        agent = _agent(test_settings)
        agent._handle_shutdown()
        agent._handle_shutdown()
        with pytest.raises(SystemExit):
            agent._handle_shutdown()

    async def test_external_cancellation_still_propagates(self, test_settings, tmp_path):
        """Cancellation the agent did NOT request (event-loop teardown) must
        keep propagating — only the deliberate abort is swallowed."""
        agent = _agent(test_settings)
        job = _job()
        started = asyncio.Event()

        async def never_finishes(**kwargs):
            started.set()
            await asyncio.sleep(120)

        with (
            patch("transcode_forge.worker.http_agent.run_pipeline", new=never_finishes),
            patch.object(agent, "_get_backend_for_job", return_value=_mock_storage(tmp_path)),
        ):
            task = asyncio.create_task(agent._process_job(job))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        agent._client.failed.assert_not_called()

    async def test_first_signal_lets_job_finish(self, test_settings, tmp_path):
        """1st SIGTERM mid-encode: the job runs to completion and reports
        complete — the drain semantics stay intact."""
        agent = _agent(test_settings)
        job = _job()
        started = asyncio.Event()
        gate = asyncio.Event()

        async def gated_pipeline(**kwargs):
            started.set()
            await gate.wait()
            return {"source_size": 10, "space_saved": 5}

        with (
            patch("transcode_forge.worker.http_agent.run_pipeline", new=gated_pipeline),
            patch.object(agent, "_get_backend_for_job", return_value=_mock_storage(tmp_path)),
        ):
            task = asyncio.create_task(agent._process_job(job))
            await started.wait()
            agent._handle_shutdown()  # drain only
            gate.set()
            await asyncio.wait_for(task, timeout=5.0)

        agent._client.complete.assert_awaited_once()
        agent._client.failed.assert_not_called()


class TestHeartbeatDuringDrain:
    async def test_heartbeat_continues_while_job_drains(self, test_settings):
        """During a graceful drain the scheduler must keep seeing the worker
        as busy — the old loop exited on the first signal and the fleet
        showed 'heartbeat lost' for the entire tail of the encode."""
        agent = _agent(test_settings)
        agent._shutting_down = True
        agent._current_job_id = "j1"

        task = asyncio.create_task(agent._heartbeat_loop())
        try:
            await _wait_for(lambda: agent._client.heartbeat.await_count >= 1)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        assert agent._client.heartbeat.call_args.kwargs["status"] == "busy"

    async def test_heartbeat_exits_when_idle_and_shutting_down(self, test_settings):
        agent = _agent(test_settings)
        agent._shutting_down = True
        agent._current_job_id = None
        await asyncio.wait_for(agent._heartbeat_loop(), timeout=2.0)
        agent._client.heartbeat.assert_not_called()


_REAL_SLEEP = asyncio.sleep
_UNREACHABLE = httpx.ConnectError("scheduler unreachable")
_FENCE_PREFIX = "Aborted: lost contact with the scheduler for "


async def _fast_sleep(_seconds: float) -> None:
    """Stand-in for the agent loops' asyncio.sleep: real time still
    passes (the fence measures it with time.monotonic), just quickly."""
    await _REAL_SLEEP(0.001)


async def _run_heartbeats_for(agent: HttpWorkerAgent, seconds: float) -> None:
    """Drive _heartbeat_loop for a stretch of real time, then stop it."""
    task = asyncio.create_task(agent._heartbeat_loop())
    try:
        await _REAL_SLEEP(seconds)
        assert not task.done()  # the loop never exits on its own
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class TestPartitionFence:
    """A worker that has lost the scheduler must not keep encoding past
    the point where the scheduler requeues its job. Incident 2026-09-02:
    a worker with a wedged network encoded, and kept its .tf_lock fresh,
    for 44 min after the scheduler marked it dead at 90 s, so the retry
    claimer met a live lock. The fence aborts the in-flight job the way
    the second shutdown signal does, a margin before the scheduler's
    orphan grace runs out, and the worker itself keeps running."""

    async def test_fence_aborts_in_flight_job_and_keeps_the_worker_running(
        self, test_settings, tmp_path
    ):
        agent = _agent(test_settings)
        agent._fence_after_seconds = 0.05
        agent._client.heartbeat = AsyncMock(side_effect=_UNREACHABLE)
        job = _job()
        started = asyncio.Event()

        async def never_finishes(**kwargs):
            started.set()
            await asyncio.Event().wait()

        with (
            patch("transcode_forge.worker.http_agent.run_pipeline", new=never_finishes),
            patch.object(agent, "_get_backend_for_job", return_value=_mock_storage(tmp_path)),
            patch("transcode_forge.worker.http_agent.asyncio.sleep", new=_fast_sleep),
        ):
            heartbeats = asyncio.create_task(agent._heartbeat_loop())
            task = asyncio.create_task(agent._process_job(job))
            await started.wait()
            await asyncio.wait_for(task, timeout=5.0)  # fenced, no raise
            assert not heartbeats.done()  # the worker keeps beating
            heartbeats.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeats

        assert not agent._shutting_down
        agent._client.failed.assert_awaited_once()
        kwargs = agent._client.failed.call_args.kwargs
        assert kwargs["error_message"].startswith(_FENCE_PREFIX)
        # Not the file's fault: the retry is not burned.
        assert kwargs["retry_count"] == job.retry_count
        assert agent._current_job_id is None

    async def test_worker_claims_again_after_a_fence(self, test_settings, tmp_path):
        """The fence is consumed with the job it aborted: once the
        scheduler answers again the next claim runs to completion, and
        the shutdown ladder is back at its first rung."""
        agent = _agent(test_settings)
        agent._fence_after_seconds = 0.05
        first, second = _job(), _job()

        async def heartbeat(**kwargs):
            if agent._current_job_id == first.id:
                raise _UNREACHABLE

        claims = [
            first.model_dump(mode="json") | {"_backend_type": "filesystem"},
            second.model_dump(mode="json") | {"_backend_type": "filesystem"},
        ]

        async def claim_job(**kwargs):
            return claims.pop(0) if claims else None

        agent._client.heartbeat = AsyncMock(side_effect=heartbeat)
        agent._client.claim_job = AsyncMock(side_effect=claim_job)

        async def pipeline(**kwargs):
            if kwargs["job_id"] == first.id:
                await asyncio.Event().wait()
            return {"source_size": 10, "space_saved": 5}

        with (
            patch("transcode_forge.worker.http_agent.run_pipeline", new=pipeline),
            patch.object(agent, "_get_backend_for_job", return_value=_mock_storage(tmp_path)),
            patch("transcode_forge.worker.http_agent.asyncio.sleep", new=_fast_sleep),
        ):
            loops = asyncio.gather(agent._heartbeat_loop(), agent._job_loop())
            try:
                await _wait_for(lambda: agent._client.complete.await_count >= 1)
            finally:
                loops.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await loops

        agent._client.failed.assert_awaited_once()
        assert agent._client.failed.call_args.kwargs["job_id"] == first.id
        assert agent._client.complete.call_args.kwargs["job_id"] == second.id
        assert not agent._abort_requested
        assert not agent._shutting_down
        agent._handle_shutdown()  # first rung: drain, not force-exit
        assert agent._shutting_down and not agent._abort_requested

    async def test_fence_fires_once_per_outage(self, test_settings):
        agent = _agent(test_settings)
        agent._fence_after_seconds = 0.05
        agent._current_job_id = "j1"
        down = True

        async def heartbeat(**kwargs):
            if down:
                raise _UNREACHABLE

        agent._client.heartbeat = AsyncMock(side_effect=heartbeat)
        with (
            patch.object(agent, "_abort_current_job", wraps=agent._abort_current_job) as fence,
            patch("transcode_forge.worker.http_agent.asyncio.sleep", new=_fast_sleep),
        ):
            task = asyncio.create_task(agent._heartbeat_loop())
            try:
                await _wait_for(lambda: fence.call_count == 1)
                await _REAL_SLEEP(0.2)  # the outage goes on: no second fence
                assert fence.call_count == 1
                down = False  # contact returns
                await _REAL_SLEEP(0.05)
                down = True  # a new outage fences again
                await _wait_for(lambda: fence.call_count == 2)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        assert fence.call_args.args[0].startswith(_FENCE_PREFIX)

    async def test_no_fence_when_heartbeats_recover_in_time(self, test_settings):
        agent = _agent(test_settings)
        agent._fence_after_seconds = 0.2
        agent._current_job_id = "j1"
        failures_left = 3

        async def heartbeat(**kwargs):
            nonlocal failures_left
            if failures_left:
                failures_left -= 1
                raise _UNREACHABLE

        agent._client.heartbeat = AsyncMock(side_effect=heartbeat)
        with (
            patch.object(agent, "_abort_current_job") as fence,
            patch("transcode_forge.worker.http_agent.asyncio.sleep", new=_fast_sleep),
        ):
            # Three failures in a few ms, then a healthy scheduler for
            # longer than the threshold.
            await _run_heartbeats_for(agent, 0.3)
        fence.assert_not_called()
        assert agent._client.heartbeat.await_count > 3

    async def test_one_signal_during_a_fence_drains_instead_of_force_exiting(self, test_settings):
        """The fence sets _abort_requested with no shutdown in progress. A
        single SIGTERM in that window must land on rung 1 (drain), not on
        rung 3; the second signal then force-exits as documented."""
        agent = _agent(test_settings)
        agent._abort_current_job("Aborted: lost contact with the scheduler for 480 s")
        agent._handle_shutdown()
        assert agent._shutting_down is True
        with pytest.raises(SystemExit):
            agent._handle_shutdown()

    async def test_worker_state_read_error_skips_the_beat_and_keeps_the_loop(self, test_settings):
        """A failing outbox read (a stale state-dir mount) is not lost
        contact with the scheduler. The beat is skipped, the loop keeps
        running, and the fence is untouched. The outbox is only consulted
        while no job is in flight, so this can never abort an encode."""
        agent = _agent(test_settings)
        agent._fence_after_seconds = 0.0
        agent._current_job_id = None
        agent._client.heartbeat = AsyncMock()

        def broken_outbox_read() -> str | None:
            raise OSError("state dir mount gone")

        agent.outbox.oldest_pending_job_id = broken_outbox_read  # type: ignore[method-assign]
        with (
            patch.object(agent, "_abort_current_job") as fence,
            patch("transcode_forge.worker.http_agent.asyncio.sleep", new=_fast_sleep),
        ):
            await _run_heartbeats_for(agent, 0.1)
        fence.assert_not_called()
        agent._client.heartbeat.assert_not_awaited()

    async def test_no_fence_without_a_job_in_flight(self, test_settings):
        agent = _agent(test_settings)
        agent._fence_after_seconds = 0.02
        agent._current_job_id = None
        agent._client.heartbeat = AsyncMock(side_effect=_UNREACHABLE)
        with (
            patch.object(agent, "_abort_current_job") as fence,
            patch("transcode_forge.worker.http_agent.asyncio.sleep", new=_fast_sleep),
        ):
            await _run_heartbeats_for(agent, 0.2)
        fence.assert_not_called()

    def test_threshold_comes_from_the_advertised_orphan_grace(self):
        """The scheduler advertises its orphan grace at /register; the
        worker fences a margin before it. A scheduler predating the field
        enforces 600 s, so its absence means the same 480 s."""
        assert fence_after_seconds({"worker_id": "w"}) == 480.0
        assert fence_after_seconds({"worker_id": "w", "orphan_grace_seconds": 600}) == 480.0
        assert fence_after_seconds({"worker_id": "w", "orphan_grace_seconds": 900}) == 780.0

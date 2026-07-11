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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from transcode_forge.models.job import Job
from transcode_forge.worker.encoder import run_encode
from transcode_forge.worker.hardware import HardwareCapabilities
from transcode_forge.worker.http_agent import HttpWorkerAgent
from transcode_forge.worker.proc import managed_subprocess

# A child that runs "forever" (far longer than any test timeout).
SLEEPER = [sys.executable, "-c", "import time; time.sleep(120)"]


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
            async with managed_subprocess(*SLEEPER, grace=2.0) as proc:
                holder["proc"] = proc
                await proc.wait()

        task = asyncio.create_task(consume())
        await _wait_for(lambda: "proc" in holder)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert holder["proc"].returncode is not None  # reaped, not orphaned

    async def test_child_killed_on_exception(self):
        holder: dict = {}
        with pytest.raises(RuntimeError, match="boom"):
            async with managed_subprocess(*SLEEPER, grace=2.0) as proc:
                holder["proc"] = proc
                raise RuntimeError("boom")
        assert holder["proc"].returncode is not None

    async def test_child_killed_on_early_return(self):
        async with managed_subprocess(*SLEEPER, grace=2.0) as proc:
            pass  # leave the block with the child still running
        assert proc.returncode is not None

    async def test_completed_child_is_untouched(self):
        async with managed_subprocess(
            sys.executable, "-c", "print('ok')", stdout=asyncio.subprocess.PIPE
        ) as proc:
            stdout, _ = await proc.communicate()
        assert proc.returncode == 0
        assert b"ok" in stdout

    async def test_missing_binary_raises_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            async with managed_subprocess("definitely-not-a-real-binary-xyz"):
                pass  # pragma: no cover

    @pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
    async def test_child_runs_in_its_own_process_group(self):
        """start_new_session must isolate the child as a group leader so a
        group kill sweeps anything the child itself spawned."""
        async with managed_subprocess(*SLEEPER, grace=2.0) as proc:
            assert os.getpgid(proc.pid) == proc.pid


class TestRunEncodeCancellation:
    async def test_run_encode_kills_ffmpeg_on_cancel(self, tmp_path):
        """run_encode must spawn through managed_subprocess so a cancelled
        encode (shutdown abort / teardown) kills its ffmpeg."""
        holder: dict = {}
        real_cm = managed_subprocess

        @contextlib.asynccontextmanager
        async def recording(*cmd, **kwargs):
            async with real_cm(*cmd, grace=2.0, **kwargs) as proc:
                holder["proc"] = proc
                yield proc

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
            async with managed_subprocess(*SLEEPER, grace=2.0, **kwargs) as proc:
                holder["proc"] = proc
                yield proc

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

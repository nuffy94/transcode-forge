"""Tests for the 8-step transcode safety pipeline."""

import json
import os
from datetime import UTC
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from transcode_forge.scanner.probe import ProbeResult
from transcode_forge.worker.pipeline import (
    PipelineError,
    SizeRegressionError,
    _verify_output,
    find_stale_locks,
    run_pipeline,
)
from transcode_forge.worker.storage.filesystem import (
    _acquire_lock,
    _atomic_swap,
    _preserve_metadata,
    _safe_delete,
)


class TestAcquireLock:
    def test_creates_lock_file(self, tmp_path):
        lock = tmp_path / "test.mkv.tf_lock"
        _acquire_lock(lock, job_id="j1", worker_id="w1")
        assert lock.exists()
        data = json.loads(lock.read_text())
        assert data["job_id"] == "j1"
        assert data["worker_id"] == "w1"
        assert "timestamp" in data

    def test_raises_if_lock_exists(self, tmp_path):
        lock = tmp_path / "test.mkv.tf_lock"
        lock.write_text('{"job_id": "other"}')
        with pytest.raises(PipelineError, match="Lock file already exists"):
            _acquire_lock(lock, job_id="j1", worker_id="w1")

    def test_concurrent_acquire_only_one_wins(self, tmp_path):
        """N threads racing on the same path → exactly one acquires the lock.

        Guards the atomic-create (open "x") behavior: a plain exists()
        check-then-write would let multiple racers slip through.
        """
        import concurrent.futures

        lock = tmp_path / "race.mkv.tf_lock"

        def attempt(n: int) -> bool:
            try:
                _acquire_lock(lock, job_id=f"j{n}", worker_id=f"w{n}")
                return True
            except PipelineError:
                return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(attempt, range(8)))

        assert sum(results) == 1
        assert lock.exists()


class TestAtomicSwap:
    def test_swap_succeeds(self, tmp_path):
        original = tmp_path / "test.mkv"
        tmp = tmp_path / "test.mkv.tf_tmp"
        bak = tmp_path / "test.mkv.tf_bak"

        original.write_bytes(b"original data")
        tmp.write_bytes(b"transcoded data")

        _atomic_swap(original, tmp, bak)

        assert not tmp.exists()
        assert bak.exists()
        assert original.exists()
        assert original.read_bytes() == b"transcoded data"
        assert bak.read_bytes() == b"original data"

    def test_swap_rollback_on_second_rename_failure(self, tmp_path):
        original = tmp_path / "test.mkv"
        bak = tmp_path / "test.mkv.tf_bak"

        original.write_bytes(b"original data")
        # tmp doesn't exist — second rename will fail
        tmp = tmp_path / "nonexistent.tf_tmp"

        with pytest.raises(PipelineError, match="Failed to rename tmp"):
            _atomic_swap(original, tmp, bak)

        # Original should be restored from backup
        assert original.exists() or bak.exists()

    def test_swap_refuses_to_clobber_existing_backup(self, tmp_path):
        """A pre-existing .tf_bak is the LAST copy of a true original
        (completed job whose backup delete failed). POSIX rename replaces
        silently — without this guard, re-encoding that path destroys it."""
        original = tmp_path / "test.mkv"
        tmp = tmp_path / "test.tf_tmp.mkv"
        bak = tmp_path / "test.tf_bak.mkv"

        original.write_bytes(b"confirmed encode")
        tmp.write_bytes(b"new encode")
        bak.write_bytes(b"the real original")

        with pytest.raises(PipelineError, match="backup"):
            _atomic_swap(original, tmp, bak)

        # Nothing moved: all three files intact.
        assert original.read_bytes() == b"confirmed encode"
        assert bak.read_bytes() == b"the real original"
        assert tmp.read_bytes() == b"new encode"


class TestSafeDelete:
    def test_deletes_existing_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("data")
        _safe_delete(f)
        assert not f.exists()

    def test_no_error_on_missing_file(self, tmp_path):
        f = tmp_path / "nonexistent.txt"
        _safe_delete(f)  # Should not raise


class TestFindStaleLocks:
    def test_finds_old_locks(self, tmp_path):
        lock = tmp_path / "test.mkv.tf_lock"
        # Write a lock with old timestamp
        lock.write_text(
            json.dumps(
                {
                    "job_id": "j1",
                    "worker_id": "w1",
                    "timestamp": "2020-01-01T00:00:00+00:00",
                }
            )
        )

        stale = find_stale_locks(tmp_path, max_age_hours=0.001)
        assert len(stale) == 1
        assert stale[0]["job_id"] == "j1"

    def test_ignores_fresh_locks(self, tmp_path):
        from datetime import datetime

        lock = tmp_path / "test.mkv.tf_lock"
        lock.write_text(
            json.dumps(
                {
                    "job_id": "j1",
                    "worker_id": "w1",
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            )
        )

        stale = find_stale_locks(tmp_path, max_age_hours=1.0)
        assert len(stale) == 0

    def test_handles_corrupt_lock(self, tmp_path):
        lock = tmp_path / "test.mkv.tf_lock"
        lock.write_text("not json")

        stale = find_stale_locks(tmp_path)
        assert len(stale) == 1
        assert "error" in stale[0]


class TestSizeRegressionError:
    def test_is_pipeline_error(self):
        err = SizeRegressionError(1000, 1500)
        assert isinstance(err, PipelineError)
        assert err.step == "COMPARE"
        assert err.source_size == 1000
        assert err.output_size == 1500


class TestPreserveMetadata:
    """The new file should look like the file it replaced — same mode and
    mtime so media servers don't re-trigger 'newly added' logic."""

    def test_preserves_mtime(self, tmp_path):
        import os

        original = tmp_path / "original.mkv"
        original.write_bytes(b"x")
        os.utime(original, (1700000000, 1700000000))
        src_stat = original.stat()

        replacement = tmp_path / "replacement.mkv"
        replacement.write_bytes(b"y")
        os.utime(replacement, (1800000000, 1800000000))

        _preserve_metadata(replacement, src_stat)

        assert int(replacement.stat().st_mtime) == 1700000000

    @pytest.mark.skipif(
        os.name == "nt",
        reason="Windows ignores most POSIX mode bits — only Linux/macOS honor 0o644",
    )
    def test_preserves_mode_on_posix(self, tmp_path):
        import stat as stat_mod

        original = tmp_path / "original.mkv"
        original.write_bytes(b"x")
        os.chmod(original, 0o644)
        src_stat = original.stat()

        replacement = tmp_path / "replacement.mkv"
        replacement.write_bytes(b"y")
        os.chmod(replacement, 0o600)

        _preserve_metadata(replacement, src_stat)

        assert stat_mod.S_IMODE(replacement.stat().st_mode) == 0o644

    def test_partial_failure_does_not_raise(self, tmp_path, caplog):
        """If chown fails (non-root, no CAP_CHOWN), the helper logs and
        moves on — it doesn't break a successful pipeline."""
        import logging

        target = tmp_path / "f.mkv"
        target.write_bytes(b"x")
        src_stat = target.stat()

        with patch("transcode_forge.worker.storage.filesystem.os") as mock_os:
            mock_os.chown.side_effect = PermissionError("CAP_CHOWN missing")
            mock_os.chmod.side_effect = OSError("read-only fs")
            mock_os.utime.side_effect = OSError("nope")

            with caplog.at_level(logging.WARNING):
                _preserve_metadata(target, src_stat)

            assert any("Could not chown" in r.message for r in caplog.records)
            assert any("Could not chmod" in r.message for r in caplog.records)
            assert any("Could not preserve mtime" in r.message for r in caplog.records)


class TestRunPipeline:
    async def test_successful_pipeline(self, tmp_path):
        # Create a source file
        source = tmp_path / "test.mkv"
        source.write_bytes(b"x" * 10000)  # 10KB original

        # Mock the encode to create a smaller output
        async def mock_run_encode(cmd, total_duration, progress_callback=None):
            from transcode_forge.worker.encoder import EncodeResult

            output = Path(cmd[-1])
            output.write_bytes(b"y" * 5000)  # 5KB output (50% savings)
            return EncodeResult(
                success=True, output_path=str(output), output_size=5000, returncode=0
            )

        # Mock ffprobe to return hevc
        mock_probe = ProbeResult(
            video_codec="hevc",
            width=1920,
            height=1080,
            bitrate=5000000,
            duration=3600.0,
            file_size=5000,
        )

        with (
            patch("transcode_forge.worker.pipeline.run_encode", side_effect=mock_run_encode),
            patch("transcode_forge.worker.pipeline.ffprobe", return_value=mock_probe),
            patch("transcode_forge.worker.pipeline._decode_check"),
        ):
            result = await run_pipeline(
                source_path=str(source),
                codec="hevc",
                backend="cpu",
                quality=21,
                source_duration=3600.0,
                job_id="test-job",
                worker_id="test-worker",
            )

        assert result["source_size"] == 10000
        assert result["output_size"] == 5000
        assert result["space_saved"] == 5000
        # Original file should now contain the transcoded data
        assert source.read_bytes() == b"y" * 5000
        # No lock or tmp files left
        assert not (tmp_path / "test.mkv.tf_lock").exists()
        assert not (tmp_path / "test.mkv.tf_tmp").exists()
        assert not (tmp_path / "test.mkv.tf_bak").exists()

    async def test_size_regression_raises(self, tmp_path):
        source = tmp_path / "test.mkv"
        source.write_bytes(b"x" * 5000)  # 5KB original

        # Mock encode creates LARGER output
        async def mock_run_encode(cmd, total_duration, progress_callback=None):
            from transcode_forge.worker.encoder import EncodeResult

            output = Path(cmd[-1])
            output.write_bytes(b"y" * 10000)  # 10KB — bigger!
            return EncodeResult(
                success=True, output_path=str(output), output_size=10000, returncode=0
            )

        mock_probe = ProbeResult(
            video_codec="hevc",
            width=1920,
            height=1080,
            bitrate=5000000,
            duration=3600.0,
            file_size=10000,
        )

        with (
            patch("transcode_forge.worker.pipeline.run_encode", side_effect=mock_run_encode),
            patch("transcode_forge.worker.pipeline.ffprobe", return_value=mock_probe),
            patch("transcode_forge.worker.pipeline._decode_check"),
        ):
            with pytest.raises(SizeRegressionError):
                await run_pipeline(
                    source_path=str(source),
                    codec="hevc",
                    backend="cpu",
                    quality=21,
                    source_duration=3600.0,
                    job_id="test-job",
                    worker_id="test-worker",
                )

        # Original untouched
        assert source.read_bytes() == b"x" * 5000

    async def test_encode_failure_cleans_up(self, tmp_path):
        source = tmp_path / "test.mkv"
        source.write_bytes(b"original")

        async def mock_run_encode(cmd, total_duration, progress_callback=None):
            from transcode_forge.worker.encoder import EncodeResult

            return EncodeResult(
                success=False,
                output_path=cmd[-1],
                output_size=0,
                returncode=1,
                error_message="encoder crashed",
            )

        with patch("transcode_forge.worker.pipeline.run_encode", side_effect=mock_run_encode):
            with pytest.raises(PipelineError, match="encoder crashed"):
                await run_pipeline(
                    source_path=str(source),
                    codec="hevc",
                    backend="cpu",
                    quality=21,
                    source_duration=3600.0,
                    job_id="test-job",
                    worker_id="test-worker",
                )

        # Original intact, no leftover files
        assert source.read_bytes() == b"original"
        assert not (tmp_path / "test.mkv.tf_lock").exists()

    async def test_preexisting_backup_aborts_before_swap(self, tmp_path):
        """End-to-end: a stranded .tf_bak at the source path must abort the
        pipeline at SWAP with the original untouched — never silently
        overwrite the backup."""
        source = tmp_path / "test.mkv"
        source.write_bytes(b"x" * 10000)
        stranded_bak = tmp_path / "test.tf_bak.mkv"
        stranded_bak.write_bytes(b"the real original")

        async def mock_run_encode(cmd, total_duration, progress_callback=None):
            from transcode_forge.worker.encoder import EncodeResult

            output = Path(cmd[-1])
            output.write_bytes(b"y" * 5000)
            return EncodeResult(
                success=True, output_path=str(output), output_size=5000, returncode=0
            )

        mock_probe = ProbeResult(
            video_codec="hevc",
            width=1920,
            height=1080,
            bitrate=5000000,
            duration=3600.0,
            file_size=5000,
        )

        with (
            patch("transcode_forge.worker.pipeline.run_encode", side_effect=mock_run_encode),
            patch("transcode_forge.worker.pipeline.ffprobe", return_value=mock_probe),
            patch("transcode_forge.worker.pipeline._decode_check"),
        ):
            with pytest.raises(PipelineError, match="backup"):
                await run_pipeline(
                    source_path=str(source),
                    codec="hevc",
                    backend="cpu",
                    quality=21,
                    source_duration=3600.0,
                    job_id="test-job",
                    worker_id="test-worker",
                )

        assert source.read_bytes() == b"x" * 10000
        assert stranded_bak.read_bytes() == b"the real original"

    async def test_lock_heartbeat_refreshes_lock_during_encode(self, tmp_path):
        """The lock timestamp must be refreshed while the pipeline runs so
        staleness means dead — a live multi-hour encode's lock may not
        decay into 'stale' for restarting neighbors on shared storage."""
        import asyncio

        source = tmp_path / "test.mkv"
        source.write_bytes(b"x" * 10000)
        lock = tmp_path / "test.mkv.tf_lock"
        seen: dict[str, str] = {}

        async def mock_run_encode(cmd, total_duration, progress_callback=None):
            from transcode_forge.worker.encoder import EncodeResult

            seen["before"] = json.loads(lock.read_text())["timestamp"]
            await asyncio.sleep(0.15)
            seen["after"] = json.loads(lock.read_text())["timestamp"]
            output = Path(cmd[-1])
            output.write_bytes(b"y" * 5000)
            return EncodeResult(
                success=True, output_path=str(output), output_size=5000, returncode=0
            )

        mock_probe = ProbeResult(
            video_codec="hevc",
            width=1920,
            height=1080,
            bitrate=5000000,
            duration=3600.0,
            file_size=5000,
        )

        with (
            patch("transcode_forge.worker.pipeline.run_encode", side_effect=mock_run_encode),
            patch("transcode_forge.worker.pipeline.ffprobe", return_value=mock_probe),
            patch("transcode_forge.worker.pipeline._decode_check"),
            patch("transcode_forge.worker.pipeline.LOCK_TOUCH_INTERVAL", 0.02),
        ):
            await run_pipeline(
                source_path=str(source),
                codec="hevc",
                backend="cpu",
                quality=21,
                source_duration=3600.0,
                job_id="test-job",
                worker_id="test-worker",
            )

        assert seen["after"] > seen["before"], "lock timestamp should refresh mid-encode"
        # Heartbeat cancelled cleanly: no lock resurrection, no temp leftovers.
        assert not lock.exists()
        assert not any(p.name.endswith(".new") for p in tmp_path.iterdir())

    async def test_phase_callback_emission_order(self, tmp_path):
        """The dashboard's station bar is only as honest as these events:
        a plain run reports encode->verify->swap; a gated run inserts
        gauge before the swap (search needs crf_search + a target)."""
        source = tmp_path / "test.mkv"
        source.write_bytes(b"x" * 10000)

        async def mock_run_encode(cmd, total_duration, progress_callback=None):
            from transcode_forge.worker.encoder import EncodeResult

            output = Path(cmd[-1])
            output.write_bytes(b"y" * 5000)
            return EncodeResult(
                success=True, output_path=str(output), output_size=5000, returncode=0
            )

        mock_probe = ProbeResult(
            video_codec="hevc",
            width=1920,
            height=1080,
            bitrate=5000000,
            duration=3600.0,
            file_size=5000,
        )

        phases: list[str] = []

        async def on_phase(phase: str) -> None:
            phases.append(str(phase))

        with (
            patch("transcode_forge.worker.pipeline.run_encode", side_effect=mock_run_encode),
            patch("transcode_forge.worker.pipeline.ffprobe", return_value=mock_probe),
            patch("transcode_forge.worker.pipeline._decode_check"),
        ):
            await run_pipeline(
                source_path=str(source),
                codec="hevc",
                backend="cpu",
                quality=21,
                source_duration=3600.0,
                job_id="test-job",
                worker_id="test-worker",
                phase_callback=on_phase,
            )
        assert phases == ["encode", "verify", "swap"]

        # Gated run: gauge appears; search needs the CRF search enabled too.
        source2 = tmp_path / "test2.mkv"
        source2.write_bytes(b"x" * 10000)
        phases.clear()

        from transcode_forge.worker.vmaf import VmafScore

        with (
            patch("transcode_forge.worker.pipeline.run_encode", side_effect=mock_run_encode),
            patch("transcode_forge.worker.pipeline.ffprobe", return_value=mock_probe),
            patch("transcode_forge.worker.pipeline._decode_check"),
            patch("transcode_forge.worker.pipeline.has_libvmaf", AsyncMock(return_value=True)),
            patch(
                "transcode_forge.worker.pipeline.measure_vmaf",
                AsyncMock(return_value=VmafScore(mean=97.0, perc5=95.0, min=94.0)),
            ),
        ):
            await run_pipeline(
                source_path=str(source2),
                codec="hevc",
                backend="cpu",
                quality=21,
                source_duration=3600.0,
                job_id="test-job2",
                worker_id="test-worker",
                target_vmaf=95.0,
                phase_callback=on_phase,
            )
        assert phases == ["encode", "verify", "gauge", "swap"]

    async def test_lock_conflict(self, tmp_path):
        source = tmp_path / "test.mkv"
        source.write_bytes(b"original")

        # Pre-existing lock
        lock = tmp_path / "test.mkv.tf_lock"
        lock.write_text('{"job_id": "other-job"}')

        with pytest.raises(PipelineError, match="Lock file already exists"):
            await run_pipeline(
                source_path=str(source),
                codec="hevc",
                backend="cpu",
                quality=21,
                source_duration=3600.0,
                job_id="test-job",
                worker_id="test-worker",
            )


class TestVerifyOutputDeepCheck:
    """The deep-check pass invokes ffmpeg to actually decode frames —
    catches bitstream corruption that ffprobe misses. _decode_check
    raises PipelineError on non-zero ffmpeg exit; tests mock the
    subprocess to assert that behavior.
    """

    async def test_failed_decode_raises_pipeline_error(self, tmp_path):
        out = tmp_path / "broken.mkv"
        out.write_bytes(b"x" * 1000)

        mock_probe = ProbeResult(
            video_codec="hevc",
            width=1920,
            height=1080,
            bitrate=5_000_000,
            duration=3600.0,
            file_size=1000,
        )

        async def mock_decode_fail(path, duration):
            raise PipelineError("VERIFY", "Decode test failed at offset 1800s: bitstream error")

        with (
            patch("transcode_forge.worker.pipeline.ffprobe", return_value=mock_probe),
            patch(
                "transcode_forge.worker.pipeline._decode_check",
                side_effect=mock_decode_fail,
            ),
        ):
            with pytest.raises(PipelineError, match="Decode test failed"):
                await _verify_output(out, expected_duration=3600.0)

    async def test_deep_check_can_be_disabled(self, tmp_path):
        """When deep_check=False, only ffprobe is called — no decode."""
        out = tmp_path / "x.mkv"
        out.write_bytes(b"x" * 1000)

        mock_probe = ProbeResult(
            video_codec="hevc",
            width=1920,
            height=1080,
            bitrate=5_000_000,
            duration=3600.0,
            file_size=1000,
        )

        with (
            patch("transcode_forge.worker.pipeline.ffprobe", return_value=mock_probe),
            patch("transcode_forge.worker.pipeline._decode_check") as decode_mock,
        ):
            await _verify_output(out, expected_duration=3600.0, deep_check=False)
            decode_mock.assert_not_called()

    async def test_short_file_decodes_whole_thing_once(self, tmp_path):
        """Files shorter than 1.5x the sample length get decoded as a whole,
        not sampled at offsets — verifies the duration branch."""
        from transcode_forge.worker.pipeline import _decode_check

        out = tmp_path / "x.mkv"
        out.write_bytes(b"x" * 100)

        # Capture the cmds the decode_check would invoke.
        invocations: list[list[str]] = []

        class MockProc:
            returncode = 0

            async def communicate(self):
                return (b"", b"")

        async def fake_exec(*args, **kwargs):
            invocations.append(list(args))
            return MockProc()

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await _decode_check(out, duration=5.0)  # < 15s threshold

        assert len(invocations) == 1, "short file should decode in a single pass"
        # And the -ss should be 0
        cmd = invocations[0]
        assert cmd[cmd.index("-ss") + 1] == "0.00"

    async def test_long_file_samples_three_offsets(self, tmp_path):
        from transcode_forge.worker.pipeline import _decode_check

        out = tmp_path / "x.mkv"
        out.write_bytes(b"x" * 100)

        invocations: list[list[str]] = []

        class MockProc:
            returncode = 0

            async def communicate(self):
                return (b"", b"")

        async def fake_exec(*args, **kwargs):
            invocations.append(list(args))
            return MockProc()

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await _decode_check(out, duration=3600.0)

        assert len(invocations) == 3, "long file should sample at 3 offsets"
        offsets = [float(cmd[cmd.index("-ss") + 1]) for cmd in invocations]
        # 5%, 50%, 95% of 3600 = 180, 1800, 3420
        assert offsets == pytest.approx([180.0, 1800.0, 3420.0])

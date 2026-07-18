"""Unit tests for VMAF measurement pooling + the target-VMAF quality search."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from transcode_forge.worker.vmaf import (
    VMAF_MODEL_4K,
    VMAF_MODEL_HD,
    VmafError,
    VmafUnavailableError,
    _pool,
    find_quality_for_target,
    measure_vmaf,
    select_model,
)


class TestPooling:
    def test_pool_mean_perc5_min(self):
        # 100 frames: 95 good frames at 98, 5 bad frames at 80.
        scores = [98.0] * 95 + [80.0] * 5
        pooled = _pool(scores)
        assert pooled.mean == pytest.approx(97.1)
        assert pooled.min == 80.0
        # The 5th percentile lands inside the bad tail — mean alone would
        # have hidden it (that's the whole point of worst-scenes pooling).
        assert pooled.perc5 == 80.0

    def test_pool_uniform(self):
        pooled = _pool([96.0] * 10)
        assert pooled.mean == 96.0
        assert pooled.perc5 == 96.0
        assert pooled.min == 96.0

    def test_pool_empty_raises(self):
        with pytest.raises(VmafError):
            _pool([])


class TestModelSelection:
    def test_hd_model_at_or_below_1080p(self):
        assert select_model(1080) == VMAF_MODEL_HD
        assert select_model(720) == VMAF_MODEL_HD
        assert select_model(None) == VMAF_MODEL_HD

    def test_4k_model_above_1080p(self):
        assert select_model(2160) == VMAF_MODEL_4K
        assert select_model(1440) == VMAF_MODEL_4K


class TestMeasureVmafErrors:
    async def test_missing_ffmpeg_is_unavailable(self, tmp_path):
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            with pytest.raises(VmafUnavailableError):
                await measure_vmaf(tmp_path / "a.mkv", tmp_path / "b.mkv")

    async def test_missing_filter_is_unavailable(self, tmp_path):
        class Proc:
            returncode = 1

            async def communicate(self):
                return b"", b"No such filter: 'libvmaf'"

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=Proc())):
            with pytest.raises(VmafUnavailableError):
                await measure_vmaf(tmp_path / "a.mkv", tmp_path / "b.mkv")

    async def test_gauge_uses_all_cores(self, tmp_path):
        """Regression (S4b bench, 2026-07-14): the filter graph pinned
        n_threads=0 — libvmaf's 'no threading' — so every gauge fleet-wide
        ran single-threaded. Found live: three idle cores during a 4K
        gauge. The graph must request the machine's core count."""
        import os

        captured: list = []

        class Proc:
            returncode = 0

            async def communicate(self):
                return b"", b""

        async def fake_exec(*args, **kwargs):
            captured.extend(args)
            return Proc()

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            with pytest.raises(VmafError):  # no log gets written; the cmd is the assertion
                await measure_vmaf(tmp_path / "a.mkv", tmp_path / "b.mkv")

        graph = next(str(a) for a in captured if "libvmaf" in str(a))
        assert f"n_threads={os.cpu_count() or 1}" in graph
        assert "n_threads=0" not in graph

    async def test_gauge_pairs_frames_by_index(self, tmp_path):
        """Regression (gauge desync, 2026-07-14): the graph used
        setpts=PTS-STARTPTS, leaving framesync to pair frames by timestamp.
        A source muxed on a different ms-rounding grid than the encode
        (1-2ms apart) paired frame N against ref frame N-1 for much of the
        file — a real 480p episode gauged 75.33/2.67 against its true
        97.25/95.98 and was falsely skipped. Both branches must rebase onto
        the same synthetic timeline so frames pair by index."""
        captured: list = []

        class Proc:
            returncode = 0

            async def communicate(self):
                return b"", b""

        async def fake_exec(*args, **kwargs):
            captured.extend(args)
            return Proc()

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            with pytest.raises(VmafError):  # no log gets written; the cmd is the assertion
                await measure_vmaf(tmp_path / "a.mkv", tmp_path / "b.mkv")

        graph = next(str(a) for a in captured if "libvmaf" in str(a))
        assert graph.count("settb=AVTB,setpts=N*100000") == 2  # dis AND ref
        assert "PTS-STARTPTS" not in graph

    async def test_other_failure_is_vmaf_error(self, tmp_path):
        class Proc:
            returncode = 1

            async def communicate(self):
                return b"", b"Invalid data found when processing input"

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=Proc())):
            with pytest.raises(VmafError):
                await measure_vmaf(tmp_path / "a.mkv", tmp_path / "b.mkv")


def _fake_curve(quality_to_vmaf):
    """Build an _evaluate_quality stand-in from a {quality: (mean, perc5)} map."""

    async def evaluate(samples, codec, backend, quality, *, height, target_height=None, work_dir):
        return quality_to_vmaf[quality]

    return evaluate


class TestQualitySearch:
    async def test_picks_largest_quality_meeting_target(self, tmp_path):
        # Monotonic curve: quality 16..30, VMAF falls as quality value rises.
        curve = {q: (100.0 - (q - 16) * 0.5, 98.0 - (q - 16) * 0.5) for q in range(16, 31)}
        # mean(q) = 100 - (q-16)/2 ≥ 97  →  q ≤ 22; perc5 ≥ 95 → q ≤ 22.
        with (
            patch(
                "transcode_forge.worker.vmaf._extract_samples",
                AsyncMock(return_value=[Path("s0.mkv")]),
            ),
            patch("transcode_forge.worker.vmaf._evaluate_quality", side_effect=_fake_curve(curve)),
        ):
            result = await find_quality_for_target(
                "/m/x.mkv",
                "hevc",
                "cpu",
                target_vmaf=97.0,
                perc5_floor=95.0,
                duration=5400.0,
            )
        assert result is not None
        assert result.quality == 22
        assert result.predicted_mean >= 97.0

    async def test_perc5_floor_constrains_even_when_mean_passes(self, tmp_path):
        # Mean stays high everywhere but perc5 collapses past q=19.
        curve = {q: (99.0, 96.0 if q <= 19 else 90.0) for q in range(16, 31)}
        with (
            patch(
                "transcode_forge.worker.vmaf._extract_samples",
                AsyncMock(return_value=[Path("s0.mkv")]),
            ),
            patch("transcode_forge.worker.vmaf._evaluate_quality", side_effect=_fake_curve(curve)),
        ):
            result = await find_quality_for_target(
                "/m/x.mkv",
                "hevc",
                "cpu",
                target_vmaf=97.0,
                perc5_floor=95.0,
                duration=5400.0,
            )
        assert result is not None
        assert result.quality == 19

    async def test_returns_none_when_target_unreachable(self, tmp_path):
        # Even the best-quality end of the range misses the target (grainy
        # source VMAF can't see) — caller falls back to the fixed preset.
        curve = {q: (92.0, 88.0) for q in range(16, 31)}
        with (
            patch(
                "transcode_forge.worker.vmaf._extract_samples",
                AsyncMock(return_value=[Path("s0.mkv")]),
            ),
            patch("transcode_forge.worker.vmaf._evaluate_quality", side_effect=_fake_curve(curve)),
        ):
            result = await find_quality_for_target(
                "/m/x.mkv",
                "hevc",
                "cpu",
                target_vmaf=97.0,
                perc5_floor=95.0,
                duration=5400.0,
            )
        assert result is None


def test_parse_out_time_ms_lines():
    """The -progress parser: out_time_ms is MICROseconds (ffmpeg quirk);
    N/A and unrelated lines are None; negative sentinels parse and are
    skipped by the caller's ms<0 guard."""
    from transcode_forge.worker.vmaf import _parse_out_time_ms

    assert _parse_out_time_ms(b"out_time_ms=4200000\n") == 4_200_000
    assert _parse_out_time_ms(b"out_time_ms=N/A\n") is None
    assert _parse_out_time_ms(b"frame=100\n") is None
    assert _parse_out_time_ms(b"out_time_us=1\n") is None
    assert _parse_out_time_ms(b"out_time_ms=-9223372036854775807\n") < 0


class TestMeasureVmafStreaming:
    """The gauge-% streaming path (PR #85). Review CRITICAL: progress and
    diagnostics must share ONE drained stream (-progress pipe:2 -nostats,
    the encoder.py pattern) — a second undrained pipe deadlocks ffmpeg
    once the OS buffer fills, stalling every production gauge."""

    class _FakeStderr:
        def __init__(self, lines):
            self._lines = list(lines)

        async def readline(self):
            return self._lines.pop(0) if self._lines else b""

    async def test_streaming_uses_single_stream_and_fires_callbacks(self, tmp_path):
        captured_args: list = []
        captured_kwargs: dict = {}
        fracs: list[float] = []

        fake_stderr = self._FakeStderr(
            [
                b"Some ffmpeg banner noise\n",
                b"frame=1 fps=0 q=-0.0\n",
                b"out_time_ms=N/A\n",
                b"out_time_ms=-9223372036854775807\n",
                b"out_time_ms=30000000\n",  # 30s of 60s -> 0.5
                b"out_time_ms=30060000\n",  # +0.1% -> throttled, no callback
                b"out_time_ms=60000000\n",  # 60s -> 1.0
                b"progress=end\n",
            ]
        )

        class Proc:
            returncode = 0
            stderr = fake_stderr

            async def wait(self):
                return 0

        async def fake_exec(*args, **kwargs):
            captured_args.extend(args)
            captured_kwargs.update(kwargs)
            return Proc()

        async def on_progress(frac: float) -> None:
            fracs.append(frac)

        import asyncio as _asyncio

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            with pytest.raises(VmafError):  # no log written; cmd + callbacks are the assertions
                await measure_vmaf(
                    tmp_path / "a.mkv",
                    tmp_path / "b.mkv",
                    duration=60.0,
                    on_progress=on_progress,
                )

        cmd = [str(a) for a in captured_args]
        assert "-progress" in cmd
        assert cmd[cmd.index("-progress") + 1] == "pipe:2"
        assert "-nostats" in cmd
        # ONE live pipe: stdout devnull, stderr piped.
        assert captured_kwargs.get("stdout") == _asyncio.subprocess.DEVNULL
        assert captured_kwargs.get("stderr") == _asyncio.subprocess.PIPE
        # N/A + negative sentinels skipped; 0.1% step throttled.
        assert fracs == [0.5, 1.0]

    async def test_non_streaming_command_is_unchanged(self, tmp_path):
        captured: list = []

        class Proc:
            returncode = 0

            async def communicate(self):
                return b"", b""

        async def fake_exec(*args, **kwargs):
            captured.extend(args)
            return Proc()

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            with pytest.raises(VmafError):
                await measure_vmaf(tmp_path / "a.mkv", tmp_path / "b.mkv")

        cmd = [str(a) for a in captured]
        assert "-progress" not in cmd
        assert "-nostats" not in cmd

    async def test_streaming_failure_reports_real_diagnostics(self, tmp_path):
        """Verify-round follow-up: a nonzero exit through the STREAMING
        branch must still surface real ffmpeg error text from the drained
        tail — including the libvmaf-unavailable sniff."""
        fake_stderr = self._FakeStderr(
            [
                b"out_time_ms=1000000\n",
                b"[AVFilterGraph] No such filter: 'libvmaf'\n",
            ]
        )

        class Proc:
            returncode = 1
            stderr = fake_stderr

            async def wait(self):
                return 1

        async def fake_exec(*args, **kwargs):
            return Proc()

        async def on_progress(frac: float) -> None:
            pass

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            with pytest.raises(VmafUnavailableError):
                await measure_vmaf(
                    tmp_path / "a.mkv",
                    tmp_path / "b.mkv",
                    duration=60.0,
                    on_progress=on_progress,
                )

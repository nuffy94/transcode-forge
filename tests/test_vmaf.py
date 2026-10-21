"""Unit tests for VMAF measurement pooling + the target-VMAF quality search."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from transcode_forge.worker.encoder import EncodeResult
from transcode_forge.worker.vmaf import (
    SAMPLE_SECONDS,
    VMAF_FFMPEG,
    VMAF_MODEL_4K,
    VMAF_MODEL_HD,
    QualitySearchResult,
    VmafError,
    VmafScore,
    VmafUnavailableError,
    _evaluate_quality,
    _extract_samples,
    _pool,
    build_gauge_graph,
    find_quality_for_target,
    has_libvmaf,
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

    def test_perc5_is_the_nearest_rank_of_the_whole_list(self):
        """41 distinct scores put the 5th percentile at index
        int(0.05 * 40) = 2. Mutation sweep 2026-09-20: the shorter lists
        above all land on index 0 or inside a flat tail, so `len - 1`
        could become `len - 2` and nothing moved."""
        pooled = _pool([float(n) for n in range(41)])
        assert pooled.perc5 == 2.0
        assert pooled.min == 0.0
        assert pooled.mean == 20.0


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


class TestMeasureVmafCommand:
    """What measure_vmaf hands to ffmpeg. Mutation sweep 2026-09-20: the
    two inputs could each become the string "None", the model choice could
    ignore both heights, and nothing here looked."""

    @staticmethod
    async def _cmd(tmp_path, **kwargs) -> list[str]:
        captured: list = []

        class Proc:
            returncode = 0

            async def communicate(self):
                return b"", b""

        async def fake_exec(*args, **kw):
            captured.extend(args)
            return Proc()

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            with pytest.raises(VmafError):  # no log gets written; the cmd is the assertion
                await measure_vmaf(tmp_path / "source.mkv", tmp_path / "encoded.mkv", **kwargs)
        return [str(a) for a in captured]

    async def test_the_encode_is_the_first_input_and_the_source_the_second(self, tmp_path):
        """The graph labels input 0 [dis] and input 1 [ref]. Swapped, libvmaf
        scores the source against the encode: a different number, and a
        downscale job would scale the wrong side."""
        cmd = await self._cmd(tmp_path)
        assert cmd[:6] == [
            VMAF_FFMPEG,
            "-hide_banner",
            "-i",
            str(tmp_path / "encoded.mkv"),
            "-i",
            str(tmp_path / "source.mkv"),
        ]
        assert cmd[-3:] == ["-f", "null", "-"]

    @pytest.mark.parametrize(
        ("height", "target_height", "model"),
        [
            (2160, None, VMAF_MODEL_4K),  # a 4K source scored at 4K
            (1080, None, VMAF_MODEL_HD),
            (2160, 1080, VMAF_MODEL_HD),  # a downscale is scored at the delivered height
            (None, None, VMAF_MODEL_HD),
        ],
    )
    async def test_the_model_follows_the_delivered_height(
        self, tmp_path, height, target_height, model
    ):
        cmd = await self._cmd(tmp_path, height=height, target_height=target_height)
        graph = cmd[cmd.index("-lavfi") + 1]
        assert f"model=version={model}" in graph

    async def test_an_explicit_thread_count_reaches_the_graph(self, tmp_path):
        cmd = await self._cmd(tmp_path, n_threads=3)
        graph = cmd[cmd.index("-lavfi") + 1]
        assert "n_threads=3" in graph

    async def test_the_graph_is_built_from_the_callers_settings(self, tmp_path):
        """The graph builder has its own golden tests; this pins what
        measure_vmaf asks it for, reference downscale included."""
        with patch(
            "transcode_forge.worker.vmaf.build_gauge_graph", wraps=build_gauge_graph
        ) as build:
            cmd = await self._cmd(
                tmp_path, height=2160, target_height=1080, n_subsample=1, n_threads=3
            )

        kwargs = build.call_args.kwargs
        assert kwargs["model"] == VMAF_MODEL_HD
        assert kwargs["n_subsample"] == 1
        assert kwargs["n_threads"] == 3
        assert kwargs["reference_scale_height"] == 1080
        assert kwargs["log_path"].endswith("/vmaf.json")
        assert cmd[cmd.index("-lavfi") + 1] == build_gauge_graph(**kwargs)

    async def test_the_measurement_runs_under_a_deadline(self, tmp_path):
        """A full-file gauge runs for minutes to hours; without a positive
        timeout a wedged ffmpeg holds the job forever."""
        seen: dict = {}

        @asynccontextmanager
        async def fake_managed(*cmd, **kwargs):
            seen.update(kwargs)
            proc = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"", b"")))
            yield SimpleNamespace(proc=proc)

        with patch("transcode_forge.worker.vmaf.managed_subprocess", fake_managed):
            with pytest.raises(VmafError):  # no log gets written; the kwargs are the assertion
                await measure_vmaf(tmp_path / "source.mkv", tmp_path / "encoded.mkv")

        assert seen["timeout"] > 0


class TestHasLibvmaf:
    """The capability check that decides whether the gate runs at all.
    Mutation sweep 2026-09-20: no test reached this function."""

    @staticmethod
    def _ffmpeg_printing(stdout: bytes, seen: dict | None = None):
        @asynccontextmanager
        async def fake_managed(*cmd, **kwargs):
            if seen is not None:
                seen.update(cmd=[str(c) for c in cmd], **kwargs)
            proc = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(stdout, None)))
            yield SimpleNamespace(proc=proc)

        return fake_managed

    async def test_a_build_that_lists_the_filter_has_it(self):
        seen: dict = {}
        listing = b" ... lut3d  V->V  Adjust colors\n ... libvmaf  VV->V  Calculate the VMAF\n"

        with patch(
            "transcode_forge.worker.vmaf.managed_subprocess", self._ffmpeg_printing(listing, seen)
        ):
            assert await has_libvmaf() is True

        assert seen["cmd"] == [VMAF_FFMPEG, "-hide_banner", "-filters"]
        assert seen["timeout"] > 0
        assert seen["stdout"] == asyncio.subprocess.PIPE

    async def test_a_build_that_does_not_list_the_filter_lacks_it(self):
        listing = b" ... lut3d  V->V  Adjust colors\n ... psnr  VV->V  Calculate the PSNR\n"

        with patch(
            "transcode_forge.worker.vmaf.managed_subprocess", self._ffmpeg_printing(listing)
        ):
            assert await has_libvmaf() is False

    @pytest.mark.parametrize("error", [FileNotFoundError, TimeoutError, OSError])
    async def test_a_binary_that_cannot_be_asked_counts_as_lacking_it(self, error):
        def refuses(*cmd, **kwargs):
            raise error()

        with patch("transcode_forge.worker.vmaf.managed_subprocess", refuses):
            assert await has_libvmaf() is False


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

    async def test_only_the_low_end_meeting_the_target_is_still_an_answer(self, tmp_path):
        """The first probe is its own result: when nothing past q=16 clears
        the bars, the search returns q=16 with that probe's predictions."""
        curve = {q: (97.5, 96.0) if q == 16 else (90.0, 80.0) for q in range(16, 31)}
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
        assert result == QualitySearchResult(quality=16, predicted_mean=97.5, predicted_perc5=96.0)

    async def test_probe_progress_counts_up_to_the_expected_total(self, tmp_path):
        """The station bar's "q3/5": one tick per probe, against the worst
        case for the 16..30 range (the low-end check plus four halvings)."""
        curve = {q: (100.0 - (q - 16) * 0.5, 98.0 - (q - 16) * 0.5) for q in range(16, 31)}
        ticks: list[tuple[int, int]] = []

        async def on_probe(done: int, total: int) -> None:
            ticks.append((done, total))

        with (
            patch(
                "transcode_forge.worker.vmaf._extract_samples",
                AsyncMock(return_value=[Path("s0.mkv")]),
            ) as extract,
            patch("transcode_forge.worker.vmaf._evaluate_quality", side_effect=_fake_curve(curve)),
        ):
            await find_quality_for_target(
                "/m/x.mkv",
                "hevc",
                "cpu",
                target_vmaf=97.0,
                perc5_floor=95.0,
                duration=5400.0,
                primary_index=1,
                on_probe=on_probe,
            )

        assert ticks == [(1, 5), (2, 5), (3, 5), (4, 5), (5, 5)]
        source, duration, work_dir, primary_index = extract.await_args.args
        assert (source, duration, primary_index) == (Path("/m/x.mkv"), 5400.0, 1)
        assert isinstance(work_dir, Path)


class TestEvaluateQuality:
    """One CRF candidate, scored across every sample. Mutation sweep
    2026-09-20: every search test used a single sample, so the mean of
    means could become a product and nothing moved."""

    async def test_every_sample_is_encoded_scored_and_pooled_pessimistically(self, tmp_path):
        samples = [tmp_path / "sample0.mkv", tmp_path / "sample1.mkv"]
        scores = iter(
            [
                VmafScore(mean=96.0, perc5=94.0, min=90.0),
                VmafScore(mean=98.0, perc5=91.0, min=85.0),
            ]
        )
        encodes: list[tuple[list[str], float]] = []
        gauges: list[tuple[Path, Path, dict]] = []

        async def fake_encode(cmd, total_duration, progress_callback=None):
            encodes.append((list(cmd), total_duration))
            return EncodeResult(success=True, output_path=cmd[-1], output_size=1, returncode=0)

        async def fake_measure(source, encoded, **kwargs):
            gauges.append((Path(source), Path(encoded), kwargs))
            return next(scores)

        with (
            patch("transcode_forge.worker.vmaf.run_encode", side_effect=fake_encode),
            patch("transcode_forge.worker.vmaf.measure_vmaf", side_effect=fake_measure),
        ):
            mean, perc5 = await _evaluate_quality(
                samples, "hevc", "cpu", 22, height=2160, target_height=1080, work_dir=tmp_path
            )

        assert mean == 97.0  # mean of the sample means
        assert perc5 == 91.0  # the worst sample's worst scenes
        outs = [tmp_path / "sample0_q22.mkv", tmp_path / "sample1_q22.mkv"]
        # Each sample is the reference for its own encode, every frame scored.
        assert [(ref, dis) for ref, dis, _ in gauges] == list(zip(samples, outs, strict=True))
        for _, _, kwargs in gauges:
            assert kwargs == {"height": 2160, "target_height": 1080, "n_subsample": 1}
        for (cmd, total_duration), sample, out in zip(encodes, samples, outs, strict=True):
            assert cmd[cmd.index("-i") + 1] == str(sample)
            assert cmd[-1] == str(out)
            assert any("scale=-2:1080" in arg for arg in cmd)
            assert total_duration == SAMPLE_SECONDS

    async def test_a_failed_sample_encode_stops_the_candidate(self, tmp_path):
        async def fake_encode(cmd, total_duration, progress_callback=None):
            return EncodeResult(
                success=False,
                output_path=cmd[-1],
                output_size=0,
                returncode=1,
                error_message="boom",
            )

        with patch("transcode_forge.worker.vmaf.run_encode", side_effect=fake_encode):
            with pytest.raises(VmafError, match="q=22: boom"):
                await _evaluate_quality(
                    [tmp_path / "sample0.mkv"], "hevc", "cpu", 22, height=1080, work_dir=tmp_path
                )


class TestExtractSamples:
    """Where the CRF search cuts its samples. Mutation sweep 2026-09-20:
    the offsets could be divided instead of multiplied, and the short-file
    rule could move, with no test looking at the command."""

    @staticmethod
    def _fake_ffmpeg(cmds: list[list[str]], *, returncode: int = 0, clip: bytes = b"clip"):
        @asynccontextmanager
        async def fake_managed(*cmd, **kwargs):
            assert kwargs["timeout"] > 0  # a wedged ffmpeg must not hold the search forever
            cmds.append([str(c) for c in cmd])
            Path(cmd[-1]).write_bytes(clip)
            proc = SimpleNamespace(
                returncode=returncode, communicate=AsyncMock(return_value=(b"", b"bad input"))
            )
            yield SimpleNamespace(proc=proc)

        return fake_managed

    async def test_a_long_file_is_sampled_at_three_fractions_of_its_length(self, tmp_path):
        cmds: list[list[str]] = []
        source = tmp_path / "film.mkv"

        with patch("transcode_forge.worker.vmaf.managed_subprocess", self._fake_ffmpeg(cmds)):
            samples = await _extract_samples(source, 1000.0, tmp_path, 1)

        assert samples == [tmp_path / f"sample{i}.mkv" for i in range(3)]
        assert [cmd[cmd.index("-ss") + 1] for cmd in cmds] == ["150.00", "500.00", "850.00"]
        for cmd in cmds:
            assert cmd[cmd.index("-i") + 1] == str(source)
            assert cmd[cmd.index("-t") + 1] == f"{SAMPLE_SECONDS:.2f}"
            assert cmd[cmd.index("-map") + 1] == "0:1"
            assert cmd[cmd.index("-c") + 1] == "copy"

    @pytest.mark.parametrize(
        ("duration", "expected_offsets"),
        [
            (SAMPLE_SECONDS * 2, ["0.00"]),  # exactly two samples long: still "short"
            (SAMPLE_SECONDS * 2 + 1, ["6.15", "20.50", "34.85"]),
        ],
    )
    async def test_a_short_file_is_one_sample_from_the_start(
        self, tmp_path, duration, expected_offsets
    ):
        cmds: list[list[str]] = []

        with patch("transcode_forge.worker.vmaf.managed_subprocess", self._fake_ffmpeg(cmds)):
            await _extract_samples(tmp_path / "clip.mkv", duration, tmp_path)

        assert [cmd[cmd.index("-ss") + 1] for cmd in cmds] == expected_offsets
        assert all("-map" not in cmd for cmd in cmds)

    @pytest.mark.parametrize(
        ("returncode", "clip"),
        [
            (1, b"clip"),  # ffmpeg failed but left a file behind
            (0, b""),  # ffmpeg exited 0 and wrote nothing
        ],
    )
    async def test_a_failed_or_empty_sample_is_a_search_failure(self, tmp_path, returncode, clip):
        fake = self._fake_ffmpeg([], returncode=returncode, clip=clip)

        with patch("transcode_forge.worker.vmaf.managed_subprocess", fake):
            with pytest.raises(VmafError, match="Sample extraction failed at 0s"):
                await _extract_samples(tmp_path / "clip.mkv", 30.0, tmp_path)


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
        # The progress flags are added to the command, they do not replace it.
        assert cmd[:6] == [
            VMAF_FFMPEG,
            "-hide_banner",
            "-i",
            str(tmp_path / "b.mkv"),
            "-i",
            str(tmp_path / "a.mkv"),
        ]
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


class TestMissingModelIsUnavailable:
    async def test_missing_v1_model_skips_gate_not_fails(self, tmp_path):
        """A binary with libvmaf but no v1 models (pre-3.2 static build on
        an un-updated worker) must raise VmafUnavailableError — gate
        skipped loudly — never a hard VmafError that fails every gated
        job on that worker (Gate 2 mixed-fleet contract)."""

        class Proc:
            returncode = 1

            async def communicate(self):
                return (
                    b"",
                    b"could not load libvmaf model with version: vmaf_v1.0.16_3d0h",
                )

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=Proc())):
            with pytest.raises(VmafUnavailableError, match="update the worker image"):
                await measure_vmaf(tmp_path / "a.mkv", tmp_path / "b.mkv")


def _exec_writing_log(payload: str):
    """A stand-in ffmpeg that exits 0 and drops `payload` at the log_path
    the filter graph asked for, so the real parser reads it."""

    async def fake_exec(*args, **kwargs):
        graph = next(str(a) for a in args if "libvmaf" in str(a))
        # Split on the NEXT option, not on ':', because a Windows log_path
        # starts with a drive letter and a colon.
        log_path = graph.split("log_path=", 1)[1].split(":n_subsample=", 1)[0]
        Path(log_path).write_text(payload, encoding="utf-8")

        class Proc:
            returncode = 0

            async def communicate(self):
                return b"", b""

        return Proc()

    return fake_exec


class TestNonfiniteScores:
    """Codex full review 2026-09-12, finding F2.

    libvmaf can write NaN or Infinity into its JSON log and Python's json
    parser accepts both literals. Pooled into a score they reached the
    gate, where every check is a "below the floor" comparison, and those
    are False for NaN and for +inf. So a gauge that produced no usable
    number passed the gate and the encode replaced the original. A score
    that is not a number at all is a measurement failure too.
    """

    @pytest.mark.parametrize("field", ["mean", "perc5", "min"])
    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_score_cannot_hold_a_nonfinite_value(self, field, bad):
        fields = {"mean": 98.0, "perc5": 96.0, "min": 95.0}
        fields[field] = bad
        with pytest.raises(VmafError, match="finite"):
            VmafScore(**fields)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_pool_rejects_a_nonfinite_frame(self, bad):
        with pytest.raises(VmafError):
            _pool([98.0] * 20 + [bad])

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param('{"frames": [{"metrics": {"vmaf": NaN}}, {"metrics": {"vmaf": 98.0}}]}'),
            pytest.param('{"frames": [{"metrics": {"vmaf": Infinity}}]}'),
            pytest.param('{"frames": [{"metrics": {"vmaf": -Infinity}}]}'),
            pytest.param('{"frames": [{"metrics": {"vmaf": "N/A"}}]}'),
            pytest.param('{"frames": [{"metrics": {"vmaf": null}}]}'),
            pytest.param('{"frames": []}'),
        ],
    )
    async def test_a_garbage_log_is_a_measurement_failure(self, tmp_path, payload):
        with patch("asyncio.create_subprocess_exec", side_effect=_exec_writing_log(payload)):
            with pytest.raises(VmafError) as exc_info:
                await measure_vmaf(tmp_path / "a.mkv", tmp_path / "b.mkv")
        # Plain VmafError, never VmafUnavailableError: that one means "this
        # worker has no libvmaf" and skips the gate loudly, which is exactly
        # the wrong answer for a gauge that ran and returned garbage.
        assert not isinstance(exc_info.value, VmafUnavailableError)

    async def test_a_real_log_still_measures(self, tmp_path):
        """Control: the same path with a valid log pools as it always did."""
        payload = json.dumps({"frames": [{"metrics": {"vmaf": v}} for v in [98.0] * 19 + [90.0]]})
        with patch("asyncio.create_subprocess_exec", side_effect=_exec_writing_log(payload)):
            score = await measure_vmaf(tmp_path / "a.mkv", tmp_path / "b.mkv")
        assert score.mean == pytest.approx(97.6)
        assert score.perc5 == 90.0
        assert score.min == 90.0

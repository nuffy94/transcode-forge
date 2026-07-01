"""Unit tests for VMAF measurement pooling + the target-VMAF quality search."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from transcode_forge.worker.vmaf import (
    VMAF_MODEL_4K,
    VMAF_MODEL_HD,
    VmafError,
    VmafScore,
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

    async def evaluate(samples, codec, backend, quality, *, height, work_dir):
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

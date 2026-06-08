"""Tests for hardware acceleration detection."""

from unittest.mock import patch

import pytest

from transcode_forge.worker.hardware import (
    HardwareCapabilities,
    detect_capabilities,
    detect_ffmpeg_version,
    detect_nvenc,
    detect_qsv,
)


class TestHardwareCapabilities:
    def test_best_encoder_prefers_qsv(self):
        caps = HardwareCapabilities(
            encoders=["qsv", "nvenc", "cpu"],
            ffmpeg_version="ffmpeg 6.0",
            os_platform="Linux",
        )
        assert caps.best_encoder() == "qsv"

    def test_best_encoder_nvenc_without_qsv(self):
        caps = HardwareCapabilities(
            encoders=["nvenc", "cpu"],
            ffmpeg_version="ffmpeg 6.0",
            os_platform="Windows",
        )
        assert caps.best_encoder() == "nvenc"

    def test_best_encoder_cpu_fallback(self):
        caps = HardwareCapabilities(
            encoders=["cpu"],
            ffmpeg_version="ffmpeg 6.0",
            os_platform="Linux",
        )
        assert caps.best_encoder() == "cpu"

    def test_best_encoder_respects_preferred(self):
        caps = HardwareCapabilities(
            encoders=["qsv", "nvenc", "cpu"],
            ffmpeg_version="ffmpeg 6.0",
            os_platform="Linux",
        )
        assert caps.best_encoder("nvenc") == "nvenc"
        assert caps.best_encoder("cpu") == "cpu"

    def test_best_encoder_ignores_unavailable_preferred(self):
        caps = HardwareCapabilities(
            encoders=["cpu"],
            ffmpeg_version="ffmpeg 6.0",
            os_platform="Linux",
        )
        assert caps.best_encoder("qsv") == "cpu"

    def test_has_qsv(self):
        caps = HardwareCapabilities(encoders=["qsv", "cpu"], ffmpeg_version="", os_platform="")
        assert caps.has_qsv is True
        assert caps.has_nvenc is False

    def test_has_nvenc(self):
        caps = HardwareCapabilities(encoders=["nvenc", "cpu"], ffmpeg_version="", os_platform="")
        assert caps.has_qsv is False
        assert caps.has_nvenc is True

    def test_frozen(self):
        caps = HardwareCapabilities(encoders=["cpu"], ffmpeg_version="", os_platform="")
        with pytest.raises(AttributeError):
            caps.encoders = ["qsv"]  # type: ignore[misc]


class TestDetectFfmpegVersion:
    async def test_detect_version(self):
        with patch(
            "transcode_forge.worker.hardware._run_probe",
            return_value=(0, "ffmpeg version 6.1.1 Copyright (c) 2000-2024\nbuilt with gcc"),
        ):
            version = await detect_ffmpeg_version()
            assert "ffmpeg version 6.1.1" in version

    async def test_detect_version_failure(self):
        with patch("transcode_forge.worker.hardware._run_probe", return_value=(1, "not found")):
            version = await detect_ffmpeg_version()
            assert version == "unknown"


class TestDetectQsv:
    async def test_qsv_available(self):
        """QSV detected: encoder listed + test encode succeeds on first method."""
        call_count = 0

        async def mock_probe(cmd, timeout=10.0):
            nonlocal call_count
            call_count += 1
            if "-encoders" in cmd:
                return (0, "hevc_qsv  HEVC (Intel Quick Sync)")
            return (0, "frame=1")  # test encode succeeds

        with patch("transcode_forge.worker.hardware._run_probe", side_effect=mock_probe):
            assert await detect_qsv() is True

    async def test_qsv_encoder_not_in_build(self):
        """QSV not available: encoder not listed in ffmpeg build."""
        with patch(
            "transcode_forge.worker.hardware._run_probe",
            return_value=(0, "libx265  libx265 encoder"),
        ):
            assert await detect_qsv() is False

    async def test_qsv_encoder_listed_but_encode_fails(self):
        """QSV listed but all test encode methods fail."""

        async def mock_probe(cmd, timeout=10.0):
            if "-encoders" in cmd:
                return (0, "hevc_qsv  HEVC (Intel Quick Sync)")
            return (1, "no qsv device")

        with patch("transcode_forge.worker.hardware._run_probe", side_effect=mock_probe):
            assert await detect_qsv() is False


class TestDetectNvenc:
    async def test_nvenc_available(self):
        with patch("transcode_forge.worker.hardware._run_probe", return_value=(0, "frame=1")):
            assert await detect_nvenc() is True

    async def test_nvenc_not_available(self):
        with patch(
            "transcode_forge.worker.hardware._run_probe", return_value=(1, "no nvidia device")
        ):
            assert await detect_nvenc() is False


class TestDetectCapabilities:
    async def test_full_detection(self):
        async def mock_probe(cmd, timeout=10.0):
            cmd_str = " ".join(cmd)
            if "-version" in cmd_str:
                return (0, "ffmpeg version 6.1.1\n")
            if "-encoders" in cmd_str:
                return (0, "hevc_qsv  HEVC QSV\nhevc_nvenc  HEVC NVENC")
            if "hevc_qsv" in cmd_str:
                return (0, "frame=1")
            if "hevc_nvenc" in cmd_str:
                return (1, "no device")
            return (1, "unknown")

        with patch("transcode_forge.worker.hardware._run_probe", side_effect=mock_probe):
            caps = await detect_capabilities()
            assert "qsv" in caps.encoders
            assert "nvenc" not in caps.encoders
            assert "cpu" in caps.encoders
            assert "6.1.1" in caps.ffmpeg_version

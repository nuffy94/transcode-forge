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


def _caps(pairs, encoders=None):
    return HardwareCapabilities(
        encoders=encoders or ["cpu"],
        pairs=pairs,
        ffmpeg_version="ffmpeg 7.0",
        os_platform="Linux",
    )


class TestHardwareCapabilities:
    def test_best_backend_prefers_qsv(self):
        caps = _caps([("hevc", "cpu"), ("hevc", "qsv"), ("hevc", "nvenc")])
        assert caps.best_backend_for("hevc") == "qsv"

    def test_best_backend_nvenc_without_qsv(self):
        caps = _caps([("hevc", "cpu"), ("hevc", "nvenc")])
        assert caps.best_backend_for("hevc") == "nvenc"

    def test_best_backend_cpu_fallback(self):
        caps = _caps([("hevc", "cpu")])
        assert caps.best_backend_for("hevc") == "cpu"

    def test_best_backend_respects_preferred(self):
        caps = _caps([("hevc", "cpu"), ("hevc", "qsv"), ("hevc", "nvenc")])
        assert caps.best_backend_for("hevc", "nvenc") == "nvenc"
        assert caps.best_backend_for("hevc", "cpu") == "cpu"

    def test_best_backend_ignores_unavailable_preferred(self):
        caps = _caps([("hevc", "cpu")])
        assert caps.best_backend_for("hevc", "qsv") == "cpu"

    def test_best_backend_is_per_codec(self):
        """A node whose QSV does HEVC but not AV1 must route AV1 to cpu —
        cpu/software is the universal per-codec fallback."""
        caps = _caps([("hevc", "cpu"), ("hevc", "qsv"), ("av1", "cpu")])
        assert caps.best_backend_for("hevc") == "qsv"
        assert caps.best_backend_for("av1") == "cpu"

    def test_best_backend_none_for_unsupported_codec(self):
        caps = _caps([("hevc", "cpu")])
        assert caps.best_backend_for("av1") is None

    def test_supported_codecs(self):
        caps = _caps([("hevc", "cpu"), ("av1", "cpu"), ("hevc", "qsv")])
        assert caps.supported_codecs == ["hevc", "av1"]
        assert _caps([("hevc", "cpu")]).supported_codecs == ["hevc"]

    def test_has_qsv_nvenc(self):
        caps = _caps([("hevc", "qsv")], encoders=["qsv", "cpu"])
        assert caps.has_qsv is True
        assert caps.has_nvenc is False

    def test_frozen(self):
        caps = _caps([("hevc", "cpu")])
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
        """QSV detected: encoder listed + 10-bit test encode succeeds."""

        async def mock_probe(cmd, timeout=10.0):
            if "-encoders" in cmd:
                return (0, "hevc_qsv  HEVC (Intel Quick Sync)")
            return (0, "frame=1")

        with patch("transcode_forge.worker.hardware._run_probe", side_effect=mock_probe):
            assert await detect_qsv() is True

    async def test_qsv_encoder_not_in_build(self):
        with patch(
            "transcode_forge.worker.hardware._run_probe",
            return_value=(0, "libx265  libx265 encoder"),
        ):
            assert await detect_qsv() is False

    async def test_qsv_encoder_listed_but_encode_fails(self):
        """QSV listed but all test encode methods fail (e.g. Skylake with a
        10-bit probe — pipeline output is always 10-bit now)."""

        async def mock_probe(cmd, timeout=10.0):
            if "-encoders" in cmd:
                return (0, "hevc_qsv  HEVC (Intel Quick Sync)")
            return (1, "Current pixel format is unsupported")

        with patch("transcode_forge.worker.hardware._run_probe", side_effect=mock_probe):
            assert await detect_qsv() is False

    async def test_qsv_probe_requests_10bit(self):
        """The test encode must feed p010le — 8-bit-only QSV is not usable."""
        commands = []

        async def mock_probe(cmd, timeout=10.0):
            commands.append(cmd)
            if "-encoders" in cmd:
                return (0, "hevc_qsv")
            return (1, "fail")

        with patch("transcode_forge.worker.hardware._run_probe", side_effect=mock_probe):
            await detect_qsv()
        encode_cmds = [c for c in commands if "-encoders" not in c]
        assert encode_cmds
        for cmd in encode_cmds:
            assert cmd[cmd.index("-vf") + 1] == "format=p010le"


class TestDetectNvenc:
    async def test_nvenc_available(self):
        with patch("transcode_forge.worker.hardware._run_probe", return_value=(0, "frame=1")):
            assert await detect_nvenc() is True

    async def test_nvenc_not_available(self):
        with patch(
            "transcode_forge.worker.hardware._run_probe", return_value=(1, "no nvidia device")
        ):
            assert await detect_nvenc() is False

    async def test_av1_nvenc_skipped_when_not_listed(self):
        """av1_nvenc absent from the build (pre-Ada driver) → no probe attempt."""
        with patch(
            "transcode_forge.worker.hardware._run_probe", return_value=(1, "should not run")
        ) as probe:
            assert await detect_nvenc("av1_nvenc", encoder_list="hevc_nvenc only") is False
            probe.assert_not_called()


class TestDetectCapabilities:
    async def test_full_detection(self):
        """i3-10100-style node: 10-bit-capable QSV for HEVC, SVT-AV1 in the
        build, no NVENC, no av1_qsv."""

        async def mock_probe(cmd, timeout=10.0):
            cmd_str = " ".join(cmd)
            if "-version" in cmd_str:
                return (0, "ffmpeg version 7.1\n")
            if "-encoders" in cmd_str:
                return (0, "libx265\nlibsvtav1\nhevc_qsv\nhevc_nvenc\nav1_qsv")
            if "hevc_qsv" in cmd_str:
                return (0, "frame=1")
            return (1, "no device")

        with patch("transcode_forge.worker.hardware._run_probe", side_effect=mock_probe):
            caps = await detect_capabilities()
        assert ("hevc", "cpu") in caps.pairs
        assert ("av1", "cpu") in caps.pairs
        assert ("hevc", "qsv") in caps.pairs
        assert ("av1", "qsv") not in caps.pairs
        assert ("hevc", "nvenc") not in caps.pairs
        assert caps.supported_codecs == ["hevc", "av1"]
        assert "qsv" in caps.encoders
        assert "nvenc" not in caps.encoders
        assert "7.1" in caps.ffmpeg_version

    async def test_skylake_loses_qsv_keeps_cpu(self):
        """Skylake: hevc_qsv listed but the 10-bit probe fails → cpu only."""

        async def mock_probe(cmd, timeout=10.0):
            cmd_str = " ".join(cmd)
            if "-version" in cmd_str:
                return (0, "ffmpeg version 7.1\n")
            if "-encoders" in cmd_str:
                return (0, "libx265\nlibsvtav1\nhevc_qsv")
            return (1, "Current pixel format is unsupported")

        with patch("transcode_forge.worker.hardware._run_probe", side_effect=mock_probe):
            caps = await detect_capabilities()
        assert caps.pairs == sorted([("hevc", "cpu"), ("av1", "cpu")])
        assert caps.encoders == ["cpu"]

    async def test_hevc_cpu_always_present(self):
        """Even a broken encoder-list probe never bricks the worker."""
        with patch("transcode_forge.worker.hardware._run_probe", return_value=(1, "boom")):
            caps = await detect_capabilities()
        assert ("hevc", "cpu") in caps.pairs


class TestRunProbeDeadline:
    async def test_hung_probe_is_killed(self):
        """R-025: a hardware probe that hangs (device init wedged) used to
        time out and leave the child running. It now goes through the one
        door, which kills it on the way out."""
        import contextlib
        import sys

        from transcode_forge.worker import hardware
        from transcode_forge.worker import proc as proc_mod

        holder: dict = {}

        @contextlib.asynccontextmanager
        async def recording(*cmd, **kwargs):
            async with proc_mod.managed_subprocess(*cmd, grace=2.0, **kwargs) as child:
                holder["proc"] = child.proc
                yield child

        with patch("transcode_forge.worker.hardware.managed_subprocess", recording):
            code, output = await hardware._run_probe(
                [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5
            )
        assert (code, output) == (1, "timeout")
        assert holder["proc"].returncode is not None

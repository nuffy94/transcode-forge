"""Tests for the media scanner and ffprobe wrapper."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from transcode_forge.scanner.probe import ProbeError, ProbeResult, ffprobe, is_video_file


class TestIsVideoFile:
    def test_mkv_is_video(self):
        assert is_video_file(Path("test.mkv")) is True

    def test_mp4_is_video(self):
        assert is_video_file(Path("test.mp4")) is True

    def test_avi_is_video(self):
        assert is_video_file(Path("test.avi")) is True

    def test_txt_is_not_video(self):
        assert is_video_file(Path("test.txt")) is False

    def test_srt_is_not_video(self):
        assert is_video_file(Path("test.srt")) is False

    def test_case_insensitive(self):
        assert is_video_file(Path("test.MKV")) is True
        assert is_video_file(Path("test.Mp4")) is True


SAMPLE_FFPROBE_OUTPUT = json.dumps(
    {
        "streams": [
            {
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "bit_rate": "5000000",
            }
        ],
        "format": {
            "duration": "3600.5",
            "size": "2250000000",
            "bit_rate": "5000000",
        },
    }
)

HEVC_FFPROBE_OUTPUT = json.dumps(
    {
        "streams": [
            {
                "codec_name": "hevc",
                "width": 3840,
                "height": 2160,
                "bit_rate": "8000000",
            }
        ],
        "format": {
            "duration": "7200.0",
            "size": "7200000000",
        },
    }
)


class TestFfprobe:
    async def test_probe_h264_file(self, tmp_path):
        # Create a dummy file
        test_file = tmp_path / "test.mkv"
        test_file.write_bytes(b"fake video data")

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(SAMPLE_FFPROBE_OUTPUT.encode(), b""))
        mock_proc.returncode = 0

        with patch(
            "transcode_forge.scanner.probe.asyncio.create_subprocess_exec", return_value=mock_proc
        ):
            result = await ffprobe(test_file)

        assert result.video_codec == "h264"
        assert result.width == 1920
        assert result.height == 1080
        assert result.resolution == "1920x1080"
        assert result.duration == pytest.approx(3600.5)
        assert result.file_size == 2250000000

    async def test_probe_hevc_file(self, tmp_path):
        test_file = tmp_path / "test.mkv"
        test_file.write_bytes(b"fake video data")

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(HEVC_FFPROBE_OUTPUT.encode(), b""))
        mock_proc.returncode = 0

        with patch(
            "transcode_forge.scanner.probe.asyncio.create_subprocess_exec", return_value=mock_proc
        ):
            result = await ffprobe(test_file)

        assert result.video_codec == "hevc"
        assert result.width == 3840
        assert result.height == 2160

    async def test_probe_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            await ffprobe("/nonexistent/path.mkv")

    async def test_probe_ffprobe_fails(self, tmp_path):
        test_file = tmp_path / "test.mkv"
        test_file.write_bytes(b"fake")

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_proc.returncode = 1

        with (
            patch(
                "transcode_forge.scanner.probe.asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ),
            pytest.raises(ProbeError, match="ffprobe failed"),
        ):
            await ffprobe(test_file)

    async def test_probe_no_video_streams(self, tmp_path):
        test_file = tmp_path / "test.mkv"
        test_file.write_bytes(b"fake")

        no_streams = json.dumps({"streams": [], "format": {}}).encode()
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(no_streams, b""))
        mock_proc.returncode = 0

        with (
            patch(
                "transcode_forge.scanner.probe.asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ),
            pytest.raises(ProbeError, match="No video streams"),
        ):
            await ffprobe(test_file)

    async def test_probe_invalid_json(self, tmp_path):
        test_file = tmp_path / "test.mkv"
        test_file.write_bytes(b"fake")

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"not json", b""))
        mock_proc.returncode = 0

        with (
            patch(
                "transcode_forge.scanner.probe.asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ),
            pytest.raises(ProbeError, match="invalid JSON"),
        ):
            await ffprobe(test_file)


class TestProbeResult:
    def test_resolution_property(self):
        result = ProbeResult(
            video_codec="h264",
            width=1920,
            height=1080,
            bitrate=5000000,
            duration=3600.0,
            file_size=2_000_000_000,
        )
        assert result.resolution == "1920x1080"

    def test_frozen(self):
        result = ProbeResult(
            video_codec="h264",
            width=1920,
            height=1080,
            bitrate=None,
            duration=100.0,
            file_size=1000,
        )
        with pytest.raises(AttributeError):
            result.video_codec = "hevc"  # type: ignore[misc]

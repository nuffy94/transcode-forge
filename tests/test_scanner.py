"""Tests for the media scanner and ffprobe wrapper."""

import dataclasses
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from transcode_forge.scanner.probe import (
    ProbeError,
    ProbeResult,
    ffprobe,
    is_pipeline_artifact,
    is_video_file,
)


class TestIsPipelineArtifact:
    """Pipeline sidecar files carry real media extensions (movie.tf_bak.mkv)
    so the extension check alone would catalog them — phantom rows, and a
    queueable backup is one queue click from being transcoded."""

    def test_bak_is_artifact(self):
        assert is_pipeline_artifact(Path("movie.tf_bak.mkv")) is True

    def test_tmp_is_artifact(self):
        assert is_pipeline_artifact(Path("movie.tf_tmp.mkv")) is True

    def test_lock_is_artifact(self):
        assert is_pipeline_artifact(Path("movie.mkv.tf_lock")) is True
        assert is_pipeline_artifact(Path("movie.mkv.tf_lock.new")) is True

    def test_regular_video_is_not(self):
        assert is_pipeline_artifact(Path("movie.mkv")) is False
        assert is_pipeline_artifact(Path("Some.Film.2020.1080p.mkv")) is False


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

    async def test_probe_accepts_https_url(self):
        """Presigned S3 probes pass URLs. Regression (found live
        2026-07-06): Path()-ifying a URL mangled '//' and failed the
        exists() check, so every presigned probe raised FileNotFoundError
        before ffprobe ever ran — the URL must reach ffprobe's argv
        untouched, and file_size must come from ffprobe's format block
        (there is no stat() for a URL)."""
        url = "https://forge-media.us-ord-1.linodeobjects.com/masters/f.mov?X-Amz-Signature=abc"
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(SAMPLE_FFPROBE_OUTPUT.encode(), b""))
        mock_proc.returncode = 0

        with patch(
            "transcode_forge.scanner.probe.asyncio.create_subprocess_exec", return_value=mock_proc
        ) as mock_exec:
            result = await ffprobe(url)

        assert mock_exec.call_args[0][-1] == url
        assert result.video_codec == "h264"
        assert result.file_size == 2250000000

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


def _fake_probe(path) -> ProbeResult:
    """ProbeResult whose file_size matches the real file, like real ffprobe."""
    return ProbeResult(
        video_codec="h264",
        width=1920,
        height=1080,
        bitrate=5_000_000,
        duration=100.0,
        file_size=Path(path).stat().st_size,
    )


class TestScanLibrary:
    """Behavioral tests for scan_library's discovery and dedup logic."""

    async def _scan(self, db, library_path):
        from transcode_forge.repos import libraries as lib_repo
        from transcode_forge.repos import scans as scan_repo
        from transcode_forge.scanner.scanner import scan_library

        lib_id = await lib_repo.create_library(
            db, name="movies", media_type="movies", path=str(library_path)
        )
        with patch(
            "transcode_forge.scanner.scanner.ffprobe",
            new=AsyncMock(side_effect=_fake_probe),
        ):
            scan = await scan_library(
                library_id=lib_id,
                library_name="movies",
                library_path=str(library_path),
                media_type="movies",
                db=db,
            )
        return await scan_repo.get_scan(db, scan.id)

    async def test_symlinks_are_skipped(self, db, tmp_path):
        import os

        lib = tmp_path / "movies"
        lib.mkdir()
        real = lib / "real.mkv"
        real.write_bytes(b"x" * 100)
        try:
            os.symlink(real, lib / "link.mkv")
        except OSError:
            pytest.skip("symlinks not supported on this platform/privilege level")

        result = await self._scan(db, lib)
        assert result.files_found == 1
        assert result.files_new == 1

    async def test_pipeline_artifacts_never_cataloged(self, db, tmp_path):
        """A scan racing a transcode (or following a crash) sees .tf_bak /
        .tf_tmp siblings — they must not become catalog rows."""
        from transcode_forge.repos import media as media_repo

        lib = tmp_path / "movies"
        lib.mkdir()
        (lib / "movie.mkv").write_bytes(b"x" * 100)
        (lib / "movie.tf_bak.mkv").write_bytes(b"x" * 100)
        (lib / "movie.tf_tmp.mkv").write_bytes(b"x" * 50)

        result = await self._scan(db, lib)
        assert result.files_found == 1
        assert result.files_new == 1

        files, total = await media_repo.list_media_files(db)
        assert total == 1
        assert files[0]["file_path"].endswith("movie.mkv")

    async def test_unchanged_file_skipped_on_rescan(self, db, tmp_path):
        from transcode_forge.repos import scans as scan_repo
        from transcode_forge.scanner.scanner import scan_library

        lib = tmp_path / "movies"
        lib.mkdir()
        (lib / "a.mkv").write_bytes(b"x" * 100)

        first = await self._scan(db, lib)
        assert first.files_new == 1

        with patch(
            "transcode_forge.scanner.scanner.ffprobe",
            new=AsyncMock(side_effect=_fake_probe),
        ):
            scan2 = await scan_library(
                library_id="whatever",  # dedup keys on file_path, not library
                library_name="movies",
                library_path=str(lib),
                media_type="movies",
                db=db,
            )
        second = await scan_repo.get_scan(db, scan2.id)
        assert second.files_skipped == 1
        assert second.files_new == 0

    async def test_same_mtime_different_size_is_rescanned(self, db, tmp_path):
        """A file replaced within the filesystem's mtime granularity must
        still be detected via its size."""
        import os

        from transcode_forge.repos import scans as scan_repo
        from transcode_forge.scanner.scanner import scan_library

        lib = tmp_path / "movies"
        lib.mkdir()
        target = lib / "a.mkv"
        target.write_bytes(b"x" * 100)

        first = await self._scan(db, lib)
        assert first.files_new == 1
        original_stat = target.stat()

        # Replace the content (different size) but pin the mtime back.
        target.write_bytes(b"y" * 250)
        os.utime(target, (original_stat.st_atime, original_stat.st_mtime))

        with patch(
            "transcode_forge.scanner.scanner.ffprobe",
            new=AsyncMock(side_effect=_fake_probe),
        ):
            scan2 = await scan_library(
                library_id="whatever",
                library_name="movies",
                library_path=str(lib),
                media_type="movies",
                db=db,
            )
        second = await scan_repo.get_scan(db, scan2.id)
        assert second.files_updated == 1
        assert second.files_skipped == 0

    async def test_swapped_file_heals_status_on_rescan(self, db, tmp_path):
        """A file the fleet swapped to HEVC (new size and mtime) is re-probed
        on the next scan; the row's status must follow the new codec instead
        of staying at what the queue-time stamp left behind (live: queued|hevc)."""
        from transcode_forge.repos import media as media_repo
        from transcode_forge.scanner.scanner import scan_library

        lib = tmp_path / "movies"
        lib.mkdir()
        target = lib / "a.mkv"
        target.write_bytes(b"x" * 100)

        await self._scan(db, lib)
        files, _total = await media_repo.list_media_files(db)
        assert files[0]["transcode_status"] == "needs_transcode"
        await media_repo.update_media_status(
            db, files[0]["id"], transcode_status="queued", job_id="job-1"
        )

        # The swap: a smaller HEVC file lands at the same path.
        target.write_bytes(b"y" * 40)

        def hevc_probe(path) -> ProbeResult:
            return dataclasses.replace(_fake_probe(path), video_codec="hevc")

        with patch(
            "transcode_forge.scanner.scanner.ffprobe",
            new=AsyncMock(side_effect=hevc_probe),
        ):
            await scan_library(
                library_id="whatever",
                library_name="movies",
                library_path=str(lib),
                media_type="movies",
                db=db,
            )

        row = await media_repo.get_media_file(db, files[0]["id"])
        assert row is not None
        assert row["video_codec"] == "hevc"
        assert row["file_size"] == 40
        assert row["transcode_status"] == "complete"
        assert row["skip_reason"] == "already_hevc"

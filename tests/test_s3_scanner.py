"""Tests for S3 library scanner.

Unit tests for S3 scanning with mocked S3 client:
- Listing objects with pagination
- Presigned-URL probing (success path)
- Presigned-probe failure → head-bytes fallback
- Fallback logging
- Media file cataloging (upsert)
- Scan statistics
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from transcode_forge.config import Settings
from transcode_forge.scanner.s3_scanner import (
    _get_filename_from_s3_key,
    _is_s3_video_file,
    scan_s3_library,
)

# Sample ffprobe outputs (same as test_scanner.py)
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


@pytest.fixture
def s3_config() -> Settings:
    """S3 config with dummy credentials."""
    return Settings(
        s3_endpoint_url="https://api.linode.com",
        s3_region="us-east-1",
        s3_access_key_id="test-access-key",
        s3_secret_access_key="test-secret-key",
    )


class TestS3ScannerHelpers:
    """Test helper functions."""

    def test_is_s3_video_file_mkv(self) -> None:
        """Test that .mkv is recognized."""
        assert _is_s3_video_file("masters/movies/film.mkv") is True

    def test_is_s3_video_file_mp4(self) -> None:
        """Test that .mp4 is recognized."""
        assert _is_s3_video_file("masters/movies/film.mp4") is True

    def test_is_s3_video_file_avi(self) -> None:
        """Test that .avi is recognized."""
        assert _is_s3_video_file("masters/movies/film.avi") is True

    def test_is_s3_video_file_not_video(self) -> None:
        """Test that non-video files are rejected."""
        assert _is_s3_video_file("masters/movies/README.txt") is False

    def test_is_s3_video_file_directory_key(self) -> None:
        """Test that directory keys (ending with /) are rejected."""
        assert _is_s3_video_file("masters/movies/") is False

    def test_get_filename_from_s3_key_simple(self) -> None:
        """Test extracting filename from a simple key."""
        assert _get_filename_from_s3_key("masters/movies/film.mkv") == "film.mkv"

    def test_get_filename_from_s3_key_deep_path(self) -> None:
        """Test extracting filename from a deep key."""
        assert (
            _get_filename_from_s3_key("masters/movies/2020/sci-fi/inception.mkv") == "inception.mkv"
        )

    def test_get_filename_from_s3_key_trailing_slash(self) -> None:
        """Test extracting filename when key has trailing slash (edge case)."""
        # This shouldn't happen in normal use, but test it anyway
        assert _get_filename_from_s3_key("masters/movies/film.mkv/") == "film.mkv"


class TestS3LibraryScan:
    """Integration tests for S3 library scanning."""

    @pytest.mark.asyncio
    async def test_scan_empty_bucket(self, s3_config: Settings, db) -> None:
        """Test scanning an empty bucket (no objects)."""
        # Mock paginator with no results
        mock_client = AsyncMock()
        mock_paginator = AsyncMock()

        async def mock_paginate(*args, **kwargs):
            # Yield one empty page (no Contents key)
            yield {}

        mock_paginator.paginate = mock_paginate
        mock_client.get_paginator = MagicMock(return_value=mock_paginator)

        # Mock session and context manager
        mock_session = MagicMock()
        mock_context = AsyncMock()
        mock_context.__aenter__.return_value = mock_client
        mock_context.__aexit__.return_value = None
        mock_session.client = MagicMock(return_value=mock_context)

        with patch("transcode_forge.scanner.s3_scanner.Session", return_value=mock_session):
            stats = await scan_s3_library(
                library_id="test-lib",
                library_name="Test Library",
                bucket="test-bucket",
                prefix="masters/",
                config=s3_config,
                db=db,
            )

        assert stats["files_found"] == 0
        assert stats["files_new"] == 0
        assert stats["files_skipped"] == 0

    @pytest.mark.asyncio
    async def test_scan_with_presigned_url_probe_success(self, s3_config: Settings, db) -> None:
        """Test scanning with successful presigned-URL probing."""
        # Create a library record for FK constraint
        now = datetime.now(UTC).isoformat()
        await db.execute(
            """INSERT INTO libraries
               (id, name, media_type, path, quality_preset, enabled, auto_scan,
                scan_interval_hours, backend, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "test-lib",
                "Test Library",
                "movies",
                "/mnt/transcode",
                23,
                1,
                0,
                24,
                "s3",
                now,
                now,
            ),
        )
        await db.commit()

        # Mock paginator with a video file
        mock_client = AsyncMock()
        mock_paginator = AsyncMock()

        async def mock_paginate(*args, **kwargs):
            # Yield one page with a video object
            yield {
                "Contents": [
                    {
                        "Key": "masters/movies/film.mkv",
                        "Size": 2250000000,
                        "LastModified": datetime.now(UTC),
                    }
                ]
            }

        mock_paginator.paginate = mock_paginate
        mock_client.get_paginator = MagicMock(return_value=mock_paginator)

        # Mock presigned URL generation
        mock_client.generate_presigned_url = MagicMock(return_value="https://s3.mock/signed")

        # Mock session
        mock_session = MagicMock()
        mock_context = AsyncMock()
        mock_context.__aenter__.return_value = mock_client
        mock_context.__aexit__.return_value = None
        mock_session.client = MagicMock(return_value=mock_context)

        with patch("transcode_forge.scanner.s3_scanner.Session", return_value=mock_session):
            # Mock ffprobe to succeed with presigned URL
            with patch("transcode_forge.scanner.s3_scanner.ffprobe") as mock_ffprobe:
                from transcode_forge.scanner.probe import ProbeResult

                mock_ffprobe.return_value = ProbeResult(
                    video_codec="h264",
                    width=1920,
                    height=1080,
                    bitrate=5000000,
                    duration=3600.5,
                    file_size=2250000000,
                )

                stats = await scan_s3_library(
                    library_id="test-lib",
                    library_name="Test Library",
                    bucket="test-bucket",
                    prefix="masters/",
                    config=s3_config,
                    db=db,
                )

        # Verify stats
        assert stats["files_found"] == 1
        assert stats["files_new"] == 1
        assert stats["files_skipped"] == 0
        assert stats["files_failed"] == 0

        # Verify file was cataloged
        async with db.execute(
            "SELECT video_codec, width, height FROM media_files WHERE file_path = ?",
            ("masters/movies/film.mkv",),
        ) as cur:
            row = await cur.fetchone()
            assert row is not None
            assert row["video_codec"] == "h264"
            assert row["width"] == 1920
            assert row["height"] == 1080

    @pytest.mark.asyncio
    async def test_scan_presigned_fallback_to_head_bytes(self, s3_config: Settings, db) -> None:
        """Test presigned-probe failure → head-bytes fallback."""
        # Create a library record
        now = datetime.now(UTC).isoformat()
        await db.execute(
            """INSERT INTO libraries
               (id, name, media_type, path, quality_preset, enabled, auto_scan,
                scan_interval_hours, backend, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "test-lib",
                "Test Library",
                "movies",
                "/mnt/transcode",
                23,
                1,
                0,
                24,
                "s3",
                now,
                now,
            ),
        )
        await db.commit()

        # Mock paginator with a video file
        mock_client = AsyncMock()
        mock_paginator = AsyncMock()

        async def mock_paginate(*args, **kwargs):
            yield {
                "Contents": [
                    {
                        "Key": "masters/movies/flaky.mkv",
                        "Size": 2250000000,
                        "LastModified": datetime.now(UTC),
                    }
                ]
            }

        mock_paginator.paginate = mock_paginate
        mock_client.get_paginator = MagicMock(return_value=mock_paginator)

        # Mock presigned URL generation (will fail)
        mock_client.generate_presigned_url = MagicMock(return_value="https://s3.mock/signed")

        # Mock get_object for head-bytes fallback
        mock_body = AsyncMock()
        mock_body.read = AsyncMock(return_value=b"fake video head bytes" * 1000)
        mock_client.get_object = AsyncMock(return_value={"Body": mock_body})

        # Mock session
        mock_session = MagicMock()
        mock_context = AsyncMock()
        mock_context.__aenter__.return_value = mock_client
        mock_context.__aexit__.return_value = None
        mock_session.client = MagicMock(return_value=mock_context)

        with patch("transcode_forge.scanner.s3_scanner.Session", return_value=mock_session):
            # Mock ffprobe: first call (presigned) fails, second call (head-bytes) succeeds
            from transcode_forge.scanner.probe import ProbeError, ProbeResult

            call_count = 0

            async def mock_ffprobe_side_effect(path):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    # Presigned URL probe fails
                    raise ProbeError("Presigned URL failed")
                # Head-bytes probe succeeds
                return ProbeResult(
                    video_codec="h264",
                    width=1280,
                    height=720,
                    bitrate=2000000,
                    duration=1800.0,
                    file_size=2250000000,
                )

            with patch("transcode_forge.scanner.s3_scanner.ffprobe") as mock_ffprobe:
                mock_ffprobe.side_effect = mock_ffprobe_side_effect

                stats = await scan_s3_library(
                    library_id="test-lib",
                    library_name="Test Library",
                    bucket="test-bucket",
                    prefix="masters/",
                    config=s3_config,
                    db=db,
                )

        # Verify stats: file should be cataloged despite fallback
        assert stats["files_found"] == 1
        assert stats["files_new"] == 1
        assert stats["files_failed"] == 0

        # Verify file was cataloged with correct metadata
        async with db.execute(
            "SELECT video_codec, width, height FROM media_files WHERE file_path = ?",
            ("masters/movies/flaky.mkv",),
        ) as cur:
            row = await cur.fetchone()
            assert row is not None
            assert row["video_codec"] == "h264"
            assert row["width"] == 1280
            assert row["height"] == 720

    @pytest.mark.asyncio
    async def test_scan_both_probes_fail(self, s3_config: Settings, db) -> None:
        """Test when both presigned-probe and head-bytes-fallback fail."""
        # Create a library record
        now = datetime.now(UTC).isoformat()
        await db.execute(
            """INSERT INTO libraries
               (id, name, media_type, path, quality_preset, enabled, auto_scan,
                scan_interval_hours, backend, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "test-lib",
                "Test Library",
                "movies",
                "/mnt/transcode",
                23,
                1,
                0,
                24,
                "s3",
                now,
                now,
            ),
        )
        await db.commit()

        # Mock paginator with a bad video file
        mock_client = AsyncMock()
        mock_paginator = AsyncMock()

        async def mock_paginate(*args, **kwargs):
            yield {
                "Contents": [
                    {
                        "Key": "masters/movies/corrupted.mkv",
                        "Size": 2250000000,
                        "LastModified": datetime.now(UTC),
                    }
                ]
            }

        mock_paginator.paginate = mock_paginate
        mock_client.get_paginator = MagicMock(return_value=mock_paginator)
        mock_client.generate_presigned_url = MagicMock(return_value="https://s3.mock/signed")

        # Mock get_object to fail
        mock_client.get_object = AsyncMock(side_effect=Exception("Download failed"))

        # Mock session
        mock_session = MagicMock()
        mock_context = AsyncMock()
        mock_context.__aenter__.return_value = mock_client
        mock_context.__aexit__.return_value = None
        mock_session.client = MagicMock(return_value=mock_context)

        with patch("transcode_forge.scanner.s3_scanner.Session", return_value=mock_session):
            from transcode_forge.scanner.probe import ProbeError

            with patch("transcode_forge.scanner.s3_scanner.ffprobe") as mock_ffprobe:
                # Both calls fail
                mock_ffprobe.side_effect = ProbeError("Probe failed")

                stats = await scan_s3_library(
                    library_id="test-lib",
                    library_name="Test Library",
                    bucket="test-bucket",
                    prefix="masters/",
                    config=s3_config,
                    db=db,
                )

        # File should be counted as failed
        assert stats["files_found"] == 1
        assert stats["files_failed"] == 1
        assert stats["files_new"] == 0

    @pytest.mark.asyncio
    async def test_scan_multiple_objects_paginated(self, s3_config: Settings, db) -> None:
        """Test scanning with paginated results (>1000 objects)."""
        # Create a library record
        now = datetime.now(UTC).isoformat()
        await db.execute(
            """INSERT INTO libraries
               (id, name, media_type, path, quality_preset, enabled, auto_scan,
                scan_interval_hours, backend, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "test-lib",
                "Test Library",
                "movies",
                "/mnt/transcode",
                23,
                1,
                0,
                24,
                "s3",
                now,
                now,
            ),
        )
        await db.commit()

        # Mock paginator with two pages (simulating pagination)
        mock_client = AsyncMock()
        mock_paginator = AsyncMock()

        async def mock_paginate(*args, **kwargs):
            # Page 1
            yield {
                "Contents": [
                    {
                        "Key": f"masters/movies/film{i}.mkv",
                        "Size": 2250000000,
                        "LastModified": datetime.now(UTC),
                    }
                    for i in range(3)
                ]
            }
            # Page 2
            yield {
                "Contents": [
                    {
                        "Key": f"masters/movies/film{i}.mkv",
                        "Size": 2250000000,
                        "LastModified": datetime.now(UTC),
                    }
                    for i in range(3, 5)
                ]
            }

        mock_paginator.paginate = mock_paginate
        mock_client.get_paginator = MagicMock(return_value=mock_paginator)
        mock_client.generate_presigned_url = MagicMock(return_value="https://s3.mock/signed")

        # Mock session
        mock_session = MagicMock()
        mock_context = AsyncMock()
        mock_context.__aenter__.return_value = mock_client
        mock_context.__aexit__.return_value = None
        mock_session.client = MagicMock(return_value=mock_context)

        with patch("transcode_forge.scanner.s3_scanner.Session", return_value=mock_session):
            from transcode_forge.scanner.probe import ProbeResult

            with patch("transcode_forge.scanner.s3_scanner.ffprobe") as mock_ffprobe:
                mock_ffprobe.return_value = ProbeResult(
                    video_codec="h264",
                    width=1920,
                    height=1080,
                    bitrate=5000000,
                    duration=3600.5,
                    file_size=2250000000,
                )

                stats = await scan_s3_library(
                    library_id="test-lib",
                    library_name="Test Library",
                    bucket="test-bucket",
                    prefix="masters/",
                    config=s3_config,
                    db=db,
                )

        # Verify all 5 files were scanned
        assert stats["files_found"] == 5
        assert stats["files_new"] == 5

    @pytest.mark.asyncio
    async def test_scan_respects_max_files(self, s3_config: Settings, db) -> None:
        """Test that max_files limit is respected."""
        # Create a library record
        now = datetime.now(UTC).isoformat()
        await db.execute(
            """INSERT INTO libraries
               (id, name, media_type, path, quality_preset, enabled, auto_scan,
                scan_interval_hours, backend, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "test-lib",
                "Test Library",
                "movies",
                "/mnt/transcode",
                23,
                1,
                0,
                24,
                "s3",
                now,
                now,
            ),
        )
        await db.commit()

        # Mock paginator with many objects
        mock_client = AsyncMock()
        mock_paginator = AsyncMock()

        async def mock_paginate(*args, **kwargs):
            yield {
                "Contents": [
                    {
                        "Key": f"masters/movies/film{i}.mkv",
                        "Size": 2250000000,
                        "LastModified": datetime.now(UTC),
                    }
                    for i in range(100)
                ]
            }

        mock_paginator.paginate = mock_paginate
        mock_client.get_paginator = MagicMock(return_value=mock_paginator)
        mock_client.generate_presigned_url = MagicMock(return_value="https://s3.mock/signed")

        # Mock session
        mock_session = MagicMock()
        mock_context = AsyncMock()
        mock_context.__aenter__.return_value = mock_client
        mock_context.__aexit__.return_value = None
        mock_session.client = MagicMock(return_value=mock_context)

        with patch("transcode_forge.scanner.s3_scanner.Session", return_value=mock_session):
            from transcode_forge.scanner.probe import ProbeResult

            with patch("transcode_forge.scanner.s3_scanner.ffprobe") as mock_ffprobe:
                mock_ffprobe.return_value = ProbeResult(
                    video_codec="h264",
                    width=1920,
                    height=1080,
                    bitrate=5000000,
                    duration=3600.5,
                    file_size=2250000000,
                )

                stats = await scan_s3_library(
                    library_id="test-lib",
                    library_name="Test Library",
                    bucket="test-bucket",
                    prefix="masters/",
                    config=s3_config,
                    db=db,
                    max_files=10,
                )

        # Verify only 10 files were scanned
        assert stats["files_found"] == 10
        assert stats["files_new"] == 10

    @pytest.mark.asyncio
    async def test_scan_skips_directory_keys(self, s3_config: Settings, db) -> None:
        """Test that directory keys (ending with /) are skipped."""
        # Create a library record
        now = datetime.now(UTC).isoformat()
        await db.execute(
            """INSERT INTO libraries
               (id, name, media_type, path, quality_preset, enabled, auto_scan,
                scan_interval_hours, backend, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "test-lib",
                "Test Library",
                "movies",
                "/mnt/transcode",
                23,
                1,
                0,
                24,
                "s3",
                now,
                now,
            ),
        )
        await db.commit()

        # Mock paginator with mixed content (files and directories)
        mock_client = AsyncMock()
        mock_paginator = AsyncMock()

        async def mock_paginate(*args, **kwargs):
            yield {
                "Contents": [
                    {"Key": "masters/", "Size": 0, "LastModified": datetime.now(UTC)},  # dir
                    {
                        "Key": "masters/movies/film.mkv",
                        "Size": 2250000000,
                        "LastModified": datetime.now(UTC),
                    },  # file
                    {"Key": "masters/movies/", "Size": 0, "LastModified": datetime.now(UTC)},  # dir
                ]
            }

        mock_paginator.paginate = mock_paginate
        mock_client.get_paginator = MagicMock(return_value=mock_paginator)
        mock_client.generate_presigned_url = MagicMock(return_value="https://s3.mock/signed")

        # Mock session
        mock_session = MagicMock()
        mock_context = AsyncMock()
        mock_context.__aenter__.return_value = mock_client
        mock_context.__aexit__.return_value = None
        mock_session.client = MagicMock(return_value=mock_context)

        with patch("transcode_forge.scanner.s3_scanner.Session", return_value=mock_session):
            from transcode_forge.scanner.probe import ProbeResult

            with patch("transcode_forge.scanner.s3_scanner.ffprobe") as mock_ffprobe:
                mock_ffprobe.return_value = ProbeResult(
                    video_codec="h264",
                    width=1920,
                    height=1080,
                    bitrate=5000000,
                    duration=3600.5,
                    file_size=2250000000,
                )

                stats = await scan_s3_library(
                    library_id="test-lib",
                    library_name="Test Library",
                    bucket="test-bucket",
                    prefix="masters/",
                    config=s3_config,
                    db=db,
                )

        # Only 1 file should be found (2 directories skipped)
        assert stats["files_found"] == 1
        assert stats["files_new"] == 1


class TestS3ScanRecords:
    """S3 scans must be visible in scan history — success AND failure.

    Found live 2026-07-05: an S3 scan that failed on bad credentials left
    NO scan record at all (the FS scanner owns its record; the S3 scanner
    never did) — the UI showed a success toast and then nothing, anywhere.
    """

    @pytest.mark.asyncio
    async def test_success_writes_complete_scan_record(self, s3_config: Settings, db) -> None:
        from transcode_forge.models.scan import ScanStatus
        from transcode_forge.repos import scans as scan_repo

        mock_client = AsyncMock()
        mock_paginator = AsyncMock()

        async def mock_paginate(*args, **kwargs):
            yield {}

        mock_paginator.paginate = mock_paginate
        mock_client.get_paginator = MagicMock(return_value=mock_paginator)
        mock_session = MagicMock()
        mock_context = AsyncMock()
        mock_context.__aenter__.return_value = mock_client
        mock_context.__aexit__.return_value = None
        mock_session.client = MagicMock(return_value=mock_context)

        with patch("transcode_forge.scanner.s3_scanner.Session", return_value=mock_session):
            await scan_s3_library(
                library_id="test-lib",
                library_name="Cloud Movies",
                bucket="test-bucket",
                prefix="masters/",
                config=s3_config,
                db=db,
            )

        scans, total = await scan_repo.list_scans(db)
        assert total == 1
        assert scans[0].library == "Cloud Movies"
        assert scans[0].status == ScanStatus.COMPLETE
        assert scans[0].completed_at is not None

    @pytest.mark.asyncio
    async def test_failure_writes_failed_scan_record(self, s3_config: Settings, db) -> None:
        """Any failure — including non-boto ones like the live endpoint
        ValueError — must leave a FAILED scan record, then re-raise."""
        from transcode_forge.models.scan import ScanStatus
        from transcode_forge.repos import scans as scan_repo

        mock_session = MagicMock()
        mock_session.client = MagicMock(
            side_effect=ValueError("Invalid endpoint: https://s3..amazonaws.com")
        )

        with patch("transcode_forge.scanner.s3_scanner.Session", return_value=mock_session):
            with pytest.raises(ValueError):
                await scan_s3_library(
                    library_id="test-lib",
                    library_name="Cloud Movies",
                    bucket="test-bucket",
                    prefix="masters/",
                    config=s3_config,
                    db=db,
                )

        scans, total = await scan_repo.list_scans(db)
        assert total == 1
        assert scans[0].library == "Cloud Movies"
        assert scans[0].status == ScanStatus.FAILED
        assert scans[0].completed_at is not None

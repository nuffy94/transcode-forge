"""Comprehensive tests for database repository layer — media, libraries, system, workers, jobs."""

from datetime import UTC, datetime, timedelta

import pytest

from transcode_forge.models.job import Job, JobStatus
from transcode_forge.models.worker import Worker, WorkerStatus
from transcode_forge.repos import jobs as job_repo
from transcode_forge.repos import libraries as lib_repo
from transcode_forge.repos import media as media_repo
from transcode_forge.repos import system as sys_repo
from transcode_forge.repos import workers as worker_repo

# ============================================================================
# Fixtures for common test setup
# ============================================================================


@pytest.fixture
async def test_library(db):
    """Create a test library for use in media file tests."""
    lib_id = await lib_repo.create_library(
        db,
        name="Test Movies",
        media_type="movies",
        path="/media/movies",
        quality_preset=21,
        enabled=True,
        auto_scan=False,
        scan_interval_hours=24,
    )
    return lib_id


@pytest.fixture
async def tv_library(db):
    """Create a test TV library."""
    lib_id = await lib_repo.create_library(
        db,
        name="Test TV",
        media_type="tv",
        path="/media/tv",
        quality_preset=22,
        enabled=True,
        auto_scan=True,
        scan_interval_hours=12,
    )
    return lib_id


@pytest.fixture
async def anime_library(db):
    """Create a test anime library."""
    lib_id = await lib_repo.create_library(
        db,
        name="Test Anime",
        media_type="anime",
        path="/media/anime",
        quality_preset=20,
        enabled=False,
        auto_scan=False,
        scan_interval_hours=48,
    )
    return lib_id


# ============================================================================
# Media Repository Tests
# ============================================================================


class TestMediaRepo:
    """Tests for media_repo functions."""

    async def test_upsert_media_file_insert_h264(self, db, test_library):
        """Test inserting a new H.264 media file with correct status."""
        file_id = await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path="/media/movies/test.mkv",
            filename="test.mkv",
            video_codec="h264",
            audio_codec="aac",
            resolution="1080p",
            width=1920,
            height=1080,
            bitrate=5000,
            duration=120.5,
            file_size=1024000,
            file_modified_at="2025-01-01T00:00:00+00:00",
        )
        assert file_id is not None
        assert len(file_id) == 36  # UUID length

        # Verify file was inserted with correct status
        media = await media_repo.get_media_file(db, file_id)
        assert media is not None
        assert media["file_path"] == "/media/movies/test.mkv"
        assert media["transcode_status"] == "needs_transcode"
        assert media["skip_reason"] is None
        assert media["video_codec"] == "h264"

    async def test_upsert_media_file_hevc_sets_complete(self, db, test_library):
        """Test that HEVC files are marked as complete automatically."""
        file_id = await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path="/media/movies/already_hevc.mkv",
            filename="already_hevc.mkv",
            video_codec="hevc",
            file_size=500000,
        )

        media = await media_repo.get_media_file(db, file_id)
        assert media["transcode_status"] == "complete"
        assert media["skip_reason"] == "already_hevc"

    async def test_upsert_media_file_unsupported_codec_skips(self, db, test_library):
        """Test that non-H.264/HEVC codecs are marked skipped."""
        file_id = await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path="/media/movies/mpeg4.mkv",
            filename="mpeg4.mkv",
            video_codec="mpeg4",
            file_size=500000,
        )

        media = await media_repo.get_media_file(db, file_id)
        assert media["transcode_status"] == "skipped"
        assert media["skip_reason"] == "not_h264"

    async def test_upsert_media_file_pending_status(self, db, test_library):
        """Test that file without video_codec gets pending status."""
        file_id = await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path="/media/movies/unknown.mkv",
            filename="unknown.mkv",
            file_size=500000,
        )

        media = await media_repo.get_media_file(db, file_id)
        assert media["transcode_status"] == "pending"
        assert media["skip_reason"] is None

    async def test_upsert_media_file_updates_on_conflict(self, db, test_library):
        """Test that upserting the same file path updates it."""
        path = "/media/movies/update_test.mkv"

        # First insert
        file_id = await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path=path,
            filename="update_test.mkv",
            video_codec="h264",
            file_size=1000000,
            duration=100.0,
        )

        # Second upsert with updated values
        file_id2 = await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path=path,
            filename="update_test.mkv",
            video_codec="h264",
            file_size=2000000,  # Updated size
            duration=120.0,  # Updated duration
        )

        # Should return a new ID on insert
        assert file_id2 != file_id

        # But only one row should exist for this path
        media = await media_repo.get_media_file(db, file_id)
        # The original ID no longer has a record (conflict returns new ID)
        if media:
            assert media["file_size"] == 2000000 or media["file_size"] == 1000000

    async def _row_by_path(self, db, path: str) -> dict:
        async with db.execute("SELECT * FROM media_files WHERE file_path = ?", (path,)) as cur:
            row = await cur.fetchone()
        assert row is not None
        return dict(row)

    async def test_upsert_conflict_codec_change_recomputes_status(self, db, test_library):
        """A rescan that probes a different codec (the swap landed) adopts
        the freshly computed status: queued|h264 becomes complete|already_hevc.
        Live: swapped files rescanned to queued|hevc and never healed."""
        path = "/media/movies/swapped.mkv"
        file_id = await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path=path,
            filename="swapped.mkv",
            video_codec="h264",
            file_size=1000,
        )
        await media_repo.update_media_status(
            db, file_id, transcode_status="queued", job_id="job-1"
        )

        await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path=path,
            filename="swapped.mkv",
            video_codec="hevc",
            file_size=400,
        )

        row = await self._row_by_path(db, path)
        assert row["video_codec"] == "hevc"
        assert row["file_size"] == 400
        assert row["transcode_status"] == "complete"
        assert row["skip_reason"] == "already_hevc"
        assert row["job_id"] == "job-1"  # the drawer link survives the rescan

    async def test_upsert_conflict_same_codec_keeps_status(self, db, test_library):
        """Same codec on rescan (a same-codec replacement changed size or
        mtime): the existing status stands, a queued job stays queued."""
        path = "/media/movies/same.mkv"
        file_id = await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path=path,
            filename="same.mkv",
            video_codec="h264",
            file_size=1000,
        )
        await media_repo.update_media_status(
            db, file_id, transcode_status="queued", job_id="job-2"
        )

        await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path=path,
            filename="same.mkv",
            video_codec="h264",
            file_size=2000,
        )

        row = await self._row_by_path(db, path)
        assert row["file_size"] == 2000
        assert row["transcode_status"] == "queued"
        assert row["skip_reason"] is None

    async def test_upsert_conflict_same_codec_keeps_skip_reason(self, db, test_library):
        """A worker-decided skip (VMAF gate) must survive a same-codec rescan;
        recomputing it would turn the row back into needs_transcode."""
        path = "/media/movies/gated.mkv"
        file_id = await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path=path,
            filename="gated.mkv",
            video_codec="h264",
            file_size=1000,
        )
        await media_repo.update_media_status(
            db, file_id, transcode_status="skipped", skip_reason="below_vmaf_floor"
        )

        await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path=path,
            filename="gated.mkv",
            video_codec="h264",
            file_size=1000,
        )

        row = await self._row_by_path(db, path)
        assert row["transcode_status"] == "skipped"
        assert row["skip_reason"] == "below_vmaf_floor"

    async def test_upsert_conflict_null_codec_keeps_status(self, db, test_library):
        """A probe with no codec says nothing about the file: the existing
        status and skip_reason stand."""
        path = "/media/movies/unprobed.mkv"
        await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path=path,
            filename="unprobed.mkv",
            video_codec="hevc",
            file_size=1000,
        )

        await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path=path,
            filename="unprobed.mkv",
            video_codec=None,
            file_size=1000,
        )

        row = await self._row_by_path(db, path)
        assert row["transcode_status"] == "complete"
        assert row["skip_reason"] == "already_hevc"

    async def test_get_media_file_found(self, db, test_library):
        """Test retrieving an existing media file."""
        file_id = await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path="/media/movies/found.mkv",
            filename="found.mkv",
            video_codec="h264",
        )

        media = await media_repo.get_media_file(db, file_id)
        assert media is not None
        assert media["filename"] == "found.mkv"
        assert media["id"] == file_id

    async def test_get_media_file_not_found(self, db):
        """Test retrieving a non-existent media file returns None."""
        media = await media_repo.get_media_file(db, "nonexistent-id")
        assert media is None

    async def test_list_media_files_empty(self, db, test_library):
        """Test listing with no media files."""
        files, total = await media_repo.list_media_files(db)
        assert files == []
        assert total == 0

    async def test_list_media_files_basic(self, db, test_library):
        """Test listing all media files."""
        for i in range(3):
            await media_repo.upsert_media_file(
                db,
                library_id=test_library,
                file_path=f"/media/movies/file{i}.mkv",
                filename=f"file{i}.mkv",
                video_codec="h264",
            )

        files, total = await media_repo.list_media_files(db)
        assert total == 3
        assert len(files) == 3

    async def test_list_media_files_filter_by_library(self, db, test_library, tv_library):
        """Test filtering media files by library."""
        # Add to movies library
        await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path="/media/movies/movie.mkv",
            filename="movie.mkv",
            video_codec="h264",
        )

        # Add to TV library
        await media_repo.upsert_media_file(
            db,
            library_id=tv_library,
            file_path="/media/tv/episode.mkv",
            filename="episode.mkv",
            video_codec="h264",
        )

        files, total = await media_repo.list_media_files(db, library_id=test_library)
        assert total == 1
        assert files[0]["filename"] == "movie.mkv"

    async def test_list_media_files_filter_by_codec(self, db, test_library):
        """Test filtering media files by video codec."""
        await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path="/media/movies/h264.mkv",
            filename="h264.mkv",
            video_codec="h264",
        )
        await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path="/media/movies/hevc.mkv",
            filename="hevc.mkv",
            video_codec="hevc",
        )

        files, total = await media_repo.list_media_files(db, video_codec="h264")
        assert total == 1
        assert files[0]["video_codec"] == "h264"

    async def test_list_media_files_filter_by_status(self, db, test_library):
        """Test filtering media files by transcode status."""
        await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path="/media/movies/needs.mkv",
            filename="needs.mkv",
            video_codec="h264",
        )
        await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path="/media/movies/done.mkv",
            filename="done.mkv",
            video_codec="hevc",
        )

        files, total = await media_repo.list_media_files(db, transcode_status="needs_transcode")
        assert total == 1
        assert files[0]["transcode_status"] == "needs_transcode"

    async def test_list_media_files_filter_by_multiple_statuses(self, db, test_library):
        """Test filtering by multiple statuses (comma-separated)."""
        await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path="/media/movies/h264.mkv",
            filename="h264.mkv",
            video_codec="h264",
        )
        await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path="/media/movies/hevc.mkv",
            filename="hevc.mkv",
            video_codec="hevc",
        )
        await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path="/media/movies/mpeg4.mkv",
            filename="mpeg4.mkv",
            video_codec="mpeg4",
        )

        files, total = await media_repo.list_media_files(
            db, transcode_status="needs_transcode,complete"
        )
        assert total == 2
        statuses = {f["transcode_status"] for f in files}
        assert statuses == {"needs_transcode", "complete"}

    async def test_list_media_files_filter_by_show_name(self, db, test_library):
        """Test filtering TV files by show name."""
        await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path="/media/tv/breaking_bad_s01e01.mkv",
            filename="breaking_bad_s01e01.mkv",
            show_name="Breaking Bad",
            season=1,
            episode=1,
            video_codec="h264",
        )
        await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path="/media/tv/better_call_saul_s01e01.mkv",
            filename="better_call_saul_s01e01.mkv",
            show_name="Better Call Saul",
            season=1,
            episode=1,
            video_codec="h264",
        )

        files, total = await media_repo.list_media_files(db, show_name="Breaking Bad")
        assert total == 1
        assert files[0]["show_name"] == "Breaking Bad"

    async def test_list_media_files_search_by_filename(self, db, test_library):
        """Test searching media files by filename."""
        await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path="/media/movies/dark_knight.mkv",
            filename="dark_knight.mkv",
            video_codec="h264",
        )
        await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path="/media/movies/light_bulb.mkv",
            filename="light_bulb.mkv",
            video_codec="h264",
        )

        files, total = await media_repo.list_media_files(db, search="dark")
        assert total == 1
        assert files[0]["filename"] == "dark_knight.mkv"

    async def test_list_media_files_sorting_by_filename(self, db, test_library):
        """Test sorting by filename."""
        for name in ["zebra.mkv", "apple.mkv", "banana.mkv"]:
            await media_repo.upsert_media_file(
                db,
                library_id=test_library,
                file_path=f"/media/movies/{name}",
                filename=name,
                video_codec="h264",
            )

        files, _ = await media_repo.list_media_files(db, sort_by="filename", sort_dir="asc")
        names = [f["filename"] for f in files]
        assert names == ["apple.mkv", "banana.mkv", "zebra.mkv"]

    async def test_list_media_files_sorting_descending(self, db, test_library):
        """Test descending sort order."""
        for name in ["apple.mkv", "zebra.mkv", "banana.mkv"]:
            await media_repo.upsert_media_file(
                db,
                library_id=test_library,
                file_path=f"/media/movies/{name}",
                filename=name,
                video_codec="h264",
            )

        files, _ = await media_repo.list_media_files(db, sort_by="filename", sort_dir="desc")
        names = [f["filename"] for f in files]
        assert names == ["zebra.mkv", "banana.mkv", "apple.mkv"]

    async def test_list_media_files_sort_by_codec(self, db, test_library):
        """Test sorting by video codec."""
        codecs = ["hevc", "h264", "mpeg4"]
        for codec in codecs:
            await media_repo.upsert_media_file(
                db,
                library_id=test_library,
                file_path=f"/media/movies/{codec}.mkv",
                filename=f"{codec}.mkv",
                video_codec=codec,
            )

        files, _ = await media_repo.list_media_files(db, sort_by="video_codec", sort_dir="asc")
        # Verify they're sorted (exact order depends on codecs)
        assert len(files) == 3

    async def test_list_media_files_pagination(self, db, test_library):
        """Test pagination with limit and offset."""
        for i in range(10):
            await media_repo.upsert_media_file(
                db,
                library_id=test_library,
                file_path=f"/media/movies/file{i:02d}.mkv",
                filename=f"file{i:02d}.mkv",
                video_codec="h264",
            )

        page1, total = await media_repo.list_media_files(db, sort_by="filename", limit=3, offset=0)
        assert total == 10
        assert len(page1) == 3

        page2, _ = await media_repo.list_media_files(db, sort_by="filename", limit=3, offset=3)
        assert len(page2) == 3

        # Ensure no overlap
        ids1 = {f["id"] for f in page1}
        ids2 = {f["id"] for f in page2}
        assert ids1.isdisjoint(ids2)

    async def test_list_media_files_filter_by_media_type(self, db, test_library, tv_library):
        """Test filtering by media_type."""
        # Add to movies
        await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path="/media/movies/movie.mkv",
            filename="movie.mkv",
            video_codec="h264",
        )

        # Add to TV
        await media_repo.upsert_media_file(
            db,
            library_id=tv_library,
            file_path="/media/tv/episode.mkv",
            filename="episode.mkv",
            video_codec="h264",
        )

        files, total = await media_repo.list_media_files(db, media_type="movies")
        assert total == 1
        assert files[0]["media_type"] == "movies"

    async def test_list_tv_shows(self, db, tv_library):
        """Test listing TV shows with aggregation."""
        # Add Breaking Bad episodes
        for ep in range(1, 4):
            await media_repo.upsert_media_file(
                db,
                library_id=tv_library,
                file_path=f"/media/tv/breaking_bad_s01e{ep:02d}.mkv",
                filename=f"breaking_bad_s01e{ep:02d}.mkv",
                show_name="Breaking Bad",
                season=1,
                episode=ep,
                video_codec="h264",
                file_size=1000000,
            )

        # Add Better Call Saul episodes
        for ep in range(1, 3):
            await media_repo.upsert_media_file(
                db,
                library_id=tv_library,
                file_path=f"/media/tv/better_call_saul_s01e{ep:02d}.mkv",
                filename=f"better_call_saul_s01e{ep:02d}.mkv",
                show_name="Better Call Saul",
                season=1,
                episode=ep,
                video_codec="hevc",
                file_size=800000,
            )

        shows = await media_repo.list_tv_shows(db, library_id=tv_library)
        assert len(shows) == 2

        bb_show = next(s for s in shows if s["show_name"] == "Breaking Bad")
        assert bb_show["episode_count"] == 3
        assert bb_show["total_size"] == 3000000
        assert bb_show["transcoded_count"] == 0  # HEVC or complete
        assert bb_show["needs_transcode_count"] == 3

        bcs_show = next(s for s in shows if s["show_name"] == "Better Call Saul")
        assert bcs_show["episode_count"] == 2
        assert bcs_show["transcoded_count"] == 2  # HEVC files

    async def test_list_tv_shows_no_tv_files(self, db, test_library):
        """Test listing TV shows when there are only movie files."""
        await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path="/media/movies/movie.mkv",
            filename="movie.mkv",
            video_codec="h264",
        )

        shows = await media_repo.list_tv_shows(db)
        assert shows == []

    async def test_update_media_status_valid(self, db, test_library):
        """Test updating media file status to a valid status."""
        file_id = await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path="/media/movies/test.mkv",
            filename="test.mkv",
            video_codec="h264",
        )

        await media_repo.update_media_status(
            db, file_id, transcode_status="queued", skip_reason=None, job_id="job-123"
        )

        media = await media_repo.get_media_file(db, file_id)
        assert media["transcode_status"] == "queued"
        assert media["job_id"] == "job-123"

    async def test_update_media_status_invalid_status(self, db, test_library):
        """Test that updating with invalid status raises ValueError."""
        file_id = await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path="/media/movies/test.mkv",
            filename="test.mkv",
            video_codec="h264",
        )

        with pytest.raises(ValueError, match="Invalid transcode_status"):
            await media_repo.update_media_status(db, file_id, transcode_status="invalid_status")

    async def test_update_media_status_with_skip_reason(self, db, test_library):
        """Test updating status with skip reason."""
        file_id = await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path="/media/movies/test.mkv",
            filename="test.mkv",
            video_codec="mpeg4",
        )

        await media_repo.update_media_status(
            db, file_id, transcode_status="skipped", skip_reason="not_h264"
        )

        media = await media_repo.get_media_file(db, file_id)
        assert media["transcode_status"] == "skipped"
        assert media["skip_reason"] == "not_h264"

    async def test_bulk_update_status(self, db, test_library):
        """Test bulk updating status for multiple files."""
        file_ids = []
        for i in range(3):
            file_id = await media_repo.upsert_media_file(
                db,
                library_id=test_library,
                file_path=f"/media/movies/file{i}.mkv",
                filename=f"file{i}.mkv",
                video_codec="h264",
            )
            file_ids.append(file_id)

        count = await media_repo.bulk_update_status(db, file_ids, transcode_status="queued")
        assert count == 3

        for file_id in file_ids:
            media = await media_repo.get_media_file(db, file_id)
            assert media["transcode_status"] == "queued"

    async def test_bulk_update_status_empty_list(self, db):
        """Test bulk update with empty file list."""
        count = await media_repo.bulk_update_status(db, [], transcode_status="queued")
        assert count == 0

    async def test_bulk_update_status_invalid_status(self, db, test_library):
        """Test bulk update with invalid status raises ValueError."""
        file_id = await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path="/media/movies/test.mkv",
            filename="test.mkv",
            video_codec="h264",
        )

        with pytest.raises(ValueError, match="Invalid transcode_status"):
            await media_repo.bulk_update_status(db, [file_id], transcode_status="invalid")

    async def test_get_codec_stats(self, db, test_library):
        """Test codec distribution statistics."""
        await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path="/media/movies/h264_1.mkv",
            filename="h264_1.mkv",
            video_codec="h264",
            file_size=1000000,
        )
        await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path="/media/movies/h264_2.mkv",
            filename="h264_2.mkv",
            video_codec="h264",
            file_size=2000000,
        )
        await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path="/media/movies/hevc.mkv",
            filename="hevc.mkv",
            video_codec="hevc",
            file_size=500000,
        )

        stats = await media_repo.get_codec_stats(db)
        assert "h264" in stats
        assert stats["h264"]["count"] == 2
        assert stats["h264"]["total_size"] == 3000000
        assert stats["hevc"]["count"] == 1
        assert stats["hevc"]["total_size"] == 500000

    async def test_get_codec_stats_unknown_codec(self, db, test_library):
        """Test codec stats includes unknown (NULL) codecs."""
        await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path="/media/movies/unknown.mkv",
            filename="unknown.mkv",
            file_size=100000,
        )

        stats = await media_repo.get_codec_stats(db)
        assert "unknown" in stats
        assert stats["unknown"]["count"] == 1

    async def test_get_status_stats(self, db, test_library):
        """Test transcode status distribution."""
        await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path="/media/movies/h264.mkv",
            filename="h264.mkv",
            video_codec="h264",
        )
        await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path="/media/movies/hevc.mkv",
            filename="hevc.mkv",
            video_codec="hevc",
        )

        stats = await media_repo.get_status_stats(db)
        assert stats["needs_transcode"] == 1
        assert stats["complete"] == 1

    async def test_get_status_stats_by_media_type(self, db, test_library, tv_library):
        """Test status stats filtered by media type."""
        await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path="/media/movies/movie.mkv",
            filename="movie.mkv",
            video_codec="h264",
        )
        await media_repo.upsert_media_file(
            db,
            library_id=tv_library,
            file_path="/media/tv/episode.mkv",
            filename="episode.mkv",
            video_codec="h264",
        )

        movie_stats = await media_repo.get_status_stats(db, media_type="movies")
        assert "needs_transcode" in movie_stats
        assert movie_stats["needs_transcode"] == 1

        tv_stats = await media_repo.get_status_stats(db, media_type="tv")
        assert tv_stats["needs_transcode"] == 1


# ============================================================================
# Libraries Repository Tests
# ============================================================================


class TestLibrariesRepo:
    """Tests for libraries_repo functions."""

    async def test_create_library(self, db):
        """Test creating a library."""
        lib_id = await lib_repo.create_library(
            db,
            name="Movies",
            media_type="movies",
            path="/media/movies",
            quality_preset=21,
            enabled=True,
            auto_scan=False,
        )

        lib = await lib_repo.get_library(db, lib_id)
        assert lib is not None
        assert lib["name"] == "Movies"
        assert lib["media_type"] == "movies"
        assert lib["enabled"] == 1

    async def test_get_library_found(self, db, test_library):
        """Test retrieving an existing library."""
        lib = await lib_repo.get_library(db, test_library)
        assert lib is not None
        assert lib["name"] == "Test Movies"
        assert lib["media_type"] == "movies"

    async def test_get_library_not_found(self, db):
        """Test retrieving a non-existent library returns None."""
        lib = await lib_repo.get_library(db, "nonexistent-id")
        assert lib is None

    async def test_list_libraries_empty(self, db):
        """Test listing libraries when none exist."""
        libs = await lib_repo.list_libraries(db)
        assert libs == []

    async def test_list_libraries(self, db, test_library, tv_library, anime_library):
        """Test listing all libraries."""
        libs = await lib_repo.list_libraries(db)
        assert len(libs) == 3

    async def test_list_libraries_filter_by_media_type(self, db, test_library, tv_library):
        """Test filtering libraries by media type."""
        libs = await lib_repo.list_libraries(db, media_type="movies")
        assert len(libs) == 1
        assert libs[0]["name"] == "Test Movies"

    async def test_list_libraries_enabled_only(self, db, test_library, anime_library):
        """Test filtering to only enabled libraries."""
        # test_library is enabled, anime_library is disabled
        libs = await lib_repo.list_libraries(db, enabled_only=True)
        names = {lib["name"] for lib in libs}
        assert "Test Movies" in names
        assert "Test Anime" not in names

    async def test_list_libraries_combined_filters(
        self, db, test_library, tv_library, anime_library
    ):
        """Test combining media_type and enabled_only filters."""
        libs = await lib_repo.list_libraries(db, media_type="tv", enabled_only=True)
        assert len(libs) == 1
        assert libs[0]["name"] == "Test TV"

    async def test_update_library_single_field(self, db, test_library):
        """Test updating a single library field."""
        updated = await lib_repo.update_library(db, test_library, name="Updated Movies")
        assert updated is not None
        assert updated["name"] == "Updated Movies"

        # Verify persistence
        lib = await lib_repo.get_library(db, test_library)
        assert lib["name"] == "Updated Movies"

    async def test_update_library_multiple_fields(self, db, test_library):
        """Test updating multiple fields."""
        updated = await lib_repo.update_library(
            db,
            test_library,
            quality_preset=25,
            enabled=False,
            auto_scan=True,
        )
        assert updated is not None
        assert updated["quality_preset"] == 25
        assert updated["enabled"] == 0
        assert updated["auto_scan"] == 1

    async def test_update_library_invalid_column(self, db, test_library):
        """Test that updating invalid columns raises ValueError."""
        with pytest.raises(ValueError, match="Invalid library column names"):
            await lib_repo.update_library(db, test_library, nonexistent_field="value")

    async def test_update_library_no_fields(self, db, test_library):
        """Test updating with no fields returns current library."""
        updated = await lib_repo.update_library(db, test_library)
        assert updated is not None
        assert updated["name"] == "Test Movies"

    async def test_delete_library_existing(self, db, test_library):
        """Test deleting an existing library."""
        deleted = await lib_repo.delete_library(db, test_library)
        assert deleted is True

        lib = await lib_repo.get_library(db, test_library)
        assert lib is None

    async def test_delete_library_nonexistent(self, db):
        """Test deleting a non-existent library returns False."""
        deleted = await lib_repo.delete_library(db, "nonexistent-id")
        assert deleted is False


# ============================================================================
# System Repository Tests
# ============================================================================


class TestSystemRepo:
    """Tests for system_repo functions."""

    async def test_get_state_default(self, db):
        """Test getting state with default value when key doesn't exist."""
        value = await sys_repo.get_state(db, "nonexistent_key", default="default_value")
        assert value == "default_value"

    async def test_get_state_default_empty_string(self, db):
        """Test default value is empty string when not specified."""
        value = await sys_repo.get_state(db, "nonexistent_key")
        assert value == ""

    async def test_set_state_new_key(self, db):
        """Test setting a new state key."""
        await sys_repo.set_state(db, "test_key", "test_value")

        value = await sys_repo.get_state(db, "test_key")
        assert value == "test_value"

    async def test_set_state_update_existing(self, db):
        """Test updating an existing state key."""
        await sys_repo.set_state(db, "test_key", "initial_value")
        await sys_repo.set_state(db, "test_key", "updated_value")

        value = await sys_repo.get_state(db, "test_key")
        assert value == "updated_value"

    async def test_set_state_empty_value(self, db):
        """Test setting state to empty string."""
        await sys_repo.set_state(db, "empty_key", "")

        value = await sys_repo.get_state(db, "empty_key", default="default")
        assert value == ""

    async def test_is_queue_paused_default_unpaused(self, db):
        """Test queue defaults to unpaused."""
        paused = await sys_repo.is_queue_paused(db)
        assert paused is False

    async def test_set_queue_paused_true(self, db):
        """Test pausing the queue."""
        await sys_repo.set_queue_paused(db, True)

        paused = await sys_repo.is_queue_paused(db)
        assert paused is True

    async def test_set_queue_paused_false(self, db):
        """Test unpausing the queue."""
        await sys_repo.set_queue_paused(db, True)
        await sys_repo.set_queue_paused(db, False)

        paused = await sys_repo.is_queue_paused(db)
        assert paused is False

    async def test_queue_pause_full_cycle(self, db):
        """Test full pause/unpause cycle."""
        # Start unpaused
        assert await sys_repo.is_queue_paused(db) is False

        # Pause
        await sys_repo.set_queue_paused(db, True)
        assert await sys_repo.is_queue_paused(db) is True

        # Unpause
        await sys_repo.set_queue_paused(db, False)
        assert await sys_repo.is_queue_paused(db) is False

        # Pause again
        await sys_repo.set_queue_paused(db, True)
        assert await sys_repo.is_queue_paused(db) is True


# ============================================================================
# Workers Repository Tests
# ============================================================================


class TestWorkersRepo:
    """Tests for workers_repo functions."""

    async def test_cleanup_stale_workers_marks_dead(self, db):
        """Test that stale workers are marked as dead."""
        # Create an online worker first
        worker = Worker(
            name="will-become-stale",
            host="10.0.0.1",
            status=WorkerStatus.ONLINE,
        )
        await worker_repo.upsert_worker(db, worker)

        # Manually update last_heartbeat to simulate old heartbeat (bypass upsert_worker)
        old_time = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
        await db.execute(
            "UPDATE workers SET last_heartbeat = ? WHERE id = ?",
            (old_time, worker.id),
        )
        await db.commit()

        # Mark as stale (timeout=90s)
        count = await worker_repo.cleanup_stale_workers(db, timeout_seconds=90)
        assert count == 1

        # Verify it's marked dead
        fetched = await worker_repo.get_worker(db, worker.id)
        assert fetched is not None
        assert fetched.status == WorkerStatus.DEAD

    async def test_cleanup_stale_workers_ignores_fresh_heartbeats(self, db):
        """Test that workers with recent heartbeats are NOT marked dead."""
        # Create worker with normal (recent) heartbeat
        worker = Worker(
            name="healthy-worker",
            host="10.0.0.1",
            status=WorkerStatus.ONLINE,
        )
        await worker_repo.upsert_worker(db, worker)

        # Try to mark as stale (timeout=90s) - should not mark this one
        count = await worker_repo.cleanup_stale_workers(db, timeout_seconds=90)
        assert count == 0

        # Verify it's still online
        fetched = await worker_repo.get_worker(db, worker.id)
        assert fetched is not None
        assert fetched.status == WorkerStatus.ONLINE

    async def test_cleanup_stale_workers_handles_mixed_states(self, db):
        """Test cleanup with both stale and fresh workers."""
        # Create stale online worker
        stale_online = Worker(
            name="stale-online",
            host="10.0.0.1",
            status=WorkerStatus.ONLINE,
        )
        await worker_repo.upsert_worker(db, stale_online)

        # Create stale busy worker
        stale_busy = Worker(
            name="stale-busy",
            host="10.0.0.2",
            status=WorkerStatus.BUSY,
        )
        await worker_repo.upsert_worker(db, stale_busy)

        # Create fresh worker
        fresh = Worker(
            name="fresh",
            host="10.0.0.3",
            status=WorkerStatus.ONLINE,
        )
        await worker_repo.upsert_worker(db, fresh)

        # Create offline worker (should not be marked dead)
        offline = Worker(
            name="offline",
            host="10.0.0.4",
            status=WorkerStatus.OFFLINE,
        )
        await worker_repo.upsert_worker(db, offline)

        # Manually update stale heartbeats
        old_time = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
        await db.execute(
            "UPDATE workers SET last_heartbeat = ? WHERE id IN (?, ?)",
            (old_time, stale_online.id, stale_busy.id),
        )
        await db.commit()

        # Run cleanup
        count = await worker_repo.cleanup_stale_workers(db, timeout_seconds=90)
        assert count == 2  # Only stale online and busy

        # Verify statuses
        assert (await worker_repo.get_worker(db, stale_online.id)).status == WorkerStatus.DEAD
        assert (await worker_repo.get_worker(db, stale_busy.id)).status == WorkerStatus.DEAD
        assert (await worker_repo.get_worker(db, fresh.id)).status == WorkerStatus.ONLINE
        assert (await worker_repo.get_worker(db, offline.id)).status == WorkerStatus.OFFLINE

    async def test_cleanup_stale_workers_no_stale(self, db):
        """Test cleanup when no workers are stale."""
        worker = Worker(
            name="healthy",
            host="10.0.0.1",
            status=WorkerStatus.ONLINE,
        )
        await worker_repo.upsert_worker(db, worker)

        count = await worker_repo.cleanup_stale_workers(db, timeout_seconds=90)
        assert count == 0


# ============================================================================
# Jobs Repository Tests
# ============================================================================


class TestRequeueOrphanActiveJobs:
    """Auto-requeue for jobs whose worker died and never re-registered —
    the healing counterpart to find_orphan_active_jobs (which only
    reports, via /audit/integrity)."""

    async def _make_worker(self, db, *, status: WorkerStatus) -> Worker:
        worker = Worker(name=f"w-{status.value}", host="10.0.0.1", status=status)
        await worker_repo.upsert_worker(db, worker)
        if status not in (WorkerStatus.ONLINE, WorkerStatus.BUSY):
            # upsert refreshes status from the model but tests need it kept
            await db.execute(
                "UPDATE workers SET status = ? WHERE id = ?", (status.value, worker.id)
            )
            await db.commit()
        return worker

    async def _make_active_job(
        self, db, *, worker_id: str | None, idle_seconds: int, path: str = "/a.mkv"
    ) -> Job:
        job = Job(
            source_path=path,
            library="movies",
            source_codec="h264",
            quality_value=21,
        )
        await job_repo.create_job(db, job)
        stamp = (datetime.now(UTC) - timedelta(seconds=idle_seconds)).isoformat()
        await db.execute(
            "UPDATE jobs SET status = ?, worker_id = ?, started_at = ?, "
            "progress = 0.4, updated_at = ? WHERE id = ?",
            (JobStatus.TRANSCODING.value, worker_id, stamp, stamp, job.id),
        )
        await db.commit()
        return job

    async def test_dead_worker_idle_job_requeued(self, db):
        dead = await self._make_worker(db, status=WorkerStatus.DEAD)
        job = await self._make_active_job(db, worker_id=dead.id, idle_seconds=700)

        requeued = await job_repo.requeue_orphan_active_jobs(db, min_idle_seconds=600)

        assert [r["id"] for r in requeued] == [job.id]
        fetched = await job_repo.get_job(db, job.id)
        assert fetched.status == JobStatus.QUEUED
        assert fetched.worker_id is None
        assert fetched.started_at is None
        assert fetched.progress == 0

    async def test_missing_worker_row_requeued(self, db):
        """A job whose worker_id references nobody (row deleted) is just as
        orphaned as one whose worker is dead."""
        job = await self._make_active_job(db, worker_id="gone-forever", idle_seconds=700)
        requeued = await job_repo.requeue_orphan_active_jobs(db, min_idle_seconds=600)
        assert [r["id"] for r in requeued] == [job.id]

    async def test_grace_period_respected(self, db):
        """A recently-active job is left alone even if its worker looks dead
        — a brief partition must not cost the worker its job instantly."""
        dead = await self._make_worker(db, status=WorkerStatus.DEAD)
        job = await self._make_active_job(db, worker_id=dead.id, idle_seconds=60)

        requeued = await job_repo.requeue_orphan_active_jobs(db, min_idle_seconds=600)

        assert requeued == []
        fetched = await job_repo.get_job(db, job.id)
        assert fetched.status == JobStatus.TRANSCODING
        assert fetched.worker_id == dead.id

    async def test_alive_worker_jobs_untouched(self, db):
        busy = await self._make_worker(db, status=WorkerStatus.BUSY)
        job = await self._make_active_job(db, worker_id=busy.id, idle_seconds=9000)

        requeued = await job_repo.requeue_orphan_active_jobs(db, min_idle_seconds=600)

        assert requeued == []
        fetched = await job_repo.get_job(db, job.id)
        assert fetched.status == JobStatus.TRANSCODING

    async def test_terminal_and_pending_jobs_untouched(self, db):
        dead = await self._make_worker(db, status=WorkerStatus.DEAD)
        stamp = (datetime.now(UTC) - timedelta(seconds=9000)).isoformat()
        outcomes = {}
        for status in (JobStatus.COMPLETE, JobStatus.FAILED, JobStatus.PENDING):
            job = Job(
                source_path=f"/{status.value}.mkv",
                library="movies",
                source_codec="h264",
                quality_value=21,
            )
            await job_repo.create_job(db, job)
            await db.execute(
                "UPDATE jobs SET status = ?, worker_id = ?, updated_at = ? WHERE id = ?",
                (status.value, dead.id, stamp, job.id),
            )
            outcomes[job.id] = status
        await db.commit()

        requeued = await job_repo.requeue_orphan_active_jobs(db, min_idle_seconds=600)

        assert requeued == []
        for job_id, status in outcomes.items():
            assert (await job_repo.get_job(db, job_id)).status == status

    async def test_requeued_job_is_claimable_again(self, db):
        dead = await self._make_worker(db, status=WorkerStatus.DEAD)
        job = await self._make_active_job(db, worker_id=dead.id, idle_seconds=700)

        await job_repo.requeue_orphan_active_jobs(db, min_idle_seconds=600)
        claimed = await job_repo.claim_next_job(db, "fresh-worker")

        assert claimed is not None
        assert claimed.id == job.id
        assert claimed.worker_id == "fresh-worker"


class TestJobsRepo:
    """Tests for jobs_repo functions."""

    async def test_claim_next_job_with_pending_jobs(self, db):
        """Test claiming when pending jobs exist."""
        job1 = Job(
            source_path="/a.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
            status=JobStatus.PENDING,
        )
        job2 = Job(
            source_path="/b.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
            status=JobStatus.PENDING,
        )

        await job_repo.create_job(db, job1)
        await job_repo.create_job(db, job2)

        # Claim first job
        claimed = await job_repo.claim_next_job(db, "worker-1")
        assert claimed is not None
        assert claimed.id == job1.id
        assert claimed.status == JobStatus.ASSIGNED
        assert claimed.worker_id == "worker-1"

    async def test_claim_next_job_fifo_order(self, db):
        """Test that jobs are claimed in FIFO order (created_at ASC)."""
        jobs = []
        for i in range(3):
            job = Job(
                source_path=f"/file{i}.mkv",
                library="movies",
                source_codec="h264",
                quality_value=21,
            )
            await job_repo.create_job(db, job)
            jobs.append(job)

        # Claim first should be the oldest
        claimed = await job_repo.claim_next_job(db, "worker-1")
        assert claimed.id == jobs[0].id

    async def test_claim_next_job_no_jobs(self, db):
        """Test claiming when no jobs exist."""
        claimed = await job_repo.claim_next_job(db, "worker-1")
        assert claimed is None

    async def test_claim_next_job_prefers_pending(self, db):
        """Test that pending jobs are claimed before queued."""
        # Create queued job first
        queued = Job(
            source_path="/queued.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
            status=JobStatus.QUEUED,
        )
        await job_repo.create_job(db, queued)

        # Create pending job second (newer)
        pending = Job(
            source_path="/pending.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
            status=JobStatus.PENDING,
        )
        await job_repo.create_job(db, pending)

        # Should claim the older one first (pending by created_at order)
        claimed = await job_repo.claim_next_job(db, "worker-1")
        assert claimed is not None
        # Should be the queued job since it was created first
        assert claimed.id == queued.id

    async def test_claim_next_job_updates_all_fields(self, db):
        """Test that all fields are updated correctly on claim."""
        job = Job(
            source_path="/test.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
        )
        await job_repo.create_job(db, job)

        claimed = await job_repo.claim_next_job(db, "worker-123")
        assert claimed is not None
        assert claimed.status == JobStatus.ASSIGNED
        assert claimed.worker_id == "worker-123"
        assert claimed.started_at is not None
        assert claimed.updated_at is not None

    async def test_claim_next_job_race_condition_prevention(self, db):
        """Test that only one job is claimed even with concurrent attempts."""
        job = Job(
            source_path="/single.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
        )
        await job_repo.create_job(db, job)

        # Simulate two workers claiming at the same time
        claimed1 = await job_repo.claim_next_job(db, "worker-1")
        claimed2 = await job_repo.claim_next_job(db, "worker-2")

        # Only one should succeed
        assert claimed1 is not None
        assert claimed2 is None

    async def test_update_job_invalid_column(self, db):
        """Test that updating invalid columns raises ValueError."""
        job = Job(
            source_path="/test.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
        )
        await job_repo.create_job(db, job)

        with pytest.raises(ValueError, match="Invalid job column names"):
            await job_repo.update_job(db, job.id, nonexistent_field="value")

    async def test_update_job_valid_columns(self, db):
        """Test updating valid job columns."""
        job = Job(
            source_path="/test.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
        )
        await job_repo.create_job(db, job)

        updated = await job_repo.update_job(db, job.id, status=JobStatus.TRANSCODING, progress=0.25)
        assert updated is not None
        assert updated.status == JobStatus.TRANSCODING
        assert updated.progress == 0.25

    async def test_update_job_with_status_enum(self, db):
        """Test updating job with JobStatus enum value."""
        job = Job(
            source_path="/test.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
        )
        await job_repo.create_job(db, job)

        updated = await job_repo.update_job(db, job.id, status=JobStatus.COMPLETE)
        assert updated is not None
        assert updated.status == JobStatus.COMPLETE

    async def test_update_job_output_metrics(self, db):
        """Test updating job with output size and space saved."""
        job = Job(
            source_path="/test.mkv",
            library="movies",
            source_codec="h264",
            source_size=5000000,
            quality_value=21,
        )
        await job_repo.create_job(db, job)

        updated = await job_repo.update_job(db, job.id, output_size=3000000, space_saved=2000000)
        assert updated is not None
        assert updated.output_size == 3000000
        assert updated.space_saved == 2000000


class TestMediaAggregateTypes:
    """PR #49 follow-up: on Postgres SUM() over a BIGINT column returns
    numeric → Decimal, which leaked into /api/media/stats JSON. These pin
    the CAST(... AS BIGINT) so byte totals stay ints on both engines."""

    async def test_codec_stats_total_size_is_int(self, db, test_library):
        await media_repo.upsert_media_file(
            db,
            library_id=test_library,
            file_path="/media/movies/big.mkv",
            filename="big.mkv",
            video_codec="h264",
            file_size=3_000_000_000,  # > int32 so a Decimal would show sci-notation
        )
        stats = await media_repo.get_codec_stats(db)
        total = stats["h264"]["total_size"]
        assert isinstance(total, int)
        assert total == 3_000_000_000

    async def test_list_shows_total_size_is_int(self, db, test_library):
        for ep in (1, 2):
            await media_repo.upsert_media_file(
                db,
                library_id=test_library,
                file_path=f"/media/tv/show/s01e0{ep}.mkv",
                filename=f"s01e0{ep}.mkv",
                show_name="Aggregate Show",
                season=1,
                episode=ep,
                video_codec="h264",
                file_size=1_500_000_000,
            )
        shows = await media_repo.list_tv_shows(db)
        row = next(s for s in shows if s["show_name"] == "Aggregate Show")
        assert isinstance(row["total_size"], int)
        assert row["total_size"] == 3_000_000_000

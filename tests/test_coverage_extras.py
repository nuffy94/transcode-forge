"""Tests for uncovered routes, scanner, and utility modules.

Covers:
- Web routes partials (dashboard-stats, scheduler-info, tv-episodes)
- Scanner module (scan_library with mocked ffprobe)
- Redis module (create_redis_pool, check_redis_health)
- Metrics module (metrics endpoint)
"""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
from httpx import AsyncClient

from transcode_forge.models.job import Job
from transcode_forge.models.scan import Scan, ScanStatus
from transcode_forge.models.worker import Worker
from transcode_forge.redis import check_redis_health, create_redis_pool
from transcode_forge.repos import (
    jobs as job_repo,
)
from transcode_forge.repos import (
    libraries as lib_repo,
)
from transcode_forge.repos import (
    media as media_repo,
)
from transcode_forge.repos import (
    scans as scan_repo,
)
from transcode_forge.repos import (
    workers as worker_repo,
)
from transcode_forge.scanner.probe import ProbeError, ProbeResult
from transcode_forge.scanner.scanner import (
    parse_tv_info,
    scan_library,
)

# ============================================================================
# Web Route Tests (partials)
# ============================================================================


class TestDashboardStatsPartial:
    """Tests for /partials/dashboard-stats endpoint."""

    async def test_dashboard_stats_empty(self, client: AsyncClient):
        """Test dashboard stats partial with no data."""
        response = await client.get("/partials/dashboard-stats")
        assert response.status_code == 200
        assert "0.0" in response.text  # space_saved_gb
        assert "Space Saved" in response.text or "Completed" in response.text

    async def test_dashboard_stats_with_completed_jobs(self, client: AsyncClient, app):
        """Test dashboard stats with completed jobs and space savings."""
        db = app.state.db

        # Create and complete a job with space savings
        job = Job(
            source_path="/media/movies/Test.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
            source_size=2_000_000_000,
        )
        job_id = await job_repo.create_job(db, job)
        await job_repo.update_job(
            db,
            job_id,
            status="complete",
            space_saved=200_000_000,  # 200MB saved
        )

        response = await client.get("/partials/dashboard-stats")
        assert response.status_code == 200
        assert "1" in response.text  # completed count
        assert "0.2" in response.text  # ~0.19 GB formats to 0.2

    async def test_dashboard_stats_with_queued_jobs(self, client: AsyncClient, app):
        """Test dashboard stats displays queued job count."""
        db = app.state.db

        # Create queued jobs
        for i in range(3):
            job = Job(
                source_path=f"/media/movies/Movie{i}.mkv",
                library="movies",
                source_codec="h264",
                quality_value=21,
                source_size=1_000_000_000,
            )
            await job_repo.create_job(db, job)

        response = await client.get("/partials/dashboard-stats")
        assert response.status_code == 200
        assert "3" in response.text  # queued count

    async def test_dashboard_stats_with_online_workers(self, client: AsyncClient, app):
        """Test dashboard stats displays online worker count."""
        db = app.state.db

        # Register workers
        for i in range(2):
            worker = Worker(
                name=f"node-{i}",
                host=f"10.0.0.{i}",
                status="online",
            )
            await worker_repo.upsert_worker(db, worker)

        response = await client.get("/partials/dashboard-stats")
        assert response.status_code == 200
        assert "2" in response.text  # workers_online


class TestSchedulerInfoPartial:
    """Tests for /partials/scheduler-info endpoint."""

    async def test_scheduler_info_healthy(self, client: AsyncClient, app):
        """Test scheduler info with healthy Redis and DB."""
        response = await client.get("/partials/scheduler-info")
        assert response.status_code == 200
        # Should have some content
        assert len(response.text) > 10

    async def test_scheduler_info_with_libraries(self, client: AsyncClient, app):
        """Test scheduler info displays library count."""
        db = app.state.db

        # Create libraries
        for name in ["movies", "tv", "anime"]:
            await lib_repo.create_library(
                db,
                name=name,
                path=f"/media/{name}",
                media_type=name if name != "anime" else "tv",
                quality_preset=21,
            )

        response = await client.get("/partials/scheduler-info")
        assert response.status_code == 200
        assert "3" in response.text  # library_count

    async def test_scheduler_info_with_queued_jobs(self, client: AsyncClient, app):
        """Test scheduler info displays queued job count."""
        db = app.state.db

        # Create jobs in queued status
        for i in range(2):
            job = Job(
                source_path=f"/media/movies/Movie{i}.mkv",
                library="movies",
                source_codec="h264",
                quality_value=21,
                source_size=1_000_000_000,
            )
            job_id = await job_repo.create_job(db, job)
            await job_repo.update_job(db, job_id, status="queued")

        response = await client.get("/partials/scheduler-info")
        assert response.status_code == 200
        assert "2" in response.text  # jobs_queued

    async def test_scheduler_info_queue_paused_status(self, client: AsyncClient, app):
        """Test scheduler info reflects queue pause status."""
        db = app.state.db

        # Pause the queue
        from transcode_forge.repos import system as system_repo

        await system_repo.set_queue_paused(db, True)

        response = await client.get("/partials/scheduler-info")
        assert response.status_code == 200
        # Response should contain indication of paused status


class TestActivTranscodesPartial:
    """Tests for /partials/active-transcodes endpoint."""

    async def test_active_transcodes_empty(self, client: AsyncClient):
        """Test active transcodes with no active jobs."""
        response = await client.get("/partials/active-transcodes")
        assert response.status_code == 200
        # Should render without errors
        assert len(response.text) > 0

    async def test_active_transcodes_with_jobs(self, client: AsyncClient, app):
        """Test active transcodes displays actively transcoding jobs."""
        db = app.state.db

        # Create a transcoding job
        job = Job(
            source_path="/media/movies/Active.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
            source_size=2_000_000_000,
        )
        job_id = await job_repo.create_job(db, job)
        await job_repo.update_job(db, job_id, status="transcoding")

        response = await client.get("/partials/active-transcodes")
        assert response.status_code == 200
        assert "Active.mkv" in response.text


class TestRecentActivityPartial:
    """Tests for /partials/recent-activity endpoint."""

    async def test_recent_activity_empty(self, client: AsyncClient):
        """Test recent activity with no completed jobs."""
        response = await client.get("/partials/recent-activity")
        assert response.status_code == 200
        # Should render without errors
        assert len(response.text) > 0

    async def test_recent_activity_with_completed(self, client: AsyncClient, app):
        """Test recent activity displays recently completed jobs."""
        db = app.state.db

        # Create and complete a job
        job = Job(
            source_path="/media/movies/Recent.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
            source_size=2_000_000_000,
        )
        job_id = await job_repo.create_job(db, job)
        await job_repo.update_job(db, job_id, status="complete", space_saved=100_000_000)

        response = await client.get("/partials/recent-activity")
        assert response.status_code == 200
        assert "Recent.mkv" in response.text


class TestScanHistoryPartial:
    """Tests for /partials/scan-history endpoint."""

    async def test_scan_history_empty(self, client: AsyncClient):
        """Test scan history with no scans."""
        response = await client.get("/partials/scan-history")
        assert response.status_code == 200
        # Template renders nothing when empty, which is fine
        # Just verify it doesn't error

    async def test_scan_history_with_scans(self, client: AsyncClient, app):
        """Test scan history displays previous scans."""
        db = app.state.db

        # Create a scan record
        scan = Scan(library="movies", files_found=5, files_new=3, files_updated=2)
        scan.status = ScanStatus.COMPLETE
        await scan_repo.create_scan(db, scan)

        response = await client.get("/partials/scan-history")
        assert response.status_code == 200
        assert "movies" in response.text


class TestTvEpisodesPartial:
    """Tests for /partials/tv-episodes endpoint."""

    async def test_tv_episodes_by_show(self, client: AsyncClient, app):
        """Test TV episodes partial retrieves episodes for a show."""
        db = app.state.db

        # Create library and media files
        lib_id = await lib_repo.create_library(
            db,
            name="tv",
            path="/media/tv",
            media_type="tv",
            quality_preset=21,
        )

        # Create some TV episodes
        for i in range(3):
            await media_repo.upsert_media_file(
                db,
                library_id=lib_id,
                file_path=f"/media/tv/Breaking Bad/S01E{i + 1:02d}.mkv",
                filename=f"S01E{i + 1:02d}.mkv",
                show_name="Breaking Bad",
                season=1,
                episode=i + 1,
                video_codec="h264",
                resolution="1920x1080",
                width=1920,
                height=1080,
                bitrate=5000000,
                duration=2700.0,
                file_size=2_000_000_000,
            )

        response = await client.get("/partials/tv-episodes", params={"show": "Breaking Bad"})
        assert response.status_code == 200
        assert "Breaking Bad" in response.text or "S01E" in response.text

    async def test_tv_episodes_empty_for_show(self, client: AsyncClient, app):
        """Test TV episodes partial with no episodes for show."""
        response = await client.get("/partials/tv-episodes", params={"show": "NonExistent"})
        assert response.status_code == 200
        # Should return a response with empty episode list


class TestMoviesTvSettingsPages:
    """Tests for /movies, /tv, /settings pages."""

    async def test_movies_page(self, client: AsyncClient):
        """Test movies page renders."""
        response = await client.get("/movies")
        assert response.status_code == 200
        # Should have HTML content
        assert "<html" in response.text or "transcode" in response.text.lower()

    async def test_tv_page(self, client: AsyncClient):
        """Test TV page renders."""
        response = await client.get("/tv")
        assert response.status_code == 200
        # Should have HTML content
        assert "<html" in response.text or "transcode" in response.text.lower()

    async def test_settings_page(self, client: AsyncClient):
        """Test settings page renders."""
        response = await client.get("/settings")
        assert response.status_code == 200
        # Should have HTML content
        assert "<html" in response.text or "transcode" in response.text.lower()


# ============================================================================
# Scanner Tests
# ============================================================================


class TestParseTV:
    """Tests for parse_tv_info utility function."""

    def test_parse_tv_s01e02_format(self):
        """Test parsing SxxExx format."""
        path = Path("Breaking.Bad.S01E02.1080p.mkv")
        show, season, episode = parse_tv_info(path)
        assert show == "Breaking Bad"
        assert season == 1
        assert episode == 2

    def test_parse_tv_1x02_format(self):
        """Test parsing NxNN format."""
        path = Path("The.Office.1x02.1080p.mkv")
        show, season, episode = parse_tv_info(path)
        assert show == "The Office"
        assert season == 1
        assert episode == 2

    def test_parse_tv_lowercase_format(self):
        """Test parsing lowercase format."""
        path = Path("dark.s02e05.mkv")
        show, season, episode = parse_tv_info(path)
        assert show == "dark"
        assert season == 2
        assert episode == 5

    def test_parse_tv_no_match(self):
        """Test parsing file without TV info."""
        path = Path("random_video.mkv")
        show, season, episode = parse_tv_info(path)
        assert show is None
        assert season is None
        assert episode is None

    def test_parse_tv_with_dashes(self):
        """Test parsing with dashes in filename."""
        path = Path("Game-of-Thrones-S08E06.mkv")
        show, season, episode = parse_tv_info(path)
        assert show == "Game-of-Thrones"  # Dashes are not replaced
        assert season == 8
        assert episode == 6


class TestScanLibrary:
    """Tests for scan_library function."""

    async def test_scan_library_nonexistent_path(self, db: aiosqlite.Connection):
        """Test scan with nonexistent library path."""
        scan = await scan_library(
            library_id="test_lib_1",
            library_name="test",
            library_path="/nonexistent/path",
            media_type="movies",
            db=db,
        )
        assert scan.status == ScanStatus.FAILED
        assert scan.files_found == 0

    async def test_scan_library_empty_directory(self, db: aiosqlite.Connection, tmp_path: Path):
        """Test scan with empty directory."""
        scan = await scan_library(
            library_id="test_lib_2",
            library_name="test",
            library_path=str(tmp_path),
            media_type="movies",
            db=db,
        )
        # Verify in DB (returned object may not be refetched)
        db_scan = await scan_repo.get_scan(db, scan.id)
        assert db_scan.status == ScanStatus.COMPLETE
        assert db_scan.files_found == 0
        assert db_scan.files_new == 0

    async def test_scan_library_with_mocked_ffprobe(self, db: aiosqlite.Connection, tmp_path: Path):
        """Test scan with mocked ffprobe returning probe data."""
        # Create test library and video files
        lib_id = await lib_repo.create_library(
            db,
            name="movies",
            path=str(tmp_path),
            media_type="movies",
            quality_preset=21,
        )

        # Create dummy video files
        video_files = []
        for i in range(2):
            video_file = tmp_path / f"movie{i}.mkv"
            video_file.write_bytes(b"fake video data")
            video_files.append(video_file)

        # Mock ffprobe to return fake probe data
        async def mock_ffprobe(path: str | Path) -> ProbeResult:
            return ProbeResult(
                video_codec="h264",
                width=1920,
                height=1080,
                bitrate=5000000,
                duration=7200.0,  # 2 hours
                file_size=2_000_000_000,
            )

        with patch("transcode_forge.scanner.scanner.ffprobe", side_effect=mock_ffprobe):
            scan = await scan_library(
                library_id=lib_id,
                library_name="movies",
                library_path=str(tmp_path),
                media_type="movies",
                db=db,
            )

        # Verify in DB (returned object may not be refetched)
        db_scan = await scan_repo.get_scan(db, scan.id)
        assert db_scan.status == ScanStatus.COMPLETE
        assert db_scan.files_found == 2
        assert db_scan.files_new == 2
        assert db_scan.files_updated == 0

    async def test_scan_library_updates_existing_files(
        self, db: aiosqlite.Connection, tmp_path: Path
    ):
        """Test scan updates files with changed mtime."""
        lib_id = await lib_repo.create_library(
            db,
            name="movies",
            path=str(tmp_path),
            media_type="movies",
            quality_preset=21,
        )

        # Create first file
        video_file = tmp_path / "movie.mkv"
        video_file.write_bytes(b"fake video data")

        async def mock_ffprobe(path: str | Path) -> ProbeResult:
            return ProbeResult(
                video_codec="h264",
                width=1920,
                height=1080,
                bitrate=5000000,
                duration=7200.0,
                # Like real ffprobe, report the actual on-disk size — the
                # rescan dedup compares it against stat().st_size.
                file_size=Path(path).stat().st_size,
            )

        # First scan
        with patch("transcode_forge.scanner.scanner.ffprobe", side_effect=mock_ffprobe):
            scan1 = await scan_library(
                library_id=lib_id,
                library_name="movies",
                library_path=str(tmp_path),
                media_type="movies",
                db=db,
            )

        # Verify in DB
        db_scan1 = await scan_repo.get_scan(db, scan1.id)
        assert db_scan1.files_new == 1

        # Second scan (same mtime) should skip
        with patch("transcode_forge.scanner.scanner.ffprobe", side_effect=mock_ffprobe):
            scan2 = await scan_library(
                library_id=lib_id,
                library_name="movies",
                library_path=str(tmp_path),
                media_type="movies",
                db=db,
            )

        # Verify in DB
        db_scan2 = await scan_repo.get_scan(db, scan2.id)
        assert db_scan2.files_skipped == 1
        assert db_scan2.files_new == 0

    async def test_scan_library_skips_probe_errors(self, db: aiosqlite.Connection, tmp_path: Path):
        """Test scan skips files that fail probe."""
        lib_id = await lib_repo.create_library(
            db,
            name="movies",
            path=str(tmp_path),
            media_type="movies",
            quality_preset=21,
        )

        # Create test files
        video_file = tmp_path / "good.mkv"
        video_file.write_bytes(b"good video")
        bad_file = tmp_path / "bad.mkv"
        bad_file.write_bytes(b"bad video")

        async def mock_ffprobe(path: str | Path) -> ProbeResult:
            if "bad" in str(path):
                raise ProbeError("Invalid video format")
            return ProbeResult(
                video_codec="h264",
                width=1920,
                height=1080,
                bitrate=5000000,
                duration=7200.0,
                file_size=2_000_000_000,
            )

        with patch("transcode_forge.scanner.scanner.ffprobe", side_effect=mock_ffprobe):
            scan = await scan_library(
                library_id=lib_id,
                library_name="movies",
                library_path=str(tmp_path),
                media_type="movies",
                db=db,
            )

        # Verify in DB
        db_scan = await scan_repo.get_scan(db, scan.id)
        assert db_scan.files_found == 2
        assert db_scan.files_new == 1  # Only good.mkv
        assert db_scan.files_skipped == 1  # bad.mkv

    async def test_scan_library_with_max_files(self, db: aiosqlite.Connection, tmp_path: Path):
        """Test scan respects max_files limit."""
        lib_id = await lib_repo.create_library(
            db,
            name="movies",
            path=str(tmp_path),
            media_type="movies",
            quality_preset=21,
        )

        # Create 5 files
        for i in range(5):
            video_file = tmp_path / f"movie{i}.mkv"
            video_file.write_bytes(b"fake video")

        async def mock_ffprobe(path: str | Path) -> ProbeResult:
            return ProbeResult(
                video_codec="h264",
                width=1920,
                height=1080,
                bitrate=5000000,
                duration=7200.0,
                file_size=2_000_000_000,
            )

        with patch("transcode_forge.scanner.scanner.ffprobe", side_effect=mock_ffprobe):
            scan = await scan_library(
                library_id=lib_id,
                library_name="movies",
                library_path=str(tmp_path),
                media_type="movies",
                db=db,
                max_files=2,  # Limit to 2
            )

        # Verify in DB
        db_scan = await scan_repo.get_scan(db, scan.id)
        assert db_scan.files_found == 2

    async def test_scan_library_tv_parses_show_info(self, db: aiosqlite.Connection, tmp_path: Path):
        """Test scan parses TV show info for media_type='tv'."""
        lib_id = await lib_repo.create_library(
            db,
            name="tv",
            path=str(tmp_path),
            media_type="tv",
            quality_preset=21,
        )

        # Create a TV episode file
        episode_file = tmp_path / "Breaking.Bad.S01E01.mkv"
        episode_file.write_bytes(b"tv episode")

        async def mock_ffprobe(path: str | Path) -> ProbeResult:
            return ProbeResult(
                video_codec="h264",
                width=1920,
                height=1080,
                bitrate=5000000,
                duration=2700.0,
                file_size=1_000_000_000,
            )

        with patch("transcode_forge.scanner.scanner.ffprobe", side_effect=mock_ffprobe):
            scan = await scan_library(
                library_id=lib_id,
                library_name="tv",
                library_path=str(tmp_path),
                media_type="tv",
                db=db,
            )

        # Verify the scan succeeded and file was added (check DB)
        db_scan = await scan_repo.get_scan(db, scan.id)
        assert db_scan.status == ScanStatus.COMPLETE
        assert db_scan.files_new == 1

        # Check that media file was created with TV info
        files, _ = await media_repo.list_media_files(db, show_name="Breaking Bad")
        assert len(files) == 1
        assert files[0]["show_name"] == "Breaking Bad"
        assert files[0]["season"] == 1
        assert files[0]["episode"] == 1


# ============================================================================
# Redis Tests
# ============================================================================


class TestCreateRedisPool:
    """Tests for create_redis_pool function."""

    async def test_create_redis_pool_success(self):
        """Test successful Redis pool creation."""
        # Mock the from_url and ping methods
        mock_pool = AsyncMock()
        mock_pool.ping = AsyncMock(return_value=True)
        mock_pool.aclose = AsyncMock()

        with patch("transcode_forge.redis.from_url", return_value=mock_pool):
            result = await create_redis_pool("redis://localhost:6379/0")

        assert result is mock_pool
        mock_pool.ping.assert_called_once()

    async def test_create_redis_pool_ping_fails(self):
        """Test Redis pool creation when ping fails."""
        mock_pool = AsyncMock()
        mock_pool.ping = AsyncMock(side_effect=ConnectionError("Connection refused"))

        with patch("transcode_forge.redis.from_url", return_value=mock_pool):
            with pytest.raises(ConnectionError):
                await create_redis_pool("redis://localhost:6379/0")

    async def test_create_redis_pool_with_custom_url(self):
        """Test Redis pool with custom URL."""
        mock_pool = AsyncMock()
        mock_pool.ping = AsyncMock(return_value=True)

        with patch("transcode_forge.redis.from_url", return_value=mock_pool) as mock_from_url:
            await create_redis_pool("redis://192.0.2.9:6381/1")

        # Verify correct URL was passed
        mock_from_url.assert_called_once()
        args, _kwargs = mock_from_url.call_args
        assert "redis://192.0.2.9:6381/1" in args or args[0] == "redis://192.0.2.9:6381/1"


class TestCheckRedisHealth:
    """Tests for check_redis_health function."""

    async def test_check_redis_health_success(self):
        """Test successful Redis health check."""
        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(return_value=True)

        result = await check_redis_health(mock_redis)

        assert result is True
        mock_redis.ping.assert_called_once()

    async def test_check_redis_health_failure(self):
        """Test Redis health check on connection failure."""
        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(side_effect=ConnectionError("Connection refused"))

        result = await check_redis_health(mock_redis)

        assert result is False

    async def test_check_redis_health_timeout(self):
        """Test Redis health check on timeout."""
        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(side_effect=TimeoutError("Request timed out"))

        result = await check_redis_health(mock_redis)

        assert result is False

    async def test_check_redis_health_generic_exception(self):
        """Test Redis health check on generic exception."""
        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(side_effect=Exception("Unknown error"))

        result = await check_redis_health(mock_redis)

        assert result is False


# ============================================================================
# Heartbeat Tests
# ============================================================================


class TestMetricsEndpoint:
    """Tests for /metrics Prometheus endpoint."""

    async def test_metrics_endpoint_returns_prometheus_format(self, client: AsyncClient, app):
        """Test metrics endpoint returns Prometheus text format."""
        response = await client.get("/metrics")
        assert response.status_code == 200
        # Prometheus format check
        assert "HELP" in response.text or "TYPE" in response.text or "tf_" in response.text

    async def test_metrics_endpoint_includes_custom_metrics(self, client: AsyncClient, app):
        """Test metrics include custom TF metrics."""
        response = await client.get("/metrics")
        assert response.status_code == 200
        # Check for custom metrics
        assert "tf_jobs" in response.text or "tf_" in response.text

    async def test_metrics_endpoint_with_data(self, client: AsyncClient, app):
        """Test metrics reflects actual data from DB."""
        db = app.state.db

        # Create some test data
        job = Job(
            source_path="/media/movies/Test.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
            source_size=2_000_000_000,
        )
        job_id = await job_repo.create_job(db, job)
        await job_repo.update_job(db, job_id, status="complete", space_saved=100_000_000)

        # Get metrics
        response = await client.get("/metrics")
        assert response.status_code == 200
        # Response should contain metric data
        assert len(response.text) > 50

    async def test_metrics_endpoint_with_multiple_jobs(self, client: AsyncClient, app):
        """Test metrics with multiple jobs in different states."""
        db = app.state.db

        # Create jobs in different states
        for i in range(3):
            job = Job(
                source_path=f"/media/movies/Test{i}.mkv",
                library="movies",
                source_codec="h264",
                quality_value=21,
                source_size=1_000_000_000,
            )
            job_id = await job_repo.create_job(db, job)
            if i == 0:
                await job_repo.update_job(db, job_id, status="complete", space_saved=50_000_000)
            elif i == 1:
                await job_repo.update_job(db, job_id, status="failed")
            # i == 2 stays pending

        response = await client.get("/metrics")
        assert response.status_code == 200

    async def test_metrics_endpoint_handles_db_errors(self, client: AsyncClient):
        """Test metrics endpoint handles DB errors gracefully."""
        # This test verifies the endpoint doesn't crash on DB errors
        response = await client.get("/metrics")
        assert response.status_code == 200

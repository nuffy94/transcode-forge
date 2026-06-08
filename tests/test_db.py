"""Tests for database repository layer and connection management."""

from unittest.mock import AsyncMock, patch

import pytest

from transcode_forge.models.job import Job, JobStatus
from transcode_forge.models.scan import Scan, ScanStatus
from transcode_forge.models.skipped import SkipReason
from transcode_forge.models.worker import Worker, WorkerStatus
from transcode_forge.repos import jobs as job_repo
from transcode_forge.repos import scans as scan_repo
from transcode_forge.repos import skipped as skip_repo
from transcode_forge.repos import workers as worker_repo


class TestTransaction:
    """db.transaction() — commit on success, roll back on error."""

    async def test_commits_on_success(self, db):
        job = Job(source_path="/tx/ok.mkv", library="movies", source_codec="h264", quality_value=21)
        async with db.transaction() as tx:
            await job_repo.create_job(tx, job)
        assert await job_repo.get_job(db, job.id) is not None

    async def test_rolls_back_on_error(self, db):
        job = Job(
            source_path="/tx/bad.mkv", library="movies", source_codec="h264", quality_value=21
        )
        with pytest.raises(ValueError, match="boom"):
            async with db.transaction() as tx:
                await job_repo.create_job(tx, job)
                raise ValueError("boom")
        # The create inside the failed block must not have persisted.
        assert await job_repo.get_job(db, job.id) is None


class TestJobRepo:
    async def test_create_and_get_job(self, db):
        job = Job(
            source_path="/media/movies/test.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
        )
        job_id = await job_repo.create_job(db, job)
        assert job_id == job.id

        fetched = await job_repo.get_job(db, job_id)
        assert fetched is not None
        assert fetched.source_path == "/media/movies/test.mkv"
        assert fetched.status == JobStatus.PENDING

    async def test_get_nonexistent_job(self, db):
        result = await job_repo.get_job(db, "nonexistent-id")
        assert result is None

    async def test_list_jobs_empty(self, db):
        jobs, total = await job_repo.list_jobs(db)
        assert jobs == []
        assert total == 0

    async def test_list_jobs_with_filter(self, db):
        job1 = Job(source_path="/a.mkv", library="movies", source_codec="h264", quality_value=21)
        job2 = Job(
            source_path="/b.mkv",
            library="tv",
            source_codec="h264",
            quality_value=24,
            status=JobStatus.COMPLETE,
        )
        await job_repo.create_job(db, job1)
        await job_repo.create_job(db, job2)

        # Filter by library
        movies, _ = await job_repo.list_jobs(db, library="movies")
        assert len(movies) == 1
        assert movies[0].library == "movies"

        # Filter by status
        complete, _ = await job_repo.list_jobs(db, status="complete")
        assert len(complete) == 1

    async def test_list_jobs_pagination(self, db):
        for i in range(10):
            job = Job(
                source_path=f"/file{i}.mkv",
                library="movies",
                source_codec="h264",
                quality_value=21,
            )
            await job_repo.create_job(db, job)

        page1, total = await job_repo.list_jobs(db, limit=3, offset=0)
        assert len(page1) == 3
        assert total == 10

        page2, _ = await job_repo.list_jobs(db, limit=3, offset=3)
        assert len(page2) == 3
        # Ensure no overlap
        ids1 = {j.id for j in page1}
        ids2 = {j.id for j in page2}
        assert ids1.isdisjoint(ids2)

    async def test_list_jobs_sort_by_source_path(self, db):
        for p in ("/c.mkv", "/a.mkv", "/b.mkv"):
            await job_repo.create_job(
                db, Job(source_path=p, library="movies", source_codec="h264", quality_value=21)
            )
        asc, _ = await job_repo.list_jobs(db, sort_by="source_path", sort_dir="asc")
        assert [j.source_path for j in asc] == ["/a.mkv", "/b.mkv", "/c.mkv"]
        desc, _ = await job_repo.list_jobs(db, sort_by="source_path", sort_dir="desc")
        assert [j.source_path for j in desc] == ["/c.mkv", "/b.mkv", "/a.mkv"]

    async def test_list_jobs_invalid_sort_falls_back(self, db):
        """A non-whitelisted sort key must never reach the ORDER BY clause — it
        falls back to the default ordering rather than erroring or injecting."""
        await job_repo.create_job(
            db, Job(source_path="/a.mkv", library="movies", source_codec="h264", quality_value=21)
        )
        _, total = await job_repo.list_jobs(
            db, sort_by="source_path; DROP TABLE jobs", sort_dir="asc"
        )
        assert total == 1
        # The table is untouched — the injection string never executed.
        again, _ = await job_repo.list_jobs(db)
        assert len(again) == 1

    async def test_update_job(self, db):
        job = Job(source_path="/a.mkv", library="movies", source_codec="h264", quality_value=21)
        await job_repo.create_job(db, job)

        updated = await job_repo.update_job(db, job.id, status=JobStatus.TRANSCODING, progress=0.5)
        assert updated is not None
        assert updated.status == JobStatus.TRANSCODING
        assert updated.progress == pytest.approx(0.5)

    async def test_job_exists_for_path(self, db):
        job = Job(source_path="/a.mkv", library="movies", source_codec="h264", quality_value=21)
        await job_repo.create_job(db, job)

        assert await job_repo.job_exists_for_path(db, "/a.mkv") is True
        assert await job_repo.job_exists_for_path(db, "/nonexistent.mkv") is False

    async def test_job_exists_ignores_terminal_states(self, db):
        job = Job(
            source_path="/a.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
            status=JobStatus.FAILED,
        )
        await job_repo.create_job(db, job)
        # Failed jobs should not block re-queuing
        assert await job_repo.job_exists_for_path(db, "/a.mkv") is False

    async def test_concurrent_claim_no_double(self, db):
        """Two workers claiming with a single queued job → exactly one wins."""
        import asyncio

        job = Job(
            source_path="/c.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
            status=JobStatus.QUEUED,
        )
        await job_repo.create_job(db, job)

        a, b = await asyncio.gather(
            job_repo.claim_next_job(db, "worker-a"),
            job_repo.claim_next_job(db, "worker-b"),
        )
        claimed = [j for j in (a, b) if j is not None]
        assert len(claimed) == 1
        assert claimed[0].id == job.id
        assert claimed[0].worker_id in ("worker-a", "worker-b")


class TestWorkerRepo:
    async def test_upsert_and_get_worker(self, db):
        worker = Worker(name="worker-1", host="192.0.2.100", capabilities=["cpu", "qsv"])
        await worker_repo.upsert_worker(db, worker)

        fetched = await worker_repo.get_worker(db, worker.id)
        assert fetched is not None
        assert fetched.name == "worker-1"
        assert "qsv" in fetched.capabilities

    async def test_upsert_updates_existing(self, db):
        worker = Worker(name="worker-1", host="192.0.2.100", status=WorkerStatus.OFFLINE)
        await worker_repo.upsert_worker(db, worker)

        worker.status = WorkerStatus.ONLINE
        await worker_repo.upsert_worker(db, worker)

        fetched = await worker_repo.get_worker(db, worker.id)
        assert fetched is not None
        assert fetched.status == WorkerStatus.ONLINE

    async def test_list_workers(self, db):
        w1 = Worker(name="alpha", host="10.0.0.1")
        w2 = Worker(name="beta", host="10.0.0.2")
        await worker_repo.upsert_worker(db, w1)
        await worker_repo.upsert_worker(db, w2)

        workers = await worker_repo.list_workers(db)
        assert len(workers) == 2
        assert workers[0].name == "alpha"  # sorted by name

    async def test_update_worker_status(self, db):
        worker = Worker(name="test", host="localhost")
        await worker_repo.upsert_worker(db, worker)

        await worker_repo.update_worker_status(db, worker.id, WorkerStatus.DEAD)
        fetched = await worker_repo.get_worker(db, worker.id)
        assert fetched is not None
        assert fetched.status == WorkerStatus.DEAD


class TestScanRepo:
    async def test_create_and_get_scan(self, db):
        scan = Scan(library="movies")
        scan_id = await scan_repo.create_scan(db, scan)

        fetched = await scan_repo.get_scan(db, scan_id)
        assert fetched is not None
        assert fetched.library == "movies"
        assert fetched.status == ScanStatus.RUNNING

    async def test_update_scan(self, db):
        scan = Scan(library="movies")
        await scan_repo.create_scan(db, scan)

        await scan_repo.update_scan(
            db,
            scan.id,
            files_found=100,
            files_new=30,
            files_updated=20,
            files_skipped=50,
            status=ScanStatus.COMPLETE,
        )

        fetched = await scan_repo.get_scan(db, scan.id)
        assert fetched is not None
        assert fetched.files_found == 100
        assert fetched.files_new == 30
        assert fetched.files_updated == 20
        assert fetched.status == ScanStatus.COMPLETE
        assert fetched.completed_at is not None

    async def test_list_scans(self, db):
        s1 = Scan(library="movies")
        s2 = Scan(library="tv")
        await scan_repo.create_scan(db, s1)
        await scan_repo.create_scan(db, s2)

        scans, total = await scan_repo.list_scans(db)
        assert total == 2
        assert len(scans) == 2


class TestSkippedRepo:
    async def test_record_and_list_skip(self, db):
        await skip_repo.record_skip(
            db,
            file_path="/media/movies/test.mkv",
            library="movies",
            codec="hevc",
            skip_reason=SkipReason.ALREADY_HEVC,
        )

        files, total = await skip_repo.list_skipped(db)
        assert total == 1
        assert files[0].file_path == "/media/movies/test.mkv"
        assert files[0].skip_reason == SkipReason.ALREADY_HEVC

    async def test_skip_upserts_on_same_path(self, db):
        await skip_repo.record_skip(
            db,
            file_path="/a.mkv",
            library="movies",
            codec="hevc",
            skip_reason=SkipReason.ALREADY_HEVC,
        )
        await skip_repo.record_skip(
            db,
            file_path="/a.mkv",
            library="movies",
            codec="mpeg4",
            skip_reason=SkipReason.NOT_H264,
        )

        files, total = await skip_repo.list_skipped(db)
        assert total == 1
        assert files[0].skip_reason == SkipReason.NOT_H264

    async def test_list_skipped_sort_by_file_path(self, db):
        for p in ("/c.mkv", "/a.mkv", "/b.mkv"):
            await skip_repo.record_skip(
                db,
                file_path=p,
                library="movies",
                codec="hevc",
                skip_reason=SkipReason.ALREADY_HEVC,
            )
        asc, _ = await skip_repo.list_skipped(db, sort_by="file_path", sort_dir="asc")
        assert [f.file_path for f in asc] == ["/a.mkv", "/b.mkv", "/c.mkv"]
        desc, _ = await skip_repo.list_skipped(db, sort_by="file_path", sort_dir="desc")
        assert [f.file_path for f in desc] == ["/c.mkv", "/b.mkv", "/a.mkv"]

    async def test_list_skipped_invalid_sort_falls_back(self, db):
        await skip_repo.record_skip(
            db,
            file_path="/a.mkv",
            library="movies",
            codec="hevc",
            skip_reason=SkipReason.ALREADY_HEVC,
        )
        _, total = await skip_repo.list_skipped(db, sort_by="file_path; DROP TABLE skipped_files")
        assert total == 1
        again, _ = await skip_repo.list_skipped(db)
        assert len(again) == 1

    async def test_filter_by_reason(self, db):
        await skip_repo.record_skip(
            db,
            file_path="/a.mkv",
            library="movies",
            codec="hevc",
            skip_reason=SkipReason.ALREADY_HEVC,
        )
        await skip_repo.record_skip(
            db,
            file_path="/b.mkv",
            library="movies",
            codec="mpeg4",
            skip_reason=SkipReason.NOT_H264,
        )

        hevc, _ = await skip_repo.list_skipped(db, reason="already_hevc")
        assert len(hevc) == 1

    async def test_skip_reason_counts(self, db):
        await skip_repo.record_skip(
            db,
            file_path="/a.mkv",
            library="movies",
            codec="hevc",
            skip_reason=SkipReason.ALREADY_HEVC,
        )
        await skip_repo.record_skip(
            db,
            file_path="/b.mkv",
            library="movies",
            codec="hevc",
            skip_reason=SkipReason.ALREADY_HEVC,
        )
        await skip_repo.record_skip(
            db,
            file_path="/c.mkv",
            library="tv",
            codec="mpeg4",
            skip_reason=SkipReason.NOT_H264,
        )

        counts = await skip_repo.skip_reason_counts(db)
        assert counts["already_hevc"] == 2
        assert counts["not_h264"] == 1

    async def test_unskip(self, db):
        await skip_repo.record_skip(
            db,
            file_path="/a.mkv",
            library="movies",
            codec="hevc",
            skip_reason=SkipReason.ALREADY_HEVC,
        )

        removed = await skip_repo.unskip(db, "/a.mkv")
        assert removed is True

        _, total = await skip_repo.list_skipped(db)
        assert total == 0

    async def test_unskip_nonexistent(self, db):
        removed = await skip_repo.unskip(db, "/nonexistent.mkv")
        assert removed is False


class TestSSLConfiguration:
    """PostgreSQL SSL is handled by asyncpg parsing sslmode from the DSN."""

    async def test_init_postgres_passes_url_without_ssl_kwarg(self):
        import sys
        from unittest.mock import MagicMock

        from transcode_forge.db import _init_postgres

        mock_asyncpg = MagicMock()
        mock_pool = AsyncMock()
        mock_asyncpg.create_pool = AsyncMock(return_value=mock_pool)
        sys.modules["asyncpg"] = mock_asyncpg

        try:
            url = "postgresql://user:pass@host/db?sslmode=require"
            with patch("transcode_forge.migrations.apply_postgres", new=AsyncMock()):
                await _init_postgres(url)

            # The DSN is passed straight through so asyncpg parses sslmode itself;
            # we must NOT inject an explicit ssl kwarg (it would override the DSN).
            assert mock_asyncpg.create_pool.call_args[0][0] == url
            assert "ssl" not in mock_asyncpg.create_pool.call_args[1]
        finally:
            if "asyncpg" in sys.modules:
                del sys.modules["asyncpg"]


class TestDatabaseValidation:
    """Tests for startup database validation."""

    async def test_validate_sqlite_always_passes(self):
        from transcode_forge.preflight import validate_db_connection

        issues = await validate_db_connection("sqlite:///test.db")
        assert issues == []

    async def test_validate_postgres_malformed_url(self):
        from transcode_forge.preflight import validate_db_connection

        issues = await validate_db_connection("postgresql://invalid")
        assert len(issues) == 1
        assert issues[0]["code"] == "db_url_malformed"
        assert "Expected format" in issues[0]["message"]

    async def test_validate_postgres_url_parse_error(self):
        from transcode_forge.preflight import validate_db_connection

        # Test URL that's completely invalid
        issues = await validate_db_connection("not a url at all")
        assert issues == []  # Not a Postgres URL, so no validation needed

    async def test_validate_postgres_connection_refused(self):
        """Test that connection errors are caught and reported correctly."""
        import sys
        from unittest.mock import MagicMock

        from transcode_forge.preflight import validate_db_connection

        # Create a custom exception that inherits from Exception
        class MockCannotConnectError(Exception):
            pass

        # Mock asyncpg module
        mock_asyncpg = MagicMock()
        mock_asyncpg.InvalidPasswordError = Exception("InvalidPasswordError")
        mock_asyncpg.CannotConnectNowError = MockCannotConnectError
        mock_asyncpg.PostgresError = Exception("PostgresError")
        mock_asyncpg.connect = AsyncMock(side_effect=MockCannotConnectError("Connection refused"))
        sys.modules["asyncpg"] = mock_asyncpg

        try:
            issues = await validate_db_connection("postgresql://user:pass@invalid-host:5432/db")
            # Should catch OSError or ConnectionRefusedError
            assert len(issues) == 1
            assert issues[0]["code"] in ("db_connection_refused", "db_error")
        finally:
            if "asyncpg" in sys.modules:
                del sys.modules["asyncpg"]

    async def test_validate_postgres_success(self):
        """Test successful database connection validation."""
        import sys
        from unittest.mock import MagicMock

        from transcode_forge.preflight import validate_db_connection

        # Mock asyncpg module
        mock_asyncpg = MagicMock()
        mock_asyncpg.InvalidPasswordError = Exception("InvalidPasswordError")
        mock_asyncpg.CannotConnectNowError = Exception("CannotConnectNowError")
        mock_asyncpg.PostgresError = Exception("PostgresError")

        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.close = AsyncMock()
        mock_asyncpg.connect = AsyncMock(return_value=mock_conn)
        sys.modules["asyncpg"] = mock_asyncpg

        try:
            issues = await validate_db_connection("postgresql://user:pass@localhost:5432/db")
            assert issues == []
            mock_conn.execute.assert_called_once_with("SELECT 1")
            mock_conn.close.assert_called_once()
        finally:
            if "asyncpg" in sys.modules:
                del sys.modules["asyncpg"]

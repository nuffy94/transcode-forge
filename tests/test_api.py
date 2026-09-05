"""Integration tests for REST API endpoints."""

from httpx import AsyncClient

from transcode_forge import __version__
from transcode_forge.models.job import Job, JobStatus
from transcode_forge.models.skipped import SkipReason
from transcode_forge.models.worker import Worker
from transcode_forge.repos import jobs as job_repo
from transcode_forge.repos import skipped as skip_repo
from transcode_forge.repos import workers as worker_repo


class TestHealthEndpoint:
    async def test_health_ok(self, client: AsyncClient):
        response = await client.get("/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["redis"] is True
        assert data["db"] is True

    async def test_liveness_always_ok(self, client: AsyncClient):
        response = await client.get("/api/health/live")
        assert response.status_code == 200
        assert response.json()["status"] == "alive"

    async def test_readiness(self, client: AsyncClient):
        response = await client.get("/api/health/ready")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_preflight_requires_auth(self, unauthed_client: AsyncClient):
        response = await unauthed_client.get("/api/health/preflight")
        assert response.status_code == 401

    async def test_preflight_authed(self, client: AsyncClient):
        response = await client.get("/api/health/preflight")
        assert response.status_code == 200
        data = response.json()
        assert "ok" in data
        assert "issues" in data


class TestLibrariesEndpoint:
    async def test_create_rejects_empty_name(self, client: AsyncClient):
        resp = await client.post(
            "/api/libraries", json={"name": "", "media_type": "movies", "path": "/tmp/lib-a"}
        )
        assert resp.status_code == 422

    async def test_create_duplicate_path_conflict(self, client: AsyncClient):
        body = {"name": "Lib A", "media_type": "movies", "path": "/tmp/dup-lib"}
        first = await client.post("/api/libraries", json=body)
        assert first.status_code == 201
        second = await client.post("/api/libraries", json={**body, "name": "Lib B"})
        assert second.status_code == 409

    async def test_create_defaults_to_scheduled_scans(self, client: AsyncClient):
        """A library created without an auto_scan value is scanned on the
        24 h schedule. Live: both production libraries sat at auto_scan=0
        and no scan had run since 2026-05-05."""
        resp = await client.post(
            "/api/libraries", json={"name": "Lib", "media_type": "movies", "path": "/tmp/lib-auto"}
        )
        assert resp.status_code == 201
        lib = resp.json()["data"]
        assert bool(lib["auto_scan"]) is True
        assert lib["scan_interval_hours"] == 24

    async def test_put_auto_scan_flips_the_flag(self, client: AsyncClient):
        """Pre-existing rows are flipped with PUT {"auto_scan": true}; the
        bool has to survive the INTEGER column on both dialects."""
        resp = await client.post(
            "/api/libraries", json={"name": "Lib", "media_type": "movies", "path": "/tmp/lib-put"}
        )
        lib_id = resp.json()["data"]["id"]
        off = await client.put(f"/api/libraries/{lib_id}", json={"auto_scan": False})
        assert off.status_code == 200
        assert bool(off.json()["data"]["auto_scan"]) is False
        on = await client.put(f"/api/libraries/{lib_id}", json={"auto_scan": True})
        assert on.status_code == 200
        assert bool(on.json()["data"]["auto_scan"]) is True

    async def test_create_filesystem_requires_path(self, client: AsyncClient):
        resp = await client.post("/api/libraries", json={"name": "Lib", "media_type": "movies"})
        assert resp.status_code == 422

    async def test_create_s3_requires_bucket(self, client: AsyncClient):
        resp = await client.post(
            "/api/libraries",
            json={"name": "Cloud", "media_type": "movies", "backend": "s3"},
        )
        assert resp.status_code == 422

    async def test_create_s3_library_derives_path(self, client: AsyncClient):
        resp = await client.post(
            "/api/libraries",
            json={
                "name": "Cloud Movies",
                "media_type": "movies",
                "backend": "s3",
                "s3_bucket": "forge-media",
                "s3_prefix": "masters/movies/",
            },
        )
        assert resp.status_code == 201
        lib = resp.json()["data"]
        assert lib["backend"] == "s3"
        assert lib["s3_bucket"] == "forge-media"
        assert lib["s3_prefix"] == "masters/movies/"
        assert lib["path"] == "s3://forge-media/masters/movies/"

    async def test_delete_scanned_library_succeeds(self, client: AsyncClient, app):
        """QA sweep H1: deleting a library that has cataloged files 500'd —
        media_files.library_id has no ON DELETE CASCADE (migration 0001), so
        the bare library delete hit the FK. Once scanned, a library was
        un-removable from the UI. The repo must remove cataloged rows with
        the library."""
        from transcode_forge.repos import media as media_repo

        created = await client.post(
            "/api/libraries",
            json={"name": "Scanned Lib", "media_type": "movies", "path": "/tmp/scanned-lib"},
        )
        lib_id = created.json()["data"]["id"]
        await media_repo.upsert_media_file(
            app.state.db,
            library_id=lib_id,
            file_path="/tmp/scanned-lib/movie.mkv",
            filename="movie.mkv",
            video_codec="h264",
            audio_codec="aac",
            resolution="1920x1080",
            width=1920,
            height=1080,
            bitrate=5_000_000,
            duration=3600.0,
            file_size=2_000_000_000,
            file_modified_at="2026-07-01T00:00:00+00:00",
        )

        resp = await client.delete(f"/api/libraries/{lib_id}")
        assert resp.status_code == 200

        async with app.state.db.execute(
            "SELECT COUNT(*) FROM media_files WHERE library_id = ?", (lib_id,)
        ) as cur:
            row = await cur.fetchone()
        assert row[0] == 0, "cataloged rows must be removed with their library"

    async def test_library_scan_dispatches_s3_scanner(self, client: AsyncClient, monkeypatch):
        """The per-library Scan button must route S3 libraries to the S3
        scanner, not the filesystem scanner (which would FAIL on s3:// paths)."""
        created = await client.post(
            "/api/libraries",
            json={
                "name": "Cloud Movies",
                "media_type": "movies",
                "backend": "s3",
                "s3_bucket": "forge-media",
                "s3_prefix": "masters/movies/",
            },
        )
        lib_id = created.json()["data"]["id"]

        called: dict = {}

        async def fake_s3_scan(**kwargs):
            called.update(kwargs)
            return {"files_found": 0}

        monkeypatch.setattr("transcode_forge.scanner.s3_scanner.scan_s3_library", fake_s3_scan)
        resp = await client.post(f"/api/libraries/{lib_id}/scan")
        assert resp.status_code == 202
        assert called["bucket"] == "forge-media"
        assert called["prefix"] == "masters/movies/"


class TestJobsEndpoint:
    async def test_list_jobs_empty(self, client: AsyncClient):
        response = await client.get("/api/jobs")
        assert response.status_code == 200
        data = response.json()
        assert data["data"] == []
        assert data["meta"]["total"] == 0

    async def test_list_jobs_with_data(self, client: AsyncClient, app):
        db = app.state.db
        job = Job(
            source_path="/test.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
        )
        await job_repo.create_job(db, job)

        response = await client.get("/api/jobs")
        assert response.status_code == 200
        data = response.json()
        assert data["meta"]["total"] == 1
        assert data["data"][0]["source_path"] == "/test.mkv"

    async def test_get_job(self, client: AsyncClient, app):
        db = app.state.db
        job = Job(
            source_path="/test.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
        )
        await job_repo.create_job(db, job)

        response = await client.get(f"/api/jobs/{job.id}")
        assert response.status_code == 200
        assert response.json()["data"]["id"] == job.id

    async def test_get_job_not_found(self, client: AsyncClient):
        response = await client.get("/api/jobs/nonexistent")
        assert response.status_code == 404

    async def test_retry_failed_job(self, client: AsyncClient, app):
        db = app.state.db
        job = Job(
            source_path="/test.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
            status=JobStatus.FAILED,
        )
        await job_repo.create_job(db, job)

        response = await client.post(f"/api/jobs/{job.id}/retry")
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "pending"
        assert response.json()["data"]["retry_count"] == 1

    async def test_retry_non_failed_job(self, client: AsyncClient, app):
        db = app.state.db
        job = Job(
            source_path="/test.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
            status=JobStatus.TRANSCODING,
        )
        await job_repo.create_job(db, job)

        response = await client.post(f"/api/jobs/{job.id}/retry")
        assert response.status_code == 400

    async def test_cancel_pending_job(self, client: AsyncClient, app):
        db = app.state.db
        job = Job(
            source_path="/test.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
        )
        await job_repo.create_job(db, job)

        response = await client.post(f"/api/jobs/{job.id}/cancel")
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "cancelled"

    async def test_filter_by_status(self, client: AsyncClient, app):
        db = app.state.db
        j1 = Job(source_path="/a.mkv", library="movies", source_codec="h264", quality_value=21)
        j2 = Job(
            source_path="/b.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
            status=JobStatus.COMPLETE,
        )
        await job_repo.create_job(db, j1)
        await job_repo.create_job(db, j2)

        response = await client.get("/api/jobs?status=complete")
        assert response.status_code == 200
        data = response.json()
        assert data["meta"]["total"] == 1
        assert data["data"][0]["status"] == "complete"


class TestWorkersEndpoint:
    async def test_list_workers_empty(self, client: AsyncClient):
        response = await client.get("/api/workers")
        assert response.status_code == 200
        assert response.json()["data"] == []

    async def test_list_workers_with_data(self, client: AsyncClient, app):
        db = app.state.db
        worker = Worker(name="worker-1", host="192.0.2.100", capabilities=["cpu", "qsv"])
        await worker_repo.upsert_worker(db, worker)

        response = await client.get("/api/workers")
        assert response.status_code == 200
        data = response.json()
        assert len(data["data"]) == 1
        assert data["data"][0]["name"] == "worker-1"

    async def test_get_worker_not_found(self, client: AsyncClient):
        response = await client.get("/api/workers/nonexistent")
        assert response.status_code == 404

    async def test_delete_worker_not_found(self, client: AsyncClient):
        response = await client.request("DELETE", "/api/workers/nonexistent")
        assert response.status_code == 404

    async def test_delete_worker_recently_active_rejected(self, client: AsyncClient, app):
        """A worker whose heartbeat is fresh must not be deletable —
        regardless of its status field, because the scheduler can't
        prove it's actually idle."""
        db = app.state.db
        worker = Worker(name="live", host="h", capabilities=["cpu"])
        await worker_repo.upsert_worker(db, worker)
        # upsert_worker stamps last_heartbeat = now()

        response = await client.request("DELETE", f"/api/workers/{worker.id}")
        assert response.status_code == 409
        assert "heartbeat" in response.json()["detail"].lower()

        # Worker still exists
        check = await client.get(f"/api/workers/{worker.id}")
        assert check.status_code == 200

    async def test_delete_worker_with_active_job_rejected(self, client: AsyncClient, app):
        """A worker that owns an active job must not be deletable —
        even if its heartbeat is stale. Orphan-job audit must run first."""
        db = app.state.db
        worker = Worker(name="stuck", host="h", capabilities=["cpu"])
        await worker_repo.upsert_worker(db, worker)
        # Age the heartbeat past the stale threshold
        await db.execute(
            "UPDATE workers SET last_heartbeat = ? WHERE id = ?",
            ("2020-01-01T00:00:00+00:00", worker.id),
        )
        # Pin a transcoding job to this worker (create_job doesn't
        # persist worker_id, so set it via update_job after).
        job = Job(
            source_path="/tmp/x.mkv",
            library="movies",
            source_codec="h264",
            source_size=1000,
            quality_value=24,
            status=JobStatus.TRANSCODING,
        )
        await job_repo.create_job(db, job)
        await job_repo.update_job(db, job.id, worker_id=worker.id)
        await db.commit()

        response = await client.request("DELETE", f"/api/workers/{worker.id}")
        assert response.status_code == 409
        assert "active job" in response.json()["detail"].lower()

    async def test_delete_stale_idle_worker_succeeds(self, client: AsyncClient, app):
        """The clean path: heartbeat stale + no active jobs → delete works."""
        db = app.state.db
        worker = Worker(name="retired", host="h", capabilities=["cpu"])
        await worker_repo.upsert_worker(db, worker)
        await db.execute(
            "UPDATE workers SET last_heartbeat = ? WHERE id = ?",
            ("2020-01-01T00:00:00+00:00", worker.id),
        )
        await db.commit()

        response = await client.request("DELETE", f"/api/workers/{worker.id}")
        assert response.status_code == 200
        assert response.json()["deleted"] is True

        check = await client.get(f"/api/workers/{worker.id}")
        assert check.status_code == 404


class TestScanEndpoint:
    async def test_trigger_scan(self, client: AsyncClient, app, tmp_path):
        # Create library directory so scan doesn't fail immediately
        (tmp_path / "movies").mkdir(exist_ok=True)

        response = await client.post("/api/scan", json={"library": "movies"})
        assert response.status_code == 202
        data = response.json()
        assert "movies" in data["scan_ids"]

    async def test_trigger_scan_all(self, client: AsyncClient, app, tmp_path):
        (tmp_path / "movies").mkdir(exist_ok=True)
        (tmp_path / "tv").mkdir(exist_ok=True)
        (tmp_path / "anime").mkdir(exist_ok=True)

        response = await client.post("/api/scan", json={})
        assert response.status_code == 202
        data = response.json()
        assert len(data["scan_ids"]) == 3

    async def test_config_seeded_libraries_are_scheduled(self, client: AsyncClient, tmp_path):
        """The first scan turns TF_LIBRARY_* into library rows; those rows
        must be on the 24 h scan schedule, or the catalog only ever reflects
        the one manual scan that created it."""
        (tmp_path / "movies").mkdir(exist_ok=True)
        (tmp_path / "tv").mkdir(exist_ok=True)
        (tmp_path / "anime").mkdir(exist_ok=True)

        response = await client.post("/api/scan", json={})
        assert response.status_code == 202

        libs = (await client.get("/api/libraries")).json()["data"]
        assert len(libs) == 3
        for lib in libs:
            assert bool(lib["auto_scan"]) is True, lib["name"]
            assert lib["scan_interval_hours"] == 24

    async def test_trigger_scan_unknown_library(self, client: AsyncClient):
        response = await client.post("/api/scan", json={"library": "nonexistent"})
        assert response.status_code == 400

    async def test_list_scans_empty(self, client: AsyncClient):
        response = await client.get("/api/scans")
        assert response.status_code == 200
        assert response.json()["data"] == []


class TestSkippedEndpoint:
    async def test_list_skipped_empty(self, client: AsyncClient):
        response = await client.get("/api/skipped")
        assert response.status_code == 200
        assert response.json()["data"] == []
        assert response.json()["meta"]["total"] == 0

    async def test_list_skipped_with_data(self, client: AsyncClient, app):
        db = app.state.db
        await skip_repo.record_skip(
            db,
            file_path="/test.mkv",
            library="movies",
            codec="hevc",
            skip_reason=SkipReason.ALREADY_HEVC,
        )

        response = await client.get("/api/skipped")
        data = response.json()
        assert data["meta"]["total"] == 1
        assert data["data"][0]["skip_reason"] == "already_hevc"

    async def test_skipped_stats(self, client: AsyncClient, app):
        db = app.state.db
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

        response = await client.get("/api/skipped/stats")
        data = response.json()
        assert data["data"]["already_hevc"] == 1
        assert data["data"]["not_h264"] == 1
        assert data["meta"]["total"] == 2

    async def test_unskip_file(self, client: AsyncClient, app):
        db = app.state.db
        await skip_repo.record_skip(
            db,
            file_path="/test.mkv",
            library="movies",
            codec="hevc",
            skip_reason=SkipReason.ALREADY_HEVC,
        )

        response = await client.request("DELETE", "/api/skipped", json={"file_path": "/test.mkv"})
        assert response.status_code == 200

        # Verify it's gone
        response = await client.get("/api/skipped")
        assert response.json()["meta"]["total"] == 0

    async def test_unskip_nonexistent(self, client: AsyncClient):
        response = await client.request(
            "DELETE", "/api/skipped", json={"file_path": "/nonexistent.mkv"}
        )
        assert response.status_code == 404


class TestSystemInfoEndpoint:
    async def test_system_info(self, client: AsyncClient):
        response = await client.get("/api/system/info")
        assert response.status_code == 200
        data = response.json()
        assert data["version"] == __version__
        assert "database" in data
        assert "uptime" in data
        assert "disk" in data
        assert "percent" in data["disk"]

    async def test_system_info_uptime_format(self, client: AsyncClient):
        response = await client.get("/api/system/info")
        data = response.json()
        # Uptime should be a string like "0d 0h 0m"
        assert "d" in data["uptime"]
        assert "h" in data["uptime"]
        assert "m" in data["uptime"]
        assert data["uptime_seconds"] >= 0


class TestStatsEndpoint:
    async def test_stats_empty(self, client: AsyncClient):
        response = await client.get("/api/stats")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["completed"] == 0
        assert data["total_space_saved_bytes"] == 0

    async def test_stats_with_completed_jobs(self, client: AsyncClient, app):
        db = app.state.db
        j = Job(
            source_path="/test.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
        )
        await job_repo.create_job(db, j)
        await job_repo.update_job(
            db,
            j.id,
            status="complete",
            source_size=2_000_000_000,
            output_size=1_000_000_000,
            space_saved=1_000_000_000,
        )

        response = await client.get("/api/stats")
        data = response.json()["data"]
        assert data["completed"] == 1
        assert data["total_space_saved_bytes"] == 1_000_000_000
        assert data["jobs_by_status"]["complete"] == 1


class TestPaginationBounds:
    """Unbounded per_page/limit must be rejected, not honored."""

    async def test_media_per_page_bounded(self, client):
        assert (
            await client.get("/api/media/movies", params={"per_page": 999_999})
        ).status_code == 422
        assert (await client.get("/api/media/tv", params={"per_page": 0})).status_code == 422
        assert (await client.get("/api/media/movies", params={"per_page": 200})).status_code == 200
        assert (await client.get("/api/media/movies", params={"page": 0})).status_code == 422

    async def test_scans_limit_bounded(self, client):
        assert (await client.get("/api/scans", params={"limit": 999_999})).status_code == 422
        assert (await client.get("/api/scans", params={"page": 0})).status_code == 422
        assert (await client.get("/api/scans")).status_code == 200

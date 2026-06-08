"""Tests for the don't-try-this-again exclusion list."""

from httpx import AsyncClient

from transcode_forge.models.job import Job, JobStatus
from transcode_forge.repos import exclusions as excl_repo
from transcode_forge.repos import jobs as job_repo
from transcode_forge.repos import libraries as lib_repo
from transcode_forge.repos import media as media_repo


class TestExclusionsRepo:
    async def test_add_then_is_excluded(self, db):
        await excl_repo.add(db, "/x.mkv", reason="test")
        assert await excl_repo.is_excluded(db, "/x.mkv") is True
        assert await excl_repo.is_excluded(db, "/y.mkv") is False

    async def test_add_is_idempotent(self, db):
        await excl_repo.add(db, "/x.mkv", reason="first")
        await excl_repo.add(db, "/x.mkv", reason="second")
        assert await excl_repo.count(db) == 1

    async def test_remove(self, db):
        await excl_repo.add(db, "/x.mkv")
        assert await excl_repo.remove(db, "/x.mkv") is True
        assert await excl_repo.is_excluded(db, "/x.mkv") is False
        # Removing twice returns False (nothing to delete)
        assert await excl_repo.remove(db, "/x.mkv") is False

    async def test_list_returns_metadata(self, db):
        await excl_repo.add(db, "/a.mkv", library="movies", reason="too small")
        rows = await excl_repo.list_all(db)
        assert len(rows) == 1
        assert rows[0]["path"] == "/a.mkv"
        assert rows[0]["library"] == "movies"
        assert rows[0]["reason"] == "too small"


class TestExclusionsApi:
    async def test_post_then_get(self, client: AsyncClient):
        resp = await client.post(
            "/api/exclusions",
            json={"path": "/m.mkv", "library": "movies", "reason": "manual"},
        )
        assert resp.status_code == 200

        resp = await client.get("/api/exclusions")
        rows = resp.json()["data"]
        assert len(rows) == 1
        assert rows[0]["path"] == "/m.mkv"

    async def test_delete_unexcludes(self, client: AsyncClient):
        await client.post("/api/exclusions", json={"path": "/m.mkv"})
        resp = await client.request("DELETE", "/api/exclusions", json={"path": "/m.mkv"})
        assert resp.status_code == 200
        assert resp.json()["removed"] is True

    async def test_empty_path_rejected(self, client: AsyncClient):
        resp = await client.post("/api/exclusions", json={"path": "   "})
        assert resp.status_code == 400


class TestQueueRespectsExclusions:
    """Excluded paths must not become jobs even if they're in the catalog
    and someone clicks 'queue' on them.
    """

    async def test_excluded_file_is_skipped_at_queue(self, client: AsyncClient, app):
        db = app.state.db
        lib_id = await lib_repo.create_library(db, name="movies", media_type="movie", path="/tmp/m")
        file_id = await media_repo.upsert_media_file(
            db,
            library_id=lib_id,
            file_path="/tmp/m/cursed.mkv",
            filename="cursed.mkv",
            video_codec="h264",
            audio_codec=None,
            resolution="1920x1080",
            width=1920,
            height=1080,
            bitrate=10_000_000,
            duration=3600,
            file_size=2_000_000_000,
            file_modified_at="2026-01-01T00:00:00+00:00",
        )

        # Sanity: without exclusion, it queues
        resp = await client.post("/api/media/queue", json={"file_ids": [file_id]})
        assert resp.json()["queued"] == 1

        # Now exclude and try a different file
        file_id_2 = await media_repo.upsert_media_file(
            db,
            library_id=lib_id,
            file_path="/tmp/m/cursed-2.mkv",
            filename="cursed-2.mkv",
            video_codec="h264",
            audio_codec=None,
            resolution="1920x1080",
            width=1920,
            height=1080,
            bitrate=10_000_000,
            duration=3600,
            file_size=2_000_000_000,
            file_modified_at="2026-01-01T00:00:00+00:00",
        )
        await excl_repo.add(db, "/tmp/m/cursed-2.mkv", reason="test")

        resp = await client.post("/api/media/queue", json={"file_ids": [file_id_2]})
        body = resp.json()
        assert body["queued"] == 0
        assert body["skipped"] == 1


class TestRetryRefusesExcluded:
    async def test_retry_blocked_when_path_excluded(self, client: AsyncClient, app):
        db = app.state.db
        job = Job(
            source_path="/m.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
            status=JobStatus.FAILED,
        )
        await job_repo.create_job(db, job)
        await job_repo.update_job(db, job.id, status="failed", error_message="x")

        await excl_repo.add(db, "/m.mkv", reason="manual")

        resp = await client.post(f"/api/jobs/{job.id}/retry")
        assert resp.status_code == 400
        assert "exclusion" in resp.json()["detail"].lower()

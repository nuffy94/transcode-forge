"""Media transcode_status must follow the job it was queued into.

The bug (found live on the LKE acceptance, 2026-07-12): nothing in the
job-outcome paths updates media_files, so a row queued from the Movies
page says 'queued' forever once its job completes/skips/fails. Filesystem
libraries self-heal on the next scan (the swap changes the file a rescan
re-probes); S3 libraries never do — the master object is unchanged.

These tests pin every job-outcome transition onto the media row, keyed by
the job_id stamped at queue time.
"""

from httpx import ASGITransport, AsyncClient

from tests.helpers import register_worker


async def _seed_catalog_job(
    app,
    path: str = "/m/movie.mkv",
    *,
    target_height: int | None = None,
    s3: bool = False,
):
    """Library + media row + PENDING job, linked the way /api/media/queue
    links them (media.job_id stamped, status 'queued'). Returns
    (file_id, job). s3=True backs the library with S3 (the master object
    is never replaced by a job)."""
    from uuid import uuid4

    from transcode_forge.models.job import Job, JobStatus
    from transcode_forge.models.library import StorageBackendType
    from transcode_forge.repos import jobs as job_repo
    from transcode_forge.repos import libraries as lib_repo
    from transcode_forge.repos import media as media_repo

    db = app.state.db
    # Unique name/path per call — libraries.path carries a UNIQUE constraint.
    suffix = uuid4().hex[:6]
    lib_name = f"movies-{suffix}"
    if s3:
        lib_id = await lib_repo.create_library(
            db,
            name=lib_name,
            media_type="movies",
            path=f"s3://forge-{suffix}/masters/movies/",
            backend=StorageBackendType.S3,
            s3_bucket=f"forge-{suffix}",
            s3_prefix="masters/movies/",
        )
    else:
        lib_id = await lib_repo.create_library(
            db, name=lib_name, media_type="movies", path=f"/m-{suffix}"
        )
    file_id = await media_repo.upsert_media_file(
        db,
        library_id=lib_id,
        file_path=path,
        filename=path.rsplit("/", 1)[-1],
        video_codec="h264",
        resolution="3840x2160",
        width=3840,
        height=2160,
        file_size=1000,
    )
    job = Job(
        source_path=path,
        # S3 jobs carry the library NAME so the claim attaches the backend.
        library=lib_name if s3 else "movies",
        source_codec="h264",
        quality_value=21,
        status=JobStatus.PENDING,
        target_height=target_height,
    )
    await job_repo.create_job(db, job)
    await media_repo.update_media_status(db, file_id, transcode_status="queued", job_id=job.id)
    return file_id, job


async def _media_row(app, file_id: str) -> dict:
    from transcode_forge.repos import media as media_repo

    row = await media_repo.get_media_file(app.state.db, file_id)
    assert row is not None
    return row


class TestWorkerOutcomeSyncsMedia:
    async def test_complete_marks_media_complete(self, client: AsyncClient, app):
        file_id, job = await _seed_catalog_job(app)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            headers, worker_id = await register_worker(client, c, "w")
            claim = await c.post(
                "/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers
            )
            assert claim.json()["job"]["id"] == job.id
            r = await c.post(
                f"/api/worker/job/{job.id}/complete",
                json={"output_size": 400, "space_saved": 600, "source_size": 1000},
                headers=headers,
            )
            assert r.status_code == 204

        row = await _media_row(app, file_id)
        assert row["transcode_status"] == "complete"
        assert row["job_id"] == job.id  # drawer stays linked to the job
        # The swap replaced the file on disk: the catalog must describe the
        # new file, not the last scan (live: 299 rows read complete|h264).
        assert row["video_codec"] == "hevc"
        assert row["file_size"] == 400
        # Not a downscale job: dimensions stay as scanned.
        assert row["width"] == 3840
        assert row["height"] == 2160
        assert row["resolution"] == "3840x2160"

    async def test_complete_with_downscale_updates_dimensions(self, client: AsyncClient, app):
        file_id, job = await _seed_catalog_job(app, target_height=1080)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            headers, worker_id = await register_worker(client, c, "w", supports_downscale=True)
            claim = await c.post(
                "/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers
            )
            assert claim.json()["job"]["id"] == job.id
            r = await c.post(
                f"/api/worker/job/{job.id}/complete",
                json={"output_size": 400, "space_saved": 600, "source_size": 1000},
                headers=headers,
            )
            assert r.status_code == 204

        row = await _media_row(app, file_id)
        assert row["video_codec"] == "hevc"
        assert row["file_size"] == 400
        assert row["height"] == 1080
        # Width follows the source aspect (the encoder's scale=-2:H), so
        # resolution reads the way the scanner would write it.
        assert row["width"] == 1920
        assert row["resolution"] == "1920x1080"

    async def test_complete_with_downscale_and_unknown_source_dims(self, client: AsyncClient, app):
        """No scanned width/height: height still lands, width and resolution
        are left for the next scan instead of guessing."""
        file_id, job = await _seed_catalog_job(app, target_height=720)
        await app.state.db.execute(
            "UPDATE media_files SET width = NULL, height = NULL, resolution = NULL WHERE id = ?",
            (file_id,),
        )
        await app.state.db.commit()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            headers, worker_id = await register_worker(client, c, "w", supports_downscale=True)
            claim = await c.post(
                "/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers
            )
            assert claim.json()["job"]["id"] == job.id
            r = await c.post(
                f"/api/worker/job/{job.id}/complete",
                json={"output_size": 400, "space_saved": 600, "source_size": 1000},
                headers=headers,
            )
            assert r.status_code == 204

        row = await _media_row(app, file_id)
        assert row["height"] == 720
        assert row["width"] is None
        assert row["resolution"] is None

    async def test_complete_on_s3_library_keeps_master_codec_and_size(
        self, client: AsyncClient, app
    ):
        """An S3 job never replaces the master object (the output lands in
        a derivative), so the catalog row still describes the master: the
        status moves to complete, codec and size do not."""
        file_id, job = await _seed_catalog_job(app, path="masters/movies/movie.mkv", s3=True)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            headers, worker_id = await register_worker(client, c, "w")
            claim = await c.post(
                "/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers
            )
            assert claim.json()["job"]["id"] == job.id
            r = await c.post(
                f"/api/worker/job/{job.id}/complete",
                json={"output_size": 400, "space_saved": 0, "source_size": 1000},
                headers=headers,
            )
            assert r.status_code == 204

        row = await _media_row(app, file_id)
        assert row["transcode_status"] == "complete"
        assert row["video_codec"] == "h264"
        assert row["file_size"] == 1000

    async def test_worker_skip_marks_media_skipped_with_reason(self, client: AsyncClient, app):
        file_id, job = await _seed_catalog_job(app)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            headers, worker_id = await register_worker(client, c, "w")
            claim = await c.post(
                "/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers
            )
            assert claim.json()["job"]["id"] == job.id
            r = await c.post(
                f"/api/worker/job/{job.id}/skipped",
                json={
                    "reason": "below_vmaf_floor",
                    "error_message": "VMAF gate: mean 88.2 < floor 90",
                    "achieved_vmaf": 88.2,
                },
                headers=headers,
            )
            assert r.status_code == 204

        row = await _media_row(app, file_id)
        assert row["transcode_status"] == "skipped"
        assert row["skip_reason"] == "below_vmaf_floor"
        # Original kept: the catalog still describes the source file.
        assert row["video_codec"] == "h264"
        assert row["file_size"] == 1000

    async def test_worker_fail_returns_media_to_needs_transcode(self, client: AsyncClient, app):
        file_id, job = await _seed_catalog_job(app)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            headers, worker_id = await register_worker(client, c, "w")
            claim = await c.post(
                "/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers
            )
            assert claim.json()["job"]["id"] == job.id
            r = await c.post(
                f"/api/worker/job/{job.id}/failed",
                json={"error_message": "ffmpeg exploded", "retry_count": 1},
                headers=headers,
            )
            assert r.status_code == 204

        row = await _media_row(app, file_id)
        assert row["transcode_status"] == "needs_transcode"
        assert row["job_id"] == job.id  # failed job stays inspectable
        # Original kept: the catalog still describes the source file.
        assert row["video_codec"] == "h264"
        assert row["file_size"] == 1000


class TestSchedulerActionsSyncMedia:
    async def test_retry_marks_media_queued_again(self, client: AsyncClient, app):
        from transcode_forge.models.job import JobStatus
        from transcode_forge.repos import jobs as job_repo
        from transcode_forge.repos import media as media_repo

        file_id, job = await _seed_catalog_job(app)
        await job_repo.update_job(app.state.db, job.id, status=JobStatus.FAILED)
        await media_repo.update_media_status(
            app.state.db, file_id, transcode_status="needs_transcode", job_id=job.id
        )

        r = await client.post(f"/api/jobs/{job.id}/retry")
        assert r.status_code == 200

        row = await _media_row(app, file_id)
        assert row["transcode_status"] == "queued"

    async def test_cancel_returns_media_to_needs_transcode(self, client: AsyncClient, app):
        file_id, job = await _seed_catalog_job(app)

        r = await client.post(f"/api/jobs/{job.id}/cancel")
        assert r.status_code == 200

        row = await _media_row(app, file_id)
        assert row["transcode_status"] == "needs_transcode"

    async def test_cancel_all_returns_media_to_needs_transcode(self, client: AsyncClient, app):
        file_a, _job_a = await _seed_catalog_job(app, "/m/a.mkv")
        file_b, _job_b = await _seed_catalog_job(app, "/m/b.mkv")

        r = await client.post("/api/jobs/cancel-all")
        assert r.status_code == 200
        assert r.json()["cancelled"] == 2

        for fid in (file_a, file_b):
            row = await _media_row(app, fid)
            assert row["transcode_status"] == "needs_transcode"

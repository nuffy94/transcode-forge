"""File-detail drawer partial + the jobs source_path filter behind it."""

from httpx import AsyncClient

from transcode_forge.models.job import Job, JobStatus
from transcode_forge.repos import exclusions as excl_repo
from transcode_forge.repos import jobs as job_repo
from transcode_forge.repos import libraries as lib_repo
from transcode_forge.repos import media as media_repo

MOVIE_PATH = "/media/movies/Test Movie (2020)/Test Movie (2020).mkv"


async def _seed_file(db, path: str = MOVIE_PATH) -> str:
    lib_id = await lib_repo.create_library(
        db,
        name="movies",
        media_type="movies",
        path="/media/movies",
        quality_preset=21,
        auto_scan=False,
        scan_interval_hours=24,
    )
    return await media_repo.upsert_media_file(
        db,
        library_id=lib_id,
        file_path=path,
        filename=path.rsplit("/", 1)[-1],
        video_codec="h264",
        audio_codec="aac",
        resolution="1080p",
        width=1920,
        height=1080,
        bitrate=8_000_000,
        duration=7200.0,
        file_size=8_000_000_000,
    )


async def _job(db, path: str, status: JobStatus, **outcome: object) -> Job:
    job = Job(
        source_path=path,
        library="movies",
        source_codec="h264",
        quality_value=21,
        source_size=8_000_000_000,
        status=status,
    )
    await job_repo.create_job(db, job)
    if outcome:
        await job_repo.update_job(db, job.id, **outcome)
    return job


class TestSourcePathFilter:
    async def test_filters_to_one_files_history(self, app):
        db = app.state.db
        await _job(db, MOVIE_PATH, JobStatus.FAILED)
        await _job(db, MOVIE_PATH, JobStatus.COMPLETE)
        await _job(db, "/media/movies/Other (2021)/Other (2021).mkv", JobStatus.COMPLETE)

        jobs, total = await job_repo.list_jobs(db, source_path=MOVIE_PATH)
        assert total == 2
        assert {j.source_path for j in jobs} == {MOVIE_PATH}


class TestFileDetailPartial:
    async def test_unknown_id_is_404_with_friendly_body(self, client: AsyncClient):
        resp = await client.get("/partials/file-detail?file_id=no-such-file")
        assert resp.status_code == 404
        assert "File not found" in resp.text

    async def test_renders_probe_and_timeline_with_vmaf(self, client: AsyncClient, app):
        db = app.state.db
        fid = await _seed_file(db)
        await _job(db, MOVIE_PATH, JobStatus.FAILED, error_message="hevc_qsv: MFX session -9")
        await _job(
            db,
            MOVIE_PATH,
            JobStatus.COMPLETE,
            output_size=4_000_000_000,
            space_saved=4_000_000_000,
            achieved_vmaf=96.3,
            resolved_crf=22,
            backend_used="hevc_qsv",
        )

        resp = await client.get(f"/partials/file-detail?file_id={fid}")
        assert resp.status_code == 200
        html = resp.text
        # identity + probe
        assert "Test Movie (2020).mkv" in html
        assert "1080p" in html
        # economics off the completed job
        assert "96.3" in html
        assert "hevc_qsv" in html
        assert "50%" in html  # savings_percent
        # timeline shows both attempts, newest first, with the failure message
        assert html.count("forge-pill--failed") >= 1
        assert "MFX session -9" in html

    async def test_vmaf_absent_reads_not_measured(self, client: AsyncClient, app):
        db = app.state.db
        fid = await _seed_file(db)
        await _job(
            db,
            MOVIE_PATH,
            JobStatus.COMPLETE,
            output_size=5_000_000_000,
            space_saved=3_000_000_000,
        )

        resp = await client.get(f"/partials/file-detail?file_id={fid}")
        assert resp.status_code == 200
        assert "not measured" in resp.text

    async def test_never_attempted_invites_queueing(self, client: AsyncClient, app):
        db = app.state.db
        fid = await _seed_file(db)

        resp = await client.get(f"/partials/file-detail?file_id={fid}")
        assert resp.status_code == 200
        assert "Never attempted" in resp.text
        assert "Queue transcode" in resp.text

    async def test_excluded_file_offers_lift_not_queue(self, client: AsyncClient, app):
        db = app.state.db
        fid = await _seed_file(db)
        await excl_repo.add(db, MOVIE_PATH, reason="manual")

        resp = await client.get(f"/partials/file-detail?file_id={fid}")
        assert resp.status_code == 200
        assert "Lift exclusion" in resp.text
        assert "Queue transcode" not in resp.text

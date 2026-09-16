"""The library id is the relationship key; the name is a label (full review F5).

jobs, scans and skipped_files used to match their library by display
name. Names are not unique and a rename touched only the libraries row,
so a renamed S3 library's queued jobs lost their bucket at claim time and
two libraries sharing a name shared one scan clock. Migration 0018 adds
library_id to all three tables and backfills it; these tests pin the
id-keyed behavior end to end.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from httpx import ASGITransport, AsyncClient

from tests.helpers import register_worker
from transcode_forge import scheduler_cron
from transcode_forge.migrations import _split_statements, discover_migrations
from transcode_forge.models.job import Job, JobStatus
from transcode_forge.models.library import StorageBackendType
from transcode_forge.models.scan import Scan
from transcode_forge.models.skipped import SkipReason
from transcode_forge.repos import jobs as job_repo
from transcode_forge.repos import libraries as lib_repo
from transcode_forge.repos import media as media_repo
from transcode_forge.repos import scans as scan_repo
from transcode_forge.repos import skipped as skip_repo


async def _s3_library(db, name: str = "Movies (S3)", bucket: str = "forge-media") -> str:
    return await lib_repo.create_library(
        db,
        name=name,
        media_type="movies",
        path=f"s3://{bucket}/masters/movies/",
        backend=StorageBackendType.S3,
        s3_bucket=bucket,
        s3_prefix="masters/movies/",
    )


async def test_rename_keeps_s3_routing_for_queued_jobs(client: AsyncClient, app):
    """Queue an S3 file, rename its library, claim: the claim still carries
    the bucket. Before 0018 the job held the old name, the lookup by name
    missed, and the worker treated the job as filesystem."""
    db = app.state.db
    lib_id = await _s3_library(db)
    file_id = await media_repo.upsert_media_file(
        db,
        library_id=lib_id,
        file_path="masters/movies/film.mkv",
        filename="film.mkv",
        video_codec="h264",
        audio_codec="aac",
        resolution="1080p",
        width=1920,
        height=1080,
        bitrate=8_000_000,
        duration=7200.0,
        file_size=8_000_000_000,
    )
    queued = await client.post("/api/media/queue", json={"file_ids": [file_id]})
    assert queued.json()["queued"] == 1

    renamed = await client.put(f"/api/libraries/{lib_id}", json={"name": "Films (S3)"})
    assert renamed.status_code == 200

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        headers, worker_id = await register_worker(client, c, "s3-w")
        claim = await c.post(
            "/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers
        )
    job = claim.json()["job"]
    assert job is not None
    assert job["library_id"] == lib_id
    assert job["library"] == "Movies (S3)", "the name at queue time stays as the label"
    assert job["_backend_type"] == "s3"
    assert job["_s3_bucket"] == "forge-media"


async def test_two_libraries_sharing_a_name_keep_separate_scan_clocks(db):
    """Library b was never scanned; a scan of library a under the same
    display name an hour ago must not count for it."""
    scan = Scan(library="Movies", library_id="a")
    await scan_repo.create_scan(db, scan)
    started = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    await db.execute("UPDATE scans SET started_at = ? WHERE id = ?", (started, scan.id))
    await db.commit()

    def lib(lib_id: str) -> dict:
        return {
            "id": lib_id,
            "name": "Movies",
            "auto_scan": True,
            "scan_interval_hours": 24,
            "path": f"/movies-{lib_id}",
            "media_type": "movies",
        }

    async def finished(**_kw):
        return None

    with (
        patch(
            "transcode_forge.scheduler_cron.lib_repo.list_libraries",
            return_value=[lib("a"), lib("b")],
        ),
        patch(
            "transcode_forge.scheduler_cron.runner.start_scan", side_effect=lambda **kw: finished()
        ) as start,
    ):
        await scheduler_cron._tick(MagicMock(), db)
    assert [call.kwargs["library_id"] for call in start.call_args_list] == ["b"]


async def test_migration_0018_backfills_by_name_then_by_stray_id(db):
    """Rows written before 0018 carry only the name. The backfill resolves
    it to the first library created under that name, adopts a pre-0008
    stray that holds the id in the name column, and leaves a name no
    library carries as NULL."""
    solo = await lib_repo.create_library(db, name="Solo", media_type="movies", path="/solo")
    dup_first = await lib_repo.create_library(db, name="Dup", media_type="movies", path="/dup1")
    await lib_repo.create_library(db, name="Dup", media_type="movies", path="/dup2")

    def job(library: str) -> Job:
        return Job(
            source_path=f"/{library}.mkv", library=library, source_codec="h264", quality_value=21
        )

    by_name, by_dup, stray, ghost = job("Solo"), job("Dup"), job(solo), job("Ghost")
    for j in (by_name, by_dup, stray, ghost):
        await job_repo.create_job(db, j)
    scan = Scan(library="Solo")
    await scan_repo.create_scan(db, scan)
    await skip_repo.record_skip(
        db, file_path="/skip.mkv", library="Solo", codec="vp9", skip_reason=SkipReason.NOT_H264
    )
    await db.commit()

    sql = next(text for version, _name, text, _pg in discover_migrations() if version == 18)
    for statement in _split_statements(sql):
        if statement.upper().startswith("UPDATE"):
            await db.execute(statement)
    await db.commit()

    async def job_library_id(job_id: str) -> str | None:
        found = await job_repo.get_job(db, job_id)
        assert found is not None
        return found.library_id

    assert await job_library_id(by_name.id) == solo
    assert await job_library_id(by_dup.id) == dup_first
    assert await job_library_id(stray.id) == solo
    assert await job_library_id(ghost.id) is None
    found_scan = await scan_repo.get_scan(db, scan.id)
    assert found_scan is not None and found_scan.library_id == solo
    skips, _ = await skip_repo.list_skipped(db, library_id=solo)
    assert [s.file_path for s in skips] == ["/skip.mkv"]


async def test_stats_by_library_is_keyed_by_id_with_the_current_name(client: AsyncClient, app):
    """A renamed library is one line under its new name; jobs whose library
    is gone are grouped under the name they were written with."""
    db = app.state.db
    lib_id = await lib_repo.create_library(db, name="Movies", media_type="movies", path="/m")
    for path, library, library_id in [
        ("/m/a.mkv", "Movies", lib_id),
        ("/m/b.mkv", "Movies", lib_id),
        ("/old/c.mkv", "Old", None),
        ("/older/d.mkv", "Older", None),
    ]:
        j = Job(
            source_path=path,
            library=library,
            library_id=library_id,
            source_codec="h264",
            quality_value=21,
            status=JobStatus.COMPLETE,
        )
        await job_repo.create_job(db, j)
        await job_repo.update_job(db, j.id, status=JobStatus.COMPLETE, space_saved=10)
    await client.put(f"/api/libraries/{lib_id}", json={"name": "Films"})

    by_library = (await client.get("/api/stats")).json()["data"]["by_library"]
    assert by_library[lib_id] == {"name": "Films", "completed": 2, "space_saved_bytes": 20}
    assert by_library["name:Old"] == {"name": "Old", "completed": 1, "space_saved_bytes": 10}
    assert by_library["name:Older"] == {"name": "Older", "completed": 1, "space_saved_bytes": 10}

    page = (await client.get("/partials/stats")).text
    assert "Films" in page and "Old" in page and "Older" in page


async def test_job_filters_match_the_id_not_the_name(client: AsyncClient, app):
    db = app.state.db
    lib_id = await lib_repo.create_library(db, name="Movies", media_type="movies", path="/m")
    j = Job(
        source_path="/m/a.mkv",
        library="Movies",
        library_id=lib_id,
        source_codec="h264",
        quality_value=21,
    )
    await job_repo.create_job(db, j)

    assert (await client.get(f"/api/jobs?library_id={lib_id}")).json()["meta"]["total"] == 1
    assert (await client.get("/api/jobs?library_id=Movies")).json()["meta"]["total"] == 0
    assert (await client.get("/api/jobs?library=Movies")).json()["meta"]["total"] == 1, (
        "an unknown query param is ignored, not treated as a name filter"
    )

"""Jobs queued via the media API must carry the library NAME.

Regression guard: /api/media/queue stored the library UUID while the
scanner/seed paths stored the name — every library filter matches on
name, so media-queued jobs were invisible to filtering. (The old tests
missed it because their fixture library had id == name.)
"""

from httpx import AsyncClient

from transcode_forge.repos import jobs as job_repo
from transcode_forge.repos import libraries as lib_repo
from transcode_forge.repos import media as media_repo


async def _seed_uuid_library_file(db) -> tuple[str, str]:
    """A library whose id is a real UUID (≠ name), with one h264 file."""
    lib_id = await lib_repo.create_library(
        db,
        name="movies",
        media_type="movies",
        path="/media/movies",
        quality_preset=21,
        auto_scan=False,
        scan_interval_hours=24,
    )
    assert lib_id != "movies", "fixture must exercise the uuid≠name case"
    file_id = await media_repo.upsert_media_file(
        db,
        library_id=lib_id,
        file_path="/media/movies/Film (2020)/Film (2020).mkv",
        filename="Film (2020).mkv",
        video_codec="h264",
        audio_codec="aac",
        resolution="1080p",
        width=1920,
        height=1080,
        bitrate=8_000_000,
        duration=7200.0,
        file_size=8_000_000_000,
    )
    return lib_id, file_id


async def test_media_queue_stores_library_name(client: AsyncClient, app):
    db = app.state.db
    lib_id, file_id = await _seed_uuid_library_file(db)

    resp = await client.post("/api/media/queue", json={"file_ids": [file_id]})
    assert resp.status_code == 200
    assert resp.json()["queued"] == 1

    jobs, _ = await job_repo.list_jobs(db)
    assert jobs[0].library == "movies"
    assert jobs[0].library != lib_id


async def test_library_filter_sees_media_queued_jobs(client: AsyncClient, app):
    db = app.state.db
    _, file_id = await _seed_uuid_library_file(db)
    await client.post("/api/media/queue", json={"file_ids": [file_id]})

    api = (await client.get("/api/jobs?library=movies")).json()
    assert api["meta"]["total"] == 1

    partial = (await client.get("/partials/jobs?library=movies")).text
    assert partial.count("data-job-id=") == 1

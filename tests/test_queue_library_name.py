"""Jobs queued via the media API carry the library id as the key and the
name as a label.

History: /api/media/queue once stored the library UUID in `library` while
the scanner/seed paths stored the name, and every filter matched on name,
so media-queued jobs were invisible to filtering (the old tests missed it
because their fixture library had id == name). Since migration 0018 the
filters match `library_id`; the name column is the label at queue time.
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


async def test_media_queue_stores_library_id_and_name(client: AsyncClient, app):
    db = app.state.db
    lib_id, file_id = await _seed_uuid_library_file(db)

    resp = await client.post("/api/media/queue", json={"file_ids": [file_id]})
    assert resp.status_code == 200
    assert resp.json()["queued"] == 1

    jobs, _ = await job_repo.list_jobs(db)
    assert jobs[0].library_id == lib_id
    assert jobs[0].library == "movies"


async def test_library_filter_sees_media_queued_jobs(client: AsyncClient, app):
    db = app.state.db
    lib_id, file_id = await _seed_uuid_library_file(db)
    await client.post("/api/media/queue", json={"file_ids": [file_id]})

    api = (await client.get(f"/api/jobs?library_id={lib_id}")).json()
    assert api["meta"]["total"] == 1

    partial = (await client.get(f"/partials/jobs?library_id={lib_id}")).text
    assert partial.count("data-job-id=") == 1

    assert (await client.get("/api/jobs?library_id=movies")).json()["meta"]["total"] == 0, (
        "the name is a label, not a key"
    )

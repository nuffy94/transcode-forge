"""A path whose last job finished (any terminal status) can be queued again.

active_paths decides "this path already has a live job"; live means
waiting or active (models/job.py), so a skipped or complete job must not
block a new one at the same path.
"""

import pytest
from httpx import AsyncClient

from tests.helpers import seed_media_file
from transcode_forge.models.job import Job, JobStatus
from transcode_forge.repos import jobs as job_repo
from transcode_forge.repos import media as media_repo


async def _job_at(db, path: str, status: JobStatus) -> str:
    job = Job(
        source_path=path,
        library="movies",
        source_codec="h264",
        quality_value=21,
        status=status,
    )
    return await job_repo.create_job(db, job)


class TestActivePaths:
    @pytest.mark.parametrize(
        "status",
        [JobStatus.SKIPPED, JobStatus.COMPLETE, JobStatus.FAILED, JobStatus.CANCELLED],
    )
    async def test_finished_job_does_not_block(self, db, status):
        await _job_at(db, "/done.mkv", status)
        assert await job_repo.active_paths(db, ["/done.mkv"]) == set()

    @pytest.mark.parametrize(
        "status",
        [JobStatus.PENDING, JobStatus.QUEUED, JobStatus.ASSIGNED, JobStatus.TRANSCODING],
    )
    async def test_live_job_blocks(self, db, status):
        await _job_at(db, "/live.mkv", status)
        assert await job_repo.active_paths(db, ["/live.mkv"]) == {"/live.mkv"}


class TestQueueAfterTerminalJob:
    async def test_skipped_file_queues_again_after_unskip(self, client: AsyncClient, app):
        db = app.state.db
        path = "/media/movies/gated.mkv"
        file_id = await seed_media_file(db, path)
        job_id = await _job_at(db, path, JobStatus.SKIPPED)
        await media_repo.update_media_status(
            db, file_id, transcode_status="skipped", skip_reason="vmaf_gate", job_id=job_id
        )

        r = await client.post("/api/media/unskip", json={"file_ids": [file_id]})
        assert r.status_code == 200
        r = await client.post("/api/media/queue", json={"file_ids": [file_id]})
        assert r.status_code == 200
        assert r.json()["queued"] == 1

    async def test_completed_path_with_new_content_queues_again(self, client: AsyncClient, app):
        """O11: a completed HEVC file replaced by a new H.264 file at the
        same path, rescanned back to needs_transcode, must queue."""
        db = app.state.db
        path = "/media/movies/replaced.mkv"
        await _job_at(db, path, JobStatus.COMPLETE)
        file_id = await seed_media_file(db, path, codec="h264")
        row = (await media_repo.get_by_ids(db, [file_id]))[0]
        assert row["transcode_status"] == "needs_transcode"

        r = await client.post("/api/media/queue", json={"file_ids": [file_id]})
        assert r.status_code == 200
        assert r.json()["queued"] == 1

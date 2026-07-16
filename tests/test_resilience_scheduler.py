"""Worker-resilience train, scheduler side — contract tests.

Written against plans/worker-resilience-spec.md D3 (PR A):

  - Idempotent terminal receipts: at-least-once delivery makes duplicate
    reports normal — same outcome again is a 204 no-op with NO side
    effects re-fired; a conflicting terminal report is refused (409),
    because the first outcome won and no report-path event may flip it.
    (Spec contract test 4 lives here scheduler-side; PR B's hostile
    harness re-exercises it end-to-end.)
  - Reconciliation sweep: a job whose LIVE worker has been heartbeating
    a different (or no) current_job_id past the grace is abandoned →
    requeued. A worker heartbeating THE job's id is NEVER swept no
    matter how stale the job row is — the long-gauge safety property
    (spec contract test 8).
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from tests.helpers import register_worker
from transcode_forge.models.job import Job, JobStatus
from transcode_forge.models.worker import Worker, WorkerStatus
from transcode_forge.repos import jobs as job_repo
from transcode_forge.repos import libraries as lib_repo
from transcode_forge.repos import media as media_repo
from transcode_forge.repos import workers as worker_repo

# ── Seeding helpers ─────────────────────────────────────────────────────────


async def _seed_h264_file(db, path: str = "/media/movies/film.mkv") -> str:
    """Movies library + one catalogued h264 file; returns file id."""
    if not await lib_repo.path_in_use(db, "/media/movies"):
        await db.execute(
            """INSERT INTO libraries (id, name, media_type, path, quality_preset,
                enabled, auto_scan, scan_interval_hours, created_at, updated_at)
            VALUES ('movies', 'movies', 'movies', '/media/movies', 21, 1, 0, 24,
                '2026-01-01', '2026-01-01')""",
        )
        await db.commit()
    return await media_repo.upsert_media_file(
        db,
        library_id="movies",
        file_path=path,
        filename=Path(path).name,
        video_codec="h264",
        audio_codec="aac",
        resolution="1920x1080",
        width=1920,
        height=1080,
        bitrate=8_000_000,
        duration=5400.0,
        file_size=4_000_000_000,
    )


def _ago(seconds: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


async def _seed_active_pair(
    db,
    *,
    worker_names_job: bool,
    job_age_seconds: float,
    mismatch_age_seconds: float | None,
    worker_status: str = "online",
    tag: str = "x",
) -> tuple[str, str]:
    """A TRANSCODING job assigned to a worker whose heartbeat state is
    fully controlled: which job it names, since when, and its liveness.
    mismatch_age_seconds=None leaves current_job_changed_at NULL (a
    pre-migration worker that has never reported a transition)."""
    worker = Worker(name=f"w-{tag}", host=f"h-{tag}", status=WorkerStatus.ONLINE)
    await worker_repo.upsert_worker(db, worker)
    job = Job(
        source_path=f"/media/movies/{tag}.mkv",
        library="movies",
        source_codec="h264",
        quality_value=21,
        status=JobStatus.PENDING,
    )
    await job_repo.create_job(db, job)
    await db.execute(
        "UPDATE jobs SET status = ?, worker_id = ?, updated_at = ? WHERE id = ?",
        (JobStatus.TRANSCODING.value, worker.id, _ago(job_age_seconds), job.id),
    )
    await db.execute(
        "UPDATE workers SET status = ?, current_job_id = ?, current_job_changed_at = ?,"
        " last_heartbeat = ? WHERE id = ?",
        (
            worker_status,
            job.id if worker_names_job else None,
            None if mismatch_age_seconds is None else _ago(mismatch_age_seconds),
            _ago(1),
            worker.id,
        ),
    )
    await db.commit()
    return job.id, worker.id


async def _job_status(db, job_id: str) -> tuple[str, str | None]:
    job = await job_repo.get_job(db, job_id)
    assert job is not None
    return job.status.value, job.worker_id


# ── Idempotent terminal receipts ────────────────────────────────────────────


async def _claimed_job(client: AsyncClient, app, c: AsyncClient, name: str) -> tuple[str, dict]:
    """Queue one file and claim it as a fresh worker; returns (job_id, headers)."""
    file_id = await _seed_h264_file(app.state.db, path=f"/media/movies/{name}.mkv")
    resp = await client.post("/api/media/queue", json={"file_ids": [file_id]})
    assert resp.status_code == 200
    headers, worker_id = await register_worker(client, c, name, ["hevc"])
    r = await c.post("/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers)
    job = r.json()["job"]
    assert job is not None
    return job["id"], headers


async def test_duplicate_complete_is_noop_and_side_effects_fire_once(client: AsyncClient, app):
    """Contract test 4 (scheduler half): the same completion delivered
    twice → both 204, job state from the FIRST report, media-status sync
    NOT re-fired on the duplicate."""
    db = app.state.db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        job_id, headers = await _claimed_job(client, app, c, "dup-complete")
        body = {"output_size": 1000, "space_saved": 9000, "source_size": 10000}
        r = await c.post(f"/api/worker/job/{job_id}/complete", json=body, headers=headers)
        assert r.status_code == 204

        # Plant a sentinel: if the duplicate re-fires the media sync, this
        # flips back to 'complete' and the assert below catches it.
        await db.execute(
            "UPDATE media_files SET transcode_status = 'queued'"
            " WHERE file_path = '/media/movies/dup-complete.mkv'"
        )
        await db.commit()

        dup = {"output_size": 555, "space_saved": 1, "source_size": 10000}
        r = await c.post(f"/api/worker/job/{job_id}/complete", json=dup, headers=headers)
        assert r.status_code == 204

    job = await job_repo.get_job(db, job_id)
    assert job.status == JobStatus.COMPLETE
    assert job.output_size == 1000  # first report won; duplicate ignored
    async with db.execute(
        "SELECT transcode_status FROM media_files"
        " WHERE file_path = '/media/movies/dup-complete.mkv'"
    ) as cur:
        row = await cur.fetchone()
    assert row["transcode_status"] == "queued"  # sentinel untouched


async def test_duplicate_skipped_records_single_skip(client: AsyncClient, app):
    """Duplicate skip delivery → 204 + exactly one skipped_files row."""
    from transcode_forge.repos import skipped as skip_repo

    db = app.state.db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        job_id, headers = await _claimed_job(client, app, c, "dup-skip")
        body = {"reason": "below_vmaf_floor", "error_message": "perc5 80", "achieved_vmaf": 80.0}
        for _ in range(2):
            r = await c.post(f"/api/worker/job/{job_id}/skipped", json=body, headers=headers)
            assert r.status_code == 204

    _, total = await skip_repo.list_skipped(db, reason="below_vmaf_floor")
    assert total == 1
    job = await job_repo.get_job(db, job_id)
    assert job.status == JobStatus.SKIPPED


async def test_duplicate_failed_is_noop(client: AsyncClient, app):
    db = app.state.db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        job_id, headers = await _claimed_job(client, app, c, "dup-fail")
        r = await c.post(
            f"/api/worker/job/{job_id}/failed",
            json={"error_message": "boom", "retry_count": 1},
            headers=headers,
        )
        assert r.status_code == 204
        r = await c.post(
            f"/api/worker/job/{job_id}/failed",
            json={"error_message": "boom again", "retry_count": 7},
            headers=headers,
        )
        assert r.status_code == 204

    job = await job_repo.get_job(db, job_id)
    assert job.status == JobStatus.FAILED
    assert job.retry_count == 1  # first report won
    assert job.error_message == "boom"


async def test_conflicting_terminal_report_is_refused(client: AsyncClient, app):
    """A report that would FLIP a terminal outcome is a 409, and neither
    the job row nor the catalog moves. The 'report failure after
    successful swap' lie dies here."""
    db = app.state.db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        job_id, headers = await _claimed_job(client, app, c, "conflict")
        body = {"output_size": 1000, "space_saved": 9000, "source_size": 10000}
        r = await c.post(f"/api/worker/job/{job_id}/complete", json=body, headers=headers)
        assert r.status_code == 204

        r = await c.post(
            f"/api/worker/job/{job_id}/failed",
            json={"error_message": "network said no", "retry_count": 1},
            headers=headers,
        )
        assert r.status_code == 409

    job = await job_repo.get_job(db, job_id)
    assert job.status == JobStatus.COMPLETE
    assert job.error_message is None
    async with db.execute(
        "SELECT transcode_status FROM media_files WHERE file_path = '/media/movies/conflict.mkv'"
    ) as cur:
        row = await cur.fetchone()
    assert row["transcode_status"] == "complete"  # not flipped to needs_transcode


async def test_complete_after_failed_is_refused(client: AsyncClient, app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        job_id, headers = await _claimed_job(client, app, c, "late-complete")
        r = await c.post(
            f"/api/worker/job/{job_id}/failed",
            json={"error_message": "x", "retry_count": 0},
            headers=headers,
        )
        assert r.status_code == 204
        r = await c.post(
            f"/api/worker/job/{job_id}/complete",
            json={"output_size": 1, "space_saved": 1, "source_size": 2},
            headers=headers,
        )
        assert r.status_code == 409

    job = await job_repo.get_job(app.state.db, job_id)
    assert job.status == JobStatus.FAILED


# ── Heartbeat transition tracking ───────────────────────────────────────────


async def _changed_at(db, worker_id: str) -> str | None:
    async with db.execute(
        "SELECT current_job_changed_at FROM workers WHERE id = ?", (worker_id,)
    ) as cur:
        row = await cur.fetchone()
    return row["current_job_changed_at"]


async def test_heartbeat_stamps_transitions_not_steady_state(db):
    """current_job_changed_at moves ONLY when the named job changes
    (None→X, X→Y, X→None) — steady-state heartbeats leave it alone, so
    'mismatch sustained since T' is readable straight off the row."""
    worker = Worker(name="hb", host="h", status=WorkerStatus.ONLINE)
    await worker_repo.upsert_worker(db, worker)

    await worker_repo.update_worker_heartbeat(db, worker.id, current_job_id=None)
    assert await _changed_at(db, worker.id) is None  # None→None: no transition

    await worker_repo.update_worker_heartbeat(db, worker.id, current_job_id="job-a")
    t1 = await _changed_at(db, worker.id)
    assert t1 is not None  # None→a stamps

    await worker_repo.update_worker_heartbeat(db, worker.id, current_job_id="job-a")
    assert await _changed_at(db, worker.id) == t1  # a→a: steady

    await worker_repo.update_worker_heartbeat(db, worker.id, current_job_id="job-b")
    t2 = await _changed_at(db, worker.id)
    assert t2 is not None and t2 >= t1  # a→b stamps

    await worker_repo.update_worker_heartbeat(db, worker.id, current_job_id=None)
    t3 = await _changed_at(db, worker.id)
    assert t3 is not None and t3 >= t2  # b→None stamps


# ── Reconciliation sweep ────────────────────────────────────────────────────


async def test_abandoned_job_is_requeued(db):
    """Contract test 8 (requeue half): live worker heartbeating NULL past
    the grace while the job claims TRANSCODING → requeued clean."""
    job_id, _ = await _seed_active_pair(
        db, worker_names_job=False, job_age_seconds=600, mismatch_age_seconds=600, tag="aband"
    )
    requeued = await job_repo.requeue_abandoned_active_jobs(db, grace_seconds=120)
    assert [r["id"] for r in requeued] == [job_id]
    status, worker_id = await _job_status(db, job_id)
    assert status == JobStatus.QUEUED.value
    assert worker_id is None


async def test_long_gauge_job_is_never_swept(db):
    """Contract test 8 (safety half): the worker names THE job in every
    heartbeat — hours of updated_at silence (a long VMAF gauge) must not
    cost it the job, under EITHER sweep."""
    job_id, _ = await _seed_active_pair(
        db, worker_names_job=True, job_age_seconds=7200, mismatch_age_seconds=7200, tag="gauge"
    )
    assert await job_repo.requeue_abandoned_active_jobs(db, grace_seconds=120) == []
    assert await job_repo.requeue_orphan_active_jobs(db, min_idle_seconds=600) == []
    status, worker_id = await _job_status(db, job_id)
    assert status == JobStatus.TRANSCODING.value
    assert worker_id is not None


async def test_fresh_claim_is_not_swept(db):
    """A just-claimed job (fresh updated_at) is safe even though the
    worker's last transition (to NULL, after its previous job) is old —
    the claim-race guard."""
    job_id, _ = await _seed_active_pair(
        db, worker_names_job=False, job_age_seconds=5, mismatch_age_seconds=3600, tag="fresh"
    )
    assert await job_repo.requeue_abandoned_active_jobs(db, grace_seconds=120) == []
    status, _ = await _job_status(db, job_id)
    assert status == JobStatus.TRANSCODING.value


async def test_recent_mismatch_is_not_swept(db):
    """Mismatch younger than the grace (report likely in flight) → wait."""
    job_id, _ = await _seed_active_pair(
        db, worker_names_job=False, job_age_seconds=600, mismatch_age_seconds=30, tag="recent"
    )
    assert await job_repo.requeue_abandoned_active_jobs(db, grace_seconds=120) == []
    status, _ = await _job_status(db, job_id)
    assert status == JobStatus.TRANSCODING.value


async def test_pre_migration_worker_is_not_swept(db):
    """current_job_changed_at NULL (no transition observed since the
    column existed) → unknown, never swept on a guess."""
    job_id, _ = await _seed_active_pair(
        db, worker_names_job=False, job_age_seconds=600, mismatch_age_seconds=None, tag="premig"
    )
    assert await job_repo.requeue_abandoned_active_jobs(db, grace_seconds=120) == []
    status, _ = await _job_status(db, job_id)
    assert status == JobStatus.TRANSCODING.value


async def test_dead_worker_is_orphan_territory_not_abandoned(db):
    """A DEAD worker's job belongs to the orphan sweep — the abandoned
    sweep only ever acts on evidence from a live heartbeat."""
    job_id, _ = await _seed_active_pair(
        db,
        worker_names_job=False,
        job_age_seconds=600,
        mismatch_age_seconds=600,
        worker_status="dead",
        tag="deadw",
    )
    assert await job_repo.requeue_abandoned_active_jobs(db, grace_seconds=120) == []
    # ...and the orphan sweep DOES take it (idle past its grace).
    requeued = await job_repo.requeue_orphan_active_jobs(db, min_idle_seconds=120)
    assert [r["id"] for r in requeued] == [job_id]


# ── Audit surface ───────────────────────────────────────────────────────────


async def test_audit_reports_abandoned_jobs(client: AsyncClient, app):
    db = app.state.db
    job_id, _ = await _seed_active_pair(
        db, worker_names_job=False, job_age_seconds=600, mismatch_age_seconds=600, tag="audit"
    )
    resp = await client.get("/api/audit/integrity")
    assert resp.status_code == 200
    data = resp.json()
    assert data["healthy"] is False
    assert data["abandoned_count"] == 1
    assert data["abandoned_active_jobs"][0]["id"] == job_id
    assert data["abandoned_active_jobs"][0]["worker_current_job_id"] is None
    assert data["orphan_count"] == 0  # live worker — not an orphan

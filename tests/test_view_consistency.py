"""Cross-view data-consistency tests.

The same database column is read by multiple endpoints — the dashboard
stat tile, the sidebar queue badge, the scheduler-info card, the queue
page banner, and the /api/stats response. Each currently issues its
own raw SQL with hand-typed status strings. Any drift between them
silently misleads the operator (e.g., dashboard says 5 queued, queue
page shows 0).

These tests seed the database into a known shape, then assert every
view of the same data agrees on the same number.

Add a new "view of the same data" — add it to the relevant test here.
"""

import re
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient

from transcode_forge.models.job import Job, JobStatus
from transcode_forge.models.worker import Worker, WorkerStatus
from transcode_forge.repos import jobs as job_repo
from transcode_forge.repos import workers as worker_repo

# --- helpers ---------------------------------------------------------


def _job(status: JobStatus, name: str, library: str = "movies") -> Job:
    return Job(
        source_path=f"/test/{name}.mkv",
        library=library,
        source_codec="h264",
        quality_value=21,
        status=status,
    )


async def _seed_jobs(db, **counts: int) -> None:
    """Insert N jobs per status. Keys must match JobStatus values."""
    for status_name, n in counts.items():
        status = JobStatus(status_name)
        for i in range(n):
            await job_repo.create_job(db, _job(status, f"{status_name}-{i}"))


def _extract_stat(html: str, label: str) -> int:
    """Pull the zero-padded integer out of a forge-stat panel by its label."""
    pattern = rf">{re.escape(label)}<.*?forge-stat-value[^>]*>(\d+)<"
    m = re.search(pattern, html, re.DOTALL)
    if m is None:
        raise AssertionError(f"stat label {label!r} not found in HTML")
    return int(m.group(1))


def _extract_scheduler_info_queue(html: str) -> int:
    """The scheduler-info card formats queue count as a tabular-nums span."""
    m = re.search(r"jobs-queued[^>]*>(\d+)<", html, re.DOTALL)
    if m is None:
        raise AssertionError("scheduler-info queue count not found")
    return int(m.group(1))


# --- the actual tests ------------------------------------------------


class TestQueueCountConsistency:
    """Three+ places count "pending OR queued" jobs. They MUST agree."""

    async def test_all_zero_when_empty(self, client: AsyncClient):
        dash = (await client.get("/partials/dashboard-stats")).text
        badge = (await client.get("/partials/queue-badge")).text.strip()
        sched = (await client.get("/partials/scheduler-info")).text
        stats = (await client.get("/api/stats")).json()["data"]["jobs_by_status"]

        assert _extract_stat(dash, "Queued") == 0
        assert badge == ""  # badge is blank (not "0") when empty
        assert _extract_scheduler_info_queue(sched) == 0
        assert stats.get("pending", 0) + stats.get("queued", 0) == 0

    async def test_pending_and_queued_both_count(self, client: AsyncClient, app):
        """A 'pending' job and a 'queued' job both count toward queue depth."""
        db = app.state.db
        await _seed_jobs(db, pending=3, queued=2)

        dash = _extract_stat((await client.get("/partials/dashboard-stats")).text, "Queued")
        badge = (await client.get("/partials/queue-badge")).text.strip()
        sched = (await client.get("/partials/scheduler-info")).text
        stats = (await client.get("/api/stats")).json()["data"]["jobs_by_status"]
        api_total = stats.get("pending", 0) + stats.get("queued", 0)

        assert dash == 5
        assert badge == "5"
        assert _extract_scheduler_info_queue(sched) == 5
        assert api_total == 5

    async def test_other_statuses_do_not_count(self, client: AsyncClient, app):
        """transcoding/complete/failed/cancelled must NOT count toward queue depth."""
        db = app.state.db
        await _seed_jobs(
            db,
            pending=1,
            queued=1,
            transcoding=4,
            complete=10,
            failed=2,
            skipped=3,
            cancelled=2,
        )

        dash = _extract_stat((await client.get("/partials/dashboard-stats")).text, "Queued")
        badge = (await client.get("/partials/queue-badge")).text.strip()

        assert dash == 2, "only pending+queued count toward queue depth"
        assert badge == "2"


class TestCompletedCountConsistency:
    """Dashboard 'Completed' tile, /api/stats completed, and stats partial must agree."""

    async def test_completed_agrees_across_sources(self, client: AsyncClient, app):
        db = app.state.db
        await _seed_jobs(db, complete=7, failed=2, skipped=3, transcoding=1)

        dash = _extract_stat((await client.get("/partials/dashboard-stats")).text, "Completed")
        stats = (await client.get("/api/stats")).json()["data"]
        stats_completed = stats["completed"]
        by_status_completed = stats["jobs_by_status"].get("complete", 0)

        assert dash == 7
        assert stats_completed == 7
        assert by_status_completed == 7


class TestActiveTranscodesConsistency:
    """The dashboard 'Active Transcodes' list and the queue page banner
    both purport to show the same set of in-flight jobs.
    """

    async def test_active_list_includes_transcoding_assigned_verifying(
        self, client: AsyncClient, app
    ):
        db = app.state.db
        await _seed_jobs(db, transcoding=2, assigned=1, verifying=1, pending=5, complete=3)

        active_html = (await client.get("/partials/active-transcodes")).text
        # Each active job is rendered as a row keyed by data-progress-bar
        active_rows = active_html.count("data-progress-bar")
        stats = (await client.get("/api/stats")).json()["data"]["jobs_by_status"]
        api_active = (
            stats.get("transcoding", 0) + stats.get("assigned", 0) + stats.get("verifying", 0)
        )

        assert active_rows == 4
        assert api_active == 4

    async def test_orphan_transcoding_jobs_are_visible(self, client: AsyncClient, app):
        """Jobs stuck in 'transcoding' whose worker is DEAD are the
        production failure mode that motivated this whole suite. The
        dashboard MUST render them — silently dropping orphans would
        let them rot indefinitely.
        """
        db = app.state.db
        # Seed a worker as DEAD, then a transcoding job assigned to it
        dead_worker = Worker(
            name="ghost-worker",
            host="worker-3",
            status=WorkerStatus.DEAD,
            last_heartbeat=datetime.now(UTC) - timedelta(days=30),
        )
        await worker_repo.upsert_worker(db, dead_worker)
        # Backdate the heartbeat (upsert always stamps now())
        await db.execute(
            "UPDATE workers SET last_heartbeat = ?, status = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(days=30)).isoformat(), "dead", dead_worker.id),
        )
        await db.commit()

        orphan = _job(JobStatus.TRANSCODING, "orphaned")
        await job_repo.create_job(db, orphan)
        await job_repo.update_job(
            db, orphan.id, status="transcoding", worker_id=dead_worker.id, progress=0.0
        )

        active_html = (await client.get("/partials/active-transcodes")).text
        assert "orphaned.mkv" in active_html, (
            "orphaned 'transcoding' jobs must surface somewhere — "
            "silently dropping them is a worse bug than the inconsistency"
        )


class TestQueueTableTotalConsistency:
    """The queue page table (HTMX partial) and the JSON API for the
    same data must agree on the unfiltered total.
    """

    async def test_jobs_partial_total_matches_api(self, client: AsyncClient, app):
        db = app.state.db
        await _seed_jobs(db, pending=2, queued=1, complete=3, failed=1)

        partial = (await client.get("/partials/jobs")).text
        api = (await client.get("/api/jobs?per_page=100")).json()

        # The partial shows row count equal to total (when total <= per_page)
        partial_rows = partial.count("data-job-id=")
        assert partial_rows == 7
        assert api["meta"]["total"] == 7

    async def test_jobs_partial_status_filter_matches_api(self, client: AsyncClient, app):
        db = app.state.db
        await _seed_jobs(db, pending=2, queued=1, complete=3)

        partial = (await client.get("/partials/jobs?status=complete")).text
        api = (await client.get("/api/jobs?status=complete")).json()

        assert partial.count("data-job-id=") == 3
        assert api["meta"]["total"] == 3


class TestStatsPartialAgreesWithApi:
    """The /partials/stats template renders numbers that must match
    /api/stats for the same DB state.
    """

    async def test_jobs_by_status_appears_in_html(self, client: AsyncClient, app):
        db = app.state.db
        await _seed_jobs(db, complete=5, failed=2, transcoding=1)

        html = (await client.get("/partials/stats")).text
        api = (await client.get("/api/stats")).json()["data"]["jobs_by_status"]

        # Stats partial shows total completed somewhere; the value must match API
        assert api == {"complete": 5, "failed": 2, "transcoding": 1}
        # The "5" for completed should appear in the stats partial output
        assert "5" in html


class TestWorkerCountConsistency:
    """Dashboard 'Workers' tile counts online+busy. /api/stats agrees."""

    async def test_only_online_and_busy_count_as_active(self, client: AsyncClient, app):
        db = app.state.db
        for state in (
            (WorkerStatus.ONLINE, "alive-1"),
            (WorkerStatus.ONLINE, "alive-2"),
            (WorkerStatus.BUSY, "working"),
            (WorkerStatus.OFFLINE, "off"),
            (WorkerStatus.DEAD, "dead-1"),
            (WorkerStatus.DEAD, "dead-2"),
        ):
            status, name = state
            await worker_repo.upsert_worker(db, Worker(name=name, host="test", status=status))

        dash = _extract_stat((await client.get("/partials/dashboard-stats")).text, "Workers")
        stats = (await client.get("/api/stats")).json()["data"]["workers_by_status"]

        assert dash == 3, "online (2) + busy (1) = 3 active workers"
        assert stats.get("online", 0) + stats.get("busy", 0) == 3


class TestDataIntegrity:
    """Whole-database invariants. These catch the failure modes that
    drift between views CANNOT catch — namely, the data being internally
    inconsistent regardless of how it's rendered.

    The prod incident that motivated this suite: 3 jobs sat in
    `transcoding` status for 30+ days while their assigned workers
    were marked `dead`. The dashboard happily showed them as live.
    """

    async def test_no_active_jobs_assigned_to_dead_workers(self, app):
        """Invariant: every job in (transcoding, assigned, verifying)
        must be assigned to a worker that is online or busy.

        Failures here mean orphaned jobs are accumulating — they will
        never make progress and need to be re-queued or marked failed.
        """
        db = app.state.db
        # Seed a healthy scenario — no orphans.
        live = Worker(name="live", host="worker-4", status=WorkerStatus.ONLINE)
        await worker_repo.upsert_worker(db, live)
        for i in range(2):
            j = _job(JobStatus.TRANSCODING, f"healthy-{i}")
            await job_repo.create_job(db, j)
            await job_repo.update_job(db, j.id, status="transcoding", worker_id=live.id)

        async with db.execute(
            """
            SELECT j.id, j.status, j.worker_id, w.status AS worker_status
            FROM jobs j
            LEFT JOIN workers w ON w.id = j.worker_id
            WHERE j.status IN ('transcoding', 'assigned', 'verifying')
              AND (w.status IS NULL OR w.status NOT IN ('online', 'busy'))
            """
        ) as cur:
            orphans = await cur.fetchall()

        assert orphans == [], (
            f"Found {len(orphans)} orphaned active jobs assigned to dead/missing workers. "
            f"These will never make progress: {orphans}"
        )

    async def test_orphans_are_detectable_when_present(self, app):
        """Same query, opposite assertion — confirms the detection
        actually works when an orphan IS planted.
        """
        db = app.state.db
        ghost = Worker(name="ghost", host="ct999", status=WorkerStatus.DEAD)
        await worker_repo.upsert_worker(db, ghost)
        await db.execute("UPDATE workers SET status = ? WHERE id = ?", ("dead", ghost.id))
        await db.commit()

        j = _job(JobStatus.TRANSCODING, "orphan")
        await job_repo.create_job(db, j)
        await job_repo.update_job(db, j.id, status="transcoding", worker_id=ghost.id)

        async with db.execute(
            """
            SELECT j.id FROM jobs j
            LEFT JOIN workers w ON w.id = j.worker_id
            WHERE j.status IN ('transcoding', 'assigned', 'verifying')
              AND (w.status IS NULL OR w.status NOT IN ('online', 'busy'))
            """
        ) as cur:
            rows = await cur.fetchall()

        assert len(rows) == 1, "orphan detection query should find the planted orphan"


class TestAuditEndpoint:
    """The /api/audit/integrity endpoint exposes the invariants
    above as a runtime check — for production smoke tests and
    Prometheus scraping.
    """

    async def test_clean_state_reports_healthy(self, client: AsyncClient, app):
        db = app.state.db
        # Healthy: an alive worker, a job in flight, no orphans
        worker = Worker(name="alive", host="worker-4", status=WorkerStatus.ONLINE)
        await worker_repo.upsert_worker(db, worker)
        j = _job(JobStatus.TRANSCODING, "in-progress")
        await job_repo.create_job(db, j)
        await job_repo.update_job(db, j.id, status="transcoding", worker_id=worker.id)

        resp = await client.get("/api/audit/integrity")
        assert resp.status_code == 200
        data = resp.json()
        assert data["healthy"] is True
        assert data["orphan_count"] == 0
        assert data["orphan_active_jobs"] == []
        assert "checked_at" in data

    async def test_orphan_is_reported(self, client: AsyncClient, app):
        db = app.state.db
        ghost = Worker(name="ghost", host="ct999", status=WorkerStatus.DEAD)
        await worker_repo.upsert_worker(db, ghost)
        await db.execute("UPDATE workers SET status = ? WHERE id = ?", ("dead", ghost.id))
        await db.commit()
        j = _job(JobStatus.TRANSCODING, "stuck")
        await job_repo.create_job(db, j)
        await job_repo.update_job(db, j.id, status="transcoding", worker_id=ghost.id)

        resp = await client.get("/api/audit/integrity")
        assert resp.status_code == 200
        data = resp.json()
        assert data["healthy"] is False
        assert data["orphan_count"] == 1
        assert len(data["orphan_active_jobs"]) == 1
        orphan = data["orphan_active_jobs"][0]
        assert orphan["status"] == "transcoding"
        assert orphan["worker_status"] == "dead"
        assert "stuck.mkv" in orphan["source_path"]

    async def test_missing_worker_id_is_also_an_orphan(self, client: AsyncClient, app):
        """A worker_id pointing to a row that doesn't exist (worker was
        deleted) is the actual production failure mode. LEFT JOIN means
        worker_status is NULL — must still be flagged.
        """
        db = app.state.db
        j = _job(JobStatus.TRANSCODING, "no-worker")
        await job_repo.create_job(db, j)
        await job_repo.update_job(
            db, j.id, status="transcoding", worker_id="00000000-dead-dead-dead-000000000000"
        )

        resp = await client.get("/api/audit/integrity")
        data = resp.json()
        assert data["healthy"] is False
        assert data["orphan_count"] == 1
        assert data["orphan_active_jobs"][0]["worker_status"] is None


class TestQueueableEligibilityConsistency:
    """The "can this file be queued?" rule renders in four places: the
    queue endpoint's validity matrix (source of truth), catalog.js
    canQueue, the tv-episodes partial's per-row button, and the drawer's
    Queue button (web/routes.py `queueable`).

    The server-rendered surfaces express the NO-downscale rule; the
    endpoint accepts strictly more when an explicit target_height rides
    the request. Invariant pinned here: a surface offers Queue IFF a
    plain (no-downscale) POST would queue that file — and the downscale
    path unlocks the hevc file both surfaces decline.
    """

    async def test_drawer_and_episodes_match_endpoint_no_downscale(self, client: AsyncClient, app):
        from tests.helpers import seed_media_file

        db = app.state.db
        h264 = await seed_media_file(
            db, "/media/tv/show/s01e01.mkv", library_id="tv", show_name="Show", season=1, episode=1
        )
        hevc = await seed_media_file(
            db,
            "/media/tv/show/s01e02.mkv",
            library_id="tv",
            codec="hevc",
            show_name="Show",
            season=1,
            episode=2,
        )

        # Drawer: Queue button IFF h264 (the no-downscale rule).
        drawer_h264 = (await client.get(f"/partials/file-detail?file_id={h264}")).text
        drawer_hevc = (await client.get(f"/partials/file-detail?file_id={hevc}")).text
        assert "Queue transcode" in drawer_h264
        assert "Queue transcode" not in drawer_hevc

        # Episode rows: same rule, same outcome.
        episodes = (await client.get("/partials/tv-episodes?show=Show")).text
        assert f'data-id="{h264}"' in episodes
        assert f'data-id="{hevc}"' not in episodes

        # Endpoint, no downscale: agrees with both surfaces.
        resp = await client.post("/api/media/queue", json={"file_ids": [h264, hevc]})
        assert resp.json() == {"queued": 1, "skipped": 1}

        # Endpoint, with downscale: unlocks exactly the file the surfaces
        # decline — the documented v1 asymmetry, not silent drift.
        resp = await client.post(
            "/api/media/queue", json={"file_ids": [hevc], "target_height": 1080}
        )
        assert resp.json() == {"queued": 1, "skipped": 0}


class TestEnumCoverage:
    """Every JobStatus value must be filterable through the API and the
    partial. If someone adds a new status to the enum but forgets to
    handle it in the SQL strings, this catches it.
    """

    async def test_every_status_is_filterable(self, client: AsyncClient):
        for status in JobStatus:
            resp = await client.get(f"/api/jobs?status={status.value}")
            assert resp.status_code == 200, f"status {status.value} broke /api/jobs"
            partial = await client.get(f"/partials/jobs?status={status.value}")
            assert partial.status_code == 200, f"status {status.value} broke /partials/jobs"

    async def test_invalid_status_is_rejected(self, client: AsyncClient):
        """Sanity check — typos must fail loud, not silently match nothing."""
        resp = await client.get("/api/jobs?status=completed")  # note: 'completed' not 'complete'
        # The list_jobs whitelist raises ValueError -> 400 / 422
        assert resp.status_code in (400, 422), (
            "typo'd status should fail validation, not silently return 0 rows"
        )


class TestRecentScansSingleSource:
    """Recent scans (with their FAILED pills) render on /, /queue, and
    /activity — all three must embed the same partial endpoint, so a
    failed scan can never be visible on one surface and absent on
    another (qa ledger: activity-failed-scans-not-surfaced).
    """

    async def test_every_surface_embeds_the_same_scan_partial(self, client: AsyncClient):
        for page in ("/", "/queue", "/activity"):
            resp = await client.get(page)
            assert resp.status_code == 200
            assert 'hx-get="/partials/scan-history"' in resp.text, (
                f"{page} does not embed the shared scan-history partial"
            )

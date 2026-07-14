"""Tests for web UI routes and HTMX partials."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from transcode_forge.models.job import Job, JobStatus
from transcode_forge.repos import jobs as job_repo
from transcode_forge.repos import schedules as sched_repo


class TestSchedulesPartial:
    """Regression guard: /partials/schedules 500'd because the template used
    an invalid Jinja `&` bitwise operator (it couldn't even compile)."""

    async def test_partial_renders_empty(self, client: AsyncClient):
        resp = await client.get("/partials/schedules")
        assert resp.status_code == 200

    async def test_partial_renders_with_schedule(self, client: AsyncClient, app):
        await sched_repo.create_schedule(
            app.state.db, name="overnight", start_hour=23, end_hour=7, days_mask=31
        )
        resp = await client.get("/partials/schedules")
        assert resp.status_code == 200
        assert "overnight" in resp.text
        assert "Mon" in resp.text  # day_names(31) -> Mon..Fri


class TestProgressPollMorph:
    """Regression guards for the progress-bar blink (fixed 2026-07-09).

    The polled panels holding animated meters must morph (idiomorph), not
    innerHTML-swap: a plain swap recreates the fill elements on every poll,
    restarting the shimmer/pulse CSS animations mid-cycle (the visible blink)
    and stomping WebSocket-set widths. Jinja must also round pct the same way
    ops.js does (Math.round) or the two renderers flip-flop the label."""

    PANELS = (
        ("/", "active-transcodes"),  # dashboard, 3s poll
        ("/queue", "job-table-container"),  # queue, 5s poll
        ("/workers", "workers-container"),  # workers, 10s poll
    )

    async def test_polled_meter_panels_use_morph_swap(self, client: AsyncClient):
        for page, container_id in self.PANELS:
            resp = await client.get(page)
            assert resp.status_code == 200
            start = resp.text.index(f'id="{container_id}"')
            tag = resp.text[start : resp.text.index(">", start)]
            assert 'hx-swap="morph:innerHTML"' in tag, (page, container_id)
            assert 'hx-ext="morph"' in tag, (page, container_id)

    async def test_idiomorph_is_vendored_and_loaded(self, client: AsyncClient):
        resp = await client.get("/static/vendor/idiomorph-ext.min.js")
        assert resp.status_code == 200
        assert "Idiomorph" in resp.text[:200]
        page = await client.get("/")
        assert "/static/vendor/idiomorph-ext.min.js" in page.text

    async def test_transcoding_pct_rounds_like_the_websocket(self, client: AsyncClient, app):
        """progress=0.478 must render 48% (Math.round), never 47% (|int)."""
        db = app.state.db
        job = Job(
            source_path="/media/movies/Rounding.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
        )
        await job_repo.create_job(db, job)
        await job_repo.update_job(db, job.id, status="transcoding", progress=0.478)

        for partial in ("/partials/active-transcodes", "/partials/jobs"):
            resp = await client.get(partial)
            assert resp.status_code == 200
            assert "48%" in resp.text, partial
            assert "47%" not in resp.text, partial

    async def test_phased_job_renders_station_bar(self, client: AsyncClient, app):
        """A job with a reported phase renders the five-station pipeline bar
        with the current station active — and gate-off jobs mark Search and
        Gauge as 'off' instead of pretending they happen."""
        db = app.state.db
        job = Job(
            source_path="/media/movies/Stations.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
            target_vmaf=97.0,
        )
        await job_repo.create_job(db, job)
        await job_repo.update_job(db, job.id, status="transcoding", progress=0.3, phase="gauge")

        resp = await client.get("/partials/active-transcodes")
        assert resp.status_code == 200
        assert 'data-phase="gauge"' in resp.text
        assert 'data-station="gauge"' in resp.text
        assert "forge-station--active forge-station--timed" in resp.text
        assert "Gauging quality" in resp.text
        # Passed stations cooled, future stations pending.
        assert "forge-station--done" in resp.text
        assert "forge-station--todo" in resp.text
        # The protocol ticks carry accessible names, not hover-only meaning.
        assert 'aria-label="Lock' in resp.text
        assert 'aria-label="Unlock' in resp.text
        # forge-faint is decoration-only BY SYSTEM RULE (fails AA as text) —
        # axe caught 11 violations when todo labels used it (PR #62 round 2).
        assert "text-forge-faint" not in resp.text

    async def test_gate_off_job_marks_search_and_gauge_off(self, client: AsyncClient, app):
        db = app.state.db
        job = Job(
            source_path="/media/movies/GateOff.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
            target_vmaf=None,
        )
        await job_repo.create_job(db, job)
        await job_repo.update_job(db, job.id, status="transcoding", progress=0.5, phase="encode")

        resp = await client.get("/partials/active-transcodes")
        assert resp.status_code == 200
        assert resp.text.count("forge-station--off") == 2  # search + gauge
        assert ">off<" in resp.text

    async def test_phaseless_job_keeps_classic_meter(self, client: AsyncClient, app):
        """Pre-phase workers report no phase — the row must render exactly
        the classic meter (no stations), so mixed fleets stay coherent."""
        db = app.state.db
        job = Job(
            source_path="/media/movies/OldWorker.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
        )
        await job_repo.create_job(db, job)
        await job_repo.update_job(db, job.id, status="transcoding", progress=0.478)

        resp = await client.get("/partials/active-transcodes")
        assert resp.status_code == 200
        assert "forge-station" not in resp.text
        assert "forge-meter" in resp.text
        assert "48%" in resp.text

    async def test_workers_partial_renders_real_job_progress(self, client: AsyncClient, app):
        """The card must carry the job's actual progress — a hardcoded 0%
        placeholder dragged the WS-driven bar back to zero on every poll."""
        from transcode_forge.models.worker import Worker, WorkerStatus
        from transcode_forge.repos import workers as worker_repo

        db = app.state.db
        job = Job(
            source_path="/media/movies/OnWorker.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
        )
        await job_repo.create_job(db, job)
        await job_repo.update_job(db, job.id, status="transcoding", progress=0.42)

        worker = Worker(
            name="w-progress",
            host="192.0.2.7",
            capabilities=["cpu"],
            status=WorkerStatus.BUSY,
        )
        await worker_repo.upsert_worker(db, worker)
        await db.execute("UPDATE workers SET current_job_id = ? WHERE id = ?", (job.id, worker.id))
        await db.commit()

        resp = await client.get("/partials/workers")
        assert resp.status_code == 200
        assert "width: 42%" in resp.text
        assert ">42%<" in resp.text
        assert "width: 0%" not in resp.text


class TestPageRoutes:
    async def test_dashboard_page(self, client: AsyncClient):
        response = await client.get("/")
        assert response.status_code == 200
        assert "Transcode Forge" in response.text
        assert "Dashboard" in response.text

    async def test_queue_page(self, client: AsyncClient):
        response = await client.get("/queue")
        assert response.status_code == 200
        assert "Job queue" in response.text

    async def test_workers_page(self, client: AsyncClient):
        response = await client.get("/workers")
        assert response.status_code == 200
        assert "Workers" in response.text

    async def test_activity_page(self, client: AsyncClient):
        response = await client.get("/activity")
        assert response.status_code == 200
        assert "Activity" in response.text
        assert "Encode outcomes" in response.text
        assert "Scan skips" in response.text

    async def test_history_redirects_to_activity(self, client: AsyncClient):
        """History merged into Activity — the old URL 301s (asserted via the
        authed client; anonymous requests never reach the route)."""
        response = await client.get("/history", follow_redirects=False)
        assert response.status_code == 301
        assert response.headers["location"] == "/activity?view=outcomes"

    async def test_skipped_redirects_to_activity(self, client: AsyncClient):
        response = await client.get("/skipped", follow_redirects=False)
        assert response.status_code == 301
        assert response.headers["location"] == "/activity?view=skips"

    async def test_stats_page(self, client: AsyncClient):
        response = await client.get("/stats")
        assert response.status_code == 200
        assert "Statistics" in response.text

    @pytest.mark.parametrize(
        ("path", "title"),
        [
            ("/", "Dashboard"),
            ("/movies", "Movies"),
            ("/tv", "TV Shows"),
            ("/queue", "Queue"),
            ("/activity", "Activity"),
            ("/workers", "Workers"),
            ("/stats", "Statistics"),
            ("/settings", "Settings"),
        ],
    )
    async def test_page_titles(self, client: AsyncClient, path: str, title: str):
        """Every page sets its section in <title> (absorbed from tests/e2e)."""
        response = await client.get(path)
        assert response.status_code == 200
        match = re.search(r"<title>([^<]*)</title>", response.text)
        assert match, f"{path} has no <title>"
        assert title in match.group(1), f"{path} title is {match.group(1)!r}"


class TestPartials:
    async def test_health_partial(self, client: AsyncClient):
        response = await client.get("/partials/health")
        assert response.status_code == 200
        assert "System OK" in response.text

    async def test_jobs_partial_empty(self, client: AsyncClient):
        response = await client.get("/partials/jobs")
        assert response.status_code == 200
        assert "No jobs in queue" in response.text

    async def test_workers_partial_empty(self, client: AsyncClient):
        response = await client.get("/partials/workers")
        assert response.status_code == 200
        assert "No workers registered" in response.text

    async def test_outcomes_partial_empty(self, client: AsyncClient):
        response = await client.get("/partials/activity-outcomes")
        assert response.status_code == 200
        assert "No outcomes yet" in response.text

    async def test_skips_partial_empty(self, client: AsyncClient):
        response = await client.get("/partials/activity-skips")
        assert response.status_code == 200
        assert "No skipped files" in response.text

    async def test_skipped_unskip_uses_json_not_hx_delete(self, client: AsyncClient, app):
        """The unskip button must call unskipFile() (a JSON fetch). It used to
        use hx-delete + hx-vals, which sends form-encoded data and 422'd against
        the JSON endpoint (found by the UX sweep)."""
        from transcode_forge.models.skipped import SkipReason
        from transcode_forge.repos import skipped as skip_repo

        await skip_repo.record_skip(
            app.state.db,
            file_path="/m/skip-me.mkv",
            library="movies",
            codec="hevc",
            skip_reason=SkipReason.ALREADY_HEVC,
        )
        resp = await client.get("/partials/activity-skips")
        assert resp.status_code == 200
        assert "unskipFile(" in resp.text
        assert 'hx-delete="/api/skipped"' not in resp.text

    async def test_stats_partial(self, client: AsyncClient):
        response = await client.get("/partials/stats")
        assert response.status_code == 200
        assert "Space saved" in response.text

    async def test_stats_library_card_never_shows_minus_zero(self, client: AsyncClient, app):
        """Regression (S4b bench): the library card prefixes savings with a
        decorative minus (U+2212), so zero-reclaimed libraries (S3 — masters
        are immutable) rendered "minus-zero GiB". Sub-GiB savings too."""
        db = app.state.db
        j = Job(
            source_path="/s3/corpus.mkv",
            library="corpus-v1",
            source_codec="h264",
            quality_value=21,
        )
        await job_repo.create_job(db, j)
        await job_repo.update_job(
            db,
            j.id,
            status=JobStatus.COMPLETE,
            space_saved=0,
            source_size=1000,
            output_size=400,
        )
        response = await client.get("/partials/stats")
        assert response.status_code == 200
        assert chr(0x2212) + "0" not in response.text  # the decorative minus glyph

    async def test_skip_stats_partial_empty(self, client: AsyncClient):
        response = await client.get("/partials/skip-stats")
        assert response.status_code == 200

    async def test_queue_badge_partial_empty(self, client: AsyncClient):
        response = await client.get("/partials/queue-badge")
        assert response.status_code == 200
        assert response.text.strip() == ""

    async def test_queue_badge_partial_with_jobs(self, client: AsyncClient, app):
        db = app.state.db
        j1 = Job(source_path="/a.mkv", library="movies", source_codec="h264", quality_value=21)
        j2 = Job(source_path="/b.mkv", library="movies", source_codec="h264", quality_value=21)
        await job_repo.create_job(db, j1)
        await job_repo.create_job(db, j2)

        response = await client.get("/partials/queue-badge")
        assert response.status_code == 200
        assert response.text.strip() == "2"

    async def test_history_partial_with_status_filter(self, client: AsyncClient, app):
        db = app.state.db
        j1 = Job(
            source_path="/a.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
            status=JobStatus.COMPLETE,
        )
        j2 = Job(
            source_path="/b.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
            status=JobStatus.FAILED,
            error_message="test error",
        )
        await job_repo.create_job(db, j1)
        await job_repo.create_job(db, j2)

        # Filter for complete only
        response = await client.get("/partials/activity-outcomes?status=complete")
        assert response.status_code == 200
        assert "a.mkv" in response.text
        assert "b.mkv" not in response.text

        # Filter for failed only
        response = await client.get("/partials/activity-outcomes?status=failed")
        assert response.status_code == 200
        assert "b.mkv" in response.text
        assert "a.mkv" not in response.text

    async def test_history_partial_duration_calculation(self, client: AsyncClient, app):
        db = app.state.db
        now = datetime.now(UTC)
        j = Job(
            source_path="/dur.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
        )
        await job_repo.create_job(db, j)
        await job_repo.update_job(
            db,
            j.id,
            status="complete",
            started_at=(now - timedelta(minutes=3, seconds=45)).isoformat(),
            completed_at=now.isoformat(),
        )

        response = await client.get("/partials/activity-outcomes")
        assert response.status_code == 200
        assert "3m 45s" in response.text

    async def test_history_partial_dynamic_codec(self, client: AsyncClient, app):
        db = app.state.db
        j = Job(
            source_path="/codec.mkv",
            library="movies",
            source_codec="mpeg4",
            target_codec="hevc",
            quality_value=21,
            status=JobStatus.COMPLETE,
        )
        await job_repo.create_job(db, j)

        response = await client.get("/partials/activity-outcomes")
        assert response.status_code == 200
        assert "MPEG4" in response.text
        assert "HEVC" in response.text

    async def test_settings_page(self, client: AsyncClient):
        response = await client.get("/settings")
        assert response.status_code == 200
        assert "Settings" in response.text
        assert "Libraries" in response.text
        assert "External services" in response.text
        assert "Queue schedules" in response.text
        # tokens moved to the Workers page — settings keeps only a pointer
        assert "Manage on Workers" in response.text

    async def test_settings_schedule_inputs_are_labelled(self, client: AsyncClient):
        """The schedule Start/End hour inputs are number fields with no
        placeholder, so without an explicit for= they have no accessible name
        (axe flagged them critical). Guard the label association."""
        response = await client.get("/settings")
        assert response.status_code == 200
        assert 'for="sched-start"' in response.text
        assert 'for="sched-end"' in response.text
        assert 'for="sched-name"' in response.text

    async def test_movies_page(self, client: AsyncClient):
        response = await client.get("/movies")
        assert response.status_code == 200
        assert "Movies" in response.text

    async def test_tv_page(self, client: AsyncClient):
        response = await client.get("/tv")
        assert response.status_code == 200
        assert "TV" in response.text

    async def test_pagination_count_is_page_aware(self, client: AsyncClient):
        """The 'X to Y of Z' count must reflect the current page, not a
        hardcoded start of 1 (regression: page 7 read 'Showing 1 - 50').
        The listing logic lives in the shared catalog module now."""
        for path in ("/movies", "/tv"):
            response = await client.get(path)
            assert response.status_code == 200
            assert "/static/js/catalog.js" in response.text
        js = (await client.get("/static/js/catalog.js")).text
        # page-aware range computation, not a hardcoded start
        assert "(meta.page - 1) * meta.per_page" in js

    async def test_jobs_partial_with_data(self, client: AsyncClient, app):
        db = app.state.db
        job = Job(
            source_path="/media/movies/Test Movie.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
            source_size=2_000_000_000,
        )
        await job_repo.create_job(db, job)

        response = await client.get("/partials/jobs")
        assert response.status_code == 200
        assert "Test Movie.mkv" in response.text
        assert "movies" in response.text

    async def test_history_partial_failed_job_has_retry_button(self, client: AsyncClient, app):
        """Failed jobs in history should expose a retry button (HX-POST to /api/jobs/{id}/retry)."""
        db = app.state.db
        job = Job(
            source_path="/m.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
            status=JobStatus.FAILED,
        )
        await job_repo.create_job(db, job)
        await job_repo.update_job(db, job.id, status="failed", error_message="ffmpeg exited 1")

        response = await client.get("/partials/activity-outcomes?status=failed")
        assert response.status_code == 200
        assert f"/api/jobs/{job.id}/retry" in response.text
        assert "ffmpeg exited 1" in response.text

    async def test_recent_activity_partial_completed_uses_status_complete(
        self, client: AsyncClient, app
    ):
        """Recent activity must check job.status == 'complete' (StrEnum value), not 'completed'."""
        db = app.state.db
        now = datetime.now(UTC)
        job = Job(
            source_path="/done.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
            status=JobStatus.COMPLETE,
        )
        await job_repo.create_job(db, job)
        await job_repo.update_job(db, job.id, status="complete", completed_at=now.isoformat())

        response = await client.get("/partials/recent-activity")
        assert response.status_code == 200
        assert "done.mkv" in response.text
        # The Forge complete pill implies the `status == 'complete'` branch fired —
        # otherwise the template would render the catch-all "pending" pill instead.
        assert "forge-pill--complete" in response.text
        assert "forge-pill--pending" not in response.text

    async def test_history_partial_filters_by_library(self, client: AsyncClient, app):
        """history?library=X should drop jobs from other libraries."""
        db = app.state.db
        for path, lib in [
            ("/movies/a.mkv", "movies"),
            ("/movies/b.mkv", "movies"),
            ("/tv/c.mkv", "tv"),
        ]:
            j = Job(
                source_path=path,
                library=lib,
                source_codec="h264",
                quality_value=21,
                status=JobStatus.COMPLETE,
            )
            await job_repo.create_job(db, j)
            await job_repo.update_job(db, j.id, status="complete")

        response = await client.get("/partials/activity-outcomes?library=tv")
        assert response.status_code == 200
        assert "c.mkv" in response.text
        assert "a.mkv" not in response.text
        assert "b.mkv" not in response.text

    async def test_workers_partial_flags_stale_heartbeat(self, client: AsyncClient, app):
        """A worker last seen 31 minutes ago should render the heartbeat-stale alert."""
        from transcode_forge.models.worker import Worker, WorkerStatus
        from transcode_forge.repos import workers as worker_repo

        db = app.state.db
        worker = Worker(
            name="worker-1",
            host="192.0.2.61",
            capabilities=["qsv"],
            status=WorkerStatus.ONLINE,
        )
        await worker_repo.upsert_worker(db, worker)

        # upsert_worker stamps last_heartbeat with now(), so backdate it directly.
        stale = (datetime.now(UTC) - timedelta(minutes=31)).isoformat()
        await db.execute(
            "UPDATE workers SET last_heartbeat = ? WHERE id = ?",
            (stale, worker.id),
        )
        await db.commit()

        response = await client.get("/partials/workers")
        assert response.status_code == 200
        assert "worker-1" in response.text
        # Stale alert copy shows up in the partial when alarm tier is reached.
        assert "Heartbeat" in response.text
        assert "stale" in response.text or "lost" in response.text

    async def test_outcomes_partial_has_sortable_headers(self, client: AsyncClient, app):
        """Column headers must be click-to-sort (wired to sortOutcomes) and the
        route must accept sort/dir without erroring."""
        await job_repo.create_job(
            app.state.db,
            Job(
                source_path="/a.mkv",
                library="movies",
                source_codec="h264",
                quality_value=21,
                status=JobStatus.COMPLETE,
            ),
        )
        resp = await client.get("/partials/activity-outcomes?sort=status&dir=asc")
        assert resp.status_code == 200
        assert "sortOutcomes(" in resp.text

    async def test_jobs_partial_has_sortable_headers(self, client: AsyncClient, app):
        await job_repo.create_job(
            app.state.db,
            Job(source_path="/q.mkv", library="movies", source_codec="h264", quality_value=21),
        )
        resp = await client.get("/partials/jobs?sort=source_size&dir=desc")
        assert resp.status_code == 200
        assert "sortQueue(" in resp.text

    async def test_skips_partial_has_sortable_headers(self, client: AsyncClient, app):
        from transcode_forge.models.skipped import SkipReason
        from transcode_forge.repos import skipped as skip_repo

        await skip_repo.record_skip(
            app.state.db,
            file_path="/s.mkv",
            library="movies",
            codec="hevc",
            skip_reason=SkipReason.ALREADY_HEVC,
        )
        resp = await client.get("/partials/activity-skips?sort=file_size&dir=asc")
        assert resp.status_code == 200
        assert "sortSkips(" in resp.text


class _FakeWebSocket:
    """Minimal stand-in for starlette's WebSocket — just enough surface
    for websocket_updates (scope, headers, app.state, accept/close)."""

    def __init__(self, *, session=None, headers=None, redis=None, settings=None):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        self.scope = {"session": session or {}}
        self.headers = headers or {}
        self.app = SimpleNamespace(state=SimpleNamespace(redis=redis, settings=settings))
        self.accept = AsyncMock()
        self.close = AsyncMock()
        self.send_json = AsyncMock()


class TestWebSocketAuth:
    """/ws/updates must require an authenticated session — AuthMiddleware
    only guards HTTP scopes, so the endpoint checks on its own."""

    async def test_unauthenticated_rejected_before_accept(self):
        from transcode_forge.web.websocket import websocket_updates

        ws = _FakeWebSocket()
        await websocket_updates(ws)
        ws.accept.assert_not_called()
        ws.close.assert_called_once_with(code=1008)

    async def test_authenticated_session_accepted(self):
        from transcode_forge.api.routes.auth import SESSION_KEY
        from transcode_forge.web.websocket import websocket_updates

        ws = _FakeWebSocket(session={SESSION_KEY: "admin"}, redis=None)
        await websocket_updates(ws)
        ws.accept.assert_called_once()
        # No Redis → graceful close; HTMX polling is the fallback.
        ws.close.assert_called_once_with(code=1001, reason="Redis not available")

    async def test_cross_origin_rejected_before_accept(self):
        from transcode_forge.api.routes.auth import SESSION_KEY
        from transcode_forge.web.websocket import websocket_updates

        ws = _FakeWebSocket(
            session={SESSION_KEY: "admin"},
            headers={"origin": "http://evil.test", "host": "app.test"},
        )
        await websocket_updates(ws)
        ws.accept.assert_not_called()
        ws.close.assert_called_once_with(code=1008)


class TestWebSocketStreamRobustness:
    async def test_malformed_pubsub_message_skipped_not_fatal(self):
        """One bad event on the Redis channel must not tear down the
        WebSocket — it's skipped and the stream continues."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        from starlette.websockets import WebSocketDisconnect

        from transcode_forge.api.routes.auth import SESSION_KEY
        from transcode_forge.web.websocket import websocket_updates

        pubsub = MagicMock()
        pubsub.subscribe = AsyncMock()
        pubsub.unsubscribe = AsyncMock()
        pubsub.aclose = AsyncMock()
        pubsub.get_message = AsyncMock(
            side_effect=[
                {"type": "message", "data": "not-json{{"},
                {"type": "message", "data": '{"job_id": "j1"}'},
                WebSocketDisconnect(1000),
            ]
        )
        redis = MagicMock()
        redis.pubsub = MagicMock(return_value=pubsub)

        ws = _FakeWebSocket(
            session={SESSION_KEY: "admin"},
            redis=redis,
            settings=SimpleNamespace(redis_prefix="tf"),
        )
        await websocket_updates(ws)

        # Malformed message skipped; valid one forwarded; clean teardown.
        ws.send_json.assert_called_once_with({"job_id": "j1"})
        pubsub.unsubscribe.assert_called_once()


class TestTemplatePaginationContract:
    def test_no_template_exceeds_api_per_page_cap(self):
        """Templates must not hardcode a per_page above the API's le=200
        cap — the request would 422 and silently break the UI feature."""
        import re
        from pathlib import Path

        templates = Path("src/transcode_forge/web/templates")
        offenders = []
        for f in templates.rglob("*.html"):
            for m in re.finditer(r"per_page=(\d+)", f.read_text(encoding="utf-8")):
                if int(m.group(1)) > 200:
                    offenders.append(f"{f.name}: per_page={m.group(1)}")
        assert not offenders, f"Templates exceed the API per_page cap: {offenders}"

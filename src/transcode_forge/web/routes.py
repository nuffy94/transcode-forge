"""Web routes — HTML page rendering and HTMX partial endpoints."""

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from redis.asyncio import Redis

from transcode_forge import __version__
from transcode_forge.api.deps import get_db, get_redis
from transcode_forge.db import DBConnection, check_db_health
from transcode_forge.redis import check_redis_health
from transcode_forge.repos import exclusions as excl_repo
from transcode_forge.repos import jobs as job_repo
from transcode_forge.repos import media as media_repo
from transcode_forge.repos import scans as scan_repo
from transcode_forge.repos import schedules as sched_repo
from transcode_forge.repos import skipped as skip_repo
from transcode_forge.repos import workers as worker_repo

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
# Make the app version available to every template (e.g. the sidebar badge).
templates.env.globals["app_version"] = __version__

# Jinja2 has no bitwise '&' operator, so a template doing `days_mask & 1`
# fails to COMPILE (TemplateSyntaxError) — which 500'd /partials/schedules
# entirely. Decode the day-of-week bitmask in Python via a filter instead.
_DAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _day_names(days_mask: int | None) -> list[str]:
    mask = days_mask or 0
    return [d for i, d in enumerate(_DAY_ABBR) if mask & (1 << i)]


templates.env.filters["day_names"] = _day_names

router = APIRouter()


def _format_duration(started_at: object, completed_at: object) -> str:
    """Calculate human-readable duration between two timestamps."""
    try:
        s = (
            started_at
            if isinstance(started_at, datetime)
            else datetime.fromisoformat(str(started_at))
        )
        e = (
            completed_at
            if isinstance(completed_at, datetime)
            else datetime.fromisoformat(str(completed_at))
        )
        secs = max(0, int((e - s).total_seconds()))
        if secs < 60:
            return f"{secs}s"
        if secs < 3600:
            return f"{secs // 60}m {secs % 60}s"
        return f"{secs // 3600}h {(secs % 3600) // 60}m"
    except (ValueError, TypeError):
        return "—"


def _render(request: Request, name: str, context: dict[str, Any] | None = None) -> Response:
    ctx = context or {}
    return templates.TemplateResponse(request, name, ctx)


# -- Full page routes --


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, db: DBConnection = Depends(get_db)) -> Response:
    from transcode_forge.repos import users as user_repo

    if not await user_repo.has_admin(db):
        return Response(status_code=302, headers={"Location": "/setup"})
    return _render(request, "login.html", {"setup_required": False})


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request, db: DBConnection = Depends(get_db)) -> Response:
    from transcode_forge.repos import users as user_repo

    if await user_repo.has_admin(db):
        return Response(status_code=302, headers={"Location": "/login"})
    return _render(request, "setup.html", {})


@router.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request) -> Response:
    return _render(request, "dashboard.html", {"active_page": "dashboard"})


@router.get("/movies", response_class=HTMLResponse)
async def movies_page(request: Request) -> Response:
    return _render(request, "movies.html", {"active_page": "movies"})


@router.get("/tv", response_class=HTMLResponse)
async def tv_page(request: Request) -> Response:
    return _render(request, "tv.html", {"active_page": "tv"})


@router.get("/queue", response_class=HTMLResponse)
async def queue_page(request: Request) -> Response:
    return _render(request, "queue.html", {"active_page": "queue"})


@router.get("/workers", response_class=HTMLResponse)
async def workers_page(request: Request) -> Response:
    return _render(request, "workers.html", {"active_page": "workers"})


@router.get("/activity", response_class=HTMLResponse)
async def activity_page(request: Request, view: str = "outcomes") -> Response:
    """One ledger, two honest facets: encode outcomes (jobs table) and
    scan skips (skipped_files table — never attempted)."""
    return _render(
        request,
        "activity.html",
        {"active_page": "activity", "view": "skips" if view == "skips" else "outcomes"},
    )


@router.get("/history", response_class=HTMLResponse)
async def history_page(request: Request) -> Response:
    """History merged into Activity (encode-outcomes facet)."""
    return Response(status_code=301, headers={"Location": "/activity?view=outcomes"})


@router.get("/skipped", response_class=HTMLResponse)
async def skipped_page(request: Request) -> Response:
    """Skipped merged into Activity (scan-skips facet)."""
    return Response(status_code=301, headers={"Location": "/activity?view=skips"})


@router.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request) -> Response:
    return _render(request, "stats.html", {"active_page": "stats"})


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> Response:
    return _render(request, "settings.html", {"active_page": "settings"})


# -- HTMX partials --


@router.get("/partials/health", response_class=HTMLResponse)
async def health_partial(
    request: Request,
    db: DBConnection = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> Response:
    redis_ok = await check_redis_health(redis) if redis is not None else False
    db_ok = await check_db_health(db)
    return _render(request, "partials/health.html", {"healthy": redis_ok and db_ok})


@router.get("/partials/scheduler-info", response_class=HTMLResponse)
async def scheduler_info_partial(
    request: Request,
    db: DBConnection = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> Response:
    from transcode_forge.repos import libraries as lib_repo
    from transcode_forge.repos import system as system_repo

    redis_ok = await check_redis_health(redis) if redis is not None else False
    db_ok = await check_db_health(db)
    queue_paused = await system_repo.is_queue_paused(db)
    libs = await lib_repo.list_libraries(db)
    jobs_queued = await job_repo.count_queued_jobs(db)

    return _render(
        request,
        "partials/scheduler_info.html",
        {
            "redis_ok": redis_ok,
            "db_ok": db_ok,
            "queue_paused": queue_paused,
            "library_count": len(libs),
            "jobs_queued": jobs_queued,
        },
    )


@router.get("/partials/dashboard-stats", response_class=HTMLResponse)
async def dashboard_stats_partial(
    request: Request,
    db: DBConnection = Depends(get_db),
) -> Response:
    # CAST to BIGINT: on Postgres SUM() over a BIGINT column returns
    # numeric → Decimal (same dialect gap the /api/stats route fixed).
    async with db.execute(
        "SELECT CAST(COALESCE(SUM(space_saved), 0) AS BIGINT) FROM jobs WHERE status = 'complete'"
    ) as cur:
        row = await cur.fetchone()
        space_saved = row[0] if row else 0

    async with db.execute("SELECT COUNT(*) FROM jobs WHERE status = 'complete'") as cur:
        row = await cur.fetchone()
        completed = row[0] if row else 0

    queued = await job_repo.count_queued_jobs(db)

    async with db.execute("SELECT COUNT(*) FROM workers WHERE status IN ('online','busy')") as cur:
        row = await cur.fetchone()
        workers_online = row[0] if row else 0

    return _render(
        request,
        "partials/dashboard_stats.html",
        {
            "space_saved_gb": space_saved / 1073741824,
            "completed": completed,
            "queued": queued,
            "workers_online": workers_online,
        },
    )


async def _job_row_context(db: DBConnection, jobs: list[Any]) -> dict[str, Any]:
    """Shared enrichment for job-row partials: worker names for honest
    attribution, and media-file ids so rows can open the file drawer."""
    worker_names = {w.id: w.name for w in await worker_repo.list_workers(db)}
    file_ids = await media_repo.ids_by_paths(db, [j.source_path for j in jobs])
    return {
        "jobs": [j.model_dump(mode="json") for j in jobs],
        "worker_names": worker_names,
        "file_ids": file_ids,
    }


@router.get("/partials/active-transcodes", response_class=HTMLResponse)
async def active_transcodes_partial(
    request: Request,
    db: DBConnection = Depends(get_db),
) -> Response:
    jobs, _ = await job_repo.list_jobs(db, status="transcoding,assigned,verifying", limit=10)
    return _render(
        request,
        "partials/active_transcodes.html",
        await _job_row_context(db, jobs),
    )


@router.get("/partials/recent-activity", response_class=HTMLResponse)
async def recent_activity_partial(
    request: Request,
    db: DBConnection = Depends(get_db),
) -> Response:
    jobs, _ = await job_repo.list_jobs(db, status="complete,failed,skipped", limit=10)
    return _render(
        request,
        "partials/recent_activity.html",
        await _job_row_context(db, jobs),
    )


@router.get("/partials/scan-history", response_class=HTMLResponse)
async def scan_history_partial(
    request: Request,
    db: DBConnection = Depends(get_db),
) -> Response:
    scans, _ = await scan_repo.list_scans(db, limit=5, offset=0)
    return _render(
        request,
        "partials/scan_history.html",
        {
            "scans": [s.model_dump(mode="json") for s in scans],
        },
    )


@router.get("/partials/jobs", response_class=HTMLResponse)
async def jobs_partial(
    request: Request,
    status: str | None = None,
    library: str | None = None,
    sort: str = "created_at",
    dir: str = "desc",
    page: int = 1,
    per_page: int = 50,
    db: DBConnection = Depends(get_db),
) -> Response:
    offset = (page - 1) * per_page
    jobs, total = await job_repo.list_jobs(
        db,
        status=status or None,
        library=library or None,
        sort_by=sort,
        sort_dir=dir,
        limit=per_page,
        offset=offset,
    )
    # Codecs at least one live worker can encode — pending jobs whose
    # target codec isn't covered get a "waiting for a capable worker" hint.
    # Same idea for downscale jobs: until an upgraded worker advertising
    # supports_downscale is online, they pend — say so instead of leaving
    # the row silent (the expected state mid-fleet-upgrade).
    online_codecs: set[str] = set()
    downscale_online = False
    workers_list = await worker_repo.list_workers(db)
    for w in workers_list:
        if w.status in ("online", "busy"):
            online_codecs.update(w.supported_codecs)
            downscale_online = downscale_online or w.supports_downscale
    return _render(
        request,
        "partials/jobs.html",
        {
            "jobs": [j.model_dump(mode="json") for j in jobs],
            "total": total,
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "dir": dir,
            "online_codecs": online_codecs,
            "downscale_online": downscale_online,
            "worker_names": {w.id: w.name for w in workers_list},
            "file_ids": await media_repo.ids_by_paths(db, [j.source_path for j in jobs]),
        },
    )


def _humanize_age(seconds: float) -> str:
    """Render an integer second count as a compact relative duration."""
    s = int(seconds)
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        h, m = divmod(s, 3600)
        return f"{h}h {m // 60}m ago"
    d, rem = divmod(s, 86400)
    return f"{d}d {rem // 3600}h ago"


@router.get("/partials/workers", response_class=HTMLResponse)
async def workers_partial(
    request: Request,
    db: DBConnection = Depends(get_db),
) -> Response:
    """List workers with computed heartbeat staleness for alerting."""
    workers_list = await worker_repo.list_workers(db)
    now = datetime.now(UTC)
    enriched = []
    for w in workers_list:
        d = w.model_dump(mode="json")
        if w.last_heartbeat:
            delta = (now - w.last_heartbeat).total_seconds()
            d["heartbeat_age_seconds"] = int(delta)
            d["heartbeat_relative"] = _humanize_age(delta)
            # Tiers: fresh < 60s, slow 60-300s, stale 300-1800s, dead > 1800s.
            if delta < 60:
                d["heartbeat_tier"] = "fresh"
            elif delta < 300:
                d["heartbeat_tier"] = "slow"
            elif delta < 1800:
                d["heartbeat_tier"] = "stale"
            else:
                d["heartbeat_tier"] = "dead"
        else:
            d["heartbeat_age_seconds"] = None
            d["heartbeat_relative"] = "never"
            d["heartbeat_tier"] = "dead"
        # Resolve current_job_id to a human-readable filename so the card
        # shows what's being transcoded, not an opaque UUID — and carry the
        # job's progress so the poll renders the real bar width instead of a
        # 0% placeholder (the WebSocket keeps it live between polls).
        d["current_job_filename"] = None
        d["current_job_progress"] = None
        if w.current_job_id:
            job = await job_repo.get_job(db, w.current_job_id)
            if job:
                d["current_job_filename"] = Path(job.source_path).name
                d["current_job_progress"] = job.progress
        enriched.append(d)
    return _render(
        request,
        "partials/workers.html",
        {"workers": enriched},
    )


@router.get("/partials/queue-badge", response_class=HTMLResponse)
async def queue_badge_partial(
    request: Request,
    db: DBConnection = Depends(get_db),
) -> Response:
    """Return queue count for sidebar badge (lightweight)."""
    count = await job_repo.count_queued_jobs(db)
    content = str(count) if count > 0 else ""
    return Response(content=content, media_type="text/html")


@router.get("/partials/activity-outcomes", response_class=HTMLResponse)
async def activity_outcomes_partial(
    request: Request,
    status: str | None = None,
    library: str | None = None,
    since: str | None = None,
    sort: str = "created_at",
    dir: str = "desc",
    page: int = 1,
    per_page: int = 50,
    db: DBConnection = Depends(get_db),
) -> Response:
    """Encode outcomes — finished/failed/discarded rows off the jobs table."""
    offset = (page - 1) * per_page
    filter_status = status or "complete,failed,skipped"
    since_map = {"24h": 24, "7d": 7 * 24, "30d": 30 * 24}
    since_hours = since_map.get(since) if since else None
    jobs, total = await job_repo.list_jobs(
        db,
        status=filter_status,
        library=library or None,
        since_hours=since_hours,
        sort_by=sort,
        sort_dir=dir,
        limit=per_page,
        offset=offset,
    )
    worker_names = {w.id: w.name for w in await worker_repo.list_workers(db)}
    file_ids = await media_repo.ids_by_paths(db, [j.source_path for j in jobs])
    job_dicts = []
    for j in jobs:
        d = j.model_dump(mode="json")
        d["duration"] = (
            _format_duration(j.started_at, j.completed_at)
            if j.started_at and j.completed_at
            else "—"
        )
        d["worker_name"] = worker_names.get(j.worker_id) if j.worker_id else None
        job_dicts.append(d)
    return _render(
        request,
        "partials/activity_outcomes.html",
        {
            "jobs": job_dicts,
            "total": total,
            "page": page,
            "per_page": per_page,
            "status_filter": status or "",
            "library_filter": library or "",
            "since_filter": since or "",
            "sort": sort,
            "dir": dir,
            "file_ids": file_ids,
        },
    )


@router.get("/partials/activity-skips", response_class=HTMLResponse)
async def activity_skips_partial(
    request: Request,
    reason: str | None = None,
    library: str | None = None,
    sort: str = "updated_at",
    dir: str = "desc",
    page: int = 1,
    per_page: int = 50,
    db: DBConnection = Depends(get_db),
) -> Response:
    offset = (page - 1) * per_page
    files, total = await skip_repo.list_skipped(
        db,
        reason=reason or None,
        library=library or None,
        sort_by=sort,
        sort_dir=dir,
        limit=per_page,
        offset=offset,
    )
    return _render(
        request,
        "partials/activity_skips.html",
        {
            "files": [f.model_dump(mode="json") for f in files],
            "total": total,
            "sort": sort,
            "dir": dir,
        },
    )


@router.get("/partials/skip-stats", response_class=HTMLResponse)
async def skip_stats_partial(
    request: Request,
    db: DBConnection = Depends(get_db),
) -> Response:
    counts = await skip_repo.skip_reason_counts(db)
    total = sum(counts.values())
    return _render(request, "partials/skip_stats.html", {"counts": counts, "total": total})


@router.get("/partials/stats", response_class=HTMLResponse)
async def stats_partial(
    request: Request,
    db: DBConnection = Depends(get_db),
) -> Response:
    stats: dict[str, Any] = {}

    async with db.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status") as cur:
        stats["jobs_by_status"] = {row[0]: row[1] for row in await cur.fetchall()}

    # CAST the sums to BIGINT — Postgres SUM(BIGINT) returns numeric/Decimal.
    async with db.execute(
        "SELECT COUNT(*), CAST(COALESCE(SUM(space_saved), 0) AS BIGINT), "
        "CAST(COALESCE(SUM(source_size), 0) AS BIGINT), "
        "CAST(COALESCE(SUM(output_size), 0) AS BIGINT) "
        "FROM jobs WHERE status = 'complete'"
    ) as cur:
        row = await cur.fetchone()
        stats["completed"] = row[0] if row else 0
        stats["total_space_saved_bytes"] = row[1] if row else 0
        stats["total_source_bytes"] = row[2] if row else 0
        stats["total_output_bytes"] = row[3] if row else 0

    async with db.execute(
        "SELECT library, COUNT(*), CAST(COALESCE(SUM(space_saved), 0) AS BIGINT) "
        "FROM jobs WHERE status = 'complete' GROUP BY library"
    ) as cur:
        stats["by_library"] = {
            row[0]: {"completed": row[1], "space_saved_bytes": row[2]}
            for row in await cur.fetchall()
        }

    async with db.execute(
        "SELECT skip_reason, COUNT(*) FROM skipped_files GROUP BY skip_reason"
    ) as cur:
        stats["skipped_by_reason"] = {row[0]: row[1] for row in await cur.fetchall()}

    async with db.execute("SELECT status, COUNT(*) FROM workers GROUP BY status") as cur:
        stats["workers_by_status"] = {row[0]: row[1] for row in await cur.fetchall()}

    async with db.execute("SELECT COUNT(*) FROM workers") as cur:
        row = await cur.fetchone()
        stats["total_workers"] = row[0] if row else 0

    # Actual worker list for performance matrix
    all_workers = await worker_repo.list_workers(db)
    stats["workers"] = [w.model_dump(mode="json") for w in all_workers]

    async with db.execute(
        "SELECT worker_id, COUNT(*) FROM jobs WHERE status = 'complete' "
        "AND worker_id IS NOT NULL GROUP BY worker_id"
    ) as cur:
        stats["jobs_by_worker"] = {row[0]: row[1] for row in await cur.fetchall()}

    # Pre-calculate avg savings %
    source = stats.get("total_source_bytes", 0)
    output = stats.get("total_output_bytes", 0)
    stats["avg_savings_pct"] = max(0, round((1 - output / source) * 100)) if source > 0 else 0

    return _render(request, "partials/stats.html", {"stats": stats})


@router.get("/partials/schedules", response_class=HTMLResponse)
async def schedules_partial(
    request: Request,
    db: DBConnection = Depends(get_db),
) -> Response:
    schedules = await sched_repo.list_schedules(db)
    return _render(request, "partials/schedules.html", {"schedules": schedules})


@router.get("/partials/worker-tokens", response_class=HTMLResponse)
async def worker_tokens_partial(
    request: Request,
    db: DBConnection = Depends(get_db),
) -> Response:
    from transcode_forge.repos import worker_tokens as token_repo

    tokens = await token_repo.list_all(db)
    return _render(request, "partials/worker_tokens.html", {"tokens": tokens})


@router.get("/partials/file-detail", response_class=HTMLResponse)
async def file_detail_partial(
    request: Request,
    file_id: str,
    db: DBConnection = Depends(get_db),
) -> Response:
    """Everything known about one file — the body of the file-detail drawer.

    404-safe: an unknown id renders a small "file not found" body with a
    404 status instead of an exception page (the drawer shows whatever
    comes back).
    """
    from transcode_forge.repos import libraries as lib_repo

    f = await media_repo.get_media_file(db, file_id)
    if f is None:
        resp = _render(request, "partials/file_detail.html", {"file": None})
        resp.status_code = 404
        return resp

    lib = await lib_repo.get_library(db, f["library_id"]) if f.get("library_id") else None
    f = {**f, "library_name": lib["name"] if lib else None}

    jobs, _ = await job_repo.list_jobs(
        db,
        source_path=f["file_path"],
        sort_by="created_at",
        sort_dir="desc",
        limit=20,
    )
    worker_names = {w.id: w.name for w in await worker_repo.list_workers(db)}
    job_dicts = []
    for j in jobs:
        d = j.model_dump(mode="json")
        d["duration"] = (
            _format_duration(j.started_at, j.completed_at)
            if j.started_at and j.completed_at
            else None
        )
        d["worker_name"] = worker_names.get(j.worker_id) if j.worker_id else None
        job_dicts.append(d)

    # Latest finished encode with a real output — the economics section.
    best = next(
        (d for d in job_dicts if d["status"] == "complete" and d.get("output_size")),
        None,
    )
    excluded = await excl_repo.is_excluded(db, f["file_path"])
    queueable = (
        f.get("video_codec") == "h264"
        and f.get("transcode_status") not in ("queued", "transcoding", "complete")
        and not excluded
    )

    return _render(
        request,
        "partials/file_detail.html",
        {
            "file": f,
            "jobs": job_dicts,
            "best": best,
            "excluded": excluded,
            "queueable": queueable,
        },
    )


@router.get("/partials/tv-episodes", response_class=HTMLResponse)
async def tv_episodes_partial(
    request: Request,
    show: str,
    db: DBConnection = Depends(get_db),
) -> Response:
    files, _ = await media_repo.list_media_files(
        db,
        media_type="tv",
        show_name=show,
        sort_by="filename",
        sort_dir="asc",
        limit=500,
    )
    return _render(request, "partials/tv_episodes.html", {"files": files})

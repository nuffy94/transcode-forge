"""FastAPI application factory and lifespan management."""

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from redis.exceptions import RedisError
from starlette.middleware.sessions import SessionMiddleware

from transcode_forge import __version__
from transcode_forge.auth import AuthMiddleware
from transcode_forge.config import Settings, get_settings
from transcode_forge.db import DBConnection, close_db, init_db
from transcode_forge.redis import close_redis_pool, create_redis_pool
from transcode_forge.repos import workers as worker_repo
from transcode_forge.scheduler_cron import run_scheduled_scans

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage application startup and shutdown."""
    settings: Settings = app.state.settings
    background_tasks: list[asyncio.Task[None]] = []

    # Startup
    if settings.demo_static:
        settings.demo_mode = True  # static implies demo
        mode_label = " (DEMO STATIC)"
    elif settings.demo_mode:
        mode_label = " (DEMO MODE)"
    else:
        mode_label = ""
    logger.info("Starting Transcode Forge v%s%s", __version__, mode_label)

    # Surface common misconfigurations early — loud, but non-fatal so the
    # local dev quick-start (default sqlite) still works.
    if not settings.demo_mode:
        if settings.db_url == "sqlite:///transcode_forge.db":
            logger.warning(
                "TF_DB_URL is unset — using the default sqlite file in the working "
                "directory. Set TF_DB_URL (postgresql://… or an absolute sqlite path) "
                "for a real deployment or your data may not persist."
            )
        if "TF_AUTH_SECRET" not in os.environ:
            logger.warning(
                "TF_AUTH_SECRET is unset — a random key was generated; admin sessions "
                "will be invalidated on restart. Pin TF_AUTH_SECRET to keep logins alive."
            )

    # Preflight: validate library paths + ffmpeg up front (non-fatal). The
    # result is surfaced in the UI via /api/health/preflight.
    from transcode_forge.preflight import log_preflight, run_preflight, validate_db_connection

    app.state.preflight = [] if settings.demo_mode else run_preflight(settings)
    log_preflight(app.state.preflight)

    # Database — always needed
    db_url = settings.db_url
    if settings.demo_mode and db_url == "sqlite:///transcode_forge.db":
        db_url = "sqlite:///demo_transcode_forge.db"

    # Validate database connection (PostgreSQL only; SQLite always succeeds)
    if not settings.demo_mode:
        db_issues = await validate_db_connection(db_url)
        app.state.preflight.extend(db_issues)
        log_preflight(db_issues)

    app.state.db = await init_db(db_url)
    logger.info("Database initialized: %s", db_url.split("@")[-1])

    # Redis — optional in demo mode
    app.state.redis = None
    if not settings.demo_mode:
        app.state.redis = await create_redis_pool(settings.redis_url)
        logger.info("Redis connected at %s", settings.redis_url)
    else:
        try:
            app.state.redis = await create_redis_pool(settings.redis_url)
            logger.info("Redis connected (optional in demo mode)")
        except (RedisError, OSError) as exc:
            # redis-py raises redis.exceptions.ConnectionError (NOT the builtin
            # ConnectionError), so catch its base RedisError or demo mode would
            # crash at startup whenever Redis is absent.
            logger.warning("Redis unavailable — skipped (demo mode): %s", exc)
            app.state.redis = None

    if settings.demo_mode:
        # Seed demo data
        from transcode_forge.demo.seed import seed_demo_data

        await seed_demo_data(app.state.db)

        if settings.demo_static:
            logger.info("Demo data seeded (STATIC — simulator disabled)")
        else:
            from transcode_forge.demo.simulator import run_simulator

            background_tasks.append(asyncio.create_task(run_simulator(app.state.db)))
            logger.info("Demo simulator started")
    else:
        # Production background tasks
        background_tasks.append(asyncio.create_task(run_scheduled_scans(settings, app.state.db)))
        background_tasks.append(
            asyncio.create_task(_stale_worker_loop(app.state.db, settings.heartbeat_timeout))
        )
        logger.info("Background tasks started (scans, stale worker cleanup)")

    yield

    # Shutdown
    logger.info("Shutting down Transcode Forge")
    for task in background_tasks:
        task.cancel()
    # Wait for cancelled tasks to finish their cleanup (finally blocks,
    # in-flight DB writes) BEFORE tearing down Redis/DB underneath them.
    if background_tasks:
        await asyncio.gather(*background_tasks, return_exceptions=True)
    if app.state.redis is not None:
        await close_redis_pool(app.state.redis)
    await close_db(app.state.db)


async def _stale_worker_loop(db: DBConnection, heartbeat_timeout: int) -> None:
    """Periodically mark stale workers as dead."""
    timeout = heartbeat_timeout * 3  # 3x heartbeat_timeout = dead
    while True:
        try:
            await worker_repo.cleanup_stale_workers(db, timeout_seconds=timeout)
        except Exception:
            logger.exception("Stale worker cleanup failed")
        await asyncio.sleep(30)  # Check every 30 seconds


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    if settings is None:
        settings = get_settings()

    # Honor TF_LOG_LEVEL for the app's own loggers (propagate to uvicorn's
    # root handler). Does not fight uvicorn's access/error logging config.
    logging.getLogger("transcode_forge").setLevel(settings.log_level.upper())

    app = FastAPI(
        title="Transcode Forge",
        version=__version__,
        description="Distributed media transcoding system",
        lifespan=lifespan,
    )
    app.state.settings = settings

    # Auth — SessionMiddleware must wrap AuthMiddleware (it injects the
    # session into the scope that AuthMiddleware then reads).
    app.add_middleware(AuthMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.auth_secret,
        session_cookie="tf_session",
        max_age=14 * 24 * 3600,  # 2 weeks
        same_site="lax",
        https_only=settings.session_secure,  # TF_SESSION_SECURE=true behind TLS
    )

    # Register routes
    from transcode_forge.api.routes.audit import router as audit_router
    from transcode_forge.api.routes.auth import router as auth_router
    from transcode_forge.api.routes.exclusions import router as exclusions_router
    from transcode_forge.api.routes.health import router as health_router
    from transcode_forge.api.routes.jobs import router as jobs_router
    from transcode_forge.api.routes.libraries import router as libraries_router
    from transcode_forge.api.routes.media import router as media_router
    from transcode_forge.api.routes.scan import router as scan_router
    from transcode_forge.api.routes.schedules import router as schedules_router
    from transcode_forge.api.routes.settings import router as settings_router
    from transcode_forge.api.routes.skipped import router as skipped_router
    from transcode_forge.api.routes.stats import router as stats_router
    from transcode_forge.api.routes.worker_api import router as worker_api_router
    from transcode_forge.api.routes.worker_tokens import router as worker_tokens_router
    from transcode_forge.api.routes.workers import router as workers_router

    app.include_router(health_router, prefix="/api")
    app.include_router(jobs_router, prefix="/api")
    app.include_router(libraries_router, prefix="/api")
    app.include_router(media_router, prefix="/api")
    app.include_router(workers_router, prefix="/api")
    app.include_router(scan_router, prefix="/api")
    app.include_router(skipped_router, prefix="/api")
    app.include_router(stats_router, prefix="/api")
    app.include_router(audit_router, prefix="/api")
    app.include_router(exclusions_router, prefix="/api")
    app.include_router(schedules_router, prefix="/api")
    app.include_router(settings_router, prefix="/api")
    app.include_router(auth_router, prefix="/api")
    app.include_router(worker_api_router, prefix="/api")
    app.include_router(worker_tokens_router, prefix="/api")

    # Prometheus metrics
    from transcode_forge.metrics import router as metrics_router

    app.include_router(metrics_router)

    # Web UI routes (HTML pages + HTMX partials)
    from transcode_forge.web.routes import router as web_router
    from transcode_forge.web.websocket import router as ws_router

    app.include_router(web_router)
    app.include_router(ws_router)

    # Static files
    static_dir = Path(__file__).parent / "web" / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    return app


app = create_app()

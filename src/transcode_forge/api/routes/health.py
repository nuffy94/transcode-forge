"""Health check and system info endpoints."""

import shutil
import time
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from redis.asyncio import Redis

from transcode_forge import __version__
from transcode_forge.api.deps import get_db, get_redis, get_settings
from transcode_forge.auth import require_admin
from transcode_forge.config import Settings
from transcode_forge.db import DBConnection, check_db_health
from transcode_forge.redis import check_redis_health

router = APIRouter(tags=["health"])

_process_start = time.monotonic()


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    """Liveness: the process is up. No external checks — always 200.

    Orchestrators use this to decide whether to *restart* the container.
    """
    return {"status": "alive"}


@router.get("/health")
@router.get("/health/ready")
async def readiness(
    response: Response,
    db: DBConnection = Depends(get_db),
    redis: Redis | None = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Readiness: DB + Redis reachable.

    200 OK when healthy; 503 Service Unavailable when degraded, so load
    balancers / orchestrators route around it (decide whether to send
    *traffic*). ``/api/health`` is kept as an alias for backward compat.

    Redis is required in production but optional in demo mode, so a demo
    instance running without Redis still reports ready (DB alone gates it).
    """
    redis_ok = await check_redis_health(redis) if redis is not None else False
    db_ok = await check_db_health(db)
    redis_required = not settings.demo_mode
    healthy = db_ok and (redis_ok or not redis_required)

    if not healthy:
        response.status_code = 503

    return {
        "status": "ok" if healthy else "degraded",
        "redis": redis_ok,
        "db": db_ok,
        "redis_required": redis_required,
    }


@router.get("/health/preflight")
async def preflight_status(
    request: Request,
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    """Config issues found at startup (library paths, ffmpeg).

    Admin-gated so filesystem paths aren't exposed to the public, even
    though it lives under the otherwise-public /api/health/* prefix.
    """
    issues = getattr(request.app.state, "preflight", [])
    has_critical = any(i.get("level") == "critical" for i in issues)
    return {"ok": not has_critical, "issues": issues}


@router.get("/system/info")
async def system_info() -> dict[str, Any]:
    """Return system version, uptime, database info, and disk usage."""
    from transcode_forge.config import get_settings

    settings = get_settings()

    # Database URL (mask password)
    db_display = settings.db_url
    if "@" in db_display:
        _prefix, suffix = db_display.split("@", 1)
        db_display = f"postgresql://***@{suffix}"

    # Uptime from process start
    elapsed = time.monotonic() - _process_start
    days = int(elapsed // 86400)
    hours = int((elapsed % 86400) // 3600)
    minutes = int((elapsed % 3600) // 60)

    # Disk usage of first available library path
    disk: dict[str, Any] = {"total": 0, "used": 0, "free": 0, "percent": 0}
    for _name, (lib_path, _quality) in settings.libraries.items():
        try:
            usage = shutil.disk_usage(lib_path)
            disk = {
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
                "percent": round(usage.used / usage.total * 100, 1) if usage.total else 0,
            }
            break
        except OSError:
            continue

    return {
        "version": __version__,
        "database": db_display,
        "uptime": f"{days}d {hours}h {minutes}m",
        "uptime_seconds": int(elapsed),
        "disk": disk,
        "max_retries": settings.max_retries,
        "heartbeat_interval": settings.heartbeat_interval,
        "heartbeat_timeout": settings.heartbeat_timeout,
    }

"""Prometheus metrics for Transcode Forge.

Custom metrics beyond what prometheus-fastapi-instrumentator provides.
These are updated by polling the database on a schedule.
"""

import logging

from fastapi import APIRouter, Depends, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from transcode_forge.api.deps import get_db
from transcode_forge.db import DBConnection

logger = logging.getLogger(__name__)

# -- Gauges (current state) --
tf_jobs_active = Gauge("tf_jobs_active", "Currently transcoding jobs")
tf_jobs_queued = Gauge("tf_jobs_queued", "Jobs waiting in queue")
tf_workers_online = Gauge("tf_workers_online", "Workers currently online or busy")

# -- Counters (cumulative) --
tf_jobs_total = Counter(
    "tf_jobs_total",
    "Total jobs by status and library",
    ["status", "library"],
)
tf_space_saved_bytes = Gauge(
    "tf_space_saved_bytes_total",
    "Cumulative bytes saved by transcoding",
)
tf_errors_total = Counter(
    "tf_errors_total",
    "Total errors by type",
    ["error_type"],
)

# -- Histograms --
tf_transcode_duration = Histogram(
    "tf_transcode_duration_seconds",
    "Transcode duration in seconds",
    ["library", "encoder"],
    buckets=[60, 120, 300, 600, 1200, 1800, 3600, 7200],
)
tf_file_size_ratio = Histogram(
    "tf_file_size_ratio",
    "Output/source file size ratio",
    ["library"],
    buckets=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
)


router = APIRouter()


@router.get("/metrics")
async def metrics_endpoint(db: DBConnection = Depends(get_db)) -> Response:
    """Prometheus metrics endpoint. Updates gauges from DB, then returns all metrics."""
    await _refresh_gauges(db)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


async def _refresh_gauges(db: DBConnection) -> None:
    """Update gauge values from the database."""
    try:
        # Active jobs
        async with db.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('transcoding', 'assigned', 'verifying')"
        ) as cursor:
            row = await cursor.fetchone()
            tf_jobs_active.set(row[0] if row else 0)

        # Queued jobs
        async with db.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('pending', 'queued')"
        ) as cursor:
            row = await cursor.fetchone()
            tf_jobs_queued.set(row[0] if row else 0)

        # Online workers
        async with db.execute(
            "SELECT COUNT(*) FROM workers WHERE status IN ('online', 'busy')"
        ) as cursor:
            row = await cursor.fetchone()
            tf_workers_online.set(row[0] if row else 0)

        # Total space saved
        async with db.execute(
            "SELECT COALESCE(SUM(space_saved), 0) FROM jobs WHERE status = 'complete'"
        ) as cursor:
            row = await cursor.fetchone()
            tf_space_saved_bytes.set(row[0] if row else 0)

    except Exception:
        logger.exception("Failed to refresh Prometheus gauges")
        tf_errors_total.labels(error_type="metrics_refresh").inc()

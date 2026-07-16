"""Worker repository — CRUD operations for transcode workers."""

import json
import logging
from datetime import UTC, datetime, timedelta

import aiosqlite

from transcode_forge.db import DBConnection
from transcode_forge.models.worker import Worker, WorkerStatus

logger = logging.getLogger(__name__)


def _row_to_worker(row: aiosqlite.Row) -> Worker:
    """Convert a database row to a Worker model."""
    data = dict(row)
    if data.get("capabilities"):
        data["capabilities"] = json.loads(data["capabilities"])
    if data.get("supported_codecs"):
        data["supported_codecs"] = json.loads(data["supported_codecs"])
    for field in ("last_heartbeat", "registered_at", "updated_at"):
        if data.get(field):
            data[field] = datetime.fromisoformat(data[field])
    return Worker.model_validate(data)


async def upsert_worker(db: DBConnection, worker: Worker) -> None:
    """Insert or update a worker (heartbeat IS registration).

    Deliberately does NOT stamp current_job_changed_at even though it
    resets current_job_id (unlike update_worker_heartbeat, which does):
    registration already releases the worker's jobs, so there is nothing
    for the reconciliation sweep to time against a stale value — and a
    left-over timestamp is either old enough to sweep promptly (correct)
    or fresh enough that the normal grace applies."""
    now = datetime.now(UTC).isoformat()
    caps_json = json.dumps(worker.capabilities)
    codecs_json = json.dumps(worker.supported_codecs)

    await db.execute(
        """INSERT INTO workers (
            id, name, host, capabilities, supported_codecs, supports_downscale,
            ffmpeg_version, max_concurrent, status, current_job_id,
            last_heartbeat, registered_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            host = excluded.host,
            capabilities = excluded.capabilities,
            supported_codecs = excluded.supported_codecs,
            supports_downscale = excluded.supports_downscale,
            ffmpeg_version = excluded.ffmpeg_version,
            max_concurrent = excluded.max_concurrent,
            status = excluded.status,
            current_job_id = excluded.current_job_id,
            last_heartbeat = excluded.last_heartbeat,
            updated_at = excluded.updated_at
        """,
        (
            worker.id,
            worker.name,
            worker.host,
            caps_json,
            codecs_json,
            int(worker.supports_downscale),
            worker.ffmpeg_version,
            worker.max_concurrent,
            worker.status.value,
            worker.current_job_id,
            now,
            now,
            now,
        ),
    )
    await db.commit()


async def get_worker(db: DBConnection, worker_id: str) -> Worker | None:
    """Fetch a single worker by ID."""
    async with db.execute("SELECT * FROM workers WHERE id = ?", (worker_id,)) as cursor:
        row = await cursor.fetchone()
        return _row_to_worker(row) if row else None


async def list_workers(db: DBConnection) -> list[Worker]:
    """List all registered workers."""
    async with db.execute("SELECT * FROM workers ORDER BY name") as cursor:
        rows = await cursor.fetchall()
        return [_row_to_worker(r) for r in rows]


async def update_worker_heartbeat(
    db: DBConnection,
    worker_id: str,
    *,
    status: str = "online",
    current_job_id: str | None = None,
) -> None:
    """Update heartbeat timestamp and status in the DB.

    current_job_changed_at records when the heartbeat last CHANGED which
    job it names (including to/from NULL) — the reconciliation sweep
    requeues a live worker's job only on a mismatch SUSTAINED past a
    grace window, so the transition timestamp is the load-bearing part
    (worker-resilience spec D3). One conditional UPDATE keeps the
    steady-state-vs-transition decision atomic under overlapping
    heartbeats; the OR clause is the portable NULL-safe equality (SQLite
    has no IS NOT DISTINCT FROM)."""
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "UPDATE workers SET last_heartbeat = ?, status = ?,"
        " current_job_changed_at = CASE"
        "   WHEN current_job_id = ? OR (current_job_id IS NULL AND ? IS NULL)"
        "   THEN current_job_changed_at ELSE ? END,"
        " current_job_id = ?, updated_at = ?"
        " WHERE id = ?",
        (now, status, current_job_id, current_job_id, now, current_job_id, now, worker_id),
    )
    await db.commit()


async def update_worker_status(db: DBConnection, worker_id: str, status: WorkerStatus) -> None:
    """Update a worker's status."""
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "UPDATE workers SET status = ?, updated_at = ? WHERE id = ?",
        (status.value, now, worker_id),
    )
    await db.commit()


async def delete_worker(db: DBConnection, worker_id: str) -> bool:
    """Delete a worker. Returns True if deleted, False if not found.

    Caller is responsible for any safety check (e.g., refusing to delete
    an active worker). The repo just executes the delete.
    """
    cur = await db.execute("DELETE FROM workers WHERE id = ?", (worker_id,))
    await db.commit()
    return bool(cur.rowcount)


# How long a worker has to be silent before it's safe to remove. The
# scheduler considers anything past 30 min effectively gone (matches the
# 'dead' tier the UI uses). 30 min covers normal worker restarts and
# transient network blips without trapping retired workers in the list.
WORKER_STALE_THRESHOLD_SECONDS = 1800


async def count_active_jobs_for_worker(db: DBConnection, worker_id: str) -> int:
    """Count jobs assigned to this worker that are still in flight.

    Used as the safety gate before deleting a worker — if any active
    job points at this worker, the orphan-job audit should handle it
    first (re-queue or fail it) before the worker row is removed.
    """
    async with db.execute(
        "SELECT COUNT(*) FROM jobs WHERE worker_id = ? "
        "AND status IN ('transcoding', 'queued', 'pending')",
        (worker_id,),
    ) as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def cleanup_stale_workers(db: DBConnection, *, timeout_seconds: int = 90) -> int:
    """Mark workers as dead if their heartbeat is older than timeout.

    Default timeout is 3x the 30s heartbeat_timeout config value.
    Returns the number of workers marked dead.
    """
    now = datetime.now(UTC)
    cutoff = (now - timedelta(seconds=timeout_seconds)).isoformat()
    now_iso = now.isoformat()

    cur = await db.execute(
        "UPDATE workers SET status = ?, updated_at = ?"
        " WHERE status IN (?, ?) AND last_heartbeat < ?",
        (
            WorkerStatus.DEAD.value,
            now_iso,
            WorkerStatus.ONLINE.value,
            WorkerStatus.BUSY.value,
            cutoff,
        ),
    )
    if cur.rowcount > 0:
        await db.commit()
        logger.warning(
            "Marked %d stale worker(s) as dead (no heartbeat for %ds)",
            cur.rowcount,
            timeout_seconds,
        )
    count: int = cur.rowcount
    return count

"""Job repository — CRUD operations for transcode jobs."""

from datetime import UTC, datetime, timedelta

import aiosqlite

from transcode_forge.db import DBConnection
from transcode_forge.models.job import Job, JobStatus
from transcode_forge.models.worker import WorkerStatus

# Valid job statuses for filtering (whitelist validation)
_VALID_JOB_STATUSES = frozenset(status.value for status in JobStatus)

# Whitelist of sortable columns -> SQL column. Maps the UI's sort key to a real
# column so user input can never reach the ORDER BY clause directly. Duration is
# intentionally absent: it's derived from started_at/completed_at, has no stored
# column, and a portable expression would differ between SQLite and Postgres.
_VALID_JOB_SORTS = {
    "source_path": "source_path",
    "status": "status",
    "space_saved": "space_saved",
    "source_size": "source_size",
    "worker_id": "worker_id",
    "completed_at": "completed_at",
    "created_at": "created_at",
}


def _row_to_job(row: aiosqlite.Row) -> Job:
    """Convert a database row to a Job model."""
    data = dict(row)
    for field in ("created_at", "started_at", "completed_at", "updated_at"):
        if data.get(field):
            data[field] = datetime.fromisoformat(data[field])
    return Job.model_validate(data)


async def create_job(db: DBConnection, job: Job) -> str:
    """Insert a new job. Returns job ID."""
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """INSERT INTO jobs (
            id, source_path, library, source_codec, source_resolution,
            source_bitrate, source_duration, source_size, target_codec,
            target_height, quality_value, target_vmaf, status, retry_count,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            job.id,
            job.source_path,
            job.library,
            job.source_codec,
            job.source_resolution,
            job.source_bitrate,
            job.source_duration,
            job.source_size,
            job.target_codec,
            job.target_height,
            job.quality_value,
            job.target_vmaf,
            job.status.value,
            job.retry_count,
            now,
            now,
        ),
    )
    await db.commit()
    return job.id


async def get_job(db: DBConnection, job_id: str) -> Job | None:
    """Fetch a single job by ID."""
    async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cursor:
        row = await cursor.fetchone()
        return _row_to_job(row) if row else None


async def list_jobs(
    db: DBConnection,
    *,
    status: str | None = None,
    library: str | None = None,
    worker_id: str | None = None,
    source_path: str | None = None,
    since_hours: int | None = None,
    sort_by: str = "created_at",
    sort_dir: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Job], int]:
    """List jobs with optional filters. Returns (jobs, total_count).

    Args:
        db: Database connection.
        status: Comma-separated job statuses to filter by (e.g., 'pending,complete').
        library: Library name to filter by.
        worker_id: Worker ID to filter by.
        source_path: Exact file path — a single file's job history.
        since_hours: Only include jobs created within the last N hours.
        sort_by: Column to sort by (validated against _VALID_JOB_SORTS).
        sort_dir: Sort direction, 'asc' or 'desc'.
        limit: Maximum number of results to return.
        offset: Number of results to skip.

    Returns:
        Tuple of (list of Job objects, total count of matching jobs).

    Raises:
        ValueError: If invalid status values are provided.
    """
    conditions: list[str] = []
    params: list[str | int] = []

    if status:
        # Validate status values against the enum to prevent SQL injection
        requested_statuses = [s.strip() for s in status.split(",") if s.strip()]
        invalid_statuses = set(requested_statuses) - _VALID_JOB_STATUSES
        if invalid_statuses:
            raise ValueError(f"Invalid job status values: {invalid_statuses}")

        placeholders = ",".join("?" * len(requested_statuses))
        conditions.append(f"status IN ({placeholders})")
        params.extend(requested_statuses)

    if library:
        conditions.append("library = ?")
        params.append(library)

    if worker_id:
        conditions.append("worker_id = ?")
        params.append(worker_id)

    if source_path:
        conditions.append("source_path = ?")
        params.append(source_path)

    if since_hours is not None and since_hours > 0:
        cutoff = (datetime.now(UTC) - timedelta(hours=since_hours)).isoformat()
        conditions.append("created_at >= ?")
        params.append(cutoff)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    # Count total
    async with db.execute(f"SELECT COUNT(*) FROM jobs {where}", params) as cursor:
        row = await cursor.fetchone()
        total = row[0] if row else 0

    # Fetch page. sort_col comes from a whitelist; direction is constrained to
    # ASC/DESC — neither value is ever interpolated from raw user input.
    sort_col = _VALID_JOB_SORTS.get(sort_by, "created_at")
    direction = "DESC" if sort_dir.lower() != "asc" else "ASC"
    query = f"SELECT * FROM jobs {where} ORDER BY {sort_col} {direction} LIMIT ? OFFSET ?"
    async with db.execute(query, [*params, limit, offset]) as cursor:
        rows = await cursor.fetchall()
        return [_row_to_job(r) for r in rows], total


_VALID_JOB_COLUMNS = frozenset(
    {
        "source_path",
        "library",
        "source_codec",
        "source_resolution",
        "source_bitrate",
        "source_duration",
        "source_size",
        "target_codec",
        "quality_value",
        "target_vmaf",
        "resolved_crf",
        "achieved_vmaf",
        "achieved_vmaf_perc5",
        "predicted_vmaf_mean",
        "predicted_vmaf_perc5",
        "backend_used",
        "status",
        "worker_id",
        "progress",
        "phase",
        "output_size",
        "space_saved",
        "error_message",
        "retry_count",
        "started_at",
        "completed_at",
        "updated_at",
    }
)


async def update_job(db: DBConnection, job_id: str, **fields: object) -> Job | None:
    """Update specific fields on a job. Returns updated job.

    Only whitelisted column names are allowed to prevent SQL injection.
    """
    if not fields:
        return await get_job(db, job_id)

    invalid_cols = set(fields.keys()) - _VALID_JOB_COLUMNS - {"updated_at"}
    if invalid_cols:
        raise ValueError(f"Invalid job column names: {invalid_cols}")

    fields["updated_at"] = datetime.now(UTC).isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = [v.value if isinstance(v, JobStatus) else v for v in fields.values()]

    await db.execute(
        f"UPDATE jobs SET {set_clause} WHERE id = ?",
        [*values, job_id],
    )
    await db.commit()
    return await get_job(db, job_id)


async def count_queued_jobs(db: DBConnection) -> int:
    """Count jobs waiting for a worker to claim them.

    Single source of truth for the dashboard tile, sidebar badge, and
    scheduler-info card. If the definition of "in the queue" changes,
    update it here and every view follows.
    """
    async with db.execute(
        "SELECT COUNT(*) FROM jobs WHERE status IN (?, ?)",
        (JobStatus.PENDING.value, JobStatus.QUEUED.value),
    ) as cur:
        row = await cur.fetchone()
        return row[0] if row else 0


async def find_orphan_active_jobs(db: DBConnection) -> list[dict[str, object]]:
    """Find jobs claiming an active status whose worker is dead/missing.

    These are the failure mode where workers crashed without releasing
    their job — the dashboard renders them as live but no progress
    will ever happen. The auto-recovery path is to re-queue them.
    """
    active = (
        JobStatus.TRANSCODING.value,
        JobStatus.ASSIGNED.value,
        JobStatus.VERIFYING.value,
    )
    alive = (WorkerStatus.ONLINE.value, WorkerStatus.BUSY.value)
    placeholders_active = ",".join("?" * len(active))
    placeholders_alive = ",".join("?" * len(alive))
    query = (
        "SELECT j.id, j.source_path, j.status, j.worker_id, j.started_at, "
        "       w.status AS worker_status "
        "FROM jobs j LEFT JOIN workers w ON w.id = j.worker_id "
        f"WHERE j.status IN ({placeholders_active}) "
        f"  AND (w.status IS NULL OR w.status NOT IN ({placeholders_alive})) "
        "ORDER BY j.started_at"
    )
    async with db.execute(query, (*active, *alive)) as cur:
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def requeue_orphan_active_jobs(
    db: DBConnection, *, min_idle_seconds: int
) -> list[dict[str, object]]:
    """Requeue active jobs whose worker is dead, offline, or missing.

    The healing counterpart to find_orphan_active_jobs (which only reports,
    via /audit/integrity). Registration already releases a worker's jobs when
    it RE-registers — this covers the worker that never comes back.

    Only jobs idle for min_idle_seconds are touched: progress reports bump
    updated_at, so idleness means no signs of life, and the grace keeps a
    briefly-partitioned worker from losing its job the moment it's marked
    dead. If a zombie does lose its job this way, the ownership checks
    reject its stale reports and its heartbeated .tf_lock makes the retry
    decline until it finishes or dies for real.

    The status/idleness guard is duplicated on the OUTER where-clause, not
    just the subquery: under Postgres READ COMMITTED the subquery's id list
    comes from the statement's snapshot, and only outer quals are re-checked
    (EvalPlanQual) against a row a concurrent writer just changed — without
    the duplication, a job completing at the exact moment of the sweep could
    be stomped back to QUEUED.
    """
    active = (
        JobStatus.TRANSCODING.value,
        JobStatus.ASSIGNED.value,
        JobStatus.VERIFYING.value,
    )
    alive = (WorkerStatus.ONLINE.value, WorkerStatus.BUSY.value)
    now = datetime.now(UTC)
    cutoff = (now - timedelta(seconds=min_idle_seconds)).isoformat()
    placeholders_active = ",".join("?" * len(active))
    placeholders_alive = ",".join("?" * len(alive))
    sql = (
        "UPDATE jobs SET status = ?, worker_id = NULL, started_at = NULL,"
        " progress = 0, phase = NULL, updated_at = ?"
        " WHERE id IN ("
        "   SELECT j.id FROM jobs j LEFT JOIN workers w ON w.id = j.worker_id"
        f"  WHERE j.status IN ({placeholders_active})"
        f"    AND (w.status IS NULL OR w.status NOT IN ({placeholders_alive}))"
        "     AND j.updated_at < ?"
        " )"
        f" AND status IN ({placeholders_active})"
        "  AND updated_at < ?"
        " RETURNING id, source_path"
    )
    async with db.execute(
        sql,
        (
            JobStatus.QUEUED.value,
            now.isoformat(),
            *active,
            *alive,
            cutoff,
            *active,
            cutoff,
        ),
    ) as cur:
        rows = await cur.fetchall()
    await db.commit()
    return [dict(r) for r in rows]


# The reconciliation grace (worker-resilience spec D3): how long a LIVE
# worker must have been heartbeating a different (or no) job before an
# active job assigned to it counts as abandoned. The spec floor is 3
# heartbeat intervals; 120s = 12 default intervals. Far tighter than the
# dead-worker 600s is safe here — the evidence is a live worker actively
# denying the job every ~10s, not mere silence. Shared by the requeue
# sweep (main.py) and the audit report so they never disagree.
ABANDONED_GRACE_SECONDS = 120

# The sustained-mismatch condition, shared verbatim by the find/requeue
# pair below. A worker heartbeating THE job's id never matches, regardless
# of how stale the job row is — a multi-hour VMAF gauge sends no progress,
# but its heartbeat names the job the whole time (the long-gauge safety
# property). current_job_changed_at IS NOT NULL keeps pre-migration rows
# (no transition observed yet) out of the sweep. j.updated_at is checked
# too: a claim bumps it, so a just-claimed job is safe even though the
# worker's previous mismatch (e.g. NULL since its last job) is old.
_ABANDONED_CONDITION = (
    "  AND w.status IN ({alive})"
    "  AND (w.current_job_id IS NULL OR w.current_job_id != j.id)"
    "  AND w.current_job_changed_at IS NOT NULL"
    "  AND w.current_job_changed_at < ?"
    "  AND j.updated_at < ?"
)


async def find_abandoned_active_jobs(
    db: DBConnection, *, grace_seconds: int = ABANDONED_GRACE_SECONDS
) -> list[dict[str, object]]:
    """Find active jobs whose worker is ALIVE but heartbeating a different
    (or no) current_job_id, sustained past the grace window.

    The live-worker counterpart to find_orphan_active_jobs: the worker
    crashed mid-pipeline and restarted, or lost its terminal report — its
    own heartbeat demonstrably disowns the job, yet the job renders as
    live and the dead-worker sweep will never touch it.
    """
    active = (
        JobStatus.TRANSCODING.value,
        JobStatus.ASSIGNED.value,
        JobStatus.VERIFYING.value,
    )
    alive = (WorkerStatus.ONLINE.value, WorkerStatus.BUSY.value)
    cutoff = (datetime.now(UTC) - timedelta(seconds=grace_seconds)).isoformat()
    condition = _ABANDONED_CONDITION.format(alive=",".join("?" * len(alive)))
    query = (
        "SELECT j.id, j.source_path, j.status, j.worker_id, j.started_at, "
        "       w.current_job_id AS worker_current_job_id "
        "FROM jobs j JOIN workers w ON w.id = j.worker_id "
        f"WHERE j.status IN ({','.join('?' * len(active))}) " + condition + " ORDER BY j.started_at"
    )
    async with db.execute(query, (*active, *alive, cutoff, cutoff)) as cur:
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def requeue_abandoned_active_jobs(
    db: DBConnection, *, grace_seconds: int = ABANDONED_GRACE_SECONDS
) -> list[dict[str, object]]:
    """Requeue active jobs their (live) worker has demonstrably abandoned.

    The healing counterpart to find_abandoned_active_jobs — same
    conditions. The outer status/updated_at re-check mirrors
    requeue_orphan_active_jobs: under Postgres READ COMMITTED only outer
    quals are re-evaluated against a concurrently-changed row, so a
    terminal report landing at the exact moment of the sweep must flip
    the job OUT of the sweep's reach, never be stomped back to QUEUED.
    """
    active = (
        JobStatus.TRANSCODING.value,
        JobStatus.ASSIGNED.value,
        JobStatus.VERIFYING.value,
    )
    alive = (WorkerStatus.ONLINE.value, WorkerStatus.BUSY.value)
    now = datetime.now(UTC)
    cutoff = (now - timedelta(seconds=grace_seconds)).isoformat()
    placeholders_active = ",".join("?" * len(active))
    condition = _ABANDONED_CONDITION.format(alive=",".join("?" * len(alive)))
    sql = (
        "UPDATE jobs SET status = ?, worker_id = NULL, started_at = NULL,"
        " progress = 0, phase = NULL, updated_at = ?"
        " WHERE id IN ("
        "   SELECT j.id FROM jobs j JOIN workers w ON w.id = j.worker_id"
        f"  WHERE j.status IN ({placeholders_active})" + condition + " )"
        f" AND status IN ({placeholders_active})"
        "  AND updated_at < ?"
        " RETURNING id, source_path"
    )
    async with db.execute(
        sql,
        (
            JobStatus.QUEUED.value,
            now.isoformat(),
            *active,
            *alive,
            cutoff,
            cutoff,
            *active,
            cutoff,
        ),
    ) as cur:
        rows = await cur.fetchall()
    await db.commit()
    return [dict(r) for r in rows]


async def claim_next_job(
    db: DBConnection,
    worker_id: str,
    supported_codecs: list[str] | None = None,
    *,
    supports_downscale: bool = False,
) -> Job | None:
    """Atomically claim the next pending/queued job this worker can encode.

    A single ``UPDATE ... RETURNING`` claims and returns the row in one
    statement. On PostgreSQL the inner SELECT takes ``FOR UPDATE SKIP
    LOCKED`` so concurrent workers grab *different* rows instead of
    contending for the oldest; SQLite serializes writers itself, so the
    plain subquery is race-safe there.

    Only jobs whose target_codec the worker advertised are eligible — a
    job with no capable worker simply stays PENDING (never fails at
    claim time). Workers that predate codec advertisement default to hevc.
    Downscale jobs (target_height set) follow the same rule keyed on
    supports_downscale: a worker that didn't advertise it would encode at
    source resolution, silently ignoring the request.
    """
    codecs = supported_codecs or ["hevc"]
    now = datetime.now(UTC).isoformat()
    lock_clause = " FOR UPDATE SKIP LOCKED" if db.dialect == "postgres" else ""
    codec_placeholders = ",".join("?" * len(codecs))
    downscale_clause = "" if supports_downscale else " AND target_height IS NULL"
    sql = f"""UPDATE jobs
        SET status = ?, worker_id = ?, started_at = ?, updated_at = ?
        WHERE id = (
            SELECT id FROM jobs
            WHERE status IN (?, ?)
              AND target_codec IN ({codec_placeholders}){downscale_clause}
            ORDER BY created_at ASC
            LIMIT 1{lock_clause}
        )
        RETURNING *"""
    async with db.execute(
        sql,
        (
            JobStatus.ASSIGNED.value,
            worker_id,
            now,
            now,
            JobStatus.PENDING.value,
            JobStatus.QUEUED.value,
            *codecs,
        ),
    ) as cur:
        row = await cur.fetchone()
    await db.commit()
    return _row_to_job(row) if row else None


async def job_exists_for_path(db: DBConnection, source_path: str) -> bool:
    """Check if a non-terminal job exists for this file path."""
    terminal = (JobStatus.FAILED.value, JobStatus.CANCELLED.value)
    async with db.execute(
        "SELECT COUNT(*) FROM jobs WHERE source_path = ? AND status NOT IN (?, ?)",
        (source_path, *terminal),
    ) as cursor:
        row = await cursor.fetchone()
        return bool(row and row[0] > 0)


async def active_paths(db: DBConnection, paths: list[str]) -> set[str]:
    """Return the subset of `paths` that already have a non-terminal job (one query)."""
    if not paths:
        return set()
    terminal = (JobStatus.FAILED.value, JobStatus.CANCELLED.value)
    placeholders = ",".join("?" * len(paths))
    async with db.execute(
        f"SELECT DISTINCT source_path FROM jobs "
        f"WHERE source_path IN ({placeholders}) AND status NOT IN (?, ?)",
        [*paths, *terminal],
    ) as cur:
        return {row["source_path"] for row in await cur.fetchall()}

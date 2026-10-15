"""Read-only tools over the catalog and the queue.

Each tool returns a dict of documented fields only (docs/MCP-THREAT-MODEL.md
rows 13 and 14): catalog strings come back as data, clipped to a bound, and
an anticipated failure is raised as ``ToolError`` so the SDK answers with
an ``isError`` result carrying the reason; anything else surfaces only as
"Error executing tool", never a traceback.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from transcode_forge.api.routes.stats import collect_stats
from transcode_forge.db import DBConnection
from transcode_forge.mcp.registry import ToolSpec, check_policy
from transcode_forge.models.job import Job, JobStatus
from transcode_forge.repos import jobs as job_repo
from transcode_forge.repos import libraries as lib_repo
from transcode_forge.repos import media as media_repo
from transcode_forge.repos import system as system_repo

MAX_LIMIT = 200
MAX_TEXT = 512

LIBRARY_FIELDS = ("id", "name", "media_type", "path", "enabled", "quality_preset")
MEDIA_FIELDS = (
    "id",
    "library_id",
    "filename",
    "file_path",
    "video_codec",
    "resolution",
    "file_size",
    "transcode_status",
)
JOB_LIST_FIELDS = (
    "id",
    "source_path",
    "library_id",
    "status",
    "target_codec",
    "progress",
    "created_at",
    "completed_at",
    "space_saved",
    "error_message",
)
JOB_DETAIL_FIELDS = (
    *JOB_LIST_FIELDS,
    "source_codec",
    "source_resolution",
    "source_size",
    "output_size",
    "quality_value",
    "resolved_crf",
    "achieved_vmaf",
    "backend_used",
    "worker_id",
    "phase",
    "phase_detail",
    "retry_count",
    "started_at",
    "updated_at",
)


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_TEXT:
        return value[:MAX_TEXT]
    return value


def _pick(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: _clip(row.get(field)) for field in fields}


def _job_row(job: Job, fields: tuple[str, ...]) -> dict[str, Any]:
    return _pick(job.model_dump(mode="json"), fields)


def _check_page(limit: int, offset: int) -> None:
    if not 1 <= limit <= MAX_LIMIT:
        raise ToolError(f"limit must be between 1 and {MAX_LIMIT}")
    if offset < 0:
        raise ToolError("offset must be 0 or more")


def _check_status(status: str | None) -> None:
    if status is None:
        return
    known = {s.value for s in JobStatus}
    unknown = [part for part in status.split(",") if part.strip() not in known]
    if unknown:
        raise ToolError(f"Unknown status: {', '.join(unknown)[:MAX_TEXT]}")


def build_tools(get_db: Callable[[], DBConnection]) -> list[ToolSpec]:
    """The tool functions, bound to the database getter. Each function's
    signature is the tool's input schema."""

    async def list_libraries() -> dict[str, Any]:
        rows = await lib_repo.list_libraries(get_db())
        return {"libraries": [_pick(row, LIBRARY_FIELDS) for row in rows]}

    async def list_media(
        library_id: str | None = None,
        transcode_status: str | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        _check_page(limit, offset)
        rows, total = await media_repo.list_media_files(
            get_db(),
            library_id=library_id,
            transcode_status=transcode_status,
            search=search,
            limit=limit,
            offset=offset,
        )
        return {"files": [_pick(row, MEDIA_FIELDS) for row in rows], "total": total}

    async def list_jobs(
        status: str | None = None,
        library_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        _check_page(limit, offset)
        _check_status(status)
        jobs, total = await job_repo.list_jobs(
            get_db(), status=status, library_id=library_id, limit=limit, offset=offset
        )
        return {"jobs": [_job_row(job, JOB_LIST_FIELDS) for job in jobs], "total": total}

    async def get_job(job_id: str) -> dict[str, Any]:
        job = await job_repo.get_job(get_db(), job_id)
        if job is None:
            raise ToolError("Job not found")
        return {"job": _job_row(job, JOB_DETAIL_FIELDS)}

    async def queue_summary() -> dict[str, Any]:
        db = get_db()
        return {
            "paused": await system_repo.is_queue_paused(db),
            "by_status": await job_repo.count_by_status(db),
        }

    async def stats() -> dict[str, Any]:
        return await collect_stats(get_db())

    specs = [
        ToolSpec(
            "get_job",
            "One job in full: source and output details, quality numbers, status and timing.",
            get_job,
        ),
        ToolSpec(
            "list_jobs",
            "Jobs in the queue and history, newest first. Filter by status "
            "(comma separated, for example 'pending,transcoding') and library id. "
            "limit is 1 to 200.",
            list_jobs,
        ),
        ToolSpec(
            "list_libraries",
            "The libraries the scheduler scans: id, name, media type, path, "
            "whether enabled and the quality preset.",
            list_libraries,
        ),
        ToolSpec(
            "list_media",
            "Files in the catalog with their codec, resolution, size and transcode "
            "status. Filter by library id, transcode status or a filename search. "
            "limit is 1 to 200.",
            list_media,
        ),
        ToolSpec(
            "queue_summary",
            "Whether the queue is paused and how many jobs sit in each status.",
            queue_summary,
        ),
        ToolSpec(
            "stats",
            "Aggregate numbers: completed jobs, bytes saved, per-library totals, "
            "skip reasons and worker counts.",
            stats,
        ),
    ]
    check_policy(specs)
    return specs

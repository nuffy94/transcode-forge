"""Media browser + queue-from-selection endpoints."""

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from transcode_forge.api.deps import get_db
from transcode_forge.db import DBConnection
from transcode_forge.models.job import Job, JobStatus
from transcode_forge.repos import exclusions as excl_repo
from transcode_forge.repos import jobs as job_repo
from transcode_forge.repos import libraries as lib_repo
from transcode_forge.repos import media as media_repo
from transcode_forge.repos import settings as settings_repo

logger = logging.getLogger(__name__)

router = APIRouter(tags=["media"])


@router.get("/media/movies")
async def browse_movies(
    library_id: str | None = None,
    codec: str | None = None,
    status: str | None = None,
    search: str | None = None,
    sort: str = "filename",
    dir: str = "asc",
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: DBConnection = Depends(get_db),
) -> dict[str, Any]:
    """Browse movie files across all movie libraries."""
    offset = (page - 1) * per_page
    files, total = await media_repo.list_media_files(
        db,
        media_type="movies",
        library_id=library_id,
        video_codec=codec,
        transcode_status=status,
        search=search,
        sort_by=sort,
        sort_dir=dir,
        limit=per_page,
        offset=offset,
    )
    return {"data": files, "meta": {"total": total, "page": page, "per_page": per_page}}


@router.get("/media/tv")
async def browse_tv(
    library_id: str | None = None,
    codec: str | None = None,
    status: str | None = None,
    show: str | None = None,
    search: str | None = None,
    sort: str = "show_name",
    dir: str = "asc",
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: DBConnection = Depends(get_db),
) -> dict[str, Any]:
    """Browse TV files across all TV libraries."""
    offset = (page - 1) * per_page
    files, total = await media_repo.list_media_files(
        db,
        media_type="tv",
        library_id=library_id,
        video_codec=codec,
        transcode_status=status,
        show_name=show,
        search=search,
        sort_by=sort,
        sort_dir=dir,
        limit=per_page,
        offset=offset,
    )
    return {"data": files, "meta": {"total": total, "page": page, "per_page": per_page}}


@router.get("/media/tv/shows")
async def list_tv_shows(
    library_id: str | None = None,
    db: DBConnection = Depends(get_db),
) -> dict[str, Any]:
    """List TV shows with episode counts and transcode stats."""
    shows = await media_repo.list_tv_shows(db, library_id=library_id)
    return {"data": shows}


class QueueRequest(BaseModel):
    file_ids: list[str]
    # Per-job output codec (D1). Omitted → the default_codec setting
    # (DB override else TF_DEFAULT_CODEC else hevc). AV1 is opt-in via
    # the UI selector, which carries the compatibility warning.
    codec: str | None = Field(default=None, pattern=r"^(hevc|av1)$")
    # Downscale target (plans/downscale-shrink-spec.md): fixed option list
    # mirrored by the UI selector. None = keep source resolution — and
    # today's h264-only eligibility rule.
    target_height: Literal[1080, 720] | None = None


# Statuses that always block queueing (an active job exists). Without a
# downscale, 'complete' blocks too; WITH one it's the whole point — an
# already-HEVC/AV1 file (which the scanner catalogs as complete/skipped)
# is exactly what the same-codec shrink exists for.
_IN_FLIGHT = ("queued", "transcoding")


def _downscale_codec(source_codec: str, explicit: str | None, h264_default: str) -> str | None:
    """The queue validity matrix, downscale side: resolve the job's target
    codec, or None when this (source, picked) combination must be skipped.

    Same-codec shrink rides the downscale: with no explicit pick an
    hevc/av1 source keeps its codec (never silently converted by the
    global default). An explicit pick wins, except av1 → hevc (a codec
    downgrade). Other sources stay out, matching the h264-only rule's
    conservatism; h264 → h264 stays out by construction (the pickable
    codecs are hevc/av1 — the product's purpose is getting OFF h264).
    """
    if source_codec == "h264":
        return h264_default
    if source_codec == "hevc":
        return explicit or "hevc"
    if source_codec == "av1":
        return None if explicit == "hevc" else "av1"
    return None


@router.post("/media/queue")
async def queue_selected_files(
    body: QueueRequest,
    request: Request,
    db: DBConnection = Depends(get_db),
) -> dict[str, Any]:
    """Queue selected media files for transcoding.

    Creates jobs in the DB — workers pick them up via claim polling.
    Deduplication: skips files that already have an active job.
    """
    queued = 0
    skipped = 0

    settings = getattr(request.app.state, "settings", None)
    target_codec = body.codec or await settings_repo.effective(db, "default_codec", settings)
    # Snapshot the quality goal on the job (one source of truth) — the
    # worker's VMAF gate and CRF search read it from there.
    target_vmaf = float(await settings_repo.effective(db, "target_vmaf", settings))

    # Batch the lookups up front — one query each — instead of ~4 per file.
    by_id = {m["id"]: m for m in await media_repo.get_by_ids(db, body.file_ids)}

    # First pass: the queue validity matrix — per-file eligibility plus the
    # resolved target codec (a downscale batch can mix h264/hevc/av1
    # sources, each landing on its own codec).
    candidates: list[dict[str, Any]] = []
    resolved_codecs: dict[str, str] = {}
    for file_id in body.file_ids:
        mf = by_id.get(file_id)
        if not mf or mf["transcode_status"] in _IN_FLIGHT:
            skipped += 1
            continue
        resolved: str | None
        if body.target_height is None:
            # Today's rule, unchanged: h264 sources only; 'complete' blocks.
            if mf["video_codec"] != "h264" or mf["transcode_status"] == "complete":
                skipped += 1
                continue
            resolved = target_codec
        else:
            # A downscale must be strictly downward — unknown dimensions
            # can't prove that, and a no-op scale is never queued.
            resolved = _downscale_codec(mf["video_codec"], body.codec, target_codec)
            if resolved is None or not mf["height"] or mf["height"] <= body.target_height:
                skipped += 1
                continue
        resolved_codecs[mf["id"]] = resolved
        candidates.append(mf)

    paths = [m["file_path"] for m in candidates]
    excluded = await excl_repo.filter_excluded(db, paths)  # "don't try this again"
    active = await job_repo.active_paths(db, paths)  # dedup vs in-flight jobs

    # One transaction for the whole batch: each file's job row and media
    # status update commit together, and a mid-batch failure rolls the
    # whole thing back — no half-queued state. Presets are read inside the
    # transaction so a concurrent library-settings change can't produce
    # jobs with a stale quality value.
    seen: set[str] = set()
    async with db.transaction() as tx:
        libs = await lib_repo.list_libraries(tx)
        presets = {lib["id"]: lib["quality_preset"] for lib in libs}
        # Jobs carry the library NAME — the queue/Activity filters and the
        # stats group-bys all match on it. Storing the UUID here made
        # media-queued jobs invisible to library filtering (fixed along
        # with migration 0008, which backfills old rows).
        lib_names = {lib["id"]: lib["name"] for lib in libs}
        for mf in candidates:
            path = mf["file_path"]
            if path in excluded or path in active or path in seen:
                skipped += 1
                continue
            seen.add(path)

            job = Job(
                source_path=path,
                library=lib_names.get(mf["library_id"], mf["library_id"]),
                source_codec=mf["video_codec"],
                source_resolution=mf["resolution"],
                source_bitrate=mf["bitrate"],
                source_duration=mf["duration"],
                source_size=mf["file_size"],
                target_codec=resolved_codecs[mf["id"]],
                target_height=body.target_height,
                quality_value=presets.get(mf["library_id"], 21),
                target_vmaf=target_vmaf,
                status=JobStatus.QUEUED,
            )
            await job_repo.create_job(tx, job)
            await media_repo.update_media_status(
                tx, mf["id"], transcode_status="queued", job_id=job.id
            )
            queued += 1

    return {"queued": queued, "skipped": skipped}


class SkipRequest(BaseModel):
    file_ids: list[str]
    reason: str = "manual_skip"


@router.post("/media/skip")
async def skip_selected_files(
    body: SkipRequest,
    db: DBConnection = Depends(get_db),
) -> dict[str, Any]:
    """Manually skip selected files."""
    count = await media_repo.bulk_update_status(
        db,
        body.file_ids,
        transcode_status="skipped",
        skip_reason=body.reason,
    )
    return {"skipped": count}


@router.post("/media/unskip")
async def unskip_selected_files(
    body: QueueRequest,  # reuse — just needs file_ids
    db: DBConnection = Depends(get_db),
) -> dict[str, Any]:
    """Un-skip selected files (reset to needs_transcode if h264)."""
    count = await media_repo.bulk_update_status(
        db,
        body.file_ids,
        transcode_status="needs_transcode",
    )
    return {"unskipped": count}


@router.get("/media/stats")
async def media_stats(
    db: DBConnection = Depends(get_db),
) -> dict[str, Any]:
    """Aggregate stats across all media files."""
    codec_stats = await media_repo.get_codec_stats(db)
    status_stats = await media_repo.get_status_stats(db)
    movie_stats = await media_repo.get_status_stats(db, media_type="movies")
    tv_stats = await media_repo.get_status_stats(db, media_type="tv")
    return {
        "data": {
            "codecs": codec_stats,
            "statuses": status_stats,
            "movies": movie_stats,
            "tv": tv_stats,
        }
    }

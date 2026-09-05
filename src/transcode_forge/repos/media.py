"""Media file repository — inventory of all files across all libraries."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from transcode_forge.db import DBConnection
from transcode_forge.scanner.probe import format_resolution

_VALID_TRANSCODE_STATUSES = frozenset(
    {
        "pending",
        "needs_transcode",
        "queued",
        "transcoding",
        "complete",
        "skipped",
    }
)


async def upsert_media_file(
    db: DBConnection,
    *,
    library_id: str,
    file_path: str,
    filename: str,
    show_name: str | None = None,
    season: int | None = None,
    episode: int | None = None,
    video_codec: str | None = None,
    audio_codec: str | None = None,
    resolution: str | None = None,
    width: int | None = None,
    height: int | None = None,
    bitrate: int | None = None,
    duration: float | None = None,
    file_size: int | None = None,
    file_modified_at: str | None = None,
) -> str:
    """Insert or update a media file in the inventory. Returns file ID."""
    now = datetime.now(UTC).isoformat()
    file_id = str(uuid4())

    # Determine initial transcode status based on codec. On a rescan of a
    # known path the ON CONFLICT clause adopts these only when the probed
    # codec changed (the swap landed); the same codec or a NULL probe keeps
    # the existing status and skip_reason, so a queued job stays queued and
    # a VMAF-gate skip survives.
    status = "pending"
    skip_reason = None
    if video_codec == "hevc":
        status = "complete"
        skip_reason = "already_hevc"
    elif video_codec and video_codec != "h264":
        status = "skipped"
        skip_reason = "not_h264"
    elif video_codec == "h264":
        status = "needs_transcode"

    await db.execute(
        """INSERT INTO media_files (id, library_id, file_path, filename,
            show_name, season, episode,
            video_codec, audio_codec, resolution, width, height,
            bitrate, duration, file_size,
            transcode_status, skip_reason,
            file_modified_at, scanned_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(file_path) DO UPDATE SET
            video_codec = excluded.video_codec,
            audio_codec = excluded.audio_codec,
            resolution = excluded.resolution,
            width = excluded.width,
            height = excluded.height,
            bitrate = excluded.bitrate,
            duration = excluded.duration,
            file_size = excluded.file_size,
            transcode_status = CASE
                WHEN excluded.video_codec IS NULL
                    OR excluded.video_codec = media_files.video_codec
                THEN media_files.transcode_status
                ELSE excluded.transcode_status END,
            skip_reason = CASE
                WHEN excluded.video_codec IS NULL
                    OR excluded.video_codec = media_files.video_codec
                THEN media_files.skip_reason
                ELSE excluded.skip_reason END,
            file_modified_at = excluded.file_modified_at,
            scanned_at = excluded.scanned_at,
            updated_at = excluded.updated_at
        """,
        (
            file_id,
            library_id,
            file_path,
            filename,
            show_name,
            season,
            episode,
            video_codec,
            audio_codec,
            resolution,
            width,
            height,
            bitrate,
            duration,
            file_size,
            status,
            skip_reason,
            file_modified_at,
            now,
            now,
        ),
    )
    await db.commit()
    return file_id


async def get_media_file(db: DBConnection, file_id: str) -> dict[str, Any] | None:
    async with db.execute("SELECT * FROM media_files WHERE id = ?", (file_id,)) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def ids_by_paths(db: DBConnection, paths: list[str]) -> dict[str, str]:
    """Map file_path -> media file id for the given paths (one query).

    Lets job rows link to the file-detail drawer without a per-row lookup.
    """
    if not paths:
        return {}
    placeholders = ",".join("?" * len(paths))
    async with db.execute(
        f"SELECT id, file_path FROM media_files WHERE file_path IN ({placeholders})",
        list(paths),
    ) as cur:
        return {row["file_path"]: row["id"] for row in await cur.fetchall()}


async def get_by_ids(db: DBConnection, file_ids: list[str]) -> list[dict[str, Any]]:
    """Fetch many media files in one query (order not preserved)."""
    if not file_ids:
        return []
    placeholders = ",".join("?" * len(file_ids))
    async with db.execute(
        f"SELECT * FROM media_files WHERE id IN ({placeholders})", list(file_ids)
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def list_media_files(
    db: DBConnection,
    *,
    media_type: str | None = None,
    library_id: str | None = None,
    video_codec: str | None = None,
    transcode_status: str | None = None,
    show_name: str | None = None,
    search: str | None = None,
    sort_by: str = "filename",
    sort_dir: str = "asc",
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """List media files with filtering, sorting, pagination."""
    conditions: list[str] = []
    params: list[Any] = []

    if media_type:
        conditions.append("l.media_type = ?")
        params.append(media_type)
    if library_id:
        conditions.append("m.library_id = ?")
        params.append(library_id)
    if video_codec:
        conditions.append("m.video_codec = ?")
        params.append(video_codec)
    if transcode_status:
        statuses = [s.strip() for s in transcode_status.split(",")]
        placeholders = ",".join("?" * len(statuses))
        conditions.append(f"m.transcode_status IN ({placeholders})")
        params.extend(statuses)
    if show_name:
        conditions.append("m.show_name = ?")
        params.append(show_name)
    if search:
        conditions.append("m.filename LIKE ?")
        params.append(f"%{search}%")

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    # Validate sort column
    valid_sorts = {
        "filename": "m.filename",
        "file_size": "m.file_size",
        "video_codec": "m.video_codec",
        "resolution": "m.width",
        "duration": "m.duration",
        "scanned_at": "m.scanned_at",
        "file_modified_at": "m.file_modified_at",
        "transcode_status": "m.transcode_status",
        "show_name": "m.show_name",
    }
    sort_col = valid_sorts.get(sort_by, "m.filename")
    direction = "DESC" if sort_dir.lower() == "desc" else "ASC"

    # Count
    async with db.execute(
        f"SELECT COUNT(*) FROM media_files m JOIN libraries l ON m.library_id = l.id {where}",
        params,
    ) as cur:
        row = await cur.fetchone()
        total = row[0] if row else 0

    # Fetch
    query = f"""
        SELECT m.*, l.name as library_name, l.media_type, l.path as library_path
        FROM media_files m
        JOIN libraries l ON m.library_id = l.id
        {where}
        ORDER BY {sort_col} {direction}
        LIMIT ? OFFSET ?
    """
    async with db.execute(query, [*params, limit, offset]) as cur:
        rows = await cur.fetchall()
        return [dict(r) for r in rows], total


async def list_tv_shows(
    db: DBConnection,
    *,
    library_id: str | None = None,
) -> list[dict[str, Any]]:
    """List TV shows with episode counts and transcode stats."""
    conditions = ["m.show_name IS NOT NULL"]
    params: list[Any] = []
    if library_id:
        conditions.append("m.library_id = ?")
        params.append(library_id)
    where = f"WHERE {' AND '.join(conditions)}"

    query = f"""
        SELECT
            m.show_name,
            COUNT(*) as episode_count,
            CAST(SUM(m.file_size) AS BIGINT) as total_size,
            SUM(CASE
                WHEN m.video_codec = 'hevc'
                    OR m.transcode_status = 'complete'
                THEN 1 ELSE 0 END) as transcoded_count,
            SUM(CASE
                WHEN m.video_codec = 'h264'
                    AND m.transcode_status IN ('pending', 'needs_transcode')
                THEN 1 ELSE 0 END) as needs_transcode_count,
            MIN(m.library_id) as library_id
        FROM media_files m
        {where}
        GROUP BY m.show_name
        ORDER BY m.show_name
    """
    async with db.execute(query, params) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def update_media_status(
    db: DBConnection,
    file_id: str,
    *,
    transcode_status: str,
    skip_reason: str | None = None,
    job_id: str | None = None,
) -> None:
    """Update the transcode status of a media file."""
    if transcode_status not in _VALID_TRANSCODE_STATUSES:
        raise ValueError(f"Invalid transcode_status: {transcode_status!r}")
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "UPDATE media_files SET transcode_status = ?, skip_reason = ?,"
        " job_id = ?, updated_at = ? WHERE id = ?",
        (transcode_status, skip_reason, job_id, now, file_id),
    )
    await db.commit()


async def update_status_by_job(
    db: DBConnection,
    job_id: str,
    *,
    transcode_status: str,
    skip_reason: str | None = None,
) -> None:
    """Reflect a job outcome onto the media row that was queued into it.

    Keyed by the job_id stamped at queue time; a no-op when no catalog row
    points at the job. The row's job_id is kept so the file drawer stays
    linked to the outcome. Without this, S3-library rows say 'queued'
    forever after the job ends — the master object never changes, so a
    rescan can't self-heal them the way a filesystem swap does.
    """
    if transcode_status not in _VALID_TRANSCODE_STATUSES:
        raise ValueError(f"Invalid transcode_status: {transcode_status!r}")
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "UPDATE media_files SET transcode_status = ?, skip_reason = ?, updated_at = ?"
        " WHERE job_id = ?",
        (transcode_status, skip_reason, now, job_id),
    )
    await db.commit()


def _scaled_width(width: int | None, height: int | None, target_height: int) -> int | None:
    """Width of a downscale output: the encoder runs scale=-2:H (aspect
    kept, width snapped to the nearest even value), so the same arithmetic
    on the scanned source dimensions gives the width the file now has.
    ffmpeg computes av_rescale(H, iw, ih * 2) * 2 (libavfilter/scale_eval.c)
    and av_rescale rounds to nearest, so this is nearest-even, not round-up.
    None when the source dimensions were never scanned."""
    if not width or not height:
        return None
    return (width * target_height + height) // (2 * height) * 2


async def update_output_by_job(
    db: DBConnection,
    job_id: str,
    *,
    video_codec: str,
    file_size: int,
    target_height: int | None = None,
) -> None:
    """Describe the swapped-in output file on the media row queued into a
    completed job: codec, size and (for downscale jobs) dimensions.

    Only the COMPLETE path calls this. update_status_by_job covers the
    status, but the codec and size it leaves behind are the last scan's,
    so a swapped file reads complete|h264 until something re-probes it.
    S3 rows are left alone: a job never replaces the master object the
    row describes (the output is a derivative), so the scan's codec and
    size still hold. A no-op when no catalog row points at the job.
    """
    now = datetime.now(UTC).isoformat()
    swapped = "job_id = ? AND library_id IN (SELECT id FROM libraries WHERE backend != 's3')"
    if target_height is None:
        await db.execute(
            "UPDATE media_files SET video_codec = ?, file_size = ?, updated_at = ?"
            f" WHERE {swapped}",
            (video_codec, file_size, now, job_id),
        )
        await db.commit()
        return
    async with db.execute(
        f"SELECT id, width, height FROM media_files WHERE {swapped}", (job_id,)
    ) as cur:
        rows = await cur.fetchall()
    for row in rows:
        width = _scaled_width(row["width"], row["height"], target_height)
        if width is None:
            # The worker does not report output dimensions and the source
            # was never measured, so the width is unknown: leave width and
            # resolution as they are and let the next scan reconcile them.
            await db.execute(
                "UPDATE media_files SET video_codec = ?, file_size = ?, height = ?,"
                " updated_at = ? WHERE id = ?",
                (video_codec, file_size, target_height, now, row["id"]),
            )
            continue
        await db.execute(
            "UPDATE media_files SET video_codec = ?, file_size = ?, width = ?, height = ?,"
            " resolution = ?, updated_at = ? WHERE id = ?",
            (
                video_codec,
                file_size,
                width,
                target_height,
                format_resolution(width, target_height),
                now,
                row["id"],
            ),
        )
    await db.commit()


async def bulk_update_status(
    db: DBConnection,
    file_ids: list[str],
    *,
    transcode_status: str,
    skip_reason: str | None = None,
) -> int:
    """Bulk update transcode status. Returns count updated."""
    if transcode_status not in _VALID_TRANSCODE_STATUSES:
        raise ValueError(f"Invalid transcode_status: {transcode_status!r}")
    if not file_ids:
        # An empty IN () is a syntax error on Postgres (SQLite tolerates it).
        return 0
    now = datetime.now(UTC).isoformat()
    placeholders = ",".join("?" * len(file_ids))
    cur = await db.execute(
        "UPDATE media_files SET transcode_status = ?, skip_reason = ?,"
        f" updated_at = ? WHERE id IN ({placeholders})",
        [transcode_status, skip_reason, now, *file_ids],
    )
    await db.commit()
    count: int = cur.rowcount
    return count


async def get_codec_stats(db: DBConnection) -> dict[str, Any]:
    """Get codec distribution across all files."""
    # CAST to BIGINT: Postgres SUM(BIGINT) returns numeric → Decimal,
    # which leaks into the /api/media/stats JSON (SQLite returns int).
    async with db.execute(
        """SELECT video_codec, COUNT(*) as count,
                  CAST(SUM(file_size) AS BIGINT) as total_size
           FROM media_files GROUP BY video_codec ORDER BY count DESC"""
    ) as cur:
        return {
            row["video_codec"] or "unknown": {
                "count": row["count"],
                "total_size": row["total_size"],
            }
            for row in await cur.fetchall()
        }


async def get_status_stats(db: DBConnection, media_type: str | None = None) -> dict[str, Any]:
    """Get transcode status distribution."""
    if media_type:
        query = """SELECT m.transcode_status, COUNT(*) as count
                   FROM media_files m JOIN libraries l ON m.library_id = l.id
                   WHERE l.media_type = ? GROUP BY m.transcode_status"""
        params: tuple[Any, ...] = (media_type,)
    else:
        query = (
            "SELECT transcode_status, COUNT(*) as count FROM media_files GROUP BY transcode_status"
        )
        params = ()
    async with db.execute(query, params) as cur:
        return {row["transcode_status"]: row["count"] for row in await cur.fetchall()}

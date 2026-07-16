"""Shared test helpers used across API/pipeline test modules.

One home for the worker-registration dance and the ffprobe stub — the
worker-token/register API shape has changed once already; a single copy
means the next change is a one-file fix.
"""

from transcode_forge.scanner.probe import ProbeResult


def make_probe(codec: str = "hevc") -> ProbeResult:
    """A 1080p hevc-ish probe result for pipeline tests."""
    return ProbeResult(
        video_codec=codec,
        width=1920,
        height=1080,
        bitrate=5_000_000,
        duration=3600.0,
        file_size=5000,
    )


async def register_worker(client, worker_client, label: str, supported_codecs=None, **extra):
    """Issue a token (admin client) and register a worker with it
    (worker client); returns (auth headers, worker_id).

    Extra keyword args land in the registration body verbatim (e.g.
    supports_downscale=True); omitted keys exercise the old-worker
    defaults, same as supported_codecs=None does."""
    issue = await client.post("/api/worker-tokens", json={"label": label})
    headers = {"Authorization": f"Bearer {issue.json()['token']}"}
    body = {"name": label, "host": "h", "capabilities": ["cpu"], **extra}
    if supported_codecs is not None:
        body["supported_codecs"] = supported_codecs
    reg = await worker_client.post("/api/worker/register", json=body, headers=headers)
    assert reg.status_code == 200
    return headers, reg.json()["worker_id"]


async def seed_library(db, lib_id: str = "movies", media_type: str = "movies") -> str:
    """Insert a library row if missing; returns the library id."""
    async with db.execute("SELECT id FROM libraries WHERE id = ?", (lib_id,)) as cur:
        if await cur.fetchone() is not None:
            return lib_id
    await db.execute(
        """INSERT INTO libraries (id, name, media_type, path, quality_preset,
            enabled, auto_scan, scan_interval_hours, created_at, updated_at)
        VALUES (?, ?, ?, ?, 21, 1, 0, 24, '2026-01-01', '2026-01-01')""",
        (lib_id, lib_id, media_type, f"/media/{lib_id}"),
    )
    await db.commit()
    return lib_id


async def seed_media_file(
    db,
    path: str,
    *,
    library_id: str = "movies",
    codec: str = "h264",
    width: int = 3840,
    height: int = 2160,
    show_name=None,
    season=None,
    episode=None,
) -> str:
    """Catalog one media file (library auto-created); returns the file id.

    transcode_status follows the scanner's codec rule inside
    upsert_media_file (h264 → needs_transcode, hevc → complete,
    anything else → skipped)."""
    from pathlib import Path

    from transcode_forge.repos import media as media_repo

    media_type = "tv" if show_name else "movies"
    await seed_library(db, library_id, media_type)
    return await media_repo.upsert_media_file(
        db,
        library_id=library_id,
        file_path=path,
        filename=Path(path).name,
        show_name=show_name,
        season=season,
        episode=episode,
        video_codec=codec,
        audio_codec="aac",
        resolution=f"{width}x{height}" if width and height else None,
        width=width,
        height=height,
        bitrate=8_000_000,
        duration=5400.0,
        file_size=4_000_000_000,
    )

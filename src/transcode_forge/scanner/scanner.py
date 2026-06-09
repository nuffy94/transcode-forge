"""Library scanner — probe files and populate the media_files inventory.

The scanner does NOT create jobs. It builds the browseable catalog.
Users select files from the catalog and queue them via the UI.
"""

import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from transcode_forge.db import DBConnection
from transcode_forge.models.scan import Scan, ScanStatus
from transcode_forge.repos import media as media_repo
from transcode_forge.repos import scans as scan_repo
from transcode_forge.scanner.probe import ProbeError, ffprobe, is_video_file

logger = logging.getLogger(__name__)

# Regex for parsing TV show filenames: "Show Name S01E02" or "Show Name - 1x02"
TV_EPISODE_RE = re.compile(
    r"^(?P<show>.+?)\s*[-.]?\s*[Ss](?P<season>\d+)[Ee](?P<episode>\d+)",
)
TV_EPISODE_ALT_RE = re.compile(
    r"^(?P<show>.+?)\s*[-.]?\s*(?P<season>\d+)[xX](?P<episode>\d+)",
)


def parse_tv_info(filepath: Path) -> tuple[str | None, int | None, int | None]:
    """Try to parse show name, season, episode from a file path.

    Uses the parent directory name and filename for matching.
    """
    # Try filename first
    for regex in (TV_EPISODE_RE, TV_EPISODE_ALT_RE):
        match = regex.match(filepath.stem)
        if match:
            show = match.group("show").replace(".", " ").strip()
            return show, int(match.group("season")), int(match.group("episode"))

    # Fallback: use grandparent dir as show name, parent as season
    parts = filepath.parts
    if len(parts) >= 3:
        show_name = parts[-3] if parts[-2].lower().startswith("season") else parts[-2]
        return show_name, None, None

    return None, None, None


async def scan_library(
    *,
    library_id: str,
    library_name: str,
    library_path: str,
    media_type: str,
    db: DBConnection,
    max_files: int = 0,
) -> Scan:
    """Scan a library and populate/update the media_files inventory.

    This does NOT create transcode jobs — it builds the browseable catalog.

    Args:
        library_id: Database ID of the library.
        library_name: Human-readable name.
        library_path: Filesystem path to scan.
        media_type: 'movies' or 'tv'.
        db: SQLite connection.
        max_files: Max files to probe (0 = unlimited). Useful for testing.
    """
    root = Path(library_path).resolve()

    if not root.exists() or not root.is_dir():
        logger.error("Library path does not exist or is not a directory: %s", library_path)
        scan = Scan(library=library_name, status=ScanStatus.FAILED)
        await scan_repo.create_scan(db, scan)
        await scan_repo.update_scan(db, scan.id, status=ScanStatus.FAILED)
        return scan

    scan = Scan(library=library_name)
    await scan_repo.create_scan(db, scan)

    logger.info("Scanning library '%s' at %s", library_name, library_path)

    files_found = 0
    files_new = 0
    files_updated = 0
    files_skipped = 0

    try:
        # Skip symlinks: following them can catalog files outside the
        # library root, and the pipeline's lock/bak files would land at
        # the link path instead of the real file.
        video_files = [
            f for f in root.rglob("*") if f.is_file() and not f.is_symlink() and is_video_file(f)
        ]
        video_files.sort()

        for file_path in video_files:
            if max_files > 0 and files_found >= max_files:
                break

            files_found += 1
            str_path = str(file_path)

            # Check if already in inventory with same mtime AND size — mtime
            # alone misses replacements within the filesystem's timestamp
            # granularity (1s on some NFS exports).
            async with db.execute(
                "SELECT id, file_modified_at, file_size FROM media_files WHERE file_path = ?",
                (str_path,),
            ) as cur:
                existing = await cur.fetchone()

            stat = file_path.stat()
            file_mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()

            if (
                existing
                and existing["file_modified_at"] == file_mtime
                and existing["file_size"] == stat.st_size
            ):
                files_skipped += 1
                continue

            # Probe the file
            try:
                probe = await ffprobe(file_path)
            except ProbeError as e:
                logger.warning("Skipping %s: %s", file_path, e)
                files_skipped += 1
                continue

            # Parse TV show info if applicable
            show_name, season, episode = (None, None, None)
            if media_type == "tv":
                show_name, season, episode = parse_tv_info(file_path)

            # Get audio codec from probe (first audio stream)
            audio_codec = None
            # probe doesn't include audio yet — could extend later

            await media_repo.upsert_media_file(
                db,
                library_id=library_id,
                file_path=str_path,
                filename=file_path.name,
                show_name=show_name,
                season=season,
                episode=episode,
                video_codec=probe.video_codec,
                audio_codec=audio_codec,
                resolution=probe.resolution,
                width=probe.width,
                height=probe.height,
                bitrate=probe.bitrate,
                duration=probe.duration,
                file_size=probe.file_size,
                file_modified_at=file_mtime,
            )

            if existing:
                files_updated += 1
            else:
                files_new += 1

            # Progress update every 50 files
            if files_found % 50 == 0:
                await scan_repo.update_scan(
                    db,
                    scan.id,
                    files_found=files_found,
                    files_new=files_new,
                    files_updated=files_updated,
                    files_skipped=files_skipped,
                )
                await db.commit()
                logger.info(
                    "Scan %s: found=%d new=%d updated=%d skipped=%d",
                    scan.id,
                    files_found,
                    files_new,
                    files_updated,
                    files_skipped,
                )

    except Exception as e:
        logger.exception("Scan failed: %s", e)
        await scan_repo.update_scan(
            db,
            scan.id,
            files_found=files_found,
            files_new=files_new,
            files_updated=files_updated,
            files_skipped=files_skipped,
            status=ScanStatus.FAILED,
        )
        raise

    # Complete
    await scan_repo.update_scan(
        db,
        scan.id,
        files_found=files_found,
        files_new=files_new,
        files_updated=files_updated,
        files_skipped=files_skipped,
        status=ScanStatus.COMPLETE,
    )

    logger.info(
        "Scan complete: found=%d new=%d updated=%d skipped=%d",
        files_found,
        files_new,
        files_updated,
        files_skipped,
    )
    return scan

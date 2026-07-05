"""S3 library scanner — list objects and probe for metadata.

This module handles scanning S3-backed libraries:
1. List objects in the bucket with the configured prefix (paginated)
2. For each video object:
   - Try probing via presigned GET URL (range-read friendly)
   - Fallback to downloading first ~64 KB if presigned-probe fails
   - Log every fallback to track flaky objects
3. Catalog results into media_files with the S3 key as source_path
"""

import logging
from datetime import UTC, datetime
from typing import Any

from aioboto3 import Session
from botocore.exceptions import BotoCoreError, ClientError

from transcode_forge.config import Settings
from transcode_forge.db import DBConnection
from transcode_forge.models.scan import Scan, ScanStatus
from transcode_forge.repos import media as media_repo
from transcode_forge.repos import scans as scan_repo
from transcode_forge.s3compat import s3_client_config
from transcode_forge.scanner.probe import ProbeError, ffprobe, is_video_file

logger = logging.getLogger(__name__)

# Presigned URL expiration (seconds). 10 minutes is plenty for a probe.
PRESIGNED_URL_EXPIRATION = 600

# Fallback head-bytes size (~64 KB).
HEAD_BYTES_SIZE = 64 * 1024


async def scan_s3_library(
    *,
    library_id: str,
    library_name: str,
    bucket: str,
    prefix: str,
    config: Settings,
    db: DBConnection,
    max_files: int = 0,
) -> dict[str, Any]:
    """Scan an S3 library and populate/update the media_files inventory.

    This does NOT create transcode jobs — it builds the browseable catalog.

    Args:
        library_id: Database ID of the library.
        library_name: Human-readable name.
        bucket: S3 bucket name.
        prefix: Object key prefix (e.g., "masters/movies/").
        config: Application settings with S3 credentials.
        db: Database connection.
        max_files: Max files to probe (0 = unlimited). Useful for testing.

    Returns:
        A dict with scan statistics (files_found, files_new, files_updated, etc.).

    Raises:
        Exception: On unrecoverable errors (logged and re-raised).
    """
    logger.info("Scanning S3 library '%s': bucket=%s, prefix=%s", library_name, bucket, prefix)

    # The scanner owns its scan record (same contract as the filesystem
    # scanner) so an S3 scan is never invisible — a failure before this
    # point in older versions left NO record: success toast, then nothing.
    scan = Scan(library=library_name)
    await scan_repo.create_scan(db, scan)

    files_found = 0
    files_new = 0
    files_updated = 0
    files_skipped = 0
    files_failed = 0

    # Create aioboto3 Session
    session = Session(
        aws_access_key_id=config.s3_access_key_id,
        aws_secret_access_key=config.s3_secret_access_key,
        region_name=config.s3_region,
    )

    try:
        # List all objects in the bucket with the prefix
        async with session.client(
            "s3",
            endpoint_url=config.s3_endpoint_url or None,
            region_name=config.s3_region,
            config=s3_client_config(),
        ) as client:
            paginator = client.get_paginator("list_objects_v2")
            page_iterator = paginator.paginate(Bucket=bucket, Prefix=prefix)

            async for page in page_iterator:
                # Handle empty page
                if "Contents" not in page:
                    continue

                for obj_summary in page["Contents"]:
                    obj_key = obj_summary["Key"]
                    obj_size = obj_summary.get("Size", 0)

                    # Skip directories (keys ending with /)
                    if obj_key.endswith("/"):
                        continue

                    # Skip if not a video file
                    if not _is_s3_video_file(obj_key):
                        continue

                    if max_files > 0 and files_found >= max_files:
                        break

                    files_found += 1

                    # Get last modified time from the object metadata
                    obj_mtime = obj_summary.get("LastModified")
                    if obj_mtime:
                        file_mtime = obj_mtime.isoformat()
                    else:
                        file_mtime = datetime.now(UTC).isoformat()

                    # Check if already in inventory with same mtime
                    async with db.execute(
                        "SELECT id, file_modified_at FROM media_files WHERE file_path = ?",
                        (obj_key,),
                    ) as cur:
                        existing = await cur.fetchone()

                    if existing and existing["file_modified_at"] == file_mtime:
                        files_skipped += 1
                        continue

                    # Probe the file: try presigned-probe first, fallback to head-bytes
                    probe = await _probe_s3_object(
                        client=client,
                        bucket=bucket,
                        key=obj_key,
                        obj_size=obj_size,
                    )

                    if probe is None:
                        files_failed += 1
                        continue

                    # Catalog the file
                    await media_repo.upsert_media_file(
                        db,
                        library_id=library_id,
                        file_path=obj_key,
                        filename=_get_filename_from_s3_key(obj_key),
                        video_codec=probe.video_codec,
                        audio_codec=None,
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
                        logger.info(
                            "S3 scan %s: found=%d new=%d updated=%d skipped=%d failed=%d",
                            library_name,
                            files_found,
                            files_new,
                            files_updated,
                            files_skipped,
                            files_failed,
                        )
                        await db.commit()

    except Exception as e:
        # Not just boto errors — endpoint/credential parsing can raise
        # ValueError before any S3 call. Whatever it was, the record must
        # say FAILED before the exception continues up.
        logger.exception("S3 scan failed for '%s': %s", library_name, e)
        await scan_repo.update_scan(db, scan.id, status=ScanStatus.FAILED)
        raise

    # Commit final changes
    await db.commit()

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
        "S3 scan complete: found=%d new=%d updated=%d skipped=%d failed=%d",
        files_found,
        files_new,
        files_updated,
        files_skipped,
        files_failed,
    )

    return {
        "files_found": files_found,
        "files_new": files_new,
        "files_updated": files_updated,
        "files_skipped": files_skipped,
        "files_failed": files_failed,
    }


async def _probe_s3_object(
    client: Any,
    bucket: str,
    key: str,
    obj_size: int,
) -> Any | None:
    """Probe an S3 object for video metadata.

    Strategy:
    1. Generate a presigned GET URL and run ffprobe on it
    2. On failure, download the first ~64 KB and probe that
    3. Log every fallback

    Args:
        client: aioboto3 S3 client (async context manager already entered).
        bucket: S3 bucket name.
        key: Object key.
        obj_size: Object size in bytes (from list_objects_v2).

    Returns:
        ProbeResult if successful, None if both presigned and fallback fail.
    """
    # Step 1: Try presigned-URL probe
    try:
        presigned_url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=PRESIGNED_URL_EXPIRATION,
        )
        logger.debug("Probing via presigned URL: %s", key)
        probe = await ffprobe(presigned_url)
        return probe

    except (ProbeError, Exception) as presigned_error:
        logger.debug(
            "Presigned-probe failed for %s, attempting fallback to head-bytes: %s",
            key,
            presigned_error,
        )

    # Step 2: Fallback to downloading first ~64 KB
    try:
        logger.warning("Fallback: downloading head-bytes for presigned-probe failure: %s", key)

        # Download the first HEAD_BYTES_SIZE bytes
        download_range = f"bytes=0-{HEAD_BYTES_SIZE - 1}"
        response = await client.get_object(
            Bucket=bucket,
            Key=key,
            Range=download_range,
        )

        # Read the partial data into a BytesIO buffer
        partial_data = await response["Body"].read()
        logger.info("Downloaded %d bytes for %s (head-bytes fallback)", len(partial_data), key)

        # Probe the partial data by writing it to a temp file
        # (ffprobe needs a seekable file-like object, so use asyncio.to_thread for blocking I/O)
        import asyncio
        import os
        import tempfile

        def _write_temp_file() -> str:
            """Synchronous: create temp file and write partial data."""
            with tempfile.NamedTemporaryFile(suffix=".tmp", delete=False) as tmp:
                tmp.write(partial_data)
                return tmp.name

        tmp_path = await asyncio.to_thread(_write_temp_file)

        try:
            probe = await ffprobe(tmp_path)
            return probe
        finally:
            # Clean up temp file
            async def _unlink_temp() -> None:
                """Asynchronous: delete temp file."""
                try:
                    await asyncio.to_thread(os.unlink, tmp_path)
                except Exception as cleanup_error:
                    logger.debug("Failed to clean up temp file %s: %s", tmp_path, cleanup_error)

            await _unlink_temp()

    except Exception as fallback_error:
        # Catch all exceptions from fallback to ensure a single bad file
        # doesn't crash the entire scan. Log specific types for debugging.
        if isinstance(fallback_error, (ProbeError, ClientError, BotoCoreError)):
            error_type = "S3/probe error"
        elif isinstance(fallback_error, OSError):
            error_type = "OS error"
        else:
            error_type = "unexpected error"
        logger.error(
            "Both presigned-probe and head-bytes-fallback failed for %s (%s): %s",
            key,
            error_type,
            fallback_error,
        )
        return None


def _is_s3_video_file(s3_key: str) -> bool:
    """Check if an S3 object key refers to a video file (by extension).

    Args:
        s3_key: S3 object key (e.g., "masters/movies/film.mkv").

    Returns:
        True if the key has a recognized video extension.
    """
    # Extract the filename from the key
    filename = s3_key.rstrip("/").split("/")[-1]
    from pathlib import Path

    return is_video_file(Path(filename))


def _get_filename_from_s3_key(s3_key: str) -> str:
    """Extract the filename from an S3 object key.

    Args:
        s3_key: S3 object key (e.g., "masters/movies/film.mkv").

    Returns:
        The filename (e.g., "film.mkv").
    """
    return s3_key.rstrip("/").split("/")[-1]

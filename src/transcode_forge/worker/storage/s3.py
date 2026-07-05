"""S3-compatible object storage backend using aioboto3.

Master + derivative model with content-addressed keys:
- fetch(): Download a master object from S3 to local scratch.
- commit(): Upload a derivative, compute content-addressed key, register in DB.
- lock/unlock(): DB-based coordination (minimal; job-claim already guards).
- cleanup(): Release scratch space.

Derivatives are named deterministically: blake2b(source_path|source_resolution|
source_audio_codec|target_resolution|target_audio_codec|encoder|crf|preset)
suffixed with encoder+crf+ext. Same source+params → same key → transparent dedup.

Bucket layout: Single bucket with prefixes for v1:
  - masters/{prefix}/{object_key}
  - derivatives/{prefix}/{derivative_key}

This simplifies lifecycle management and avoids cross-bucket rate limits.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from transcode_forge.config import Settings
from transcode_forge.db import DBConnection
from transcode_forge.s3compat import s3_client_config
from transcode_forge.worker.storage.base import CommitResult
from transcode_forge.worker.storage.scratch import ScratchManager

logger = logging.getLogger(__name__)


# aioboto3.Session is imported lazily within methods to handle moto mocking.


class S3Backend:
    """S3-compatible object storage backend (aioboto3).

    CRITICAL INVARIANT:
    fetch() returns a LOCAL filesystem path that is passed to run_pipeline().
    Never return S3 keys or remote identifiers.

    This backend uses aioboto3 to manage an S3-compatible service
    (e.g., Linode Object Storage, MinIO, AWS S3). A single Session is
    created at init and reused for all operations.
    """

    def __init__(
        self,
        config: Settings,
        db: DBConnection,
        scratch_manager: ScratchManager,
        library_id: str,
        bucket: str,
        prefix: str = "",
    ) -> None:
        """Initialize the S3 backend.

        Args:
            config: Application settings with S3 credentials.
            db: Database connection for derivatives registry.
            scratch_manager: ScratchManager for temporary downloads.
            library_id: Library identifier (for derivatives table).
            bucket: S3 bucket name.
            prefix: Optional prefix for all objects (e.g., "library/movies/").
        """
        self.config = config
        self.db = db
        self.scratch_manager = scratch_manager
        self.library_id = library_id
        self.bucket = bucket
        self.prefix = prefix

        # Create a single aioboto3 Session, reused for all S3 operations.
        # Import here to support test mocking.
        from aioboto3 import Session

        self.session = Session(
            aws_access_key_id=config.s3_access_key_id,
            aws_secret_access_key=config.s3_secret_access_key,
            region_name=config.s3_region,
        )
        logger.info(
            "S3Backend initialized: bucket=%s, prefix=%s, endpoint=%s",
            bucket,
            prefix,
            config.s3_endpoint_url or "(default AWS)",
        )

    async def lock(self, key: str) -> None:
        """Acquire a lock on a source key.

        For S3 backend, the job-claim mechanism already prevents concurrent
        work on the same source_path. This is a no-op; documented for clarity.

        Args:
            key: Source identifier (S3 object key).
        """
        # The existing job-claim guard (job.status=ASSIGNED to ASSIGNED+worker_id)
        # prevents two workers from claiming the same source. No additional
        # DB-row lock needed.
        pass

    async def unlock(self, key: str) -> None:
        """Release a lock on a source key.

        No-op for S3 backend (see lock() above).

        Args:
            key: Source identifier.
        """
        pass

    async def fetch(self, source: str) -> Path:
        """Download a master object from S3 to local scratch.

        CRITICAL: Returns a LOCAL filesystem path suitable for run_pipeline().

        Args:
            source: S3 object key (typically "masters/{prefix}/{original_name}").

        Returns:
            Local path to the downloaded file in scratch space.

        Raises:
            OSError: If disk space is insufficient or download fails.
            ClientError: If the S3 object is not found or access is denied.
        """
        fetch_job_id = f"s3-fetch-{source.replace('/', '-')}"

        try:
            async with self.session.client(
                "s3",
                endpoint_url=self.config.s3_endpoint_url or None,
                region_name=self.config.s3_region,
                config=s3_client_config(),
            ) as client:
                # Get the object metadata to determine actual size.
                logger.info("Getting S3 object metadata %s/%s", self.bucket, source)
                head = await client.head_object(Bucket=self.bucket, Key=source)
                object_size = head.get("ContentLength", 100 * 1024**3)

        except (ClientError, BotoCoreError) as e:
            logger.error("Failed to fetch S3 object metadata %s/%s: %s", self.bucket, source, e)
            raise

        # Reserve scratch space based on actual object size.
        scratch_dir = await self.scratch_manager.reserve(
            job_id=fetch_job_id,
            size_bytes=object_size,
        )

        # Derive a local filename from the S3 key.
        local_name = Path(source).name
        local_path = scratch_dir / local_name

        try:
            async with self.session.client(
                "s3",
                endpoint_url=self.config.s3_endpoint_url or None,
                region_name=self.config.s3_region,
                config=s3_client_config(),
            ) as client:
                logger.info("Downloading S3 object %s/%s to %s", self.bucket, source, local_path)

                await client.download_file(
                    Bucket=self.bucket,
                    Key=source,
                    Filename=str(local_path),
                )
                logger.info("Downloaded successfully: %s", local_path)

        except (ClientError, BotoCoreError) as e:
            logger.error("Failed to download S3 object %s/%s: %s", self.bucket, source, e)
            # Clean up scratch on failure.
            await self.scratch_manager.release(job_id=fetch_job_id)
            raise

        return local_path

    async def commit(
        self,
        local_output: Path,
        source: str,
        job: Any,
        space_saved: int = 0,
    ) -> CommitResult:
        """Upload a transcoded output as a derivative.

        Computes the content-addressed derivative key and uploads to S3.
        DOES NOT register in the database — the caller (http_agent) will
        call the scheduler's register-derivative endpoint to do that.

        Args:
            local_output: Path to the transcoded file (local filesystem).
            source: S3 object key of the master.
            job: Job object (Pydantic model or dict) with id, source_path,
                target_resolution, encoder, crf, etc.
            space_saved: Unused for S3 backend (always returns 0).

        Returns:
            CommitResult with output_size and space_saved=0.

        Raises:
            IOError: If upload fails or output file is missing.
        """
        from transcode_forge.models.derivative import compute_derivative_key

        stat_result = await asyncio.to_thread(local_output.stat)
        output_size = stat_result.st_size

        # Extract job fields, supporting both Pydantic models and dicts.
        def _get_field(obj: Any, field: str, default: Any = "") -> Any:
            """Get a field from a Pydantic model or dict."""
            if hasattr(obj, field):
                return getattr(obj, field) or default
            if isinstance(obj, dict):
                return obj.get(field, default)
            return default

        source_path = _get_field(job, "source_path", "")
        source_resolution = _get_field(job, "source_resolution", "") or ""
        source_audio_codec = _get_field(job, "source_audio_codec", "") or ""
        # Mirror http_agent._derivative_key_for exactly — the upload key must
        # equal the key the agent registers/dedup-checks with the scheduler.
        target_resolution = _get_field(job, "target_resolution", "") or source_resolution
        target_audio_codec = _get_field(job, "target_audio_codec", "") or "copy"
        target_codec = _get_field(job, "target_codec", "hevc") or "hevc"
        target_vmaf = _get_field(job, "target_vmaf", None)
        job_id = _get_field(job, "id", "")

        # Compute the goal-keyed derivative key.
        derivative_key = compute_derivative_key(
            source_path=source_path,
            source_resolution=source_resolution,
            source_audio_codec=source_audio_codec,
            target_resolution=target_resolution,
            target_audio_codec=target_audio_codec,
            target_codec=target_codec,
            target_vmaf=target_vmaf,
            local_output=local_output,
        )

        logger.info(
            "Uploading derivative for job %s: %s → s3://%s/%s",
            job_id,
            local_output,
            self.bucket,
            derivative_key,
        )

        try:
            async with self.session.client(
                "s3",
                endpoint_url=self.config.s3_endpoint_url or None,
                region_name=self.config.s3_region,
                config=s3_client_config(),
            ) as client:
                # Upload file to S3 with tuned multipart settings.
                from boto3.s3.transfer import TransferConfig

                config = TransferConfig(
                    multipart_threshold=50 * 1024 * 1024,  # 50 MB
                    multipart_chunksize=50 * 1024 * 1024,  # 50 MB per part
                    max_concurrency=4,
                )

                with open(local_output, "rb") as f:
                    await client.upload_fileobj(
                        f,
                        self.bucket,
                        derivative_key,
                        Config=config,
                    )

                logger.info(
                    "Derivative uploaded successfully: s3://%s/%s (%s bytes)",
                    self.bucket,
                    derivative_key,
                    output_size,
                )

        except (ClientError, BotoCoreError) as e:
            logger.error("Failed to upload derivative for job %s: %s", job_id, e)
            raise OSError(f"S3 upload failed: {e}") from e

        # Release scratch space.
        await self.scratch_manager.release(job_id=job_id)

        # For S3 backend, space_saved is 0 because the master is untouched.
        # The caller will register the derivative via the scheduler API.
        return CommitResult(output_size=output_size, space_saved=0)

    async def scan(self, library_id: str, library_name: str) -> dict[str, Any]:
        """Scan an S3 library for media files and catalog them.

        Lists objects in the bucket with the configured prefix, probes each
        for metadata (presigned-URL first, fallback to head-bytes on failure),
        and catalogs into media_files.

        Args:
            library_id: Database library ID.
            library_name: Human-readable library name.

        Returns:
            Scan statistics dict with keys: files_found, files_new,
            files_updated, files_skipped, files_failed.
        """
        from transcode_forge.scanner.s3_scanner import scan_s3_library

        return await scan_s3_library(
            library_id=library_id,
            library_name=library_name,
            bucket=self.bucket,
            prefix=self.prefix,
            config=self.config,
            db=self.db,
        )

    async def cleanup(self, job: Any) -> None:
        """Clean up temporary resources after a job.

        Releases scratch space and orphaned S3 parts.

        Args:
            job: Job model or dict with id (the agent passes the Pydantic
                Job model; assuming a dict here crashed the job loop).
        """
        job_id = job.id if hasattr(job, "id") else job.get("id", "")
        logger.info("Cleaning up S3 job %s", job_id)
        await self.scratch_manager.release(job_id=job_id)

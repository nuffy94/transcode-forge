"""HTTP-only worker — connects to the scheduler with a bearer token.

This replaces the older DB-direct worker for shareable installs. The
worker holds no credentials beyond the scheduler URL and its token;
everything else (job claiming, progress reporting, completion) flows
through the HTTP API in api/routes/worker_api.py.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import socket
from pathlib import Path
from typing import Any

import httpx

from transcode_forge.config import Settings
from transcode_forge.models.job import Job
from transcode_forge.models.library import StorageBackendType
from transcode_forge.worker.hardware import HardwareCapabilities, detect_capabilities
from transcode_forge.worker.http_client import WorkerHttpClient
from transcode_forge.worker.pipeline import (
    PipelineError,
    SizeRegressionError,
    VmafGateError,
    run_pipeline,
)
from transcode_forge.worker.vmaf import has_libvmaf

logger = logging.getLogger(__name__)


class HttpWorkerAgent:
    """Worker process that talks to the scheduler over HTTP only."""

    def __init__(self, settings: Settings, server_url: str, token: str) -> None:
        self.settings = settings
        self.server_url = server_url
        self.worker_name = settings.worker_name or f"worker-{socket.gethostname()}"
        self.host = socket.gethostname()
        self._shutting_down = False
        self.worker_id: str | None = None
        self._current_job_id: str | None = None
        self._current_progress: float = 0.0
        self.capabilities: HardwareCapabilities | None = None
        self._client: WorkerHttpClient = WorkerHttpClient(server_url, token)

        # Initialize scratch manager once for the worker's lifetime.
        from transcode_forge.worker.storage.scratch import ScratchManager

        scratch_root = Path(settings.scratch_dir or "/tmp/transcode-scratch")
        self.scratch_manager = ScratchManager(scratch_root)

    async def start(self) -> None:
        logging.basicConfig(
            level=getattr(logging, self.settings.log_level.upper(), logging.INFO),
            format=f"%(asctime)s [{self.worker_name}] %(levelname)s %(name)s: %(message)s",
        )
        logger.info("Starting HTTP worker %s → %s", self.worker_name, self.server_url)

        # Clean up orphaned scratch directories from previous runs.
        await self.scratch_manager.cleanup_orphans()

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._handle_shutdown)
            except NotImplementedError:
                # Windows: signal handlers via add_signal_handler not supported
                signal.signal(sig, lambda *_a: self._handle_shutdown())

        self.capabilities = await detect_capabilities()
        logger.info(
            "Capabilities: pairs=%s codecs=%s (preferred backend: %s)",
            self.capabilities.pairs,
            self.capabilities.supported_codecs,
            self.settings.preferred_backend,
        )
        if not await has_libvmaf():
            logger.warning(
                "ffmpeg on this worker has no libvmaf — the VMAF quality gate "
                "cannot run here. Update the worker image to restore it."
            )

        try:
            registration = await self._client.register(
                name=self.worker_name,
                host=self.host,
                capabilities=self.capabilities.encoders,
                supported_codecs=self.capabilities.supported_codecs,
                ffmpeg_version=self.capabilities.ffmpeg_version,
                max_concurrent=self.settings.worker_max_concurrent,
            )
        except httpx.HTTPStatusError as e:
            logger.error(
                "Registration rejected: %s — check TF_WORKER_TOKEN and TF_SERVER_URL",
                e.response.text,
            )
            raise
        self.worker_id = registration["worker_id"]
        logger.info("Registered as worker_id=%s", self.worker_id)

        try:
            await asyncio.gather(
                self._heartbeat_loop(),
                self._job_loop(),
            )
        finally:
            await self._cleanup()

    def _handle_shutdown(self) -> None:
        if self._shutting_down:
            logger.warning("Force shutdown")
            raise SystemExit(1)
        logger.info("Shutdown requested — finishing current job")
        self._shutting_down = True

    async def _heartbeat_loop(self) -> None:
        if self.worker_id is None:
            raise RuntimeError("worker_id is unset — registration must succeed before this runs")
        while not self._shutting_down:
            status = "busy" if self._current_job_id else "online"
            try:
                await self._client.heartbeat(
                    worker_id=self.worker_id,
                    status=status,
                    current_job_id=self._current_job_id,
                )
            except (httpx.HTTPError, OSError) as e:
                logger.warning("Heartbeat failed (will retry): %s", e)
            await asyncio.sleep(self.settings.heartbeat_interval)

    async def _job_loop(self) -> None:
        if self.worker_id is None:
            raise RuntimeError("worker_id is unset — registration must succeed before this runs")
        while not self._shutting_down:
            try:
                job_dict = await self._client.claim_job(worker_id=self.worker_id)
            except (httpx.HTTPError, OSError) as e:
                logger.warning("Claim failed: %s — backing off", e)
                await asyncio.sleep(5)
                continue
            if not job_dict:
                await asyncio.sleep(2)
                continue
            job = Job.model_validate(job_dict)
            # Claim-time extras (library backend, media type, VMAF floor)
            # ride on private attrs outside the validated model.
            for extra in ("_backend_type", "_s3_bucket", "_s3_prefix", "_media_type"):
                if extra in job_dict:
                    object.__setattr__(job, extra, job_dict[extra])
            await self._process_job(job, vmaf_floor=job_dict.get("_vmaf_min_floor"))

    def _resolve_backend(self, codec: str) -> str | None:
        """Best backend for the job's codec — preferred if capable, else
        qsv > nvenc > cpu, else None (worker can't encode this codec)."""
        if self.capabilities is None:
            return None
        return self.capabilities.best_backend_for(codec, self.settings.preferred_backend)

    async def _process_job(self, job: Job, vmaf_floor: float | None = None) -> None:
        if self.worker_id is None:
            raise RuntimeError("worker_id is unset — registration must succeed before this runs")
        self._current_job_id = job.id
        self._current_progress = 0.0
        codec = job.target_codec or "hevc"
        backend = self._resolve_backend(codec)
        if backend is None:
            # The claim filter should make this unreachable; if capability
            # drifted (driver died between register and claim), fail loudly
            # rather than encoding with the wrong codec.
            logger.error("No backend for codec %s — failing job %s", codec, job.id)
            await self._client.failed(
                job_id=job.id,
                error_message=f"Worker has no backend capable of encoding {codec}",
                retry_count=job.retry_count + 1,
            )
            self._current_job_id = None
            return
        logger.info(
            "Processing job %s: %s (%s→%s via %s)",
            job.id,
            job.source_path,
            job.source_codec,
            codec,
            backend,
        )

        # Construct the backend for this job's library.
        # The claim-job response includes backend_type, s3_bucket, s3_prefix.
        backend_type_str = getattr(job, "_backend_type", "filesystem")
        is_s3 = backend_type_str == StorageBackendType.S3 or backend_type_str == "s3"
        backend = await self._get_backend_for_job(job)

        # Fetch the source via the backend. For filesystem, this is a no-op
        # (returns the path-mapped path). For S3, this downloads to scratch.
        # TF_PATH_MAP only applies to filesystem paths — S3 keys are bucket
        # coordinates, not mount points.
        source_ref = job.source_path if is_s3 else self._translate_path(job.source_path)
        try:
            source_path_local = await backend.fetch(source_ref)
        except (OSError, Exception) as e:
            logger.error("Failed to fetch source for job %s: %s", job.id, e)
            await self._client.failed(
                job_id=job.id,
                error_message=f"Failed to fetch source: {e}",
                retry_count=job.retry_count + 1,
            )
            return

        # Check for dedup/reuse opportunity (S3-only for now).
        # For S3 libraries, compute the derivative key and look it up via the scheduler.
        # If a derivative exists, the scheduler marks the job COMPLETE and we skip encoding.
        if is_s3:
            dedup_result = await self._try_dedup(job, backend)
            if dedup_result:
                # Job was marked COMPLETE via dedup. Report completion with reused output size.
                await self._client.complete(
                    job_id=job.id,
                    output_size=int(dedup_result["output_size"]),
                    space_saved=0,  # S3 doesn't reclaim space; derivative is new.
                    source_size=int(job.source_size or 0),
                )
                logger.info(
                    "Job %s complete via dedup (reused %s)",
                    job.id,
                    dedup_result.get("derivative_key", "unknown"),
                )
                await backend.cleanup(job)
                return

        async def on_progress(progress: float, speed: float | None) -> None:
            self._current_progress = progress
            try:
                await self._client.progress(job_id=job.id, progress=progress, speed=speed)
            except (httpx.HTTPError, OSError):
                logger.debug("Progress update failed", exc_info=True)

        media_type = getattr(job, "_media_type", "") or ""
        try:
            result = await run_pipeline(
                source_path=str(source_path_local),
                codec=codec,
                backend=backend,
                quality=job.quality_value,
                source_duration=job.source_duration or 0,
                job_id=job.id,
                worker_id=self.worker_id,
                target_vmaf=job.target_vmaf,
                vmaf_perc5_floor=vmaf_floor,
                crf_search=self.settings.crf_search_enabled,
                content="anime" if media_type == "anime" else None,
                progress_callback=on_progress,
            )

            # Commit the output via the backend.
            # For filesystem: swap already happened in run_pipeline; commit() validates sizes.
            # For S3: upload the transcoded file to S3 (but don't register yet).
            # For filesystem, space_saved comes from the pipeline result (bak file size).
            space_saved = 0 if is_s3 else int(result.get("space_saved", 0))
            commit_result = await backend.commit(
                local_output=source_path_local,
                source=job.source_path,
                job=job,
                space_saved=space_saved,
            )

            # For S3, register the derivative on the scheduler side.
            if is_s3:
                derivative_key = self._derivative_key_for(job, source_path_local)

                try:
                    await self._client.register_derivative(
                        job_id=job.id,
                        derivative_key=derivative_key,
                        output_size=int(commit_result.output_size),
                        achieved_vmaf=result.get("vmaf_mean"),
                        resolved_crf=result.get("resolved_crf"),
                        backend_used=result.get("backend", backend),
                    )
                    logger.info("Derivative registered on scheduler: %s", derivative_key)
                except (httpx.HTTPError, OSError) as e:
                    logger.error(
                        "Failed to register derivative (S3 file may be orphaned): %s",
                        e,
                    )
                    raise

            await self._client.complete(
                job_id=job.id,
                output_size=int(commit_result.output_size),
                space_saved=int(commit_result.space_saved),
                source_size=int(result["source_size"]),
                achieved_vmaf=result.get("vmaf_mean"),
                resolved_crf=result.get("resolved_crf"),
                backend_used=result.get("backend", backend),
            )
            logger.info(
                "Job %s complete — saved %d bytes",
                job.id,
                int(commit_result.space_saved),
            )
        except VmafGateError as e:
            await self._client.skipped(
                job_id=job.id,
                reason="below_vmaf_floor",
                error_message=str(e),
                achieved_vmaf=e.vmaf_mean,
            )
            logger.info("Job %s skipped (below VMAF floor): %s", job.id, e)
        except SizeRegressionError as e:
            await self._client.skipped(
                job_id=job.id,
                reason="size_regression",
                error_message=str(e),
            )
            logger.info("Job %s skipped (size regression)", job.id)
        except PipelineError as e:
            new_retry = job.retry_count + 1
            await self._client.failed(
                job_id=job.id,
                error_message=str(e),
                retry_count=new_retry,
            )
            logger.warning("Job %s failed (attempt %d): %s", job.id, new_retry, e)
        finally:
            self._current_job_id = None
            self._current_progress = 0.0
            await backend.cleanup(job)

    def _translate_path(self, path: str) -> str:
        for linux_prefix, local_prefix in self.settings.path_map.items():
            if path.startswith(linux_prefix):
                return path.replace(linux_prefix, local_prefix, 1)
        return path

    def _derivative_key_for(self, job: Job, local_output: Path | str) -> str:
        """Goal-keyed derivative key for a job (D6): source identity +
        target codec/resolution/audio + target VMAF. Recipe details
        (backend/crf/preset) deliberately don't participate — any worker's
        gate-passing encode satisfies the same goal."""
        from transcode_forge.models.derivative import compute_derivative_key

        return compute_derivative_key(
            source_path=job.source_path,
            source_resolution=job.source_resolution or "",
            source_audio_codec=getattr(job, "source_audio_codec", "") or "",
            # No rescaling / audio transcoding in the pipeline: the target
            # keeps the source resolution and copies audio streams.
            target_resolution=getattr(job, "target_resolution", "")
            or (job.source_resolution or ""),
            target_audio_codec=getattr(job, "target_audio_codec", "") or "copy",
            target_codec=job.target_codec or "hevc",
            target_vmaf=job.target_vmaf,
            local_output=Path(local_output),
        )

    async def _try_dedup(self, job: Job, backend: Any) -> dict[str, Any] | None:
        """Check for a reusable derivative (S3 only).

        Computes the goal-keyed derivative key and calls the scheduler API
        to check if it already exists. If found, the scheduler marks the
        job COMPLETE and returns the derivative info.

        Args:
            job: The job object.
            backend: The S3Backend (unused; kept for future extensibility).

        Returns:
            A dict with output_size and derivative_key if found, None otherwise.
        """
        derivative_key = self._derivative_key_for(job, Path(job.source_path))

        logger.debug("Checking for derivative: %s", derivative_key)

        try:
            result = await self._client.check_derivative(
                job_id=job.id, derivative_key=derivative_key
            )
            if result.get("found"):
                return result
        except (httpx.HTTPError, OSError) as e:
            logger.warning("Dedup check failed (will proceed with encode): %s", e)

        return None

    async def _get_backend_for_job(self, job: Job) -> Any:
        """Construct the storage backend for a job's library.

        The claim-job response includes backend_type, s3_bucket, s3_prefix
        in the job metadata. Construct the appropriate backend (Filesystem or S3).

        Args:
            job: The job object (from claim-job response).

        Returns:
            A StorageBackend implementation (FilesystemBackend or S3Backend).

        Raises:
            ValueError: If backend type is unsupported or required config is missing.
        """
        # Extract backend info from the job. The claim-job response includes these fields.
        backend_type_str = getattr(job, "_backend_type", "filesystem")
        s3_bucket = getattr(job, "_s3_bucket", "")
        s3_prefix = getattr(job, "_s3_prefix", "")

        if backend_type_str == StorageBackendType.S3 or backend_type_str == "s3":
            # S3 backend: reuse the per-worker scratch manager singleton.
            from transcode_forge.worker.storage.s3 import S3Backend

            if not s3_bucket:
                raise ValueError("S3 backend selected but s3_bucket not provided")

            # HTTP worker doesn't have DB access; the scheduler registers derivatives
            # via the register-derivative endpoint.
            backend = S3Backend(
                config=self.settings,
                db=None,  # type: ignore
                scratch_manager=self.scratch_manager,
                library_id=job.library,
                bucket=s3_bucket,
                prefix=s3_prefix,
            )
            return backend
        else:
            # Filesystem backend
            from transcode_forge.worker.storage.filesystem import FilesystemBackend

            return FilesystemBackend()

    async def _cleanup(self) -> None:
        logger.info("Worker shutting down")
        if self.worker_id is not None:
            try:
                await self._client.heartbeat(worker_id=self.worker_id, status="offline")
            except (httpx.HTTPError, OSError):
                pass
        await self._client.aclose()
        # Clean up scratch directories on shutdown.
        await self.scratch_manager.cleanup_on_shutdown()
        logger.info("Worker stopped")

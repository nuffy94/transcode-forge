"""Tests for HTTP worker agent and storage backends integration."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from transcode_forge.models.job import Job
from transcode_forge.worker.hardware import HardwareCapabilities
from transcode_forge.worker.http_agent import HttpWorkerAgent


def _cpu_caps() -> HardwareCapabilities:
    return HardwareCapabilities(
        encoders=["cpu"],
        pairs=[("av1", "cpu"), ("hevc", "cpu")],
        ffmpeg_version="ffmpeg 7.0",
        os_platform="Linux",
    )
from transcode_forge.worker.storage.filesystem import FilesystemBackend
from transcode_forge.worker.storage.s3 import S3Backend


class TestGetBackendForJob:
    """Tests for http_agent._get_backend_for_job() routing logic."""

    @pytest.mark.asyncio
    async def test_filesystem_backend_routing(self, test_settings):
        """Routing to FilesystemBackend when backend_type is filesystem."""
        agent = HttpWorkerAgent(test_settings, "http://scheduler", "test-token")
        job = Job(
            source_path="/media/test.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
        )
        # Set filesystem backend flag
        object.__setattr__(job, "_backend_type", "filesystem")

        backend = await agent._get_backend_for_job(job)
        assert isinstance(backend, FilesystemBackend)

    @pytest.mark.asyncio
    async def test_s3_backend_routing(self, test_settings):
        """Routing to S3Backend when backend_type is s3."""
        agent = HttpWorkerAgent(test_settings, "http://scheduler", "test-token")
        job = Job(
            source_path="s3://bucket/path/test.mkv",
            library="s3-movies",
            source_codec="h264",
            quality_value=21,
        )
        object.__setattr__(job, "_backend_type", "s3")
        object.__setattr__(job, "_s3_bucket", "test-bucket")
        object.__setattr__(job, "_s3_prefix", "movies/")

        backend = await agent._get_backend_for_job(job)
        assert isinstance(backend, S3Backend)

    @pytest.mark.asyncio
    async def test_s3_backend_missing_bucket_raises(self, test_settings):
        """S3Backend routing raises ValueError if bucket is missing."""
        agent = HttpWorkerAgent(test_settings, "http://scheduler", "test-token")
        job = Job(
            source_path="test.mkv",
            library="s3-movies",
            source_codec="h264",
            quality_value=21,
        )
        object.__setattr__(job, "_backend_type", "s3")
        object.__setattr__(job, "_s3_bucket", "")  # Missing bucket

        with pytest.raises(ValueError, match="S3 backend selected but s3_bucket not provided"):
            await agent._get_backend_for_job(job)


class TestTryDedup:
    """Tests for http_agent._try_dedup() derivative key computation."""

    @pytest.mark.asyncio
    async def test_dedup_key_computed_correctly(self, test_settings):
        """_try_dedup computes the derivative key from job parameters."""
        agent = HttpWorkerAgent(test_settings, "http://scheduler", "test-token")
        job = Job(
            source_path="/media/test.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
        )
        job.source_resolution = "1920x1080"
        # Use object.__setattr__ to bypass Pydantic validation for test attributes
        object.__setattr__(job, "encoder", "libx265")
        object.__setattr__(job, "preset", "medium")
        object.__setattr__(job, "target_resolution", "1280x720")
        object.__setattr__(job, "target_audio_codec", "aac")

        # Mock the client to return not found (we're testing key computation, not the API call)
        agent._client = AsyncMock()
        agent._client.check_derivative = AsyncMock(return_value={"found": False})

        result = await agent._try_dedup(job, None)
        assert result is None
        # Verify the key was computed and check_derivative was called
        agent._client.check_derivative.assert_called_once()
        call_args = agent._client.check_derivative.call_args
        assert "derivative_key" in call_args.kwargs

    @pytest.mark.asyncio
    async def test_dedup_found_returns_result(self, test_settings):
        """_try_dedup returns the result when a derivative is found."""
        agent = HttpWorkerAgent(test_settings, "http://scheduler", "test-token")
        job = Job(
            source_path="/media/test.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
        )
        job.source_resolution = "1920x1080"
        object.__setattr__(job, "encoder", "libx265")
        object.__setattr__(job, "preset", "medium")
        object.__setattr__(job, "target_resolution", "1280x720")
        object.__setattr__(job, "target_audio_codec", "aac")

        agent._client = AsyncMock()
        agent._client.check_derivative = AsyncMock(
            return_value={
                "found": True,
                "output_size": 5000000,
                "derivative_key": "abc123_libx265-crf21.mkv",
            }
        )

        result = await agent._try_dedup(job, None)
        assert result is not None
        assert result["found"] is True
        assert result["output_size"] == 5000000


class TestProcessJobS3HappyPath:
    """Tests for http_agent._process_job() S3 flow."""

    @pytest.mark.asyncio
    async def test_process_job_s3_upload_and_register(self, test_settings, tmp_path):
        """_process_job uploads to S3 and calls register-derivative endpoint."""
        # Create a dummy output file
        output_file = tmp_path / "output.mkv"
        output_file.write_bytes(b"fake video data" * 1000)

        agent = HttpWorkerAgent(test_settings, "http://scheduler", "test-token")
        agent.worker_id = "worker-1"
        agent.capabilities = _cpu_caps()

        job = Job(
            source_path="s3://bucket/masters/test.mkv",
            library="s3-movies",
            source_codec="h264",
            quality_value=21,
        )
        job._backend_type = "s3"
        job._s3_bucket = "test-bucket"
        job._s3_prefix = ""
        job.source_resolution = "1920x1080"
        object.__setattr__(job, "target_resolution", "1280x720")
        object.__setattr__(job, "target_audio_codec", "aac")
        object.__setattr__(job, "encoder", "libx265")
        object.__setattr__(job, "preset", "medium")

        # Mock the storage backend
        mock_backend = AsyncMock()
        mock_backend.fetch = AsyncMock(return_value=output_file)
        mock_backend.commit = AsyncMock(return_value=MagicMock(output_size=5000000, space_saved=0))
        mock_backend.cleanup = AsyncMock()

        # Mock the HTTP client
        agent._client = AsyncMock()
        agent._client.check_derivative = AsyncMock(return_value={"found": False})
        agent._client.register_derivative = AsyncMock()
        agent._client.complete = AsyncMock()
        agent._client.progress = AsyncMock()

        # Mock run_pipeline to avoid real encoding
        with patch("transcode_forge.worker.http_agent.run_pipeline") as mock_pipeline:
            mock_pipeline.return_value = {
                "source_size": 10000000,
                "space_saved": 5000000,
            }
            # Mock _get_backend_for_job to return our mock
            with patch.object(agent, "_get_backend_for_job", return_value=mock_backend):
                await agent._process_job(job)

        # Verify register_derivative was called for S3
        agent._client.register_derivative.assert_called_once()
        call_kwargs = agent._client.register_derivative.call_args.kwargs
        assert call_kwargs["job_id"] == job.id
        assert "derivative_key" in call_kwargs
        assert call_kwargs["output_size"] == 5000000

        # Verify complete was called with results
        agent._client.complete.assert_called_once()


class TestProcessJobFilesystemHappyPath:
    """Tests for http_agent._process_job() filesystem flow."""

    @pytest.mark.asyncio
    async def test_process_job_filesystem_reports_space_saved(self, test_settings, tmp_path):
        """_process_job reports space_saved from pipeline result for filesystem backend."""
        output_file = tmp_path / "output.mkv"
        output_file.write_bytes(b"fake video data" * 1000)

        agent = HttpWorkerAgent(test_settings, "http://scheduler", "test-token")
        agent.worker_id = "worker-1"
        agent.capabilities = _cpu_caps()

        job = Job(
            source_path="/media/movies/test.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
        )
        object.__setattr__(job, "_backend_type", "filesystem")

        # Mock the storage backend
        mock_backend = AsyncMock()
        mock_backend.fetch = AsyncMock(return_value=output_file)
        mock_backend.commit = AsyncMock(
            return_value=MagicMock(output_size=5000000, space_saved=5000000)
        )
        mock_backend.cleanup = AsyncMock()

        # Mock the HTTP client
        agent._client = AsyncMock()
        agent._client.complete = AsyncMock()
        agent._client.progress = AsyncMock()

        # Mock run_pipeline
        with patch("transcode_forge.worker.http_agent.run_pipeline") as mock_pipeline:
            mock_pipeline.return_value = {
                "source_size": 10000000,
                "space_saved": 5000000,
            }

            # Mock _get_backend_for_job to return our mock
            with patch.object(agent, "_get_backend_for_job", return_value=mock_backend):
                await agent._process_job(job)

        # Verify complete was called with space_saved from pipeline result
        agent._client.complete.assert_called_once()
        call_kwargs = agent._client.complete.call_args.kwargs
        assert call_kwargs["space_saved"] == 5000000


class TestPathMapTranslation:
    """TF_PATH_MAP must be applied to filesystem sources before fetch — and
    never to S3 keys (they're bucket coordinates, not mount points)."""

    @pytest.mark.asyncio
    async def test_filesystem_fetch_receives_mapped_path(self, test_settings, tmp_path):
        settings = test_settings.model_copy(update={"path_map": {"/data/media": "/mnt/media"}})
        agent = HttpWorkerAgent(settings, "http://scheduler", "test-token")
        agent.worker_id = "worker-1"
        agent.capabilities = _cpu_caps()

        job = Job(
            source_path="/data/media/movies/test.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
        )
        object.__setattr__(job, "_backend_type", "filesystem")

        output_file = tmp_path / "out.mkv"
        output_file.write_bytes(b"fake video data")
        mock_backend = AsyncMock()
        mock_backend.fetch = AsyncMock(return_value=output_file)
        mock_backend.commit = AsyncMock(return_value=MagicMock(output_size=5, space_saved=5))
        mock_backend.cleanup = AsyncMock()
        agent._client = AsyncMock()

        with patch("transcode_forge.worker.http_agent.run_pipeline") as mock_pipeline:
            mock_pipeline.return_value = {"source_size": 10, "space_saved": 5}
            with patch.object(agent, "_get_backend_for_job", return_value=mock_backend):
                await agent._process_job(job)

        mock_backend.fetch.assert_called_once_with("/mnt/media/movies/test.mkv")

    @pytest.mark.asyncio
    async def test_s3_fetch_key_is_never_mapped(self, test_settings, tmp_path):
        # A path_map whose prefix would match the S3 key if (wrongly) applied.
        settings = test_settings.model_copy(update={"path_map": {"masters": "/mnt/masters"}})
        agent = HttpWorkerAgent(settings, "http://scheduler", "test-token")
        agent.worker_id = "worker-1"
        agent.capabilities = _cpu_caps()

        job = Job(
            source_path="masters/movies/test.mkv",
            library="s3-movies",
            source_codec="h264",
            quality_value=21,
        )
        object.__setattr__(job, "_backend_type", "s3")
        object.__setattr__(job, "_s3_bucket", "bkt")

        output_file = tmp_path / "out.mkv"
        output_file.write_bytes(b"fake video data")
        mock_backend = AsyncMock()
        mock_backend.fetch = AsyncMock(return_value=output_file)
        mock_backend.commit = AsyncMock(return_value=MagicMock(output_size=5, space_saved=0))
        mock_backend.cleanup = AsyncMock()
        agent._client = AsyncMock()
        agent._client.check_derivative = AsyncMock(return_value={"found": False})

        with patch("transcode_forge.worker.http_agent.run_pipeline") as mock_pipeline:
            mock_pipeline.return_value = {"source_size": 10, "space_saved": 0}
            with patch.object(agent, "_get_backend_for_job", return_value=mock_backend):
                await agent._process_job(job)

        mock_backend.fetch.assert_called_once_with("masters/movies/test.mkv")


class TestScratchManagerLifecycle:
    """Tests for ScratchManager singleton lifecycle."""

    @pytest.mark.asyncio
    async def test_scratch_manager_created_once(self, test_settings):
        """ScratchManager is created once per agent and reused."""
        agent = HttpWorkerAgent(test_settings, "http://scheduler", "test-token")
        first_manager = agent.scratch_manager

        # Create another agent — should have a different manager instance
        agent2 = HttpWorkerAgent(test_settings, "http://scheduler", "test-token-2")
        second_manager = agent2.scratch_manager

        # Both should exist (not None)
        assert first_manager is not None
        assert second_manager is not None
        # But they are different instances
        assert first_manager is not second_manager

    @pytest.mark.asyncio
    async def test_scratch_cleanup_on_shutdown(self, test_settings):
        """Scratch manager cleanup_on_shutdown is called during agent cleanup."""
        agent = HttpWorkerAgent(test_settings, "http://scheduler", "test-token")
        agent.worker_id = "worker-1"
        agent.capabilities = _cpu_caps()

        # Mock the scratch manager
        agent.scratch_manager = AsyncMock()
        agent.scratch_manager.cleanup_on_shutdown = AsyncMock()

        # Mock the HTTP client
        agent._client = AsyncMock()
        agent._client.aclose = AsyncMock()

        await agent._cleanup()

        # Verify cleanup_on_shutdown was called
        agent.scratch_manager.cleanup_on_shutdown.assert_called_once()


class TestDerivativeKeyConsistency:
    """Tests to ensure derivative key computation is consistent (goal-keyed)."""

    def test_compute_derivative_key_consistent(self):
        """compute_derivative_key produces the same hash for identical parameters."""
        from transcode_forge.models.derivative import compute_derivative_key

        local_output = Path("/output/test.mkv")
        params = {
            "source_path": "/media/test.mkv",
            "source_resolution": "1920x1080",
            "source_audio_codec": "aac",
            "target_resolution": "1280x720",
            "target_audio_codec": "aac",
            "target_codec": "hevc",
            "target_vmaf": 97,
            "local_output": local_output,
        }

        key1 = compute_derivative_key(**params)
        key2 = compute_derivative_key(**params)
        assert key1 == key2

    def test_compute_derivative_key_ignores_recipe(self):
        """The key is the GOAL — backend/crf/preset must not change it
        (same goal via a different recipe is the same derivative)."""
        from transcode_forge.models.derivative import compute_derivative_key

        local_output = Path("/output/test.mkv")
        params1 = {
            "source_path": "/media/test.mkv",
            "source_resolution": "1920x1080",
            "source_audio_codec": "aac",
            "target_resolution": "1280x720",
            "target_audio_codec": "aac",
            "target_codec": "hevc",
            "target_vmaf": 97,
            "backend": "cpu",
            "crf": 21,
            "preset": "slow",
            "local_output": local_output,
        }
        params2 = {**params1, "backend": "nvenc", "crf": 32, "preset": "p7"}

        assert compute_derivative_key(**params1) == compute_derivative_key(**params2)

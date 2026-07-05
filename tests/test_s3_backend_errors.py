"""S3 error-path hardening tests (review item 14).

The worker agent flow (worker/http_agent.py + worker/storage/s3.py) must
convert S3 failures into a FAILED job — an exception escaping
_process_job kills the agent's job loop; the worker restarts,
re-registers (releasing the job), and the next worker hits the same
error: a fleet-wide crash loop.

Mocking style mirrors tests/test_s3_backend.py: a scripted mock S3
client behind a mock aioboto3 Session. The real S3Backend,
ScratchManager, and HttpWorkerAgent code runs — only the network (S3
and the scheduler HTTP client) is mocked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from botocore.exceptions import ClientError

from transcode_forge.config import Settings
from transcode_forge.models.job import Job
from transcode_forge.worker.http_agent import HttpWorkerAgent
from transcode_forge.worker.storage.s3 import S3Backend
from transcode_forge.worker.storage.scratch import ScratchManager

TEST_DATA = b"fake video bytes" * 64

PIPELINE_RESULT = {
    "source_size": len(TEST_DATA),
    "space_saved": 0,
    "vmaf_mean": 95.0,
    "resolved_crf": 22,
    "backend": "cpu",
}


def _client_error(operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "RequestTimeout", "Message": "simulated S3 timeout"}},
        operation,
    )


class _ScriptedClient:
    """Mock S3 client with injectable failures per operation."""

    def __init__(
        self,
        *,
        head_error: Exception | None = None,
        download_error: Exception | None = None,
        upload_error: Exception | None = None,
    ) -> None:
        self.head_error = head_error
        self.download_error = download_error
        self.upload_error = upload_error
        self.uploaded: dict[str, bytes] = {}

    async def head_object(self, **kwargs: Any) -> dict[str, Any]:
        if self.head_error is not None:
            raise self.head_error
        return {"ContentLength": len(TEST_DATA)}

    async def download_file(self, **kwargs: Any) -> None:
        if self.download_error is not None:
            raise self.download_error
        Path(kwargs["Filename"]).write_bytes(TEST_DATA)

    async def upload_fileobj(self, fileobj: Any, bucket: str, key: str, **kwargs: Any) -> None:
        if self.upload_error is not None:
            raise self.upload_error
        self.uploaded[key] = fileobj.read()


class _MockSession:
    """Stands in for aioboto3.Session — client() returns an async context
    manager yielding the scripted client."""

    def __init__(self, client: _ScriptedClient) -> None:
        self._client = client

    def client(self, *args: Any, **kwargs: Any) -> Any:
        scripted = self._client

        class _Ctx:
            async def __aenter__(self) -> _ScriptedClient:
                return scripted

            async def __aexit__(self, *exc: Any) -> None:
                return None

        return _Ctx()


def _make_s3_job() -> Job:
    """A claimed S3-library job, with the claim-time extras the agent's
    job loop attaches as private attrs (mirrors _job_loop exactly)."""
    job = Job(
        source_path="masters/movie.mkv",
        library="s3-movies",
        source_codec="h264",
        source_resolution="1920x1080",
        source_size=1_000_000,
        source_duration=120.0,
        quality_value=21,
    )
    extras = {
        "_backend_type": "s3",
        "_s3_bucket": "test-bucket",
        "_s3_prefix": "",
        "_media_type": "movies",
    }
    for key, value in extras.items():
        object.__setattr__(job, key, value)
    return job


@pytest.fixture
def s3_settings(tmp_path: Path) -> Settings:
    return Settings(
        s3_endpoint_url="",
        s3_region="us-east-1",
        s3_access_key_id="testing",
        s3_secret_access_key="testing",
        scratch_dir=str(tmp_path / "scratch"),
    )


@pytest.fixture
def agent(s3_settings: Settings) -> HttpWorkerAgent:
    """A registered agent with a mocked scheduler client — no network."""
    a = HttpWorkerAgent(s3_settings, "http://scheduler.test", "test-token")
    a.worker_id = "worker-1"
    a.capabilities = MagicMock()
    a.capabilities.best_backend_for.return_value = "cpu"
    a._client = AsyncMock()
    a._client.check_derivative.return_value = {"found": False}
    return a


class TestWorkerAgentS3Failures:
    """S3 failures inside _process_job end the JOB as failed — the agent
    itself must survive (no exception may escape _process_job)."""

    async def test_fetch_failure_fails_job_not_worker(self, agent: HttpWorkerAgent):
        scripted = _ScriptedClient(head_error=_client_error("HeadObject"))
        job = _make_s3_job()

        with patch("aioboto3.Session", return_value=_MockSession(scripted)):
            # Must not raise — an escaping exception kills the job loop.
            await agent._process_job(job, vmaf_floor=None)

        agent._client.failed.assert_awaited_once()
        kwargs = agent._client.failed.await_args.kwargs
        assert kwargs["job_id"] == job.id
        assert kwargs["retry_count"] == job.retry_count + 1
        assert "Failed to fetch source" in kwargs["error_message"]
        agent._client.complete.assert_not_awaited()
        # The agent must return to idle — not heartbeat 'busy' with a dead job.
        assert agent._current_job_id is None

    async def test_upload_failure_fails_job_not_worker(self, agent: HttpWorkerAgent):
        scripted = _ScriptedClient(upload_error=_client_error("PutObject"))
        job = _make_s3_job()

        with (
            patch("aioboto3.Session", return_value=_MockSession(scripted)),
            patch(
                "transcode_forge.worker.http_agent.run_pipeline",
                new=AsyncMock(return_value=dict(PIPELINE_RESULT)),
            ),
        ):
            await agent._process_job(job, vmaf_floor=None)

        agent._client.failed.assert_awaited_once()
        kwargs = agent._client.failed.await_args.kwargs
        assert kwargs["job_id"] == job.id
        assert kwargs["retry_count"] == job.retry_count + 1
        assert "S3 upload failed" in kwargs["error_message"]
        agent._client.complete.assert_not_awaited()
        agent._client.register_derivative.assert_not_awaited()
        assert agent._current_job_id is None

    async def test_register_derivative_failure_fails_job_not_worker(self, agent: HttpWorkerAgent):
        scripted = _ScriptedClient()
        job = _make_s3_job()
        agent._client.register_derivative.side_effect = httpx.HTTPError("500 from scheduler")

        with (
            patch("aioboto3.Session", return_value=_MockSession(scripted)),
            patch(
                "transcode_forge.worker.http_agent.run_pipeline",
                new=AsyncMock(return_value=dict(PIPELINE_RESULT)),
            ),
        ):
            await agent._process_job(job, vmaf_floor=None)

        # The upload succeeded but registration failed — the job ends FAILED
        # (the S3 object may be orphaned; the job stays retryable), and
        # /complete is never reported for an unregistered derivative.
        agent._client.register_derivative.assert_awaited_once()
        agent._client.failed.assert_awaited_once()
        assert agent._client.failed.await_args.kwargs["job_id"] == job.id
        agent._client.complete.assert_not_awaited()
        assert agent._current_job_id is None

    async def test_dedup_hit_completes_without_encoding(self, agent: HttpWorkerAgent):
        scripted = _ScriptedClient()
        job = _make_s3_job()
        agent._client.check_derivative.return_value = {
            "found": True,
            "output_size": 4242,
            "derivative_key": "reused-key",
        }
        pipeline = AsyncMock(return_value=dict(PIPELINE_RESULT))

        with (
            patch("aioboto3.Session", return_value=_MockSession(scripted)),
            patch("transcode_forge.worker.http_agent.run_pipeline", new=pipeline),
        ):
            await agent._process_job(job, vmaf_floor=None)

        pipeline.assert_not_awaited()
        agent._client.complete.assert_awaited_once()
        kwargs = agent._client.complete.await_args.kwargs
        assert kwargs["output_size"] == 4242
        assert kwargs["space_saved"] == 0
        agent._client.failed.assert_not_awaited()
        assert agent._current_job_id is None


class TestS3BackendErrorContracts:
    """Direct S3Backend contracts the agent flow relies on."""

    async def test_fetch_download_failure_releases_scratch(
        self, s3_settings: Settings, tmp_path: Path
    ):
        scratch = ScratchManager(tmp_path / "scratch-direct")
        backend = S3Backend(
            config=s3_settings,
            db=None,  # HTTP workers have no DB access
            scratch_manager=scratch,
            library_id="lib",
            bucket="test-bucket",
        )
        backend.session = _MockSession(_ScriptedClient(download_error=_client_error("GetObject")))

        with pytest.raises(ClientError):
            await backend.fetch("masters/movie.mkv")

        # The per-fetch scratch reservation must not leak on failure.
        assert list(scratch.scratch_root.iterdir()) == []

    async def test_cleanup_accepts_job_model_and_dict(self, s3_settings: Settings, tmp_path: Path):
        """_process_job passes a Pydantic Job model to cleanup() — it must
        not assume a dict (job.get raised AttributeError, escaping the
        job loop from the finally block)."""
        scratch = ScratchManager(tmp_path / "scratch-cleanup")
        backend = S3Backend(
            config=s3_settings,
            db=None,  # HTTP workers have no DB access
            scratch_manager=scratch,
            library_id="lib",
            bucket="test-bucket",
        )
        await backend.cleanup(_make_s3_job())
        await backend.cleanup({"id": "dict-job"})

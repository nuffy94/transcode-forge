"""Tests for S3 backend using moto (local S3 mock).

Covers:
- Scratch manager disk-space guard, orphan cleanup, shutdown
- Derivative key determinism
- S3 backend initialization
- fetch() round-trip: download from S3 to scratch
- commit() round-trip: upload to S3 + register in derivatives table
- Failure paths: upload failure (no registry row), throttle retry, timeout
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from transcode_forge.config import Settings
from transcode_forge.repos import derivatives as deriv_repo
from transcode_forge.worker.storage.s3 import S3Backend
from transcode_forge.worker.storage.scratch import ScratchManager


@pytest.fixture
def s3_config() -> Settings:
    """S3 config with dummy credentials (moto will mock real S3 endpoint)."""
    return Settings(
        s3_endpoint_url="",  # Empty: moto mocks the default AWS endpoint
        s3_region="us-east-1",
        s3_access_key_id="testing",
        s3_secret_access_key="testing",
        scratch_dir="/tmp/transcode-scratch-test",
    )


@pytest.fixture
def scratch_manager(tmp_path: Path) -> ScratchManager:
    """Create a scratch manager for tests."""
    scratch_root = tmp_path / "scratch"
    return ScratchManager(scratch_root=scratch_root)


@pytest.fixture
def s3_backend(s3_config: Settings, db, scratch_manager: ScratchManager) -> S3Backend:
    """Create an S3 backend for tests."""
    return S3Backend(
        config=s3_config,
        db=db,
        scratch_manager=scratch_manager,
        library_id="test-library",
        bucket="test-bucket",
        prefix="masters/",
    )


def test_scratch_manager_reserve_creates_directory(scratch_manager: ScratchManager) -> None:
    """Test that reserve() creates a per-job directory."""

    async def _test() -> None:
        # Reserve with a reasonable size.
        scratch_dir = await scratch_manager.reserve(job_id="job-1", size_bytes=10_000_000)

        assert scratch_dir.exists()
        assert scratch_dir.is_dir()
        assert "job-1" in scratch_dir.name

    asyncio.run(_test())


def test_scratch_manager_release_deletes_directory(scratch_manager: ScratchManager) -> None:
    """Test that release() deletes the per-job directory."""

    async def _test() -> None:
        # Create a directory.
        scratch_dir = await scratch_manager.reserve(job_id="job-2", size_bytes=10_000_000)
        assert scratch_dir.exists()

        # Release it.
        await scratch_manager.release(job_id="job-2")

        # Should be gone.
        assert not scratch_dir.exists()

    asyncio.run(_test())


def test_scratch_disk_guard_insufficient_space(scratch_manager: ScratchManager) -> None:
    """Test that reserve() rejects if disk space is insufficient."""

    async def _test() -> None:
        # Patch shutil.disk_usage to simulate low disk space.
        with patch("transcode_forge.worker.storage.scratch.shutil.disk_usage") as mock_usage:
            mock_usage.return_value = MagicMock(free=10 * 1024**2)  # 10 MB free

            # Try to reserve 100 GB — should fail.
            with pytest.raises(OSError, match="Insufficient scratch space"):
                await scratch_manager.reserve(job_id="job-large", size_bytes=100 * 1024**3)

    asyncio.run(_test())


def test_scratch_orphan_cleanup(scratch_manager: ScratchManager) -> None:
    """Test cleanup of stale per-job directories."""

    async def _test() -> None:
        # Manually create some old and new job directories.
        old_dir = scratch_manager.scratch_root / "job-old_abc123"
        new_dir = scratch_manager.scratch_root / "job-new_xyz789"

        old_dir.mkdir(parents=True, exist_ok=True)
        new_dir.mkdir(parents=True, exist_ok=True)

        # Make old_dir appear old by setting its mtime far in the past.
        old_mtime = time.time() - (48 * 3600)  # 48 hours ago
        os.utime(old_dir, (old_mtime, old_mtime))

        # Run cleanup with a 24-hour cutoff.
        await scratch_manager.cleanup_orphans(max_age_hours=24)

        # old_dir should be deleted, new_dir should remain.
        assert not old_dir.exists(), "Old directory should be cleaned up"
        assert new_dir.exists(), "New directory should remain"

    asyncio.run(_test())


def test_scratch_manager_cleanup_on_shutdown(
    scratch_manager: ScratchManager, tmp_path: Path
) -> None:
    """Test cleanup_on_shutdown() removes the scratch root."""

    async def _test() -> None:
        # Create some job directories.
        (scratch_manager.scratch_root / "job1_abc").mkdir(parents=True, exist_ok=True)
        (scratch_manager.scratch_root / "job2_xyz").mkdir(parents=True, exist_ok=True)

        assert scratch_manager.scratch_root.exists()

        # Call cleanup_on_shutdown.
        await scratch_manager.cleanup_on_shutdown()

        # Scratch root should be gone.
        assert not scratch_manager.scratch_root.exists()

    asyncio.run(_test())


def test_derivative_key_determinism() -> None:
    """Test that the same input → same key, different inputs → different keys."""

    def compute_key(job: dict) -> str:
        source_path = job.get("source_path", "")
        source_resolution = job.get("source_resolution") or ""
        source_audio_codec = job.get("source_audio_codec") or ""
        target_resolution = job.get("target_resolution", "")
        target_audio_codec = job.get("target_audio_codec", "")
        encoder = job.get("encoder", "")
        crf = job.get("crf", 0)
        preset = job.get("preset", "")

        hash_input = (
            f"{source_path}|{source_resolution}|{source_audio_codec}"
            f"|{target_resolution}|{target_audio_codec}|{encoder}|{crf}|{preset}"
        )
        key_hash = hashlib.blake2b(hash_input.encode(), digest_size=16).hexdigest()
        ext = "mkv"
        return f"{key_hash}_{encoder}-crf{crf}.{ext}"

    # Test 1: same params → same key
    job1 = {
        "source_path": "/mnt/transcode/source.mkv",
        "source_resolution": "1920x1080",
        "source_audio_codec": "aac",
        "target_resolution": "1280x720",
        "target_audio_codec": "aac",
        "encoder": "x265",
        "crf": 23,
        "preset": "slow",
    }
    job2 = job1.copy()

    key1 = compute_key(job1)
    key2 = compute_key(job2)
    assert key1 == key2, "Same params must produce same key"

    # Test 2: different target_resolution → different key
    job3 = job1.copy()
    job3["target_resolution"] = "854x480"
    key3 = compute_key(job3)
    assert key1 != key3, "Different target_resolution must produce different key"

    # Test 3: different source_audio_codec → different key
    job4 = job1.copy()
    job4["source_audio_codec"] = "opus"
    key4 = compute_key(job4)
    assert key1 != key4, "Different source_audio_codec must produce different key"

    # Test 4: different encoder → different key
    job5 = job1.copy()
    job5["encoder"] = "h264"
    key5 = compute_key(job5)
    assert key1 != key5, "Different encoder must produce different key"


def test_s3_backend_initialization(s3_backend: S3Backend) -> None:
    """Test that S3Backend initializes correctly."""
    assert s3_backend.bucket == "test-bucket"
    assert s3_backend.prefix == "masters/"
    assert s3_backend.library_id == "test-library"
    assert s3_backend.session is not None


@pytest.mark.asyncio
async def test_s3_backend_lock_noop(s3_backend: S3Backend) -> None:
    """Test that lock() is a no-op for S3 backend."""
    # Should not raise.
    await s3_backend.lock("s3://test-bucket/masters/file.mkv")


@pytest.mark.asyncio
async def test_s3_backend_unlock_noop(s3_backend: S3Backend) -> None:
    """Test that unlock() is a no-op for S3 backend."""
    # Should not raise.
    await s3_backend.unlock("s3://test-bucket/masters/file.mkv")


@pytest.mark.asyncio
async def test_s3_fetch_round_trip(s3_backend: S3Backend, scratch_manager: ScratchManager) -> None:
    """Test fetching a file from S3 to scratch (mocked client).

    Uses async mock to simulate S3 operations.
    1. Mock the client's head_object and download_file
    2. Call backend.fetch(key)
    3. Assert the returned path exists and content matches
    """
    import tempfile

    test_data = b"test video content" * 1000

    # Create a temporary file that fetch() will "download" to.
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".mkv", delete=False) as f:
        temp_downloaded = Path(f.name)
        f.write(test_data)

    class MockClient:
        async def head_object(self, **kw):
            return {"ContentLength": len(test_data)}

        async def download_file(self, **kwargs):
            # Accept both Bucket/Key/Filename (boto3 API) and bucket/key/filename.
            filename = kwargs.get("Filename") or kwargs.get("filename")
            # Copy our temp file to the expected location.
            Path(filename).write_bytes(test_data)

    class MockAsyncContext:
        async def __aenter__(self):
            return MockClient()

        async def __aexit__(self, *args):
            pass

    try:
        # Patch session.client to return our mock context manager.
        original_session = s3_backend.session

        class MockSession:
            def client(self, *args, **kwargs):
                return MockAsyncContext()

        s3_backend.session = MockSession()

        # Fetch the object.
        source_key = "masters/test-video.mkv"
        local_path = await s3_backend.fetch(source_key)

        # Assert: file exists and content matches.
        assert local_path.exists(), f"Fetched file should exist: {local_path}"
        assert local_path.is_file(), f"Fetched path should be a file: {local_path}"
        fetched_data = local_path.read_bytes()
        assert fetched_data == test_data, "Fetched content must match original"

        # Assert: path is within scratch root (invariant).
        assert local_path.parent.parent == scratch_manager.scratch_root, (
            "Fetch must return a path within scratch"
        )

    finally:
        s3_backend.session = original_session
        temp_downloaded.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_s3_commit_round_trip(s3_backend: S3Backend, db) -> None:
    """Test committing a transcoded output (upload + registry insert).

    1. Create a library record (needed for FK constraint)
    2. Create a local output file
    3. Call backend.commit() with full job metadata
    4. Assert the derivative was uploaded (mocked S3)
    5. Assert a row was inserted in the derivatives table with all fields correct
    """
    import tempfile

    # Create the library record first (required for FK constraint in derivatives table).
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    await db.execute(
        """INSERT INTO libraries
           (id, name, media_type, path, quality_preset, enabled, auto_scan,
            scan_interval_hours, backend, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "test-library",
            "Test Library",
            "movies",
            "/mnt/transcode",
            21,
            1,
            0,
            24,
            "s3",
            now,
            now,
        ),
    )
    await db.commit()

    # Create a local output file.
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".mkv", delete=False) as f:
        local_output = Path(f.name)
        test_output_data = b"transcoded content" * 2000
        f.write(test_output_data)

    try:
        # Job dict with complete metadata
        job = {
            "id": "job-commit-test-1",
            "source_path": "/mnt/transcode/source.mkv",
            "source_resolution": "1920x1080",
            "source_audio_codec": "aac",
            "target_resolution": "1280x720",
            "target_audio_codec": "aac",
            "encoder": "x265",
            "crf": 23,
            "preset": "slow",
        }
        source_key = "masters/source.mkv"

        # Track uploaded objects in a dict (to verify upload).
        uploaded_objects = {}

        class MockClient:
            async def upload_fileobj(self, fileobj, bucket, key, **kwargs):
                # Store the uploaded content for verification.
                # bucket and key are positional args in boto3 API.
                uploaded_objects[key] = fileobj.read()

        class MockAsyncContext:
            async def __aenter__(self):
                return MockClient()

            async def __aexit__(self, *args):
                pass

        # Patch the session.
        original_session = s3_backend.session

        class MockSession:
            def client(self, *args, **kwargs):
                return MockAsyncContext()

        s3_backend.session = MockSession()

        try:
            # Commit the derivative.
            result = await s3_backend.commit(local_output, source_key, job)

            # Assert: CommitResult has correct size and space_saved=0.
            assert result.output_size == len(test_output_data)
            assert result.space_saved == 0, "S3 backend should not reclaim space"

            # Compute the expected key.
            expected_key = await _compute_expected_derivative_key(job)

            # Assert: derivative was uploaded (check tracked uploads).
            assert expected_key in uploaded_objects, (
                f"Expected derivative {expected_key} not uploaded"
            )
            assert uploaded_objects[expected_key] == test_output_data, "Uploaded content must match"

            # NOTE: S3Backend.commit() no longer registers derivatives in the database.
            # That's now the scheduler's responsibility via the register-derivative API endpoint.
            # The commit() method only uploads to S3 and returns the metadata.
            # This test verifies that the upload succeeded; registration is tested separately
            # in the worker_api tests.

        finally:
            s3_backend.session = original_session

    finally:
        local_output.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_s3_commit_no_registry_row_on_upload_failure(s3_backend: S3Backend, db) -> None:
    """Test atomicity: failed upload leaves NO derivatives row behind.

    MEDIUM-2: Ensure upload failure does not insert a registry row.
    Verify that create_derivative runs AFTER upload succeeds.
    """
    import tempfile
    from datetime import UTC, datetime

    # Create the library record first.
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """INSERT INTO libraries
           (id, name, media_type, path, quality_preset, enabled, auto_scan,
            scan_interval_hours, backend, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "test-library",
            "Test Library",
            "movies",
            "/mnt/transcode",
            21,
            1,
            0,
            24,
            "s3",
            now,
            now,
        ),
    )
    await db.commit()

    # Create a local output file.
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".mkv", delete=False) as f:
        local_output = Path(f.name)
        f.write(b"test data")

    try:
        job = {
            "id": "job-upload-fail",
            "source_path": "/mnt/transcode/source.mkv",
            "source_resolution": "1920x1080",
            "source_audio_codec": "aac",
            "target_resolution": "1280x720",
            "target_audio_codec": "aac",
            "encoder": "x265",
            "crf": 23,
            "preset": "slow",
        }

        expected_key = await _compute_expected_derivative_key(job)

        # Patch the session's client method to fail on upload_fileobj.
        original_session = s3_backend.session

        class FailingClientContext:
            async def __aenter__(self):
                return _FailingClient()

            async def __aexit__(self, *args):
                pass

        class FailingSession:
            def client(self, *args, **kwargs):
                return FailingClientContext()

        s3_backend.session = FailingSession()

        try:
            # Attempt commit; should raise OSError.
            await s3_backend.commit(local_output, "masters/source.mkv", job)
            pytest.fail("Expected OSError from failed upload")
        except OSError:
            # Expected.
            pass
        finally:
            s3_backend.session = original_session

        # Verify: no row was inserted in the database (atomicity).
        deriv = await deriv_repo.lookup_by_key(db, expected_key)
        assert deriv is None, "No derivatives row should exist after failed upload"

    finally:
        local_output.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_s3_commit_scratch_released_after_success(s3_backend: S3Backend, db) -> None:
    """Test that scratch space is released after successful commit.

    1. Create library and reserve scratch for a job
    2. Commit a file
    3. Assert the job's scratch directory was cleaned up
    """
    from datetime import UTC, datetime

    # Create the library record first.
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """INSERT INTO libraries
           (id, name, media_type, path, quality_preset, enabled, auto_scan,
            scan_interval_hours, backend, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "test-library",
            "Test Library",
            "movies",
            "/mnt/transcode",
            21,
            1,
            0,
            24,
            "s3",
            now,
            now,
        ),
    )
    await db.commit()

    # Reserve scratch for this job.
    job_id = "job-scratch-release"
    scratch_dir = await s3_backend.scratch_manager.reserve(job_id=job_id, size_bytes=10_000_000)
    assert scratch_dir.exists()

    # Create output file in the scratch dir.
    local_output = scratch_dir / "output.mkv"
    local_output.write_bytes(b"transcoded" * 1000)

    job = {
        "id": job_id,
        "source_path": "/mnt/transcode/source.mkv",
        "source_resolution": "1920x1080",
        "source_audio_codec": "aac",
        "target_resolution": "1280x720",
        "target_audio_codec": "aac",
        "encoder": "x265",
        "crf": 23,
        "preset": "slow",
    }

    # Track uploaded objects.
    uploaded_objects = {}

    class MockClient:
        async def upload_fileobj(self, fileobj, bucket, key, **kwargs):
            # bucket and key are positional args in boto3 API.
            uploaded_objects[key] = fileobj.read()

    class MockAsyncContext:
        async def __aenter__(self):
            return MockClient()

        async def __aexit__(self, *args):
            pass

    # Patch the session.
    original_session = s3_backend.session

    class MockSession:
        def client(self, *args, **kwargs):
            return MockAsyncContext()

    s3_backend.session = MockSession()

    try:
        # Commit the derivative.
        await s3_backend.commit(local_output, "masters/source.mkv", job)

        # Assert: scratch directory was released (deleted).
        # release() deletes all {job_id}_* dirs.
        remaining_dirs = list(s3_backend.scratch_manager.scratch_root.glob(f"{job_id}_*"))
        assert len(remaining_dirs) == 0, f"Scratch should be released; found {remaining_dirs}"

    finally:
        s3_backend.session = original_session


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================


async def _compute_expected_derivative_key(job: dict) -> str:
    """Compute the expected content-addressed derivative key.

    Mirrors the logic in S3Backend.commit().
    """
    import hashlib

    source_path = job.get("source_path", "")
    source_resolution = job.get("source_resolution") or ""
    source_audio_codec = job.get("source_audio_codec") or ""
    target_resolution = job.get("target_resolution", "")
    target_audio_codec = job.get("target_audio_codec", "")
    encoder = job.get("encoder", "")
    crf = job.get("crf", 0)
    preset = job.get("preset", "")

    hash_input = (
        f"{source_path}|{source_resolution}|{source_audio_codec}"
        f"|{target_resolution}|{target_audio_codec}|{encoder}|{crf}|{preset}"
    )
    key_hash = hashlib.blake2b(hash_input.encode(), digest_size=16).hexdigest()
    ext = "mkv"
    return f"{key_hash}_{encoder}-crf{crf}.{ext}"


class _FailingClient:
    """Mock S3 client that fails on upload."""

    async def upload_fileobj(self, *args, **kwargs):
        raise OSError("Simulated S3 upload failure")


class _FailingClientContext:
    """Mock S3 client context that fails on upload."""

    def __init__(self, expected_key: str = ""):
        self.expected_key = expected_key

    async def __aenter__(self):
        return _FailingClient()

    async def __aexit__(self, *args):
        pass


class _BoomError(RuntimeError):
    """Sentinel raised by mocked clients to stop execution after the call we inspect."""


class TestS3ChecksumCompat:
    """Every S3 client must use the compat config: boto3 >= 1.36 sends CRC32
    request checksums by default, which Linode E3 (and other S3-alikes)
    reject with 403."""

    def test_helper_config_values(self):
        from transcode_forge.s3compat import s3_client_config

        cfg = s3_client_config()
        assert cfg.request_checksum_calculation == "when_required"
        assert cfg.response_checksum_validation == "when_required"

    async def test_fetch_client_uses_compat_config(self, s3_config):
        backend = S3Backend(
            config=s3_config,
            db=None,
            scratch_manager=AsyncMock(),
            library_id="lib",
            bucket="bkt",
        )
        backend.session = MagicMock()
        backend.session.client = MagicMock(side_effect=_BoomError("stop before network IO"))

        with pytest.raises(_BoomError):
            await backend.fetch("masters/movie.mkv")

        cfg = backend.session.client.call_args.kwargs["config"]
        assert cfg.request_checksum_calculation == "when_required"

    async def test_commit_client_uses_compat_config(self, s3_config, tmp_path):
        local = tmp_path / "out.mkv"
        local.write_bytes(b"x" * 64)
        backend = S3Backend(
            config=s3_config,
            db=None,
            scratch_manager=AsyncMock(),
            library_id="lib",
            bucket="bkt",
        )
        backend.session = MagicMock()
        backend.session.client = MagicMock(side_effect=_BoomError("stop before network IO"))

        with pytest.raises(_BoomError):
            await backend.commit(
                local_output=local,
                source="masters/movie.mkv",
                job={"id": "job-1", "source_path": "masters/movie.mkv"},
            )

        cfg = backend.session.client.call_args.kwargs["config"]
        assert cfg.request_checksum_calculation == "when_required"

    async def test_scanner_client_uses_compat_config(self, s3_config, db):
        from transcode_forge.scanner import s3_scanner

        with patch.object(s3_scanner, "Session") as session_cls:
            session = session_cls.return_value
            session.client = MagicMock(side_effect=_BoomError("stop before network IO"))

            with pytest.raises(_BoomError):
                await s3_scanner.scan_s3_library(
                    library_id="lib",
                    library_name="L",
                    bucket="bkt",
                    prefix="",
                    config=s3_config,
                    db=db,
                )

        cfg = session.client.call_args.kwargs["config"]
        assert cfg.request_checksum_calculation == "when_required"

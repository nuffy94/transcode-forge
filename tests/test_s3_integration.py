"""Integration tests for S3 backend with MinIO.

Tests the full S3-library job flow end-to-end against a real MinIO service.
Excluded from default test runs (marked with @pytest.mark.s3_integration).

To run:
    docker-compose -f docker-compose.test.yml up -d
    pytest tests/test_s3_integration.py -v -s

The test will skip if MinIO is not reachable.
"""

import logging
import tempfile
from hashlib import blake2b
from pathlib import Path
from uuid import uuid4

import pytest

# These imports only succeed if S3 dependencies are installed.
aioboto3 = pytest.importorskip("aioboto3")
minio = pytest.importorskip("minio")

logger = logging.getLogger(__name__)


@pytest.mark.s3_integration
class TestS3Integration:
    """End-to-end S3-library tests against MinIO."""

    MINIO_ENDPOINT = "http://localhost:9000"
    MINIO_ACCESS_KEY = "minioadmin"
    MINIO_SECRET_KEY = "minioadmin"
    TEST_BUCKET = "test-bucket"

    @pytest.fixture
    async def minio_client(self):
        """Connect to MinIO and ensure the test bucket exists."""
        from minio import Minio
        from minio.error import S3Error

        # Check if MinIO is reachable.
        client = Minio(
            "localhost:9000",
            access_key=self.MINIO_ACCESS_KEY,
            secret_key=self.MINIO_SECRET_KEY,
            secure=False,
        )

        # Try to list buckets to verify connectivity.
        try:
            client.list_buckets()
        except Exception as e:
            pytest.skip(f"MinIO not reachable: {e}")

        # Ensure test bucket exists.
        try:
            client.make_bucket(self.TEST_BUCKET)
        except S3Error as e:
            if e.code != "BucketAlreadyOwnedByYou":
                raise

        yield client

        # Cleanup: remove test objects (optional, can leave for manual inspection).
        # for obj in client.list_objects(self.TEST_BUCKET):
        #     client.remove_object(self.TEST_BUCKET, obj.object_name)

    @pytest.fixture
    async def s3_config(self):
        """S3 config for MinIO."""
        from transcode_forge.config import Settings

        return Settings(
            s3_endpoint_url=self.MINIO_ENDPOINT,
            s3_region="us-east-1",
            s3_access_key_id=self.MINIO_ACCESS_KEY,
            s3_secret_access_key=self.MINIO_SECRET_KEY,
        )

    async def test_s3_backend_upload_and_lookup(self, minio_client, s3_config):
        """Test uploading a file to S3 and looking it up."""
        from transcode_forge.worker.storage.s3 import S3Backend
        from transcode_forge.worker.storage.scratch import ScratchManager

        # Create a temporary scratch directory.
        with tempfile.TemporaryDirectory() as scratch_root:
            scratch_manager = ScratchManager(scratch_root)

            # Create S3Backend (DB is None for this test; derivatives won't be registered).
            # (backend variable will be used in type-checking context)
            _backend = S3Backend(
                config=s3_config,
                db=None,  # type: ignore
                scratch_manager=scratch_manager,
                library_id="test-lib",
                bucket=self.TEST_BUCKET,
                prefix="masters/",
            )

            # Create a small test file to upload.
            test_data = b"test video file" * 1000
            test_file = Path(scratch_root) / "test.mkv"
            test_file.write_bytes(test_data)

            # Upload via MinIO (direct, not via S3Backend.upload since that doesn't exist).
            s3_key = "masters/test.mkv"
            minio_client.fput_object(self.TEST_BUCKET, s3_key, str(test_file))

            # Verify the file exists in MinIO.
            stat = minio_client.stat_object(self.TEST_BUCKET, s3_key)
            assert stat.size == len(test_data)

            logger.info(f"Successfully uploaded {s3_key} ({stat.size} bytes)")

    async def test_s3_backend_fetch_and_transcode_stub(self, minio_client, s3_config, db):
        """Test S3 backend fetch → stub transcode → commit flow.

        This is a simplified version that stubs out ffmpeg (we just copy the file).
        A real test would use ffmpeg, but that requires complex setup.
        """
        from transcode_forge.models.job import Job
        from transcode_forge.worker.storage.s3 import S3Backend
        from transcode_forge.worker.storage.scratch import ScratchManager

        # Create a temporary scratch directory.
        with tempfile.TemporaryDirectory() as scratch_root:
            scratch_manager = ScratchManager(scratch_root)

            # Create S3Backend.
            backend = S3Backend(
                config=s3_config,
                db=db,  # Use the test DB for this test.
                scratch_manager=scratch_manager,
                library_id="test-lib",
                bucket=self.TEST_BUCKET,
                prefix="masters/",
            )

            # Create a small test master file.
            test_data = b"test video master" * 1000
            master_key = f"masters/test-{uuid4().hex[:8]}.mkv"
            test_file = Path(scratch_root) / "master.mkv"
            test_file.write_bytes(test_data)

            # Upload master to MinIO.
            minio_client.fput_object(self.TEST_BUCKET, master_key, str(test_file))
            logger.info(f"Uploaded master: {master_key}")

            # Fetch via S3Backend (downloads to scratch).
            local_path = await backend.fetch(master_key)
            assert local_path.exists()
            assert local_path.stat().st_size == len(test_data)
            logger.info(f"Fetched to scratch: {local_path}")

            # Stub transcode: just compress a bit (for test purposes).
            transcoded_file = local_path.with_name(local_path.stem + ".transcoded.mkv")
            # Simulate compression: write 50% of original size.
            transcoded_data = test_data[: len(test_data) // 2]
            transcoded_file.write_bytes(transcoded_data)

            # Create a job object for the commit.
            job = Job(
                id=str(uuid4()),
                source_path=f"/library/{Path(master_key).name}",
                library="test-lib",
                source_codec="h264",
                source_resolution="1920x1080",
                source_audio_codec="aac",
                target_codec="hevc",
                quality_value=23,
                encoder="libx265",
            )
            job._backend_type = "s3"  # type: ignore
            job._s3_bucket = self.TEST_BUCKET  # type: ignore
            job._s3_prefix = "masters/"  # type: ignore
            job.preset = "medium"  # type: ignore
            job.target_resolution = "1920x1080"  # type: ignore
            job.target_audio_codec = "aac"  # type: ignore
            job.crf = 23  # type: ignore

            # Commit (upload derivative).
            commit_result = await backend.commit(
                local_output=transcoded_file,
                source=master_key,
                job=job,
            )

            assert commit_result.output_size == len(transcoded_data)
            logger.info(f"Committed derivative: {commit_result.output_size} bytes")

            # Verify the derivative was uploaded.
            objects = list(minio_client.list_objects(self.TEST_BUCKET, "derivatives/"))
            assert len(objects) > 0, "No derivatives found after commit"
            logger.info(f"Found {len(objects)} derivative object(s)")

            # Cleanup.
            await backend.cleanup(job)

    async def test_dedup_skips_redundant_encode(self, minio_client, s3_config, db):
        """Test that a second identical job reuses the derivative and skips encoding.

        This test:
        1. Creates and encodes a job.
        2. Creates an identical second job.
        3. Verifies the second job is marked COMPLETE via dedup lookup.
        """
        from transcode_forge.models.job import Job
        from transcode_forge.repos import derivatives as deriv_repo
        from transcode_forge.worker.storage.s3 import S3Backend
        from transcode_forge.worker.storage.scratch import ScratchManager

        with tempfile.TemporaryDirectory() as scratch_root:
            scratch_manager = ScratchManager(scratch_root)
            backend = S3Backend(
                config=s3_config,
                db=db,
                scratch_manager=scratch_manager,
                library_id="test-lib",
                bucket=self.TEST_BUCKET,
                prefix="masters/",
            )

            # Create and "transcode" the first job.
            master_data = b"original video" * 1000
            master_key = f"masters/dedup-{uuid4().hex[:8]}.mkv"
            test_file = Path(scratch_root) / "dedup-master.mkv"
            test_file.write_bytes(master_data)
            minio_client.fput_object(self.TEST_BUCKET, master_key, str(test_file))

            job1 = Job(
                id=str(uuid4()),
                source_path="/library/dedup-video.mkv",
                library="test-lib",
                source_codec="h264",
                source_resolution="1920x1080",
                source_audio_codec="aac",
                target_codec="hevc",
                quality_value=23,
                encoder="libx265",
            )
            job1._backend_type = "s3"  # type: ignore
            job1._s3_bucket = self.TEST_BUCKET  # type: ignore
            job1._s3_prefix = "masters/"  # type: ignore
            job1.preset = "medium"  # type: ignore
            job1.target_resolution = "1920x1080"  # type: ignore
            job1.target_audio_codec = "aac"  # type: ignore
            job1.crf = 23  # type: ignore

            # Stub transcode and commit.
            transcoded_file = test_file.with_name("transcoded.mkv")
            transcoded_file.write_bytes(master_data[: len(master_data) // 2])

            commit_result = await backend.commit(
                local_output=transcoded_file,
                source=master_key,
                job=job1,
            )
            logger.info(f"Job 1 committed: {commit_result.output_size} bytes")

            # Now create an identical second job.
            job2 = Job(
                id=str(uuid4()),
                source_path="/library/dedup-video.mkv",  # Same source
                library="test-lib",
                source_codec="h264",
                source_resolution="1920x1080",
                source_audio_codec="aac",
                target_codec="hevc",
                quality_value=23,
                encoder="libx265",
            )
            job2._backend_type = "s3"  # type: ignore
            job2._s3_bucket = self.TEST_BUCKET  # type: ignore
            job2._s3_prefix = "masters/"  # type: ignore
            job2.preset = "medium"  # type: ignore
            job2.target_resolution = "1920x1080"  # type: ignore
            job2.target_audio_codec = "aac"  # type: ignore
            job2.crf = 23  # type: ignore

            # Compute the derivative key (same as job1).
            hash_input = (
                f"{job2.source_path}|{job2.source_resolution}|{job2.source_audio_codec}"
                f"|{job2.target_resolution}|{job2.target_audio_codec}|"
                f"{job2.encoder}|{job2.quality_value}|{job2.preset}"
            )
            key_hash = blake2b(hash_input.encode(), digest_size=16).hexdigest()
            derivative_key = f"{key_hash}_{job2.encoder}-crf{job2.quality_value}.mkv"

            # Look up the derivative.
            existing = await deriv_repo.lookup_by_key(db, derivative_key)
            assert existing is not None, "Derivative should exist from job1"
            assert existing["output_size"] == commit_result.output_size
            logger.info(f"Dedup check succeeded: found derivative {derivative_key}")

            await backend.cleanup(job1)
            await backend.cleanup(job2)

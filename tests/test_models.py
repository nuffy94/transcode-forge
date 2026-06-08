"""Tests for Pydantic domain models."""

import pytest
from pydantic import ValidationError

from transcode_forge.models.job import Job, JobStatus
from transcode_forge.models.library import Library
from transcode_forge.models.scan import Scan, ScanStatus
from transcode_forge.models.skipped import SkippedFile, SkipReason
from transcode_forge.models.worker import Worker, WorkerStatus


class TestJobModel:
    def test_create_minimal_job(self):
        job = Job(
            source_path="/media/movies/test.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
        )
        assert job.status == JobStatus.PENDING
        assert job.target_codec == "hevc"
        assert job.progress == 0.0
        assert job.retry_count == 0
        assert job.id  # UUID generated

    def test_job_status_validation(self):
        job = Job(
            source_path="/test.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
            status=JobStatus.TRANSCODING,
        )
        assert job.status == JobStatus.TRANSCODING

    def test_job_invalid_status(self):
        with pytest.raises(ValidationError):
            Job(
                source_path="/test.mkv",
                library="movies",
                source_codec="h264",
                quality_value=21,
                status="nonexistent_status",
            )

    def test_job_progress_bounds(self):
        with pytest.raises(ValidationError):
            Job(
                source_path="/test.mkv",
                library="movies",
                source_codec="h264",
                quality_value=21,
                progress=1.5,
            )

    def test_compression_ratio_computed(self):
        job = Job(
            source_path="/test.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
            source_size=1000,
            output_size=400,
            space_saved=600,
        )
        assert job.compression_ratio == pytest.approx(0.4)
        assert job.savings_percent == pytest.approx(60.0)

    def test_compression_ratio_none_when_no_sizes(self):
        job = Job(
            source_path="/test.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
        )
        assert job.compression_ratio is None
        assert job.savings_percent is None

    def test_job_serialization_roundtrip(self):
        job = Job(
            source_path="/media/movies/test.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
        )
        data = job.model_dump(mode="json")
        restored = Job.model_validate(data)
        assert restored.id == job.id
        assert restored.source_path == job.source_path
        assert restored.status == job.status


class TestWorkerModel:
    def test_create_worker(self):
        worker = Worker(name="worker-1", host="192.0.2.100")
        assert worker.status == WorkerStatus.OFFLINE
        assert worker.capabilities == ["cpu"]
        assert worker.max_concurrent == 1

    def test_worker_with_capabilities(self):
        worker = Worker(
            name="scheduler-1",
            host="192.0.2.9",
            capabilities=["cpu", "qsv"],
        )
        assert "qsv" in worker.capabilities

    def test_worker_status_values(self):
        for status in WorkerStatus:
            worker = Worker(name="test", host="localhost", status=status)
            assert worker.status == status


class TestLibraryModel:
    def test_create_library(self):
        lib = Library(name="movies", path="/media/movies", quality_preset=21)
        assert lib.quality_preset == 21

    def test_quality_bounds(self):
        with pytest.raises(ValidationError):
            Library(name="movies", path="/test", quality_preset=0)
        with pytest.raises(ValidationError):
            Library(name="movies", path="/test", quality_preset=52)


class TestScanModel:
    def test_create_scan(self):
        scan = Scan(library="movies")
        assert scan.status == ScanStatus.RUNNING
        assert scan.files_found == 0

    def test_scan_status_values(self):
        for status in ScanStatus:
            scan = Scan(library="test", status=status)
            assert scan.status == status


class TestSkippedFileModel:
    def test_create_skipped(self):
        sf = SkippedFile(
            file_path="/media/movies/test.mkv",
            library="movies",
            codec="hevc",
            skip_reason=SkipReason.ALREADY_HEVC,
        )
        assert sf.skip_reason == SkipReason.ALREADY_HEVC

    def test_skip_reason_values(self):
        assert SkipReason.ALREADY_HEVC == "already_hevc"
        assert SkipReason.NOT_H264 == "not_h264"
        assert SkipReason.SIZE_REGRESSION == "size_regression"
        assert SkipReason.TOO_SMALL == "too_small"
        assert SkipReason.MANUAL_SKIP == "manual_skip"

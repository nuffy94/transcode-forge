"""Job model — represents a single transcode task."""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field, computed_field


class JobStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    ASSIGNED = "assigned"
    TRANSCODING = "transcoding"
    VERIFYING = "verifying"
    COMPLETE = "complete"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class TargetCodec(StrEnum):
    """Output codecs a job can target. Extending (VP9/AV2 later) means a new
    value here plus builder entries in worker/encoder.py — nothing structural."""

    HEVC = "hevc"
    AV1 = "av1"


class Job(BaseModel):
    """A single transcode job tracking source file through the pipeline."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    source_path: str
    library: str
    source_codec: str
    source_resolution: str | None = None
    source_bitrate: int | None = None
    source_duration: float | None = None
    source_size: int | None = None
    target_codec: str = "hevc"
    quality_value: int
    # Quality goal (snapshot at queue time) + encode outcome. NULL
    # target_vmaf = no VMAF gate/search — pre-feature jobs behave as before.
    # predicted_* = the CRF search's winning sample scores; achieved_* = the
    # full-file measurement. Persisting both sides makes the sample-vs-full
    # gap a measured quantity (plans/vmaf-decoupling-spec.md §4.1).
    target_vmaf: float | None = None
    resolved_crf: int | None = None
    achieved_vmaf: float | None = None
    achieved_vmaf_perc5: float | None = None
    predicted_vmaf_mean: float | None = None
    predicted_vmaf_perc5: float | None = None
    backend_used: str | None = None
    status: JobStatus = JobStatus.PENDING
    worker_id: str | None = None
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    output_size: int | None = None
    space_saved: int | None = None
    error_message: str | None = None
    retry_count: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def compression_ratio(self) -> float | None:
        """Output size / source size. Lower is better."""
        if self.source_size and self.output_size:
            return self.output_size / self.source_size
        return None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def savings_percent(self) -> float | None:
        """Percentage of space saved."""
        if self.source_size and self.space_saved:
            return (self.space_saved / self.source_size) * 100
        return None

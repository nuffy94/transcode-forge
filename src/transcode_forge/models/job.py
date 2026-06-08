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

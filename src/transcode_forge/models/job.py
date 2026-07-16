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


class JobPhase(StrEnum):
    """The pipeline's human-watchable time phases, reported by the worker
    with each progress update and rendered as the dashboard's station bar.

    These are the five stretches a person can WATCH, not the 8 protocol
    steps — LOCK/CONFIRM/CLEANUP/UNLOCK are sub-second bookkeeping (the UI
    shows them as tick marks with no duration). Only ENCODE carries a true
    percentage; the others render as elapsed time."""

    SEARCH = "search"  # CRF search probes on samples (optional pre-step)
    ENCODE = "encode"  # the full transcode — the only honest %
    VERIFY = "verify"  # ffprobe + decode samples on the output
    GAUGE = "gauge"  # full-file VMAF vs the original (COMPARE's long half)
    SWAP = "swap"  # atomic swap + post-swap confirm


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
    # Requested downscale height (1080/720 — plans/downscale-shrink-spec.md).
    # None = keep source resolution; pre-feature jobs behave identically.
    target_height: int | None = None
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
    # Pipeline phase (JobPhase value) — NULL until a phase-aware worker
    # reports one; the dashboard falls back to the plain meter row.
    phase: str | None = None
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

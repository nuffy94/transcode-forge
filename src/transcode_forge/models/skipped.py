"""SkippedFile model — tracks files intentionally not transcoded."""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field


class SkipReason(StrEnum):
    ALREADY_HEVC = "already_hevc"
    NOT_H264 = "not_h264"
    SIZE_REGRESSION = "size_regression"
    TOO_SMALL = "too_small"
    MANUAL_SKIP = "manual_skip"


class SkippedFile(BaseModel):
    """A file intentionally skipped during scanning or transcoding."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    file_path: str
    library: str
    codec: str
    resolution: str | None = None
    file_size: int | None = None
    skip_reason: SkipReason
    scan_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

"""Scan model — represents a library scan operation."""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field


class ScanStatus(StrEnum):
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class Scan(BaseModel):
    """A library scan operation tracking files discovered."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    library: str
    files_found: int = 0
    files_new: int = 0
    files_updated: int = 0
    files_skipped: int = 0
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    status: ScanStatus = ScanStatus.RUNNING

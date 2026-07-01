"""Worker model — represents a transcode node."""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field


class WorkerStatus(StrEnum):
    ONLINE = "online"
    BUSY = "busy"
    OFFLINE = "offline"
    DEAD = "dead"


class Worker(BaseModel):
    """A registered transcode worker node."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    host: str
    capabilities: list[str] = Field(default_factory=lambda: ["cpu"])
    # Codecs this worker can actually encode (probed at startup). Workers
    # predating the multi-codec release don't advertise — default to hevc
    # so a rolling update never hands them an AV1 job.
    supported_codecs: list[str] = Field(default_factory=lambda: ["hevc"])
    ffmpeg_version: str | None = None
    max_concurrent: int = 1
    status: WorkerStatus = WorkerStatus.OFFLINE
    current_job_id: str | None = None
    last_heartbeat: datetime | None = None
    registered_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

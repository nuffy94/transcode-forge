"""Library model — represents a media library with quality presets."""

from enum import StrEnum

from pydantic import BaseModel, Field


class StorageBackendType(StrEnum):
    """Storage backend for a library."""

    FILESYSTEM = "filesystem"
    S3 = "s3"


class Library(BaseModel):
    """A media library configuration."""

    name: str
    path: str
    quality_preset: int = Field(ge=1, le=51)
    backend: StorageBackendType = StorageBackendType.FILESYSTEM
    s3_bucket: str | None = None
    s3_prefix: str | None = None

"""Storage backend abstraction for transcode-forge.

Provides pluggable storage backends (filesystem, S3, etc.) behind a
unified interface. This allows the worker to transcode media from
different sources using the same pipeline.
"""

from typing import Any

from transcode_forge.worker.storage.base import (
    CommitResult,
    StorageBackend,
)
from transcode_forge.worker.storage.filesystem import FilesystemBackend

__all__ = [
    "CommitResult",
    "FilesystemBackend",
    "StorageBackend",
]


def get_backend(library: dict[str, Any]) -> StorageBackend:
    """Factory: return the storage backend for a library.

    Args:
        library: Library dict from the database (with 'backend' field).

    Returns:
        A StorageBackend implementation.

    For now, only FilesystemBackend is supported. Future steps will
    add S3Backend conditionally based on library.backend == 's3'.
    """
    # Step 2 will add conditional logic here:
    # if library.get("backend") == "s3":
    #     return S3Backend(...)
    return FilesystemBackend()

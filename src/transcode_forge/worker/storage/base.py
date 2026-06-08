"""Storage backend abstraction — the seam for pluggable media sources.

A StorageBackend handles the I/O and metadata for a library: retrieving
files for transcoding and committing the results. The critical invariant
is that backend.fetch() ALWAYS returns a LOCAL filesystem path that
run_pipeline() can operate on directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class CommitResult:
    """Result of committing a transcoded output.

    Attributes:
        output_size: Size of the committed file in bytes.
        space_saved: Bytes reclaimed (source_size - output_size), if in-place.
            For S3, may be 0 since the master is untouched.
    """

    output_size: int
    space_saved: int


class StorageBackend(Protocol):
    """Abstract interface for storage backends.

    CRITICAL INVARIANT:
    run_pipeline() MUST ALWAYS be called with a LOCAL filesystem path.
    It derives lock_path/tmp_path/bak_path from source_path and performs
    literal Path.rename() + os.chown/chmod operations. An S3 key must
    NEVER reach run_pipeline().

    The seam: backend.fetch(...) returns a local working path:
    - Filesystem backend: the path-mapped original (no copy).
    - S3 backend: a scratch path after download.
    """

    async def lock(self, key: str) -> None:
        """Acquire an exclusive lock on a source.

        For filesystem backend: a no-op (the pipeline's .tf_lock handles it).
        For S3 backend: a DB row lock prevents concurrent work on the same key.

        Args:
            key: Source identifier (filesystem path or S3 object key).

        Raises:
            LockError: If the lock is already held.
        """
        ...

    async def unlock(self, key: str) -> None:
        """Release a lock on a source.

        Args:
            key: Source identifier.
        """
        ...

    async def fetch(self, source: str) -> Path:
        """Retrieve a source and return a LOCAL working path.

        Filesystem backend: returns the path-mapped source (no copy).
        S3 backend: downloads to a local scratch path and returns that.

        CRITICAL: This must always return a Path that can be passed to
        run_pipeline(). An S3 key is NOT a valid return value.

        Args:
            source: Source identifier (filesystem path for FS, S3 key for S3).

        Returns:
            A Path to a LOCAL file that can be read/transcoded.

        Raises:
            IOError: If the source cannot be retrieved.
        """
        ...

    async def commit(
        self,
        local_output: Path,
        source: str,
        job: Any,
        space_saved: int = 0,
    ) -> CommitResult:
        """Commit a transcoded output.

        Filesystem backend: atomically swaps the original with the
        transcoded file (the 8-step pipeline's "swap" step already did
        this, so commit validates sizes and may record metadata).
        S3 backend: uploads the derivative to object storage.

        Args:
            local_output: Path to the transcoded file (local filesystem).
            source: Source identifier (path or S3 key).
            job: Job dict with id, source_path, quality_value, etc.
            space_saved: For filesystem backend, the bytes reclaimed from swap.
                For S3 backend, ignored (always 0).

        Returns:
            CommitResult with output_size and space_saved.
        """
        ...

    async def scan(self, library: str) -> list[dict[str, Any]]:
        """Scan a library and return a list of media files.

        Filesystem backend: walks the directory tree, probes each file.
        S3 backend: lists objects and probes via presigned URLs.

        Args:
            library: Library identifier (filesystem path for FS, bucket for S3).

        Returns:
            List of dicts with source_path, duration, codec, resolution, etc.
        """
        ...

    async def cleanup(self, job: Any) -> None:
        """Clean up temporary resources for a job.

        Filesystem backend: a no-op (the pipeline cleans up .tf_* files).
        S3 backend: release scratch space and orphaned parts.

        Args:
            job: Job dict.
        """
        ...

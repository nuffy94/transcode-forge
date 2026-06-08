"""Derivative model — represents a cached/reused transcoded output."""

import hashlib
from pathlib import Path

from pydantic import BaseModel


class Derivative(BaseModel):
    """A cached transcoded output, identified by content-addressed key."""

    id: str
    library_id: str
    source_key: str
    source_path: str
    source_resolution: str | None = None
    source_audio_codec: str | None = None
    target_resolution: str
    target_audio_codec: str
    encoder: str
    crf: int
    preset: str
    derivative_key: str
    output_size: int
    created_at: str


def compute_derivative_key(
    source_path: str,
    source_resolution: str,
    source_audio_codec: str,
    target_resolution: str,
    target_audio_codec: str,
    encoder: str,
    crf: int,
    preset: str,
    local_output: Path,
) -> str:
    """Compute the content-addressed derivative key deterministically.

    All parameters that affect the output contribute to the hash, ensuring
    that identical source + parameters → identical key → transparent dedup.

    Args:
        source_path: Source file path/key.
        source_resolution: Source video resolution.
        source_audio_codec: Source audio codec.
        target_resolution: Target video resolution.
        target_audio_codec: Target audio codec.
        encoder: Video encoder (libx265, hevc_nvenc, etc.).
        crf: Constant rate factor (quality).
        preset: Encoding preset (fast, medium, slow).
        local_output: Path to the transcoded output (for extension).

    Returns:
        Content-addressed derivative key (e.g., "abc123_hevc-crf21.mkv").
    """
    hash_input = (
        f"{source_path}|{source_resolution or ''}|{source_audio_codec or ''}"
        f"|{target_resolution}|{target_audio_codec}|{encoder}|{crf}|{preset}"
    )
    key_hash = hashlib.blake2b(hash_input.encode(), digest_size=16).hexdigest()
    ext = local_output.suffix.lstrip(".") or "mkv"
    return f"{key_hash}_{encoder}-crf{crf}.{ext}"

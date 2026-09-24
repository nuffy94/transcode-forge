"""Derivative model — represents a cached/reused transcoded output."""

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from transcode_forge.models.job import Job


class Derivative(BaseModel):
    """A cached transcoded output, identified by a goal-keyed content address.

    The key captures the *goal* (what rendition of which source); the recipe
    that produced it (backend, crf, preset) is recorded as attributes only —
    any worker's passing encode satisfies future same-goal requests.
    """

    id: str
    library_id: str
    source_key: str
    source_path: str
    source_resolution: str | None = None
    source_audio_codec: str | None = None
    target_resolution: str
    target_audio_codec: str
    target_codec: str = "hevc"
    target_vmaf: float | None = None
    achieved_vmaf: float | None = None
    backend: str
    crf: int
    preset: str
    derivative_key: str
    output_size: int
    created_at: str


def target_resolution_for(target_height: int | None, source_resolution: str | None) -> str:
    """The derivative's target_resolution: height-keyed for downscale jobs
    ("1080p") so different heights are different derivatives; otherwise the
    source resolution (the pipeline never rescales without target_height).

    ONE home on purpose — the worker's key builder (http_agent) and the
    scheduler's register-derivative row (worker_api) both call this, so the
    stored row can never drift from the key it describes."""
    return f"{target_height}p" if target_height else (source_resolution or "")


def derivative_key_for_job(job: "Job", local_output: Path | str) -> str:
    """Goal-keyed derivative key for a job (D6): source identity +
    target codec/resolution/audio + target VMAF. Recipe details
    (backend/crf/preset) deliberately don't participate, so any worker's
    gate-passing encode satisfies the same goal.

    ONE home on purpose: the worker agent's dedup check and register
    call and S3Backend.commit's upload all use this, so the object in the
    bucket always carries the key the scheduler records."""
    return compute_derivative_key(
        source_path=job.source_path,
        source_resolution=job.source_resolution or "",
        source_audio_codec=getattr(job, "source_audio_codec", "") or "",
        # Height-keyed for downscale jobs (shared rule: the scheduler's
        # register-derivative row uses the same helper). Audio streams
        # are always copied.
        target_resolution=target_resolution_for(job.target_height, job.source_resolution),
        target_audio_codec=getattr(job, "target_audio_codec", "") or "copy",
        target_codec=job.target_codec or "hevc",
        target_vmaf=job.target_vmaf,
        local_output=Path(local_output),
    )


def compute_derivative_key(
    *,
    source_path: str,
    source_resolution: str | None,
    source_audio_codec: str | None,
    target_resolution: str,
    target_audio_codec: str,
    target_codec: str,
    target_vmaf: float | int | None,
    backend: str | None = None,
    crf: int | None = None,
    preset: str | None = None,
    local_output: Path | None = None,
) -> str:
    """Compute the goal-keyed derivative key deterministically.

    The key is a function of the GOAL only: (source identity, target codec,
    target resolution, target audio codec, target VMAF) — all computable at
    queue time, before any CRF search. Recipe details (backend/crf/preset)
    are accepted for caller convenience but deliberately excluded from the
    hash: the VMAF gate guarantees the quality, so "an av1, VMAF≥97
    rendition of this source" is the dedup identity regardless of which
    worker or hardware produced it.

    Returns:
        Goal-keyed derivative filename (e.g., "abc123…_av1.mkv").
    """
    del backend, crf, preset  # recipe attributes — never part of the goal key
    vmaf_part = "" if target_vmaf is None else f"{float(target_vmaf):g}"
    hash_input = (
        f"{source_path}|{source_resolution or ''}|{source_audio_codec or ''}"
        f"|{target_resolution}|{target_audio_codec}|{target_codec}|{vmaf_part}"
    )
    key_hash = hashlib.blake2b(hash_input.encode(), digest_size=16).hexdigest()
    ext = (local_output.suffix.lstrip(".") if local_output else "") or "mkv"
    return f"{key_hash}_{target_codec}.{ext}"

"""VMAF measurement + per-file target-VMAF CRF search.

Two jobs:

1. measure_vmaf() — score an encode against its source with the
   resolution-matched model (vmaf_v0.6.1 ≤1080p, vmaf_4k_v0.6.1 above),
   pooled on worst-scenes (perc5/min) as well as mean. The quality gate in
   pipeline.py NEVER pools on arithmetic mean alone — mean hides bad scenes.

2. find_quality_for_target() — ab-av1-style search: sample-encode short
   clips at candidate qualities, measure VMAF, binary-search the largest
   quality value (= smallest file) that still meets the target. Falls back
   to the fixed preset when disabled or when no candidate qualifies.

Both require ffmpeg built with libvmaf (models are compiled into
libvmaf ≥ 2.0, so `model=version=…` needs no external files). When libvmaf
is missing, VmafUnavailableError is raised — the pipeline logs loudly and
proceeds without the gate (pre-feature behavior), keeping rolling worker
updates safe.
"""

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from transcode_forge.worker.encoder import build_encode_command, run_encode
from transcode_forge.worker.proc import managed_subprocess

logger = logging.getLogger(__name__)

# ffmpeg binary used for MEASUREMENT only (encodes keep using `ffmpeg`).
# Debian's distro ffmpeg ships libsvtav1 but not the libvmaf filter, so the
# worker image carries a second, static ffmpeg for scoring and points this
# env var at it. Default: the regular ffmpeg on PATH.
VMAF_FFMPEG = os.environ.get("TF_VMAF_FFMPEG", "ffmpeg")

VMAF_MODEL_HD = "vmaf_v0.6.1"
VMAF_MODEL_4K = "vmaf_4k_v0.6.1"
# Sources taller than 1080p are scored with the 4K model.
VMAF_4K_MIN_HEIGHT = 1440

# Score every Nth frame. Full-rate VMAF on a 2h movie takes hours on the
# CPU fleet; 1-in-5 keeps full-duration coverage (perc5 stays meaningful)
# at a fifth of the cost.
VMAF_SUBSAMPLE = 5

# CRF search: three clips of this length at these fractions of the runtime.
SAMPLE_SECONDS = 20.0
SAMPLE_OFFSETS = (0.15, 0.50, 0.85)
# Search bounds on the reference quality scale (x265-CRF-like, mapped per
# encoder by build_encode_command). Spans "visually lossless" to "clearly
# beyond any sane archival target".
SEARCH_RANGE = (16, 30)

_SUBPROCESS_TIMEOUT = 4 * 3600.0  # hard stop for a runaway ffmpeg


class VmafError(Exception):
    """VMAF measurement failed (ffmpeg error, unparseable log, …)."""


class VmafUnavailableError(VmafError):
    """ffmpeg has no libvmaf filter — measurement impossible on this worker."""


@dataclass(frozen=True)
class VmafScore:
    """Pooled VMAF metrics for one comparison."""

    mean: float
    perc5: float
    min: float


@dataclass(frozen=True)
class QualitySearchResult:
    """Outcome of the target-VMAF quality search."""

    quality: int  # reference-scale quality that met the target
    predicted_mean: float
    predicted_perc5: float


def select_model(height: int | None) -> str:
    """Pick the resolution-matched VMAF model for a source height."""
    if height is not None and height >= VMAF_4K_MIN_HEIGHT:
        return VMAF_MODEL_4K
    return VMAF_MODEL_HD


def _pool(scores: list[float]) -> VmafScore:
    """Pool per-frame scores: mean, 5th percentile (nearest-rank), min."""
    if not scores:
        raise VmafError("VMAF log contained no frame scores")
    ordered = sorted(scores)
    perc5_idx = max(0, int(0.05 * (len(ordered) - 1)))
    return VmafScore(
        mean=sum(ordered) / len(ordered),
        perc5=ordered[perc5_idx],
        min=ordered[0],
    )


async def has_libvmaf() -> bool:
    """True if the measurement ffmpeg (TF_VMAF_FFMPEG or plain ffmpeg) is
    built with the libvmaf filter."""
    try:
        async with managed_subprocess(
            VMAF_FFMPEG,
            "-hide_banner",
            "-filters",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        ) as proc:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15.0)
    except (TimeoutError, FileNotFoundError, OSError):
        return False
    return b"libvmaf" in stdout


async def measure_vmaf(
    source: str | Path,
    encoded: str | Path,
    *,
    height: int | None = None,
    n_subsample: int = VMAF_SUBSAMPLE,
) -> VmafScore:
    """Score `encoded` against `source` with the resolution-matched model.

    Frame scores are pooled in Python (mean / perc5 / min) from the JSON
    log so the gate can insist on worst-scene quality, not just the mean.

    Raises:
        VmafUnavailableError: If ffmpeg lacks the libvmaf filter.
        VmafError: On measurement failure.
    """
    model = select_model(height)
    with tempfile.TemporaryDirectory(prefix="tf-vmaf-") as tmp:
        log_path = Path(tmp) / "vmaf.json"
        # libvmaf: first input = distorted, second = reference. Both sides
        # are normalized to the same 10-bit format before scoring so an
        # 8-bit source vs 10-bit encode doesn't fail the filter.
        graph = (
            "[0:v]setpts=PTS-STARTPTS,format=yuv420p10le[dis];"
            "[1:v]setpts=PTS-STARTPTS,format=yuv420p10le[ref];"
            "[dis][ref]libvmaf="
            f"model=version={model}"
            f":log_fmt=json:log_path={log_path.as_posix()}"
            f":n_subsample={n_subsample}:n_threads=0"
        )
        cmd = [
            VMAF_FFMPEG,
            "-hide_banner",
            "-i",
            str(encoded),
            "-i",
            str(source),
            "-lavfi",
            graph,
            "-f",
            "null",
            "-",
        ]
        try:
            # managed_subprocess kills the child on timeout, cancellation
            # (worker shutdown), or any error — a full-file VMAF pass runs
            # for minutes-to-hours and must never be orphaned.
            async with managed_subprocess(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            ) as proc:
                try:
                    _, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=_SUBPROCESS_TIMEOUT
                    )
                except TimeoutError as exc:
                    raise VmafError("VMAF measurement timed out") from exc
        except FileNotFoundError as exc:
            raise VmafUnavailableError("ffmpeg binary not found") from exc

        if proc.returncode != 0:
            err = (stderr or b"").decode(errors="replace")
            if "libvmaf" in err and ("No such filter" in err or "Unknown filter" in err):
                raise VmafUnavailableError("ffmpeg is not built with libvmaf")
            raise VmafError(f"VMAF measurement failed (exit {proc.returncode}): {err[-300:]}")

        try:
            data = json.loads(log_path.read_text(encoding="utf-8"))
            scores = [float(frame["metrics"]["vmaf"]) for frame in data["frames"]]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise VmafError(f"Could not parse VMAF log: {exc}") from exc

    score = _pool(scores)
    logger.info(
        "VMAF (%s, 1/%d frames): mean=%.2f perc5=%.2f min=%.2f",
        model,
        n_subsample,
        score.mean,
        score.perc5,
        score.min,
    )
    return score


async def _extract_samples(source: Path, duration: float, out_dir: Path) -> list[Path]:
    """Stream-copy short clips from the source at the sample offsets."""
    # Short file → one sample is the whole thing (still stream-copied so the
    # encode candidates all start from identical input).
    short = duration <= SAMPLE_SECONDS * 2
    offsets = [0.0] if short else [duration * f for f in SAMPLE_OFFSETS]

    samples: list[Path] = []
    for i, offset in enumerate(offsets):
        sample = out_dir / f"sample{i}{source.suffix}"
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-v",
            "error",
            "-ss",
            f"{offset:.2f}",
            "-i",
            str(source),
            "-t",
            f"{SAMPLE_SECONDS:.2f}",
            "-c",
            "copy",
            "-an",
            "-sn",
            "-y",
            str(sample),
        ]
        async with managed_subprocess(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        ) as proc:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300.0)
        if proc.returncode != 0 or not sample.exists() or sample.stat().st_size == 0:
            err = (stderr or b"").decode(errors="replace").strip()[:200]
            raise VmafError(f"Sample extraction failed at {offset:.0f}s: {err or 'empty sample'}")
        samples.append(sample)
    return samples


async def _evaluate_quality(
    samples: list[Path],
    codec: str,
    backend: str,
    quality: int,
    *,
    height: int | None,
    work_dir: Path,
) -> tuple[float, float]:
    """Encode every sample at `quality`, measure VMAF, return
    (mean of means, min of perc5s) — worst-sample-pessimistic on purpose."""
    means: list[float] = []
    perc5s: list[float] = []
    for sample in samples:
        out = work_dir / f"{sample.stem}_q{quality}{sample.suffix}"
        cmd = build_encode_command(codec, backend, str(sample), str(out), quality)
        result = await run_encode(cmd, total_duration=SAMPLE_SECONDS)
        if not result.success:
            raise VmafError(f"Sample encode failed at q={quality}: {result.error_message}")
        # Samples are short — score every frame.
        score = await measure_vmaf(sample, out, height=height, n_subsample=1)
        means.append(score.mean)
        perc5s.append(score.perc5)
    return (sum(means) / len(means), min(perc5s))


async def find_quality_for_target(
    source: str | Path,
    codec: str,
    backend: str,
    *,
    target_vmaf: float,
    perc5_floor: float,
    duration: float,
    height: int | None = None,
) -> QualitySearchResult | None:
    """Binary-search the largest reference quality (smallest file) whose
    sample encodes still meet mean ≥ target_vmaf AND perc5 ≥ perc5_floor.

    Returns None when even the best-quality end of the range can't meet the
    target (the caller falls back to the fixed preset and lets the full-file
    gate decide) — a search failure never blocks the encode.

    Raises:
        VmafUnavailableError: If ffmpeg lacks libvmaf (caller should fall
            back to the fixed preset).
    """
    src = Path(source)
    lo, hi = SEARCH_RANGE
    with tempfile.TemporaryDirectory(prefix="tf-crf-search-") as tmp:
        work_dir = Path(tmp)
        samples = await _extract_samples(src, duration, work_dir)

        async def meets(q: int) -> tuple[bool, float, float]:
            mean, perc5 = await _evaluate_quality(
                samples, codec, backend, q, height=height, work_dir=work_dir
            )
            ok = mean >= target_vmaf and perc5 >= perc5_floor
            logger.info(
                "CRF search q=%d → mean=%.2f perc5=%.2f (%s)",
                q,
                mean,
                perc5,
                "meets target" if ok else "below target",
            )
            return ok, mean, perc5

        best: QualitySearchResult | None = None
        ok, mean, perc5 = await meets(lo)
        if not ok:
            logger.info(
                "CRF search: even q=%d misses VMAF %.1f (mean=%.2f perc5=%.2f) — "
                "falling back to the fixed preset",
                lo,
                target_vmaf,
                mean,
                perc5,
            )
            return None
        best = QualitySearchResult(quality=lo, predicted_mean=mean, predicted_perc5=perc5)

        while lo < hi:
            mid = (lo + hi + 1) // 2
            ok, mean, perc5 = await meets(mid)
            if ok:
                best = QualitySearchResult(quality=mid, predicted_mean=mean, predicted_perc5=perc5)
                lo = mid
            else:
                hi = mid - 1

    logger.info(
        "CRF search resolved q=%d (predicted mean=%.2f perc5=%.2f)",
        best.quality,
        best.predicted_mean,
        best.predicted_perc5,
    )
    return best

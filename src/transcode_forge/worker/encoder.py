"""ffmpeg transcoding engine — build commands and parse progress."""

import asyncio
import logging
import re
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from transcode_forge.worker.proc import managed_subprocess

logger = logging.getLogger(__name__)

PROGRESS_RE = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")
SPEED_RE = re.compile(r"speed=\s*([\d.]+)x")

# Lines emitted by `-progress pipe:2` — useless noise in error diagnostics.
_PROGRESS_KEYS = (
    "frame=",
    "fps=",
    "stream_",
    "bitrate=",
    "total_size=",
    "out_time_us=",
    "out_time_ms=",
    "out_time=",
    "dup_frames=",
    "drop_frames=",
    "speed=",
    "progress=",
)

ERROR_LINES_BUFFER = 10
DEFAULT_PROGRESS_INTERVAL = 2.0


@dataclass(frozen=True)
class EncodeResult:
    """Result of a single ffmpeg encode operation."""

    success: bool
    output_path: str
    output_size: int
    returncode: int
    error_message: str | None = None


# ── Quality mapping ────────────────────────────────────────────────────
#
# `quality` everywhere in this project is on the x265-CRF reference scale
# (the historical TF_QUALITY_* presets). Feeding that one number verbatim
# to every encoder produces three different qualities — nvenc -cq 21 is
# roughly x265 crf ~8, i.e. massively bloated. Each (codec, backend) maps
# the reference value onto its native scale instead. Offsets come from the
# VMAF-matched research in plans/codec-quality-defaults.md:
#   hevc/nvenc  cq  ≈ crf + 11   (rigorous match: cq 33.4 ↔ crf 20.6)
#   av1/cpu     crf ≈ crf + 7    (SVT-AV1 crf 27 ≈ x265 crf 20)
#   av1/nvenc   cq  ≈ crf + 6
#   av1/qsv     gq  ≈ crf + 4
# (offset, min, max) per pair — clamped to the encoder's native range.
_QUALITY_MAP: dict[tuple[str, str], tuple[int, int, int]] = {
    ("hevc", "cpu"): (0, 0, 51),
    ("hevc", "qsv"): (0, 1, 51),
    ("hevc", "nvenc"): (11, 0, 51),
    ("av1", "cpu"): (7, 0, 63),
    ("av1", "nvenc"): (6, 0, 51),
    ("av1", "qsv"): (4, 1, 51),
}


def map_quality(codec: str, backend: str, quality: int) -> int:
    """Map a reference-scale quality value onto the native scale of the
    (codec, backend) encoder. Raises ValueError for unknown pairs."""
    entry = _QUALITY_MAP.get((codec, backend))
    if entry is None:
        raise ValueError(
            f"Unknown (codec, backend) pair: ({codec}, {backend})."
            f" Valid: {sorted(_QUALITY_MAP.keys())}"
        )
    offset, lo, hi = entry
    return max(lo, min(hi, quality + offset))


def _scale_args(target_height: int | None) -> list[str]:
    """`-vf scale=-2:H` when the job carries a downscale: height fixed,
    width auto and always even, aspect preserved. Software scale feeds all
    three backends — frames pass through system memory in every builder
    here (hardware vpp_qsv/scale_cuda is a later optimization, not v1)."""
    if target_height is None:
        return []
    return ["-vf", f"scale=-2:{target_height}"]


# Shared tail: copy audio/subs, keep every stream, newline-terminated
# progress on stderr (default rolling stats use \r which readline() never
# returns until the process exits).
_COMMON_TAIL = [
    "-c:a",
    "copy",
    "-c:s",
    "copy",
    "-map",
    "0",
    "-progress",
    "pipe:2",
    "-nostats",
    "-y",
]


def build_hevc_cpu_command(
    input_path: str,
    output_path: str,
    quality: int,
    content: str | None = None,
    target_height: int | None = None,
) -> list[str]:
    """Software x265. Preset slow: this is a replace-the-original archival
    encode — quality-per-byte beats throughput. 10-bit output kills banding
    even from 8-bit sources. Anime gets aq-mode=3 (mandatory for banding)."""
    x265_params = ["-x265-params", "aq-mode=3"] if content == "anime" else []
    return [
        "ffmpeg",
        "-i",
        input_path,
        "-c:v",
        "libx265",
        "-crf",
        str(map_quality("hevc", "cpu", quality)),
        "-preset",
        "slow",
        *x265_params,
        *_scale_args(target_height),
        "-pix_fmt",
        "yuv420p10le",
        *_COMMON_TAIL,
        output_path,
    ]


def build_hevc_qsv_command(
    input_path: str,
    output_path: str,
    quality: int,
    content: str | None = None,
    target_height: int | None = None,
) -> list[str]:
    """Intel QSV HEVC. Decodes via QSV to system memory (no
    -hwaccel_output_format qsv) so the 8→10-bit p010le conversion happens
    before upload — required for Main10 output. Skylake-and-older iGPUs
    can't encode 10-bit HEVC; capability detection probes with p010le so
    such nodes never advertise qsv."""
    return [
        "ffmpeg",
        "-hwaccel",
        "qsv",
        "-hwaccel_device",
        "/dev/dri/renderD128",
        "-i",
        input_path,
        "-c:v",
        "hevc_qsv",
        "-global_quality",
        str(map_quality("hevc", "qsv", quality)),
        "-preset",
        "fast",
        "-look_ahead",
        "1",
        "-low_power",
        "0",
        *_scale_args(target_height),
        "-pix_fmt",
        "p010le",
        *_COMMON_TAIL,
        output_path,
    ]


def build_hevc_nvenc_command(
    input_path: str,
    output_path: str,
    quality: int,
    content: str | None = None,
    target_height: int | None = None,
) -> list[str]:
    """NVIDIA NVENC HEVC — the modern VBR+cq/p7/10-bit recipe. -b:v 0 makes
    -cq the sole rate control (true constant quality)."""
    return [
        "ffmpeg",
        "-hwaccel",
        "cuda",
        "-i",
        input_path,
        "-c:v",
        "hevc_nvenc",
        "-cq",
        str(map_quality("hevc", "nvenc", quality)),
        "-preset",
        "p7",
        "-tune",
        "hq",
        "-rc",
        "vbr",
        "-b:v",
        "0",
        *_scale_args(target_height),
        "-pix_fmt",
        "p010le",
        *_COMMON_TAIL,
        output_path,
    ]


def build_av1_cpu_command(
    input_path: str,
    output_path: str,
    quality: int,
    content: str | None = None,
    target_height: int | None = None,
) -> list[str]:
    """SVT-AV1 — the real AV1 path for the CPU fleet. tune=0 (VQ mode),
    scm=0 (film content, not screen content)."""
    return [
        "ffmpeg",
        "-i",
        input_path,
        "-c:v",
        "libsvtav1",
        "-crf",
        str(map_quality("av1", "cpu", quality)),
        "-preset",
        "6",
        "-svtav1-params",
        "tune=0:scm=0",
        *_scale_args(target_height),
        "-pix_fmt",
        "yuv420p10le",
        *_COMMON_TAIL,
        output_path,
    ]


def build_av1_nvenc_command(
    input_path: str,
    output_path: str,
    quality: int,
    content: str | None = None,
    target_height: int | None = None,
) -> list[str]:
    """NVIDIA NVENC AV1 (Ada / RTX 40xx+)."""
    return [
        "ffmpeg",
        "-hwaccel",
        "cuda",
        "-i",
        input_path,
        "-c:v",
        "av1_nvenc",
        "-cq",
        str(map_quality("av1", "nvenc", quality)),
        "-preset",
        "p7",
        "-tune",
        "hq",
        "-rc",
        "vbr",
        "-b:v",
        "0",
        "-multipass",
        "fullres",
        *_scale_args(target_height),
        "-pix_fmt",
        "p010le",
        *_COMMON_TAIL,
        output_path,
    ]


def build_av1_qsv_command(
    input_path: str,
    output_path: str,
    quality: int,
    content: str | None = None,
    target_height: int | None = None,
) -> list[str]:
    """Intel QSV AV1 (Arc / gen12+). Detection-gated seam — no such
    hardware in the current fleet, but the builder is real."""
    return [
        "ffmpeg",
        "-hwaccel",
        "qsv",
        "-hwaccel_device",
        "/dev/dri/renderD128",
        "-i",
        input_path,
        "-c:v",
        "av1_qsv",
        "-global_quality",
        str(map_quality("av1", "qsv", quality)),
        "-preset",
        "veryslow",
        *_scale_args(target_height),
        "-pix_fmt",
        "p010le",
        *_COMMON_TAIL,
        output_path,
    ]


# Two-axis lookup: (codec, backend) → builder. Adding VP9/AV2 later is a
# new codec value plus builder entries here — nothing structural.
ENCODER_BUILDERS: dict[tuple[str, str], Callable[..., list[str]]] = {
    ("hevc", "cpu"): build_hevc_cpu_command,
    ("hevc", "qsv"): build_hevc_qsv_command,
    ("hevc", "nvenc"): build_hevc_nvenc_command,
    ("av1", "cpu"): build_av1_cpu_command,
    ("av1", "nvenc"): build_av1_nvenc_command,
    ("av1", "qsv"): build_av1_qsv_command,
}


def build_encode_command(
    codec: str,
    backend: str,
    input_path: str,
    output_path: str,
    quality: int,
    *,
    content: str | None = None,
    target_height: int | None = None,
) -> list[str]:
    """Build the ffmpeg command for the given (codec, backend) pair.

    Args:
        codec: Target codec ('hevc' | 'av1').
        backend: Hardware axis ('cpu' | 'qsv' | 'nvenc').
        quality: Reference-scale quality (x265-CRF-like); mapped per encoder.
        content: Optional content hint ('anime' enables x265 aq-mode=3).
        target_height: Downscale height (`scale=-2:H`); None = keep source
            resolution (pre-feature identical).
    """
    builder = ENCODER_BUILDERS.get((codec, backend))
    if builder is None:
        raise ValueError(
            f"Unknown (codec, backend) pair: ({codec}, {backend})."
            f" Valid: {sorted(ENCODER_BUILDERS.keys())}"
        )
    return builder(input_path, output_path, quality, content, target_height)


def parse_progress(line: str, total_duration: float) -> float | None:
    """Parse ffmpeg stderr line and return progress as 0.0-1.0, or None if not a progress line."""
    if total_duration <= 0:
        return None
    match = PROGRESS_RE.search(line)
    if not match:
        return None
    h, m, s = int(match.group(1)), int(match.group(2)), float(match.group(3))
    current = h * 3600 + m * 60 + s
    return min(current / total_duration, 1.0)


def parse_speed(line: str) -> float | None:
    """Parse ffmpeg stderr line and return encode speed multiplier (e.g. 2.1x -> 2.1)."""
    match = SPEED_RE.search(line)
    if not match:
        return None
    return float(match.group(1))


async def run_encode(
    cmd: list[str],
    total_duration: float,
    progress_callback: Callable[[float, float | None], Coroutine[Any, Any, None]] | None = None,
    progress_interval: float = DEFAULT_PROGRESS_INTERVAL,
) -> EncodeResult:
    """Run an ffmpeg encode command, streaming progress updates.

    Args:
        cmd: Full ffmpeg command list.
        total_duration: Source file duration in seconds (for progress calculation).
        progress_callback: Async callable(progress: float, speed: float | None)
                          called periodically during encoding.
        progress_interval: Minimum seconds between progress callbacks.

    Returns:
        EncodeResult with success flag, output path, and size.
    """
    output_path = cmd[-1]  # Last arg is always the output file
    logger.info("Starting encode: %s", " ".join(cmd[:6]) + " ...")

    last_callback_time = 0.0
    error_lines: list[str] = []

    try:
        # managed_subprocess guarantees the ffmpeg process tree dies if this
        # coroutine is cancelled (worker shutdown/abort) or errors out — a
        # cancelled encode must never leave an orphaned ffmpeg behind.
        async with managed_subprocess(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=1024 * 1024,  # 1MB line buffer (ffmpeg metadata can be huge)
        ) as proc:
            # Validate stderr is available (should always be true with PIPE, but be safe)
            if proc.stderr is None:
                return EncodeResult(
                    success=False,
                    output_path=output_path,
                    output_size=0,
                    returncode=-1,
                    error_message="ffmpeg subprocess stderr not available",
                )

            # Read stderr line by line for progress
            while True:
                try:
                    line_bytes = await proc.stderr.readline()
                except ValueError:
                    # Line exceeded buffer limit — skip it
                    continue
                if not line_bytes:
                    break
                line = line_bytes.decode(errors="replace").strip()
                if not line:
                    continue

                # Capture potential error lines (last N), skipping progress key=value
                # spam from -progress pipe:2 so failure diagnostics stay useful.
                if not line.startswith(_PROGRESS_KEYS):
                    error_lines.append(line)
                    if len(error_lines) > ERROR_LINES_BUFFER:
                        error_lines.pop(0)

                # Parse progress
                progress = parse_progress(line, total_duration)
                if progress is not None and progress_callback is not None:
                    now = time.monotonic()
                    if now - last_callback_time >= progress_interval:
                        speed = parse_speed(line)
                        await progress_callback(progress, speed)
                        last_callback_time = now

            await proc.wait()
    except FileNotFoundError:
        return EncodeResult(
            success=False,
            output_path=output_path,
            output_size=0,
            returncode=-1,
            error_message="ffmpeg binary not found",
        )

    out_path = Path(output_path)
    output_size = out_path.stat().st_size if out_path.exists() else 0

    if proc.returncode != 0:
        error_msg = "\n".join(error_lines[-5:])
        logger.error("Encode failed (exit %d): %s", proc.returncode, error_msg)
        return EncodeResult(
            success=False,
            output_path=output_path,
            output_size=output_size,
            returncode=proc.returncode or 1,
            error_message=error_msg,
        )

    logger.info("Encode complete: %s (%d bytes)", output_path, output_size)
    return EncodeResult(
        success=True,
        output_path=output_path,
        output_size=output_size,
        returncode=0,
    )

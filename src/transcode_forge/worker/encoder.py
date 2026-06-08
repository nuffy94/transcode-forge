"""ffmpeg transcoding engine — build commands and parse progress."""

import asyncio
import logging
import re
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


def build_qsv_command(input_path: str, output_path: str, quality: int) -> list[str]:
    """Build ffmpeg command for Intel QSV (hevc_qsv) encoding.

    Uses -hwaccel qsv with explicit device path for Linux VAAPI-backed QSV.
    Preset 'fast' matches common defaults — the QSV difference between
    'slow' and 'fast' is ~3x in throughput for ~0.5dB PSNR. We're not
    archival mastering here, we're just shrinking h264 to hevc.
    """
    return [
        "ffmpeg",
        "-hwaccel",
        "qsv",
        "-hwaccel_device",
        "/dev/dri/renderD128",
        "-hwaccel_output_format",
        "qsv",
        "-i",
        input_path,
        "-c:v",
        "hevc_qsv",
        "-global_quality",
        str(quality),
        "-preset",
        "fast",
        "-look_ahead",
        "0",
        "-low_power",
        "0",
        "-c:a",
        "copy",
        "-c:s",
        "copy",
        "-map",
        "0",
        # Newline-terminated progress on stderr; default rolling stats use \r
        # which readline() never returns until the process exits.
        "-progress",
        "pipe:2",
        "-nostats",
        "-y",
        output_path,
    ]


def build_nvenc_command(input_path: str, output_path: str, quality: int) -> list[str]:
    """Build ffmpeg command for NVIDIA NVENC (hevc_nvenc) encoding."""
    return [
        "ffmpeg",
        "-hwaccel",
        "cuda",
        "-hwaccel_output_format",
        "cuda",
        "-i",
        input_path,
        "-c:v",
        "hevc_nvenc",
        "-cq",
        str(quality),
        "-preset",
        "p7",
        "-tune",
        "hq",
        "-rc",
        "vbr",
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
        output_path,
    ]


def build_software_command(input_path: str, output_path: str, quality: int) -> list[str]:
    """Build ffmpeg command for software x265 (libx265) encoding.

    Preset 'fast' uses every available CPU thread well and is a
    common default. 'medium' and 'slow' more than double the encode
    time for marginal PSNR gains.
    """
    return [
        "ffmpeg",
        "-i",
        input_path,
        "-c:v",
        "libx265",
        "-crf",
        str(quality),
        "-preset",
        "fast",
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
        output_path,
    ]


ENCODER_BUILDERS = {
    "qsv": build_qsv_command,
    "nvenc": build_nvenc_command,
    "cpu": build_software_command,
}


def build_encode_command(
    encoder: str, input_path: str, output_path: str, quality: int
) -> list[str]:
    """Build the ffmpeg command for the given encoder type."""
    builder = ENCODER_BUILDERS.get(encoder)
    if builder is None:
        raise ValueError(f"Unknown encoder: {encoder}. Valid: {list(ENCODER_BUILDERS.keys())}")
    return builder(input_path, output_path, quality)


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

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=1024 * 1024,  # 1MB line buffer (ffmpeg metadata can be huge)
        )
    except FileNotFoundError:
        return EncodeResult(
            success=False,
            output_path=output_path,
            output_size=0,
            returncode=-1,
            error_message="ffmpeg binary not found",
        )

    # Validate stderr is available (should always be true with PIPE, but be safe)
    if proc.stderr is None:
        return EncodeResult(
            success=False,
            output_path=output_path,
            output_size=0,
            returncode=-1,
            error_message="ffmpeg subprocess stderr not available",
        )

    last_callback_time = 0.0
    error_lines: list[str] = []

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

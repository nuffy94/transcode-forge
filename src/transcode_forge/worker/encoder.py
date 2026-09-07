"""ffmpeg transcoding engine — build commands and parse progress."""

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable, Coroutine, Sequence
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

# Warning/banner spam that drowned the real error in stored messages —
# swscaler deprecation chatter (from cover-art JPEG conversion), x265
# banners, container NUMA noise, ffmpeg's console hint. The fatal line
# ("Error initializing output stream…") was pushed out of the ring by
# these, which is why failed jobs read as gibberish (observed 2026-07-20).
# Prefixes are level-scoped or static banners; the swscaler case matches
# the specific benign MESSAGE, not the bare "[swscaler" component tag —
# a genuine fatal swscaler line must stay in the ring (review of #88).
_NOISE_PREFIXES = (
    "x265 [info]:",
    "set_mempolicy:",
    "Press [q] to stop",
)
_NOISE_SUBSTRINGS = ("deprecated pixel format used",)


def _is_noise(line: str) -> bool:
    """True for known-benign stderr spam excluded from the error ring."""
    if line.startswith(_PROGRESS_KEYS) or line.startswith(_NOISE_PREFIXES):
        return True
    return any(s in line for s in _NOISE_SUBSTRINGS)


ERROR_LINES_BUFFER = 10
DEFAULT_PROGRESS_INTERVAL = 2.0
# An encode has no sensible wall-clock bound (CPU x265 on 4K can run for a
# day), so its deadline slides: ffmpeg writes a progress block about every
# half second while it is alive, and this many seconds of silence means it
# is wedged (GPU driver, dead NFS mount) and gets killed (ledger R-001).
ENCODE_STALL_SECONDS = 15 * 60.0


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
    # quadra offsets are PLACEHOLDERS (0) until the S4c calibration pass
    # (plans/vpu-bench-spec.md, Phase 2 step 1) — do NOT trust quality
    # parity with x265 before then.
    ("hevc", "quadra"): (0, 0, 51),
    ("av1", "quadra"): (0, 0, 51),
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


# Shared tail: FIRST real video stream only (0:V:0 — capital V excludes
# attached cover art, which `-map 0` used to feed to the video encoder,
# killing the whole encode on the JPEG's dimensions; observed fleet-wide
# 2026-07-20), all audio/subtitle/attachment streams, unknown-TYPE
# streams dropped instead of fatal. Data-type (d) streams are dropped
# DELIBERATELY: mkv's data-stream muxing support is spotty and carrying
# them would reintroduce the exotic-stream failure class this mapping
# exists to end (review of #88 — the original file keeps everything).
# Copy audio/subs. Newline-terminated progress on stderr (default
# rolling stats use \r which readline() never returns until the process
# exits).
_COMMON_TAIL = [
    "-c:a",
    "copy",
    "-c:s",
    "copy",
    "-map",
    "0:V:0",
    "-map",
    "0:a?",
    "-map",
    "0:s?",
    "-map",
    "0:t?",
    "-ignore_unknown",
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


def build_hevc_quadra_command(
    input_path: str,
    output_path: str,
    quality: int,
    content: str | None = None,
    target_height: int | None = None,
) -> list[str]:
    """NETINT Quadra ASIC HEVC (h265_ni_quadra_enc). The encode runs fully
    off-CPU on the T1U; frames pass through system memory like every builder
    here. RcEnable=0 is required for CRF — without it the default rate
    controller runs and crf= is ignored (NETINT capped-CRF app note)."""
    return [
        "ffmpeg",
        "-i",
        input_path,
        "-c:v",
        "h265_ni_quadra_enc",
        "-xcoder-params",
        f"RcEnable=0:crf={map_quality('hevc', 'quadra', quality)}",
        *_scale_args(target_height),
        "-pix_fmt",
        "yuv420p10le",
        *_COMMON_TAIL,
        output_path,
    ]


def build_av1_quadra_command(
    input_path: str,
    output_path: str,
    quality: int,
    content: str | None = None,
    target_height: int | None = None,
) -> list[str]:
    """NETINT Quadra ASIC AV1 — 10-bit is supported (AV1 Main, 8+10-bit
    per the T1U datasheet), so the pipeline's 10-bit norm holds here too.
    RcEnable=0 required for CRF, same as the HEVC builder."""
    return [
        "ffmpeg",
        "-i",
        input_path,
        "-c:v",
        "av1_ni_quadra_enc",
        "-xcoder-params",
        f"RcEnable=0:crf={map_quality('av1', 'quadra', quality)}",
        *_scale_args(target_height),
        "-pix_fmt",
        "yuv420p10le",
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
    ("hevc", "quadra"): build_hevc_quadra_command,
    ("av1", "quadra"): build_av1_quadra_command,
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
    drop_sub_streams: Sequence[int] = (),
) -> list[str]:
    """Build the ffmpeg command for the given (codec, backend) pair.

    Args:
        codec: Target codec ('hevc' | 'av1').
        backend: Hardware axis ('cpu' | 'qsv' | 'nvenc' | 'quadra').
        quality: Reference-scale quality (x265-CRF-like); mapped per encoder.
        content: Optional content hint ('anime' enables x265 aq-mode=3).
        target_height: Downscale height (`scale=-2:H`); None = keep source
            resolution (pre-feature identical).
        drop_sub_streams: Per-type subtitle indexes to exclude via
            negative mapping (see unmuxable_subtitle_indexes).
    """
    builder = ENCODER_BUILDERS.get((codec, backend))
    if builder is None:
        raise ValueError(
            f"Unknown (codec, backend) pair: ({codec}, {backend})."
            f" Valid: {sorted(ENCODER_BUILDERS.keys())}"
        )
    cmd = builder(input_path, output_path, quality, content, target_height)
    if drop_sub_streams:
        # Negative maps must follow the inclusive maps — insert just
        # before the shared tail's -progress marker.
        i = cmd.index("-progress")
        for n in drop_sub_streams:
            cmd[i:i] = ["-map", f"-0:s:{n}"]
            i += 2
    return cmd


async def unmuxable_subtitle_indexes(input_path: str) -> list[int]:
    """Per-type indexes of subtitle streams whose codec ffprobe cannot
    name (codec_id 0 / damaged metadata). matroska refuses to stream-copy
    those and fails the WHOLE encode ("Subtitle codec 0 is not
    supported" — observed fleet-wide 2026-07-20); the pipeline drops
    them via negative mapping with a loud log line instead. Fail-open:
    any probe error returns [] and the encode proceeds exactly as
    before."""
    try:
        async with managed_subprocess(
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "s",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "json",
            input_path,
            timeout=60.0,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        ) as child:
            out, _ = await child.proc.communicate()
        streams = json.loads(out or b"{}").get("streams", [])
        return [
            i for i, s in enumerate(streams) if s.get("codec_name") in (None, "", "none", "unknown")
        ]
    except Exception:
        logger.warning("Subtitle probe failed for %s — mapping all streams", input_path)
        return []


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
            timeout=ENCODE_STALL_SECONDS,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=1024 * 1024,  # 1MB line buffer (ffmpeg metadata can be huge)
        ) as child:
            proc = child.proc
            # Validate stderr is available (should always be true with PIPE, but be safe)
            if proc.stderr is None:
                return EncodeResult(
                    success=False,
                    output_path=output_path,
                    output_size=0,
                    returncode=-1,
                    error_message="ffmpeg subprocess stderr not available",
                )

            # Read stderr line by line for progress. Each line re-arms the
            # deadline: the encode dies after ENCODE_STALL_SECONDS of silence,
            # never for merely being slow.
            while True:
                child.extend()
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
                if not _is_noise(line):
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
    except TimeoutError:
        tail = "\n".join(error_lines[-3:])
        logger.error("Encode stalled: no ffmpeg output for %gs, killed it", ENCODE_STALL_SECONDS)
        return EncodeResult(
            success=False,
            output_path=output_path,
            output_size=0,
            returncode=-1,
            error_message=f"ffmpeg went silent for {ENCODE_STALL_SECONDS:g}s and was killed"
            + (f"\n{tail}" if tail else ""),
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

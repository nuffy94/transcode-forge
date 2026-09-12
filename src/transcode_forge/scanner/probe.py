"""ffprobe wrapper — extract video metadata from media files."""

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from transcode_forge.worker.proc import managed_subprocess

logger = logging.getLogger(__name__)

FFPROBE_TIMEOUT = 15  # seconds per file
VIDEO_EXTENSIONS = frozenset(
    {
        ".mkv",
        ".mp4",
        ".avi",
        ".wmv",
        ".flv",
        ".mov",
        ".m4v",
        ".ts",
        ".m2ts",
        ".webm",
    }
)


def format_resolution(width: int, height: int) -> str:
    """The catalog's resolution string ("1920x1080"). One home so the
    scanner and the job-complete catalog sync write the same shape."""
    return f"{width}x{height}"


@dataclass(frozen=True)
class ProbeResult:
    """Parsed ffprobe output for a single video file."""

    video_codec: str
    width: int
    height: int
    bitrate: int | None
    duration: float
    file_size: int
    pix_fmt: str = ""  # e.g. "yuv420p" (8-bit), "yuv420p10le" (10-bit)

    @property
    def resolution(self) -> str:
        return format_resolution(self.width, self.height)

    @property
    def is_10bit(self) -> bool:
        """True if the source uses a 10-bit pixel format.

        Skylake-and-older Intel iGPUs cannot encode 10-bit hevc_qsv
        — the encode fails with 'Current pixel format is unsupported'.
        Routed to libx265 instead in pipeline.py.
        """
        return "10" in self.pix_fmt or "p010" in self.pix_fmt


# ffprobe spells "nobody tagged a language" two ways: matroska stores the
# literal "und", mp4 and mkv both leave the tag off entirely. Both mean the
# same thing, so both normalize to "" before they reach an inventory key.
_UNTAGGED_LANGUAGES = frozenset({"und", "unknown"})


@dataclass(frozen=True)
class StreamInfo:
    """One stream of a media file, reduced to the fields that decide
    whether it survived a transcode.

    Deliberately excluded: title, and every disposition except `forced`.
    A copy through a muxer rewrites those (matroska promotes the first
    video stream to default, measured 2026-09-12), so keying on them
    would report a loss that did not happen.
    """

    index: int
    codec_type: str
    codec_name: str
    language: str
    attached_pic: bool
    forced: bool


class ProbeError(Exception):
    """Raised when ffprobe fails or returns unexpected output."""


async def ffprobe(path: str | Path) -> ProbeResult:
    """Run ffprobe on a media file and parse the result.

    Args:
        path: Path to the media file.

    Returns:
        ProbeResult with video codec, resolution, bitrate, duration, file size.

    Raises:
        ProbeError: If ffprobe fails or output cannot be parsed.
        FileNotFoundError: If the file does not exist.
    """
    # Presigned S3 probes pass an http(s) URL — Path()-ifying one mangles
    # '//' and fails exists(), so only local inputs get the Path treatment.
    local: Path | None = None
    if isinstance(path, str) and path.startswith(("http://", "https://")):
        target = path
    else:
        local = Path(path)
        if not local.exists():
            raise FileNotFoundError(f"File not found: {local}")
        target = str(local)

    cmd = [
        "ffprobe",
        "-v",
        "error",  # not 'quiet': failures must carry a reason on stderr
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-select_streams",
        "v:0",
        target,
    ]

    try:
        async with managed_subprocess(
            *cmd,
            timeout=FFPROBE_TIMEOUT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        ) as child:
            stdout, stderr = await child.proc.communicate()
    except TimeoutError as exc:
        raise ProbeError(f"ffprobe timed out after {FFPROBE_TIMEOUT}s: {target}") from exc
    except FileNotFoundError as exc:
        raise ProbeError("ffprobe binary not found — is ffmpeg installed?") from exc

    proc = child.proc
    if proc.returncode != 0:
        raise ProbeError(f"ffprobe failed (exit {proc.returncode}): {stderr.decode().strip()}")

    try:
        data = json.loads(stdout.decode())
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe returned invalid JSON: {target}") from exc

    streams = data.get("streams", [])
    if not streams:
        raise ProbeError(f"No video streams found: {target}")

    stream = streams[0]
    fmt = data.get("format", {})

    codec = stream.get("codec_name", "unknown")
    width = int(stream.get("width", 0))
    height = int(stream.get("height", 0))

    # Bitrate: prefer stream-level, fall back to format-level
    raw_bitrate = stream.get("bit_rate") or fmt.get("bit_rate")
    bitrate = int(raw_bitrate) if raw_bitrate else None

    # Duration: prefer format-level (more reliable for containers)
    raw_duration = fmt.get("duration") or stream.get("duration")
    if not raw_duration:
        raise ProbeError(f"Could not determine duration: {target}")
    duration = float(raw_duration)

    # URLs have no stat(); S3 callers overwrite file_size with the listed
    # object size anyway (the partial-download fallback would report the
    # temp file's size otherwise).
    file_size = int(fmt.get("size", 0)) or (local.stat().st_size if local is not None else 0)

    return ProbeResult(
        video_codec=codec,
        width=width,
        height=height,
        bitrate=bitrate,
        duration=duration,
        file_size=file_size,
        pix_fmt=stream.get("pix_fmt", ""),
    )


async def stream_inventory(path: str | Path) -> tuple[StreamInfo, ...]:
    """Every stream in the file, in index order.

    This is the reference the transcode pipeline holds its encode plan
    against, so it never fails open: a file whose streams cannot be listed
    is a file whose content nothing can promise to keep, and the caller
    must stop rather than transcode blind.

    Raises:
        ProbeError: If ffprobe fails, times out, or returns invalid JSON.
    """
    target = str(path)
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        target,
    ]

    try:
        async with managed_subprocess(
            *cmd,
            timeout=FFPROBE_TIMEOUT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        ) as child:
            stdout, stderr = await child.proc.communicate()
    except TimeoutError as exc:
        raise ProbeError(f"ffprobe timed out after {FFPROBE_TIMEOUT}s: {target}") from exc
    except FileNotFoundError as exc:
        raise ProbeError("ffprobe binary not found, is ffmpeg installed?") from exc

    if child.proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        raise ProbeError(f"ffprobe failed (exit {child.proc.returncode}): {detail}")

    try:
        data = json.loads(stdout.decode(errors="replace"))
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe returned invalid JSON: {target}") from exc

    inventory: list[StreamInfo] = []
    for stream in data.get("streams", []):
        disposition = stream.get("disposition") or {}
        language = str((stream.get("tags") or {}).get("language") or "").strip().lower()
        inventory.append(
            StreamInfo(
                index=int(stream.get("index", len(inventory))),
                codec_type=str(stream.get("codec_type") or "unknown"),
                codec_name=str(stream.get("codec_name") or "unknown"),
                language="" if language in _UNTAGGED_LANGUAGES else language,
                attached_pic=bool(disposition.get("attached_pic")),
                forced=bool(disposition.get("forced")),
            )
        )
    return tuple(inventory)


def is_video_file(path: Path) -> bool:
    """Check if a file has a recognized video extension."""
    return path.suffix.lower() in VIDEO_EXTENSIONS


# The transcode pipeline's sidecar markers (worker/storage/filesystem.py:
# LOCK_SUFFIX / TMP_SUFFIX / BAK_SUFFIX — the on-disk format is frozen).
# Imported here as literals to keep the scheduler-side scanner decoupled
# from the worker package.
_PIPELINE_ARTIFACT_MARKERS = (".tf_lock", ".tf_tmp", ".tf_bak")


def is_pipeline_artifact(path: Path) -> bool:
    """True for the pipeline's sidecar files (movie.tf_bak.mkv,
    movie.tf_tmp.mkv, movie.mkv.tf_lock[.new]).

    These carry real media extensions, so the extension check alone would
    catalog them — phantom rows, and a cataloged backup is one queue click
    from being transcoded."""
    return any(marker in path.name for marker in _PIPELINE_ARTIFACT_MARKERS)

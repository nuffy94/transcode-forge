"""ffprobe wrapper — extract video metadata from media files."""

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

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
        return f"{self.width}x{self.height}"

    @property
    def is_10bit(self) -> bool:
        """True if the source uses a 10-bit pixel format.

        Skylake-and-older Intel iGPUs cannot encode 10-bit hevc_qsv
        — the encode fails with 'Current pixel format is unsupported'.
        Routed to libx265 instead in pipeline.py.
        """
        return "10" in self.pix_fmt or "p010" in self.pix_fmt


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
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=FFPROBE_TIMEOUT)
    except TimeoutError as exc:
        # Ensure process is terminated on timeout
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        raise ProbeError(f"ffprobe timed out after {FFPROBE_TIMEOUT}s: {target}") from exc
    except FileNotFoundError as exc:
        raise ProbeError("ffprobe binary not found — is ffmpeg installed?") from exc

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

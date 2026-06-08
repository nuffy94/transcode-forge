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
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-select_streams",
        "v:0",
        str(path),
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
        raise ProbeError(f"ffprobe timed out after {FFPROBE_TIMEOUT}s: {path}") from exc
    except FileNotFoundError as exc:
        raise ProbeError("ffprobe binary not found — is ffmpeg installed?") from exc

    if proc.returncode != 0:
        raise ProbeError(f"ffprobe failed (exit {proc.returncode}): {stderr.decode().strip()}")

    try:
        data = json.loads(stdout.decode())
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe returned invalid JSON: {path}") from exc

    streams = data.get("streams", [])
    if not streams:
        raise ProbeError(f"No video streams found: {path}")

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
        raise ProbeError(f"Could not determine duration: {path}")
    duration = float(raw_duration)

    file_size = int(fmt.get("size", 0)) or path.stat().st_size

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

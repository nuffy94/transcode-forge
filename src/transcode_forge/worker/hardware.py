"""Hardware acceleration detection — runs once at worker startup."""

import asyncio
import logging
import platform
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HardwareCapabilities:
    """Detected hardware encoding capabilities for this node."""

    encoders: list[str]  # e.g. ["qsv", "nvenc", "cpu"]
    ffmpeg_version: str
    os_platform: str

    @property
    def has_qsv(self) -> bool:
        return "qsv" in self.encoders

    @property
    def has_nvenc(self) -> bool:
        return "nvenc" in self.encoders

    def best_encoder(self, preferred: str = "auto") -> str:
        """Select the best available encoder.

        Priority: preferred (if available) > qsv > nvenc > cpu
        """
        if preferred != "auto" and preferred in self.encoders:
            return preferred

        for encoder in ("qsv", "nvenc", "cpu"):
            if encoder in self.encoders:
                return encoder
        return "cpu"


async def _run_probe(cmd: list[str], timeout: float = 10.0) -> tuple[int, str]:
    """Run a command and return (returncode, combined output)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = stdout.decode() + stderr.decode()
        return proc.returncode or 0, output
    except TimeoutError:
        return 1, "timeout"
    except FileNotFoundError:
        return 1, "binary not found"


async def detect_ffmpeg_version() -> str:
    """Get the installed ffmpeg version string."""
    code, output = await _run_probe(["ffmpeg", "-version"])
    if code != 0:
        return "unknown"
    first_line = output.split("\n")[0]
    return first_line.strip()


async def detect_qsv() -> bool:
    """Check if Intel QSV (hevc_qsv) encoding is available.

    Tries multiple init methods since different ffmpeg builds use different
    device initialization paths (qsv via VAAPI, direct qsv, or just encoder check).
    """
    # Method 1: Check /dev/dri exists and encoder is listed
    code, output = await _run_probe(["ffmpeg", "-hide_banner", "-encoders"], timeout=10.0)
    if code != 0 or "hevc_qsv" not in output:
        logger.info("QSV encoder not found in ffmpeg build")
        return False

    # Method 2: Try actual encode via VAAPI-backed QSV (Linux standard path)
    for init_args in [
        ["-hwaccel", "qsv", "-hwaccel_device", "/dev/dri/renderD128"],
        ["-init_hw_device", "vaapi=va:/dev/dri/renderD128", "-init_hw_device", "qsv=qs@va"],
        ["-init_hw_device", "qsv=hw"],
    ]:
        code, output = await _run_probe(
            [
                "ffmpeg",
                "-hide_banner",
                *init_args,
                "-f",
                "lavfi",
                "-i",
                "nullsrc=s=256x256:d=0.1",
                "-c:v",
                "hevc_qsv",
                "-frames:v",
                "1",
                "-f",
                "null",
                "-",
            ],
            timeout=15.0,
        )
        if code == 0:
            logger.info("QSV (hevc_qsv) detected and working via: %s", " ".join(init_args))
            return True

    logger.info("QSV encoder listed but test encode failed: %s", output[:200])
    return False


async def detect_nvenc() -> bool:
    """Check if NVIDIA NVENC (hevc_nvenc) encoding is available."""
    code, output = await _run_probe(
        [
            "ffmpeg",
            "-hide_banner",
            "-f",
            "lavfi",
            "-i",
            "nullsrc=s=256x256:d=0.1",
            "-c:v",
            "hevc_nvenc",
            "-frames:v",
            "1",
            "-f",
            "null",
            "-",
        ],
        timeout=15.0,
    )
    if code == 0:
        logger.info("NVENC (hevc_nvenc) detected and working")
        return True
    logger.info("NVENC not available: %s", output[:200])
    return False


async def detect_capabilities(preferred_encoder: str = "auto") -> HardwareCapabilities:
    """Detect all hardware capabilities for this node.

    Runs QSV and NVENC probes in parallel for speed.
    """
    ffmpeg_version, qsv_ok, nvenc_ok = await asyncio.gather(
        detect_ffmpeg_version(),
        detect_qsv(),
        detect_nvenc(),
    )

    encoders = ["cpu"]
    if qsv_ok:
        encoders.insert(0, "qsv")
    if nvenc_ok:
        encoders.insert(0 if not qsv_ok else 1, "nvenc")

    caps = HardwareCapabilities(
        encoders=encoders,
        ffmpeg_version=ffmpeg_version,
        os_platform=platform.system(),
    )
    logger.info(
        "Hardware detection complete: encoders=%s ffmpeg=%s",
        caps.encoders,
        caps.ffmpeg_version,
    )
    return caps

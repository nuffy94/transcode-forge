"""Hardware acceleration detection — runs once at worker startup.

Capability is detected as (codec, backend) pairs: which of the real
encoders (libx265, libsvtav1, hevc_qsv, av1_qsv, hevc_nvenc, av1_nvenc,
h265_ni_quadra_enc, av1_ni_quadra_enc) actually work on this node.
Hardware probes do a real 10-bit test encode — the pipeline outputs 10-bit
everywhere, so an encoder that can't take p010le (e.g. hevc_qsv on
Skylake) is not usable and must not be advertised.
"""

import asyncio
import logging
import platform
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# codec → backend → ffmpeg encoder name. The detection matrix mirrors
# ENCODER_BUILDERS in encoder.py.
_ENCODER_NAMES: dict[tuple[str, str], str] = {
    ("hevc", "cpu"): "libx265",
    ("av1", "cpu"): "libsvtav1",
    ("hevc", "qsv"): "hevc_qsv",
    ("av1", "qsv"): "av1_qsv",
    ("hevc", "nvenc"): "hevc_nvenc",
    ("av1", "nvenc"): "av1_nvenc",
    ("hevc", "quadra"): "h265_ni_quadra_enc",
    ("av1", "quadra"): "av1_ni_quadra_enc",
}

# quadra is deliberately NOT in the auto-priority order: it is only used
# when a worker sets TF_PREFERRED_BACKEND=quadra (where an ASIC slots for
# mixed fleets is a later decision, not v1).
_BACKEND_PRIORITY = ("qsv", "nvenc", "cpu")


@dataclass(frozen=True)
class HardwareCapabilities:
    """Detected encoding capabilities for this node."""

    encoders: list[str]  # backends, e.g. ["qsv", "cpu"] (registration wire format)
    pairs: list[tuple[str, str]]  # working (codec, backend) combinations
    ffmpeg_version: str
    os_platform: str

    @property
    def has_qsv(self) -> bool:
        return "qsv" in self.encoders

    @property
    def has_nvenc(self) -> bool:
        return "nvenc" in self.encoders

    @property
    def supported_codecs(self) -> list[str]:
        """Codecs this node can encode via at least one backend."""
        return [codec for codec in ("hevc", "av1") if any(c == codec for c, _ in self.pairs)]

    def best_backend_for(self, codec: str, preferred: str = "auto") -> str | None:
        """Pick the backend for a codec: preferred if it supports the codec,
        else qsv > nvenc > cpu. None when no backend can encode the codec."""
        available = [b for c, b in self.pairs if c == codec]
        if preferred != "auto" and preferred in available:
            return preferred
        for backend in _BACKEND_PRIORITY:
            if backend in available:
                return backend
        return None


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


async def _list_encoders() -> str:
    """Return ffmpeg's encoder list (empty string on failure)."""
    code, output = await _run_probe(["ffmpeg", "-hide_banner", "-encoders"], timeout=10.0)
    return output if code == 0 else ""


def _test_encode_cmd(init_args: list[str], encoder: str) -> list[str]:
    """A minimal 10-bit test encode: one p010le frame through the encoder.

    10-bit is deliberate — the real pipeline always encodes 10-bit, so an
    encoder that only takes 8-bit input (Skylake hevc_qsv) must fail here."""
    return [
        "ffmpeg",
        "-hide_banner",
        *init_args,
        "-f",
        "lavfi",
        "-i",
        "nullsrc=s=256x256:d=0.1",
        "-vf",
        "format=p010le",
        "-c:v",
        encoder,
        "-frames:v",
        "1",
        "-f",
        "null",
        "-",
    ]


async def detect_qsv(encoder: str = "hevc_qsv", *, encoder_list: str | None = None) -> bool:
    """Check if a QSV encoder works, including 10-bit input.

    Tries multiple init methods since different ffmpeg builds use different
    device initialization paths (qsv via VAAPI, direct qsv, or just encoder check).
    """
    listed = encoder_list if encoder_list is not None else await _list_encoders()
    if encoder not in listed:
        logger.info("%s not found in ffmpeg build", encoder)
        return False

    output = ""
    for init_args in [
        ["-hwaccel", "qsv", "-hwaccel_device", "/dev/dri/renderD128"],
        ["-init_hw_device", "vaapi=va:/dev/dri/renderD128", "-init_hw_device", "qsv=qs@va"],
        ["-init_hw_device", "qsv=hw"],
    ]:
        code, output = await _run_probe(_test_encode_cmd(init_args, encoder), timeout=15.0)
        if code == 0:
            logger.info("QSV (%s) detected and working via: %s", encoder, " ".join(init_args))
            return True

    logger.info("%s listed but 10-bit test encode failed: %s", encoder, output[:200])
    return False


async def detect_nvenc(encoder: str = "hevc_nvenc", *, encoder_list: str | None = None) -> bool:
    """Check if an NVENC encoder works, including 10-bit input."""
    listed = encoder_list if encoder_list is not None else await _list_encoders()
    if encoder_list is not None and encoder not in listed:
        logger.info("%s not found in ffmpeg build", encoder)
        return False
    code, output = await _run_probe(_test_encode_cmd([], encoder), timeout=15.0)
    if code == 0:
        logger.info("NVENC (%s) detected and working", encoder)
        return True
    logger.info("%s not available: %s", encoder, output[:200])
    return False


async def detect_quadra(
    encoder: str = "h265_ni_quadra_enc", *, encoder_list: str | None = None
) -> bool:
    """Check if a NETINT Quadra encoder works, including 10-bit input.

    The ni encoders only exist in NETINT's patched ffmpeg build, and the
    test encode only succeeds when a Quadra device answers — so a stock
    worker (or a NETINT build with no card) never advertises quadra."""
    listed = encoder_list if encoder_list is not None else await _list_encoders()
    if encoder not in listed:
        logger.info("%s not found in ffmpeg build", encoder)
        return False
    code, output = await _run_probe(_test_encode_cmd([], encoder), timeout=15.0)
    if code == 0:
        logger.info("Quadra (%s) detected and working", encoder)
        return True
    logger.info("%s listed but 10-bit test encode failed: %s", encoder, output[:200])
    return False


async def detect_capabilities() -> HardwareCapabilities:
    """Detect all (codec, backend) capabilities for this node.

    Software encoders are trusted from the encoder list; hardware encoders
    must pass a real 10-bit test encode. Probes run in parallel for speed.
    """
    ffmpeg_version, encoder_list = await asyncio.gather(
        detect_ffmpeg_version(),
        _list_encoders(),
    )

    pairs: list[tuple[str, str]] = []
    for (codec, backend), name in _ENCODER_NAMES.items():
        if backend == "cpu" and name in encoder_list:
            pairs.append((codec, backend))

    hw_probes = {
        ("hevc", "qsv"): detect_qsv("hevc_qsv", encoder_list=encoder_list),
        ("av1", "qsv"): detect_qsv("av1_qsv", encoder_list=encoder_list),
        ("hevc", "nvenc"): detect_nvenc("hevc_nvenc", encoder_list=encoder_list),
        ("av1", "nvenc"): detect_nvenc("av1_nvenc", encoder_list=encoder_list),
        ("hevc", "quadra"): detect_quadra("h265_ni_quadra_enc", encoder_list=encoder_list),
        ("av1", "quadra"): detect_quadra("av1_ni_quadra_enc", encoder_list=encoder_list),
    }
    results = await asyncio.gather(*hw_probes.values())
    pairs.extend(pair for pair, ok in zip(hw_probes.keys(), results, strict=True) if ok)

    # hevc/cpu is the universal fallback — even if the encoder list probe
    # failed entirely (weird build), the pipeline's libx265 path is the
    # historical baseline and losing it would brick the worker.
    if ("hevc", "cpu") not in pairs:
        logger.warning("libx265 not detected in ffmpeg build — assuming cpu fallback regardless")
        pairs.insert(0, ("hevc", "cpu"))

    backends = ["cpu"]
    if any(b == "qsv" for _, b in pairs):
        backends.insert(0, "qsv")
    if any(b == "nvenc" for _, b in pairs):
        backends.insert(0 if "qsv" not in backends else 1, "nvenc")
    if any(b == "quadra" for _, b in pairs):
        backends.insert(backends.index("cpu"), "quadra")

    caps = HardwareCapabilities(
        encoders=backends,
        pairs=sorted(pairs),
        ffmpeg_version=ffmpeg_version,
        os_platform=platform.system(),
    )
    logger.info(
        "Hardware detection complete: pairs=%s codecs=%s ffmpeg=%s",
        caps.pairs,
        caps.supported_codecs,
        caps.ffmpeg_version,
    )
    return caps

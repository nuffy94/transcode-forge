"""End-to-end pipeline test with a REAL ffmpeg encode.

Every other pipeline test mocks ffmpeg, so a broken encode command or a
mis-wired (codec, backend) pair sails straight through CI. This drives the
real 8-step pipeline through a real ffmpeg on a tiny synthetic lavfi clip —
the exact gap that let the 0.8.1 backend-shadowing crash reach production.

Skipped automatically when ffmpeg/ffprobe aren't on PATH (e.g. Windows dev
boxes); CI installs ffmpeg so it always runs there.
"""

import asyncio
import shutil
import subprocess

import pytest

from transcode_forge.scanner.probe import ffprobe
from transcode_forge.worker.pipeline import PipelineError, _decode_check, run_pipeline

_FFMPEG = shutil.which("ffmpeg")
_FFPROBE = shutil.which("ffprobe")

pytestmark = pytest.mark.skipif(
    not (_FFMPEG and _FFPROBE),
    reason="ffmpeg/ffprobe not on PATH (installed in CI)",
)


def _encoder_available(name: str) -> bool:
    """True if this ffmpeg build has the named encoder compiled in."""
    out = subprocess.run(
        [_FFMPEG, "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        check=False,
    )
    return name in out.stdout


def _make_source(path, *, duration: float = 2.0) -> None:
    """Render a detailed lavfi clip encoded LOSSLESS (h264 -qp 0) so any
    lossy HEVC/AV1 re-encode is guaranteed smaller — keeps the pipeline's
    size-regression gate from tripping on a synthetic clip."""
    subprocess.run(
        [
            _FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=640x480:rate=24:duration={duration}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-qp",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(path),
        ],
        check=True,
    )


@pytest.mark.parametrize(
    "codec,encoder",
    [
        pytest.param("hevc", "libx265", id="hevc-cpu"),
        pytest.param("av1", "libsvtav1", id="av1-cpu"),
    ],
)
async def test_real_cpu_encode_replaces_original(tmp_path, codec, encoder):
    """A real CPU encode runs the full pipeline: original is replaced in
    place by a smaller, 10-bit stream of the target codec, and no lock/
    tmp/bak litter is left behind."""
    if not _encoder_available(encoder):
        pytest.skip(f"ffmpeg build lacks {encoder}")

    source = tmp_path / "clip.mkv"
    await asyncio.to_thread(_make_source, source, duration=2.0)
    source_size = source.stat().st_size

    result = await run_pipeline(
        source_path=str(source),
        codec=codec,
        backend="cpu",
        quality=28,
        source_duration=2.0,
        job_id="itest",
        worker_id="itest-worker",
    )

    # The real encode came out smaller and the pipeline reported it honestly.
    assert result["output_size"] < source_size
    assert result["space_saved"] == source_size - result["output_size"]
    assert result["backend"] == "cpu"

    # The original path now holds the re-encoded, 10-bit stream of the target
    # codec — proves command build + encode + verify + swap + confirm all ran.
    probe = await ffprobe(source)
    assert probe.video_codec == codec
    assert probe.is_10bit

    # UNLOCK/CLEANUP left nothing behind.
    for suffix in (".tf_lock", ".tf_tmp", ".tf_bak"):
        leftovers = list(tmp_path.glob(f"*{suffix}*"))
        assert leftovers == [], f"leftover {suffix} files: {leftovers}"


# --- R-005: VERIFY's decode check must fail on damage ffmpeg only complains about


def _make_hevc(path, *, duration: float = 4.0) -> None:
    """A tiny 10-bit HEVC clip in mkv, the shape of a pipeline output."""
    subprocess.run(
        [
            _FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=640x480:rate=24:duration={duration}",
            "-c:v",
            "libx265",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p10le",
            "-x265-params",
            "log-level=error",
            str(path),
        ],
        check=True,
    )


def _truncate_container(src, dst) -> None:
    """Cut the file at 60%: the demuxer reports 'File ended prematurely'
    and ffmpeg still exits 0."""
    data = src.read_bytes()
    dst.write_bytes(data[: int(len(data) * 0.6)])


def _corrupt_slices(src, dst) -> None:
    """Damage the packet payloads with ffmpeg's own noise bitstream filter,
    container fields intact: the decoder complains on nearly every frame
    and ffmpeg still exits 0. Blind byte-flipping was tried first and
    decoded silently five times out of eight (garbage picture, no error:
    that case is the VMAF gate's job), so the fixture is built this way."""
    subprocess.run(
        [
            _FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            "-c",
            "copy",
            "-bsf:v",
            "noise=amount=50",
            str(dst),
        ],
        check=True,
    )


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param(_truncate_container, id="truncated-container"),
        pytest.param(_corrupt_slices, id="corrupt-slices"),
    ],
)
async def test_decode_check_fails_a_damaged_encode(tmp_path, damage):
    """Ledger R-005: the deep decode check read only ffmpeg's exit code, and
    ffmpeg exits 0 after recoverable decoder and demuxer errors. Both
    damage shapes must fail VERIFY; the clean clip must still pass."""
    if not _encoder_available("libx265"):
        pytest.skip("ffmpeg build lacks libx265")
    clean = tmp_path / "clean.mkv"
    _make_hevc(clean)
    duration = (await ffprobe(clean)).duration
    assert duration > 0

    await _decode_check(clean, duration)  # a clean encode passes

    damaged = tmp_path / "damaged.mkv"
    damage(clean, damaged)
    with pytest.raises(PipelineError, match="Decode test failed"):
        await _decode_check(damaged, duration)

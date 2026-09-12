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

from transcode_forge.scanner.probe import ffprobe, stream_inventory
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


def _make_multi_stream_mkv(path, *, duration: float = 2.0) -> None:
    """A source with everything the old mapping used to lose: a second
    video angle and a picture stream beside the primary, plus audio, a
    forced subtitle and a font attachment. The primary is LOSSLESS so the
    lossy re-encode is guaranteed smaller."""
    directory = path.parent
    subs = directory / "subs.srt"
    subs.write_text("1\n00:00:00,000 --> 00:00:02,000\nhi\n\n", encoding="utf-8")
    font = directory / "font.ttf"
    font.write_bytes(b"not a real font, just bytes to attach")
    cover = directory / "cover.png"
    subprocess.run(
        [
            _FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=64x64:d=0.04",
            "-frames:v",
            "1",
            "-y",
            str(cover),
        ],
        check=True,
    )
    subprocess.run(
        [
            _FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=320x240:rate=24:duration={duration}",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=160x120:rate=24:duration={duration}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration}",
            "-i",
            str(subs),
            "-i",
            str(cover),
            "-attach",
            str(font),
            "-metadata:s:t:0",
            "mimetype=application/x-truetype-font",
            "-map",
            "0:v",
            "-map",
            "1:v",
            "-map",
            "2:a",
            "-map",
            "3:s",
            "-map",
            "4:v",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-qp",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-c:v:2",
            "copy",
            "-c:a",
            "aac",
            "-c:s",
            "copy",
            "-disposition:s:0",
            "forced",
            "-y",
            str(path),
        ],
        check=True,
    )


def _make_cover_art_mp4(path, *, duration: float = 2.0) -> None:
    """An mp4 whose cover art is a real attached_pic stream, the shape the
    old mapping dropped without a word."""
    cover = path.parent / "cover.png"
    subprocess.run(
        [
            _FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x64:d=0.04",
            "-frames:v",
            "1",
            "-y",
            str(cover),
        ],
        check=True,
    )
    subprocess.run(
        [
            _FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=320x240:rate=24:duration={duration}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration}",
            "-i",
            str(cover),
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-map",
            "2:v",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-qp",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-c:v:1",
            "copy",
            "-c:a",
            "aac",
            "-disposition:v:1",
            "attached_pic",
            "-y",
            str(path),
        ],
        check=True,
    )


@pytest.mark.parametrize(
    "name,build,expected",
    [
        pytest.param(
            "angles.mkv",
            _make_multi_stream_mkv,
            [
                ("video", "hevc", False),
                ("video", "h264", False),
                ("audio", "aac", False),
                ("subtitle", "subrip", False),
                ("video", "png", False),
                ("attachment", "ttf", False),
            ],
            id="mkv-second-angle-and-picture",
        ),
        pytest.param(
            "cover.mp4",
            _make_cover_art_mp4,
            [
                ("video", "hevc", False),
                ("audio", "aac", False),
                ("video", "png", True),
            ],
            id="mp4-attached-cover-art",
        ),
    ],
)
async def test_secondary_video_and_cover_art_survive_a_real_encode(tmp_path, name, build, expected):
    """Only a real mux can drop a stream, so only a real encode can prove
    it does not. The file left at the original path must carry every
    stream the source had, in the planned order, with the primary video
    re-encoded and everything else copied byte for byte."""
    if not _encoder_available("libx265"):
        pytest.skip("ffmpeg build lacks libx265")

    source = tmp_path / name
    await asyncio.to_thread(build, source)
    before = await stream_inventory(source)
    source_size = source.stat().st_size

    result = await run_pipeline(
        source_path=str(source),
        codec="hevc",
        backend="cpu",
        quality=28,
        source_duration=2.0,
        job_id="itest-streams",
        worker_id="itest-worker",
    )
    assert result["output_size"] < source_size

    after = await stream_inventory(source)
    assert [(s.codec_type, s.codec_name, s.attached_pic) for s in after] == expected

    # The copied streams keep the attributes the inventory key checks.
    for original, replacement in zip(before[1:], after[1:], strict=True):
        assert (original.codec_type, original.codec_name) == (
            replacement.codec_type,
            replacement.codec_name,
        )
        assert original.forced == replacement.forced
        assert original.language == replacement.language

    for suffix in (".tf_lock", ".tf_tmp", ".tf_bak"):
        assert list(tmp_path.glob(f"*{suffix}*")) == [], f"leftover {suffix} files"


def _make_chapter_mp4(path, *, duration: float = 2.0) -> None:
    """An mp4 that carries chapters. ffmpeg stores them as a data stream
    AND as chapter metadata, and the muxer writes a fresh chapter track of
    its own on the way out."""
    meta = path.parent / "chapters.txt"
    meta.write_text(
        ";FFMETADATA1\ntitle=test\n\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=One\n\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=1000\nEND=2000\ntitle=Two\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            _FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=320x240:rate=24:duration={duration}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration}",
            "-i",
            str(meta),
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-map_metadata",
            "2",
            "-map_chapters",
            "2",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-qp",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-y",
            str(path),
        ],
        check=True,
    )


async def test_a_chapter_bearing_mp4_is_not_refused_for_the_track_the_muxer_adds(tmp_path):
    """mp4 chapters ride as a data stream, which the plan leaves out by
    name, and the muxer then writes a chapter track of its own from the
    copied chapter metadata. That is an addition, not a loss. Refusing a
    good encode over it would skip every chapter-bearing file in a
    library."""
    if not _encoder_available("libx265"):
        pytest.skip("ffmpeg build lacks libx265")

    source = tmp_path / "chapters.mp4"
    await asyncio.to_thread(_make_chapter_mp4, source)
    before = await stream_inventory(source)
    assert [s.codec_type for s in before] == ["video", "audio", "data"]

    result = await run_pipeline(
        source_path=str(source),
        codec="hevc",
        backend="cpu",
        quality=28,
        source_duration=2.0,
        job_id="itest-chapters",
        worker_id="itest-worker",
    )
    assert result["output_size"] > 0

    after = await stream_inventory(source)
    assert [(s.codec_type, s.codec_name) for s in after][:2] == [
        ("video", "hevc"),
        ("audio", "aac"),
    ]


async def test_verify_decodes_the_re_encoded_primary_not_a_copied_angle(tmp_path):
    """Now that the output can hold more than one video stream, ffmpeg's
    default selection is a hazard in VERIFY too: it prefers the default
    disposition and then the larger frame, so the deep decode check would
    read a copied angle and pass a damaged encode straight through to SWAP
    and CLEANUP."""
    if not _encoder_available("libx265"):
        pytest.skip("ffmpeg build lacks libx265")

    clean = tmp_path / "clean.mkv"
    _make_hevc(clean)
    damaged = tmp_path / "damaged.mkv"
    _drop_reference_frames(clean, damaged)

    # The shape the widened mapping produces: the encoded primary at v:0
    # and a copied second angle that wins both default-selection
    # tie-breaks against it.
    multi = tmp_path / "multi.mkv"
    subprocess.run(
        [
            _FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(damaged),
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=640x480:rate=24:duration=16",
            "-map",
            "0:v:0",
            "-map",
            "1:v",
            "-c:v:0",
            "copy",
            "-c:v:1",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-disposition:v:0",
            "0",
            "-disposition:v:1",
            "default",
            "-y",
            str(multi),
        ],
        check=True,
    )
    duration = (await ffprobe(multi)).duration

    with pytest.raises(PipelineError, match="Decode test failed"):
        await _decode_check(multi, duration)


async def test_crf_samples_come_from_the_stream_the_encoder_will_re_encode(tmp_path):
    """ffmpeg's default video selection prefers the default disposition,
    not the first real video stream. On a file whose second angle carries
    that flag the two disagree, and a CRF search tuned against a stream
    the encoder never touches is tuned against nothing."""
    from transcode_forge.worker.vmaf import _extract_samples

    source = tmp_path / "angles.mkv"
    await asyncio.to_thread(
        lambda: subprocess.run(
            [
                _FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=320x240:rate=24:duration=2",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=640x480:rate=24:duration=2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=2",
                "-map",
                "0:v",
                "-map",
                "1:v",
                "-map",
                "2:a",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-qp",
                "0",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-disposition:v:0",
                "0",
                "-disposition:v:1",
                "default",
                "-y",
                str(source),
            ],
            check=True,
        )
    )
    # The plan's primary is source stream 0: the first video that is not an
    # attached picture, whatever the disposition flags say.
    assert [s.index for s in await stream_inventory(source) if s.codec_type == "video"] == [0, 1]

    picked = tmp_path / "picked"
    picked.mkdir()
    left_alone = tmp_path / "left-alone"
    left_alone.mkdir()

    with_index = await _extract_samples(source, 2.0, picked, 0)
    without_index = await _extract_samples(source, 2.0, left_alone, None)

    assert (await ffprobe(with_index[0])).width == 320, "the sample must be the primary"
    assert (await ffprobe(without_index[0])).width == 640, (
        "if this ever equals 320, ffmpeg's default selection changed and the"
        " -map is no longer what makes the sample match the encode"
    )


# --- R-005: VERIFY's decode check must fail on damage ffmpeg only complains about


def _make_hevc(path, *, duration: float = 16.0) -> None:
    """A small 10-bit HEVC clip in mkv, the shape of a pipeline output. 16 s
    is past the 1.5x DECODE_SAMPLE_SECONDS threshold, so the check takes
    its real three-offset seek path rather than the short-file pass."""
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


def _drop_reference_frames(src, dst) -> None:
    """Drop video packets 20 to 25 with ffmpeg's noise bitstream filter,
    container fields intact: every later frame that references them makes
    the decoder say 'Could not find ref with POC' until the next keyframe,
    and ffmpeg still exits 0. Which packets go is a function of the packet
    index, so the damage is the same on every build. Blind byte-flipping
    was tried first and decoded silently five times out of eight (garbage
    picture, no error: that case is the VMAF gate's job)."""
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
            r"noise=drop=between(n\,20\,25)",
            str(dst),
        ],
        check=True,
    )
    assert dst.stat().st_size < src.stat().st_size, "the drop filter removed nothing"


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param(_truncate_container, id="truncated-container"),
        pytest.param(_drop_reference_frames, id="missing-reference-frames"),
    ],
)
async def test_decode_check_fails_a_damaged_encode(tmp_path, damage):
    """Ledger R-005: the deep decode check read only ffmpeg's exit code, and
    ffmpeg exits 0 after recoverable decoder and demuxer errors. Both
    damage shapes must fail VERIFY through the three-offset seek path,
    and the clean clip must pass through the same path (a complaint on a
    clean seek would be a false positive on every fleet encode)."""
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

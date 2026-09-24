"""measure_vmaf against a REAL libvmaf.

Every other VMAF test fakes the ffmpeg subprocess, so they prove what
command gets built and never whether ffmpeg accepts it or what it scores.
Both production VMAF bugs so far (n_threads=0, frame mispairing) lived in
that gap. This file runs the real filter on small synthetic clips.

It needs an ffmpeg with libvmaf AND the v1 models, which distro builds do
not have, so it skips almost everywhere. The `image-build` CI job runs it
with the static binary copied out of the freshly built image (the one the
fleet runs) and sets TF_REQUIRE_LIBVMAF=1, which turns "cannot measure
here" from a skip into a failure: a broken binary must not go green.
"""

import math
import os
import shutil
import subprocess

import pytest

from transcode_forge.worker.vmaf import VMAF_FFMPEG, VMAF_MODEL_4K, VMAF_MODEL_HD, measure_vmaf

_REQUIRED = os.environ.get("TF_REQUIRE_LIBVMAF") == "1"
_FRAMES = 24  # one second at 24 fps


def _why_this_machine_cannot_measure() -> str | None:
    binary = shutil.which(VMAF_FFMPEG)
    if binary is None:
        return f"measurement ffmpeg {VMAF_FFMPEG!r} not found"
    encoders = subprocess.run(
        [binary, "-hide_banner", "-encoders"], capture_output=True, text=True, check=False
    )
    if "libx264" not in encoders.stdout:
        return f"{binary} has no libx264 to build the clips with"
    for model in (VMAF_MODEL_HD, VMAF_MODEL_4K):
        smoke = subprocess.run(
            [
                binary,
                "-hide_banner",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "nullsrc=s=1920x1080:d=0.2",
                "-f",
                "lavfi",
                "-i",
                "nullsrc=s=1920x1080:d=0.2",
                "-lavfi",
                "[0:v]format=yuv420p10le[a];[1:v]format=yuv420p10le[b];"
                f"[a][b]libvmaf=model=version={model}",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if smoke.returncode != 0:
            return f"{binary} cannot score with {model}: {smoke.stderr.strip()[-200:]}"
    return None


_PROBLEM = _why_this_machine_cannot_measure()

pytestmark = pytest.mark.skipif(
    _PROBLEM is not None and not _REQUIRED,
    reason=f"no real libvmaf here ({_PROBLEM}); the image-build CI job runs this",
)


def _ffmpeg(*args: str) -> None:
    subprocess.run([VMAF_FFMPEG, "-hide_banner", "-loglevel", "error", *args, "-y"], check=True)


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    """A lossless reference at 1080p and 2160p, a careful encode of each, a
    wrecked 1080p encode, and the 2160p reference delivered at 1080p."""
    if _PROBLEM is not None:
        pytest.fail(f"TF_REQUIRE_LIBVMAF=1 but this lane cannot measure: {_PROBLEM}")
    root = tmp_path_factory.mktemp("vmaf-clips")
    x264 = ("-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p")
    made = {}
    for name, size in (("hd", "1920x1080"), ("uhd", "3840x2160")):
        ref = root / f"ref_{name}.mkv"
        _ffmpeg(
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={size}:rate=24:duration=1",
            *x264,
            "-qp",
            "0",
            str(ref),
        )
        good = root / f"good_{name}.mkv"
        _ffmpeg("-i", str(ref), *x264, "-crf", "12", str(good))
        made[f"ref_{name}"], made[f"good_{name}"] = ref, good
    bad = root / "bad_hd.mkv"
    _ffmpeg(
        "-i",
        str(made["ref_hd"]),
        "-vf",
        "scale=320:180,scale=1920:1080",
        *x264,
        "-crf",
        "40",
        str(bad),
    )
    delivered = root / "delivered_hd.mkv"
    _ffmpeg("-i", str(made["ref_uhd"]), "-vf", "scale=-2:1080", *x264, "-crf", "12", str(delivered))
    return {**made, "bad_hd": bad, "delivered_hd": delivered}


def _assert_sane(score) -> None:
    for value in (score.mean, score.perc5, score.min):
        assert math.isfinite(value)
        assert 0.0 <= value <= 100.0
    assert score.min <= score.perc5 <= score.mean


async def test_a_careful_encode_scores_high_and_a_wrecked_one_far_lower(clips):
    good = await measure_vmaf(clips["ref_hd"], clips["good_hd"], height=1080, n_subsample=1)
    bad = await measure_vmaf(clips["ref_hd"], clips["bad_hd"], height=1080, n_subsample=1)

    _assert_sane(good)
    _assert_sane(bad)
    assert good.mean >= 90.0
    assert bad.mean <= good.mean - 20.0


async def test_the_encode_is_judged_against_the_source_not_the_other_way_round(clips):
    """VMAF is not symmetric: a blurred picture judged against a sharp
    reference scores low, a sharp picture judged against a blurred
    reference scores higher (45.2 against 55.5 on the pinned n8.1.2 build,
    2026-09-20). With the two inputs swapped in the command this ordering
    flips."""
    blurred_against_sharp = await measure_vmaf(
        clips["ref_hd"], clips["bad_hd"], height=1080, n_subsample=1
    )
    sharp_against_blurred = await measure_vmaf(
        clips["bad_hd"], clips["ref_hd"], height=1080, n_subsample=1
    )

    assert blurred_against_sharp.mean + 5.0 <= sharp_against_blurred.mean


async def test_a_4k_pair_is_scored_with_the_4k_model(clips):
    score = await measure_vmaf(clips["ref_uhd"], clips["good_uhd"], height=2160, n_subsample=1)

    _assert_sane(score)
    assert score.mean >= 90.0


async def test_a_downscale_is_scored_against_the_reference_at_the_delivered_height(clips):
    """2160p source, 1080p output: the graph scales the reference down
    inside ffmpeg. Without that the two inputs differ in size and libvmaf
    refuses the pair."""
    score = await measure_vmaf(
        clips["ref_uhd"], clips["delivered_hd"], height=2160, target_height=1080, n_subsample=1
    )

    _assert_sane(score)
    assert score.mean >= 85.0


async def test_the_streaming_gauge_reports_progress_and_still_scores(clips):
    """The -progress pipe:2 path, drained for real: a second undrained
    pipe once deadlocked ffmpeg (PR #85 review)."""
    fracs: list[float] = []

    async def on_progress(frac: float) -> None:
        fracs.append(frac)

    score = await measure_vmaf(
        clips["ref_hd"],
        clips["good_hd"],
        height=1080,
        n_subsample=1,
        duration=_FRAMES / 24,
        on_progress=on_progress,
    )

    _assert_sane(score)
    assert fracs, "no progress was reported"
    assert fracs == sorted(fracs)
    assert all(0.0 < frac <= 1.0 for frac in fracs)

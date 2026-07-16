"""Downscale + same-codec shrink — worker-side contract tests (PR B).

Written against plans/downscale-shrink-spec.md before the implementation.
Blocks map to the spec's worker touch points:

  E — encoder: target_height → `-vf scale=-2:H` composed into every
      (codec, backend) builder; absent when no downscale
  R — the gauge's reference contract: the filter graph is built by ONE
      pure function; the no-downscale graph is pinned byte-for-byte (it
      carries the settb/setpts INDEX-pairing fix from the #66 desync),
      the downscale graph adds a pinned-lanczos scale to the REFERENCE
      chain only, and the model follows the TARGET height ("did the
      encode add damage beyond the downscale I asked for?")
  V — VERIFY pins the output height; the pipeline refuses an upscale
      even if a bad job row reaches it
  W — wiring: the pipeline forwards target_height to the encoder, the
      gauge, and the CRF search; the derivative key forks by height
"""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from tests.helpers import make_probe
from transcode_forge.worker.encoder import build_encode_command
from transcode_forge.worker.vmaf import (
    VMAF_MODEL_4K,
    VMAF_MODEL_HD,
    build_gauge_graph,
    select_model,
)

# ── E — encoder builders compose the scale filter ────────────────────────────

ALL_PAIRS = [
    ("hevc", "cpu"),
    ("hevc", "qsv"),
    ("hevc", "nvenc"),
    ("av1", "cpu"),
    ("av1", "nvenc"),
    ("av1", "qsv"),
]


@pytest.mark.parametrize("codec,backend", ALL_PAIRS)
def test_builder_adds_scale_filter_for_target_height(codec, backend):
    """E: every (codec, backend) pair gains `-vf scale=-2:H` when a
    downscale is requested — width auto, always even, aspect preserved."""
    cmd = build_encode_command(codec, backend, "in.mkv", "out.mkv", quality=20, target_height=1080)
    assert "-vf" in cmd
    assert cmd[cmd.index("-vf") + 1] == "scale=-2:1080"


@pytest.mark.parametrize("codec,backend", ALL_PAIRS)
def test_builder_no_scale_filter_without_target_height(codec, backend):
    """E: no downscale → the command is scale-free (pre-feature identical)."""
    cmd = build_encode_command(codec, backend, "in.mkv", "out.mkv", quality=20)
    assert "-vf" not in cmd
    assert not any("scale=" in arg for arg in cmd)


# ── R — the gauge graph contract ─────────────────────────────────────────────


def test_gauge_graph_golden_no_downscale():
    """R: the no-downscale graph is pinned BYTE-FOR-BYTE. It carries the
    settb/setpts index-pairing contract from the #66 desync fix — any
    change to this string is a scoring-behavior change and must be a
    deliberate one (update this golden in the same commit, with data)."""
    graph = build_gauge_graph(
        model="vmaf_v0.6.1", log_path="/tmp/vmaf.json", n_subsample=5, n_threads=8
    )
    assert graph == (
        "[0:v]settb=AVTB,setpts=N*100000,format=yuv420p10le[dis];"
        "[1:v]settb=AVTB,setpts=N*100000,format=yuv420p10le[ref];"
        "[dis][ref]libvmaf=model=version=vmaf_v0.6.1"
        ":log_fmt=json:log_path=/tmp/vmaf.json"
        ":n_subsample=5:n_threads=8"
    )


def test_gauge_graph_golden_downscale_reference():
    """R: with a downscale, the REFERENCE chain (input 1) gains a
    pinned-lanczos scale — the reference is the best possible rendition at
    the delivered resolution, and pinning the scaler keeps scores identical
    across workers whatever their ffmpeg's default. The DISTORTED chain and
    the index-pairing ops on both chains are untouched."""
    graph = build_gauge_graph(
        model="vmaf_v0.6.1",
        log_path="/tmp/vmaf.json",
        n_subsample=5,
        n_threads=8,
        reference_scale_height=1080,
    )
    assert graph == (
        "[0:v]settb=AVTB,setpts=N*100000,format=yuv420p10le[dis];"
        "[1:v]settb=AVTB,setpts=N*100000,scale=-2:1080:flags=lanczos,format=yuv420p10le[ref];"
        "[dis][ref]libvmaf=model=version=vmaf_v0.6.1"
        ":log_fmt=json:log_path=/tmp/vmaf.json"
        ":n_subsample=5:n_threads=8"
    )


def test_model_follows_target_height_not_source():
    """R: a 2160→1080 downscale is scored with the HD model (the question
    is asked AT the delivered resolution); the same source without a
    downscale keeps the 4K model."""
    assert select_model(2160) == VMAF_MODEL_4K
    assert select_model(1080) == VMAF_MODEL_HD


# ── V / W — pipeline: VERIFY pins height, wiring forwards it ─────────────────


async def _mock_encode_ok(cmd, total_duration, progress_callback=None):
    from transcode_forge.worker.encoder import EncodeResult

    output = Path(cmd[-1])
    output.write_bytes(b"y" * 5000)
    return EncodeResult(success=True, output_path=str(output), output_size=5000, returncode=0)


def _probe_sequence(*probes):
    """ffprobe mock returning the given probes in order (pre-flight source
    probe, VERIFY on tmp, CONFIRM on the swapped file)."""
    seq = list(probes)

    async def _fake(path):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return _fake


async def test_pipeline_verify_rejects_wrong_output_height(tmp_path):
    """V: an encode that comes out at the wrong height never swaps —
    VERIFY fails, the original stays untouched."""
    from transcode_forge.worker.pipeline import PipelineError, run_pipeline

    source = tmp_path / "test.mkv"
    source.write_bytes(b"x" * 10000)

    with (
        patch("transcode_forge.worker.pipeline.run_encode", side_effect=_mock_encode_ok),
        patch(
            "transcode_forge.worker.pipeline.ffprobe",
            side_effect=_probe_sequence(
                make_probe("h264", height=2160),  # pre-flight source probe
                make_probe("hevc", height=2160),  # VERIFY: output NOT downscaled
            ),
        ),
        patch("transcode_forge.worker.pipeline._decode_check"),
    ):
        with pytest.raises(PipelineError) as exc_info:
            await run_pipeline(
                source_path=str(source),
                codec="hevc",
                backend="cpu",
                quality=21,
                source_duration=3600.0,
                job_id="j1",
                worker_id="w1",
                target_height=1080,
            )
    assert exc_info.value.step == "VERIFY"
    assert source.read_bytes() == b"x" * 10000  # original untouched


async def test_pipeline_refuses_upscale_even_if_job_row_lies(tmp_path):
    """V: defense in depth — the scheduler validates strictly-downward at
    queue time, but the pipeline replaces originals, so it re-checks."""
    from transcode_forge.worker.pipeline import PipelineError, run_pipeline

    source = tmp_path / "test.mkv"
    source.write_bytes(b"x" * 10000)

    with (
        patch("transcode_forge.worker.pipeline.run_encode", side_effect=_mock_encode_ok),
        patch(
            "transcode_forge.worker.pipeline.ffprobe",
            side_effect=_probe_sequence(make_probe("h264", height=720)),
        ),
        patch("transcode_forge.worker.pipeline._decode_check"),
    ):
        with pytest.raises(PipelineError):
            await run_pipeline(
                source_path=str(source),
                codec="hevc",
                backend="cpu",
                quality=21,
                source_duration=3600.0,
                job_id="j1",
                worker_id="w1",
                target_height=1080,
            )
    assert source.read_bytes() == b"x" * 10000


async def test_pipeline_downscale_happy_path_wires_everything(tmp_path):
    """W: target_height reaches the encoder command (scale filter), the
    gauge (target_height kwarg + source height for context), and the
    output swaps after VERIFY sees the target height."""
    from transcode_forge.worker.pipeline import run_pipeline
    from transcode_forge.worker.vmaf import VmafScore

    source = tmp_path / "test.mkv"
    source.write_bytes(b"x" * 10000)

    seen_cmds: list[list[str]] = []

    async def capture_encode(cmd, total_duration, progress_callback=None):
        seen_cmds.append(cmd)
        return await _mock_encode_ok(cmd, total_duration, progress_callback)

    gauge_kwargs: dict = {}

    async def fake_gauge(src, enc, **kwargs):
        gauge_kwargs.update(kwargs)
        return VmafScore(mean=98.0, perc5=97.0, min=96.0)

    with (
        patch("transcode_forge.worker.pipeline.run_encode", side_effect=capture_encode),
        patch(
            "transcode_forge.worker.pipeline.ffprobe",
            side_effect=_probe_sequence(
                make_probe("h264", height=2160),  # pre-flight source probe
                make_probe("hevc", height=1080),  # VERIFY on tmp
                make_probe("hevc", height=1080),  # CONFIRM post-swap
            ),
        ),
        patch("transcode_forge.worker.pipeline._decode_check"),
        patch("transcode_forge.worker.pipeline.has_libvmaf", AsyncMock(return_value=True)),
        patch("transcode_forge.worker.pipeline.measure_vmaf", side_effect=fake_gauge),
    ):
        result = await run_pipeline(
            source_path=str(source),
            codec="hevc",
            backend="cpu",
            quality=21,
            source_duration=3600.0,
            job_id="j1",
            worker_id="w1",
            target_vmaf=97.0,
            target_height=1080,
        )
    assert result["vmaf_mean"] == 98.0
    assert source.read_bytes() == b"y" * 5000  # swapped
    assert any("scale=-2:1080" in arg for cmd in seen_cmds for arg in cmd)
    assert gauge_kwargs.get("target_height") == 1080
    assert gauge_kwargs.get("height") == 2160  # source height still carried


async def test_pipeline_without_target_height_is_byte_identical(tmp_path):
    """W: no downscale → no scale filter, no target_height at the gauge —
    pre-feature jobs run exactly as before."""
    from transcode_forge.worker.pipeline import run_pipeline
    from transcode_forge.worker.vmaf import VmafScore

    source = tmp_path / "test.mkv"
    source.write_bytes(b"x" * 10000)

    seen_cmds: list[list[str]] = []

    async def capture_encode(cmd, total_duration, progress_callback=None):
        seen_cmds.append(cmd)
        return await _mock_encode_ok(cmd, total_duration, progress_callback)

    gauge_kwargs: dict = {}

    async def fake_gauge(src, enc, **kwargs):
        gauge_kwargs.update(kwargs)
        return VmafScore(mean=98.0, perc5=97.0, min=96.0)

    with (
        patch("transcode_forge.worker.pipeline.run_encode", side_effect=capture_encode),
        patch("transcode_forge.worker.pipeline.ffprobe", return_value=make_probe()),
        patch("transcode_forge.worker.pipeline._decode_check"),
        patch("transcode_forge.worker.pipeline.has_libvmaf", AsyncMock(return_value=True)),
        patch("transcode_forge.worker.pipeline.measure_vmaf", side_effect=fake_gauge),
    ):
        await run_pipeline(
            source_path=str(source),
            codec="hevc",
            backend="cpu",
            quality=21,
            source_duration=3600.0,
            job_id="j1",
            worker_id="w1",
            target_vmaf=97.0,
        )
    assert not any("scale=" in arg for cmd in seen_cmds for arg in cmd)
    assert gauge_kwargs.get("target_height") is None


# ── W — CRF search + derivative key carry the height ─────────────────────────


async def test_crf_search_forwards_target_height(tmp_path):
    """W: the search optimizes quality-at-target — sample encodes carry the
    scale filter and sample gauges score against the downscaled reference,
    consistent with the full-file gate."""
    from transcode_forge.worker import vmaf as vmaf_mod

    sample = tmp_path / "sample0.mkv"
    sample.write_bytes(b"s" * 1000)

    eval_calls: list[dict] = []

    async def fake_extract(source, duration, out_dir):
        return [sample]

    async def fake_encode(cmd, total_duration, progress_callback=None):
        eval_calls.append({"cmd": cmd})
        out = Path(cmd[-1])
        out.write_bytes(b"e" * 500)
        from transcode_forge.worker.encoder import EncodeResult

        return EncodeResult(success=True, output_path=str(out), output_size=500, returncode=0)

    async def fake_gauge(src, enc, **kwargs):
        eval_calls.append({"gauge": kwargs})
        return vmaf_mod.VmafScore(mean=99.0, perc5=98.0, min=97.0)

    with (
        patch.object(vmaf_mod, "_extract_samples", side_effect=fake_extract),
        patch.object(vmaf_mod, "run_encode", side_effect=fake_encode),
        patch.object(vmaf_mod, "measure_vmaf", side_effect=fake_gauge),
    ):
        result = await vmaf_mod.find_quality_for_target(
            tmp_path / "src.mkv",
            "hevc",
            "cpu",
            target_vmaf=97.0,
            perc5_floor=95.0,
            duration=3600.0,
            height=2160,
            target_height=1080,
        )
    assert result is not None
    encode_cmds = [c["cmd"] for c in eval_calls if "cmd" in c]
    gauge_kwargs = [c["gauge"] for c in eval_calls if "gauge" in c]
    assert encode_cmds and all(any("scale=-2:1080" in arg for arg in cmd) for cmd in encode_cmds)
    assert gauge_kwargs and all(k.get("target_height") == 1080 for k in gauge_kwargs)


def test_derivative_key_forks_by_target_height():
    """W: same source, same codec, same VMAF goal — different heights are
    different derivatives; same height dedups (S3 dedup stays goal-keyed)."""
    from transcode_forge.models.derivative import compute_derivative_key

    base = dict(
        source_path="/m/x.mkv",
        source_resolution="3840x2160",
        source_audio_codec="aac",
        target_audio_codec="copy",
        target_codec="hevc",
        target_vmaf=97,
        backend="cpu",
        crf=20,
        preset="slow",
    )
    full = compute_derivative_key(**base, target_resolution="3840x2160")
    at_1080 = compute_derivative_key(**base, target_resolution="1080p")
    at_720 = compute_derivative_key(**base, target_resolution="720p")
    assert len({full, at_1080, at_720}) == 3
    assert compute_derivative_key(**base, target_resolution="1080p") == at_1080

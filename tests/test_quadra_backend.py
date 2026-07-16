"""NETINT Quadra (VPU) backend — contract tests.

Written against the locked decisions in plans/vpu-bench-spec.md:

  D4 — Quadra CRF (0-51) mapped from the reference scale; offset is a
       PLACEHOLDER 0 until the Phase 2 calibration pass
  D6 — backend name `quadra`: new (codec, backend) pairs, probe-based
       advertisement, zero effect on nodes without NETINT silicon;
       default backend priority order unchanged (preferred-only)
"""

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from tests.helpers import register_worker
from transcode_forge.models.job import Job, JobStatus
from transcode_forge.repos import jobs as job_repo
from transcode_forge.worker.encoder import build_encode_command
from transcode_forge.worker.hardware import (
    HardwareCapabilities,
    detect_capabilities,
    detect_quadra,
)

# ── D6 — builder resolution ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "codec,expected_encoder",
    [
        ("hevc", "h265_ni_quadra_enc"),
        ("av1", "av1_ni_quadra_enc"),
    ],
)
def test_builder_resolves_ni_quadra_encoder(codec, expected_encoder):
    """D6: build_encode_command(codec, 'quadra', ...) emits the ni encoder."""
    cmd = build_encode_command(codec, "quadra", "in.mkv", "out.mkv", quality=20)
    assert cmd[cmd.index("-c:v") + 1] == expected_encoder


@pytest.mark.parametrize("codec", ["hevc", "av1"])
def test_quadra_output_is_10bit(codec):
    """D4: quadra encodes request 10-bit output like every other backend
    (yuv420p10le — the planar layout NETINT's own examples feed)."""
    cmd = build_encode_command(codec, "quadra", "in.mkv", "out.mkv", quality=20)
    assert "yuv420p10le" in cmd


@pytest.mark.parametrize("codec", ["hevc", "av1"])
def test_quadra_crf_mode_disables_default_rc(codec):
    """D4: RcEnable=0 must ride with crf= — without it the encoder's
    default rate controller runs and the crf value is silently ignored."""
    cmd = build_encode_command(codec, "quadra", "in.mkv", "out.mkv", quality=20)
    params = cmd[cmd.index("-xcoder-params") + 1]
    assert "RcEnable=0" in params.split(":")


def test_quadra_downscale_passes_through():
    """target_height rides the shared _scale_args seam."""
    cmd = build_encode_command("hevc", "quadra", "in.mkv", "out.mkv", quality=20, target_height=720)
    assert "scale=-2:720" in cmd


# ── D4 — quality mapping: placeholder offset 0, native 0-51 clamp ───────────


def _crf_of(cmd: list[str]) -> int:
    """Extract the crf value from the -xcoder-params bag."""
    params = cmd[cmd.index("-xcoder-params") + 1]
    for part in params.split(":"):
        if part.startswith("crf="):
            return int(part.removeprefix("crf="))
    raise AssertionError(f"no crf= in {params!r}")


@pytest.mark.parametrize("codec", ["hevc", "av1"])
def test_quadra_quality_placeholder_is_identity(codec):
    """D4: pre-calibration the reference value passes through unchanged
    (offset 0). The Phase 2 calibration pass replaces this — the test pins
    the *placeholder*, so a calibrated offset must update it deliberately."""
    cmd = build_encode_command(codec, "quadra", "i", "o", quality=20)
    assert _crf_of(cmd) == 20


def test_quadra_quality_clamps_to_native_range():
    """D4: Quadra CRF is 0-51 — reference values beyond it clamp."""
    assert _crf_of(build_encode_command("hevc", "quadra", "i", "o", quality=60)) == 51
    assert _crf_of(build_encode_command("hevc", "quadra", "i", "o", quality=0)) == 0


# ── D6 — preferred-only backend selection (auto priority unchanged) ─────────


def _caps(pairs: list[tuple[str, str]]) -> HardwareCapabilities:
    backends = sorted({b for _, b in pairs})
    return HardwareCapabilities(
        encoders=backends, pairs=pairs, ffmpeg_version="7.1", os_platform="Linux"
    )


def test_auto_never_picks_quadra():
    """D6: with quadra AND cpu available, auto still resolves cpu — the
    ASIC is used only when a worker explicitly prefers it."""
    caps = _caps([("hevc", "cpu"), ("hevc", "quadra")])
    assert caps.best_backend_for("hevc", "auto") == "cpu"


def test_preferred_quadra_is_honored():
    """D6: TF_PREFERRED_BACKEND=quadra selects the ASIC when capable."""
    caps = _caps([("hevc", "cpu"), ("hevc", "quadra")])
    assert caps.best_backend_for("hevc", "quadra") == "quadra"


def test_preferred_quadra_falls_back_when_incapable():
    """D6: preferring quadra on a codec it can't encode falls back to the
    normal priority chain instead of failing."""
    caps = _caps([("av1", "cpu")])
    assert caps.best_backend_for("av1", "quadra") == "cpu"


def test_config_accepts_preferred_backend_quadra(monkeypatch):
    """D6: TF_PREFERRED_BACKEND=quadra validates."""
    monkeypatch.setenv("TF_PREFERRED_BACKEND", "quadra")
    from transcode_forge.config import Settings

    assert Settings().preferred_backend == "quadra"


# ── D6 — probe-based advertisement ──────────────────────────────────────────


async def test_detect_capabilities_advertises_quadra_when_silicon_answers():
    """D6: NETINT build + card → quadra pairs advertised, wire list gains
    'quadra' (never displacing cpu as the trailing fallback)."""

    async def mock_probe(cmd, timeout=10.0):
        cmd_str = " ".join(cmd)
        if "-version" in cmd_str:
            return (0, "ffmpeg version 7.1\n")
        if "-encoders" in cmd_str:
            return (0, "libx265\nlibsvtav1\nh265_ni_quadra_enc\nav1_ni_quadra_enc")
        if "ni_quadra" in cmd_str:
            return (0, "frame=1")
        return (1, "no device")

    with patch("transcode_forge.worker.hardware._run_probe", side_effect=mock_probe):
        caps = await detect_capabilities()
    assert ("hevc", "quadra") in caps.pairs
    assert ("av1", "quadra") in caps.pairs
    assert caps.encoders == ["quadra", "cpu"]
    assert caps.supported_codecs == ["hevc", "av1"]


async def test_detect_capabilities_no_quadra_on_stock_build():
    """D6: a stock ffmpeg build (home fleet) never probes or advertises
    quadra — zero effect on nodes without NETINT silicon."""

    async def mock_probe(cmd, timeout=10.0):
        cmd_str = " ".join(cmd)
        if "-version" in cmd_str:
            return (0, "ffmpeg version 7.1\n")
        if "-encoders" in cmd_str:
            return (0, "libx265\nlibsvtav1")
        return (1, "no device")

    with patch("transcode_forge.worker.hardware._run_probe", side_effect=mock_probe):
        caps = await detect_capabilities()
    assert not any(b == "quadra" for _, b in caps.pairs)
    assert "quadra" not in caps.encoders


async def test_detect_quadra_listed_but_no_card_fails():
    """D6: NETINT ffmpeg build with no card (or a dead one) → the real
    test encode fails → not advertised."""
    with patch(
        "transcode_forge.worker.hardware._run_probe",
        return_value=(1, "no NETINT device found"),
    ):
        assert await detect_quadra(encoder_list="h265_ni_quadra_enc") is False


# ── scheduler accepts quadra as a reported backend ──────────────────────────


async def test_complete_accepts_backend_used_quadra(client: AsyncClient, app):
    """The completion payload validates backend_used='quadra' and persists it."""
    job = Job(
        source_path="/m/vpu.mkv",
        library="movies",
        source_codec="h264",
        quality_value=21,
        status=JobStatus.PENDING,
    )
    await job_repo.create_job(app.state.db, job)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        headers, worker_id = await register_worker(client, c, "vpu-node", ["hevc", "av1"])
        await c.post("/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers)
        r = await c.post(
            f"/api/worker/job/{job.id}/complete",
            json={
                "output_size": 1000,
                "space_saved": 9000,
                "source_size": 10000,
                "achieved_vmaf": 96.5,
                "resolved_crf": 27,
                "backend_used": "quadra",
            },
            headers=headers,
        )
        assert r.status_code == 204

    final = await job_repo.get_job(app.state.db, job.id)
    assert final.status == JobStatus.COMPLETE
    assert final.backend_used == "quadra"


async def test_complete_accepts_unknown_future_backend(client: AsyncClient, app):
    """Review finding (PR #73): outcome reporting must be maximally
    accepting on backend names — by report time the transcode is
    irreversible, so a scheduler older than its workers must never 422 a
    successful outcome over a name it doesn't know. Shape-checked only."""

    async def _seed() -> Job:
        job = Job(
            source_path=f"/m/future-{id(object())}.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
            status=JobStatus.PENDING,
        )
        await job_repo.create_job(app.state.db, job)
        return job

    ok_job = await _seed()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        headers, worker_id = await register_worker(client, c, "future-node", ["hevc"])
        await c.post("/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers)
        r = await c.post(
            f"/api/worker/job/{ok_job.id}/complete",
            json={"output_size": 1, "space_saved": 1, "source_size": 2, "backend_used": "vulkan"},
            headers=headers,
        )
        assert r.status_code == 204

        # Shape violations (not mere unknown names) are still rejected.
        bad_job = await _seed()
        await c.post("/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers)
        r = await c.post(
            f"/api/worker/job/{bad_job.id}/complete",
            json={
                "output_size": 1,
                "space_saved": 1,
                "source_size": 2,
                "backend_used": "NOT A SLUG",
            },
            headers=headers,
        )
        assert r.status_code == 422

    final = await job_repo.get_job(app.state.db, ok_job.id)
    assert final.status == JobStatus.COMPLETE
    assert final.backend_used == "vulkan"

"""AV1 / multi-codec — contract tests (the "graded exam").

Written during the design phase (plans/av1-codec-spec.md) and reconciled
against the real code seams during the build. Each test maps to a locked
decision (D#) in the spec:

  D1  — per-job codec selection at queue time (AV1 opt-in, HEVC default)
  D2  — encoder builder keyed on (codec, backend)
  D3  — SVT-AV1 / av1_nvenc / av1_qsv implementations
  D4  — per-encoder quality mapping + 10-bit output everywhere
  D5  — VMAF quality gate (below floor → SKIPPED, original untouched)
  D6  — goal-keyed derivative cache
  D7  — worker capability advertisement + codec-filtered claim
  D9  — settings-override layer (DB override else env default)
  D10 — TF_PREFERRED_ENCODER honored as deprecated backend alias
"""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from tests.helpers import make_probe, register_worker
from transcode_forge.models.job import Job, JobStatus
from transcode_forge.repos import jobs as job_repo
from transcode_forge.repos import libraries as lib_repo
from transcode_forge.repos import media as media_repo

# ── D2 / D3 / D4 — encoder builder is keyed on (codec, backend); 10-bit output ──────


@pytest.mark.parametrize(
    "codec,backend,expected_encoder",
    [
        ("hevc", "cpu", "libx265"),
        ("hevc", "qsv", "hevc_qsv"),
        ("hevc", "nvenc", "hevc_nvenc"),
        ("av1", "cpu", "libsvtav1"),
        ("av1", "nvenc", "av1_nvenc"),
        ("av1", "qsv", "av1_qsv"),
    ],
)
def test_builder_resolves_ffmpeg_encoder_per_codec_backend(codec, backend, expected_encoder):
    """D2/D3: build_encode_command(codec, backend, ...) emits the right -c:v encoder."""
    from transcode_forge.worker.encoder import build_encode_command

    cmd = build_encode_command(codec, backend, "in.mkv", "out.mkv", quality=20)
    assert "-c:v" in cmd
    assert cmd[cmd.index("-c:v") + 1] == expected_encoder


@pytest.mark.parametrize(
    "codec,backend",
    [
        ("hevc", "cpu"),
        ("hevc", "qsv"),
        ("hevc", "nvenc"),
        ("av1", "cpu"),
        ("av1", "nvenc"),
        ("av1", "qsv"),
    ],
)
def test_builder_output_is_10bit(codec, backend):
    """D4: every encode requests a 10-bit pixel format (yuv420p10le / p010le)."""
    from transcode_forge.worker.encoder import build_encode_command

    cmd = build_encode_command(codec, backend, "in.mkv", "out.mkv", quality=20)
    assert any(px in cmd for px in ("yuv420p10le", "p010le"))


def test_builder_unknown_codec_backend_raises():
    """D2: an unsupported (codec, backend) pair fails loudly, not silently."""
    from transcode_forge.worker.encoder import build_encode_command

    with pytest.raises(ValueError):
        build_encode_command("av1", "no_such_backend", "in.mkv", "out.mkv", quality=27)
    with pytest.raises(ValueError):
        build_encode_command("no_such_codec", "cpu", "in.mkv", "out.mkv", quality=27)


def test_quality_maps_per_encoder_not_shared():
    """D4: one target must NOT feed x265 -crf, qsv -global_quality, nvenc -cq verbatim.
    nvenc cq ≈ crf + 11 (VMAF-matched); the same input quality resolves differently."""
    from transcode_forge.worker.encoder import build_encode_command

    cpu = build_encode_command("hevc", "cpu", "i", "o", quality=20)
    nvenc = build_encode_command("hevc", "nvenc", "i", "o", quality=20)
    assert cpu[cpu.index("-crf") + 1] == "20"
    assert nvenc[nvenc.index("-cq") + 1] != "20"  # must be mapped, not shared
    assert nvenc[nvenc.index("-cq") + 1] == "31"


def test_av1_quality_maps_from_reference_scale():
    """D4: AV1 has its own CRF scale — x265-reference 20 lands at SVT-AV1 CRF 27
    (research: AV1 crf ≈ x265 crf + 6..9), av1_nvenc cq 26, av1_qsv 24."""
    from transcode_forge.worker.encoder import build_encode_command

    cpu = build_encode_command("av1", "cpu", "i", "o", quality=20)
    assert cpu[cpu.index("-crf") + 1] == "27"
    nvenc = build_encode_command("av1", "nvenc", "i", "o", quality=20)
    assert nvenc[nvenc.index("-cq") + 1] == "26"
    qsv = build_encode_command("av1", "qsv", "i", "o", quality=20)
    assert qsv[qsv.index("-global_quality") + 1] == "24"


# ── Shared seeding helpers ───────────────────────────────────────────────────────────


async def _seed_h264_file(db, path: str = "/media/movies/film.mkv") -> str:
    """Create a movies library + one catalogued h264 media file; returns file id."""
    if not await lib_repo.path_in_use(db, "/media/movies"):
        await db.execute(
            """INSERT INTO libraries (id, name, media_type, path, quality_preset,
                enabled, auto_scan, scan_interval_hours, created_at, updated_at)
            VALUES ('movies', 'movies', 'movies', '/media/movies', 21, 1, 0, 24,
                '2026-01-01', '2026-01-01')""",
        )
        await db.commit()
    return await media_repo.upsert_media_file(
        db,
        library_id="movies",
        file_path=path,
        filename=Path(path).name,
        video_codec="h264",
        audio_codec="aac",
        resolution="1920x1080",
        width=1920,
        height=1080,
        bitrate=8_000_000,
        duration=5400.0,
        file_size=4_000_000_000,
    )


# Shared across test modules — one home in tests/helpers.py.
_register_worker = register_worker


# ── D1 — per-job codec selection at queue time ──────────────────────────────────────


async def test_queue_with_codec_av1_sets_target_codec(client: AsyncClient, app):
    """D1: POST /api/media/queue with codec=av1 → created job has target_codec='av1'."""
    file_id = await _seed_h264_file(app.state.db)
    resp = await client.post("/api/media/queue", json={"file_ids": [file_id], "codec": "av1"})
    assert resp.status_code == 200
    assert resp.json()["queued"] == 1
    jobs, _ = await job_repo.list_jobs(app.state.db)
    assert jobs[0].target_codec == "av1"


async def test_queue_default_codec_is_hevc(client: AsyncClient, app):
    """D1: omitting codec falls back to HEVC (current behavior preserved)."""
    file_id = await _seed_h264_file(app.state.db)
    resp = await client.post("/api/media/queue", json={"file_ids": [file_id]})
    assert resp.status_code == 200
    jobs, _ = await job_repo.list_jobs(app.state.db)
    assert jobs[0].target_codec == "hevc"


async def test_queue_rejects_unknown_codec(client: AsyncClient, app):
    """D1: an unknown codec is rejected at the boundary, not stored."""
    file_id = await _seed_h264_file(app.state.db)
    resp = await client.post("/api/media/queue", json={"file_ids": [file_id], "codec": "vp8"})
    assert resp.status_code == 422


async def test_global_default_codec_prefills_when_set(client: AsyncClient, app):
    """D1/D9: the default_codec override sets the omitted-codec default."""
    from transcode_forge.repos import settings as settings_repo

    await settings_repo.set_override(app.state.db, "default_codec", "av1")
    file_id = await _seed_h264_file(app.state.db)
    resp = await client.post("/api/media/queue", json={"file_ids": [file_id]})
    assert resp.status_code == 200
    jobs, _ = await job_repo.list_jobs(app.state.db)
    assert jobs[0].target_codec == "av1"


# ── D6 — derivative cache is goal-keyed ─────────────────────────────────────────────


def test_derivative_key_is_goal_keyed_not_recipe():
    """D6: key depends on (source, codec, target_vmaf, resolutions, audio) — NOT on
    backend/crf/preset. Same goal via different backends → identical key."""
    from transcode_forge.models.derivative import compute_derivative_key

    common = dict(
        source_path="/m/x.mkv",
        source_resolution="1080p",
        source_audio_codec="aac",
        target_resolution="1080p",
        target_audio_codec="aac",
        target_codec="av1",
        target_vmaf=97,
    )
    # backend/crf/preset differ but must NOT change the key:
    k_cpu = compute_derivative_key(**common, backend="cpu", crf=27, preset="6")
    k_nvenc = compute_derivative_key(**common, backend="nvenc", crf=26, preset="p7")
    assert k_cpu == k_nvenc


def test_derivative_key_differs_by_codec():
    """D6: HEVC vs AV1 of the same source are distinct cache entries."""
    from transcode_forge.models.derivative import compute_derivative_key

    base = dict(
        source_path="/m/x.mkv",
        source_resolution="1080p",
        source_audio_codec="aac",
        target_resolution="1080p",
        target_audio_codec="aac",
        target_vmaf=97,
        backend="cpu",
        crf=20,
        preset="slow",
    )
    assert compute_derivative_key(**{**base, "target_codec": "hevc"}) != compute_derivative_key(
        **{**base, "target_codec": "av1"}
    )


def test_derivative_key_differs_by_target_vmaf():
    """D6: the quality goal (target VMAF) is part of the identity."""
    from transcode_forge.models.derivative import compute_derivative_key

    base = dict(
        source_path="/m/x.mkv",
        source_resolution="1080p",
        source_audio_codec="aac",
        target_resolution="1080p",
        target_audio_codec="aac",
        target_codec="av1",
    )
    assert compute_derivative_key(**base, target_vmaf=97) != compute_derivative_key(
        **base, target_vmaf=93
    )


def test_derivative_filename_is_hash_codec_ext():
    """D6: filename shape is {hash}_{codec}.{ext} — recipe details live in the DB row."""
    from transcode_forge.models.derivative import compute_derivative_key

    key = compute_derivative_key(
        source_path="/m/x.mkv",
        source_resolution="1080p",
        source_audio_codec="aac",
        target_resolution="1080p",
        target_audio_codec="aac",
        target_codec="av1",
        target_vmaf=97,
        local_output=Path("/scratch/out.mkv"),
    )
    assert key.endswith("_av1.mkv")


# ── D7 — worker capability advertisement + codec-filtered claim ─────────────────────


async def _seed_av1_job(app) -> Job:
    job = Job(
        source_path="/m/av1-target.mkv",
        library="movies",
        source_codec="h264",
        quality_value=21,
        target_codec="av1",
        status=JobStatus.PENDING,
    )
    await job_repo.create_job(app.state.db, job)
    return job


async def test_hevc_only_worker_does_not_claim_av1_job(client: AsyncClient, app):
    """D7: a worker advertising supported_codecs=['hevc'] must not be handed an AV1 job."""
    await _seed_av1_job(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        headers, worker_id = await _register_worker(client, c, "hevc-only", ["hevc"])
        r = await c.post("/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers)
        assert r.json()["job"] is None


async def test_av1_capable_worker_claims_av1_job(client: AsyncClient, app):
    """D7: a worker advertising 'av1' claims the queued AV1 job."""
    job = await _seed_av1_job(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        headers, worker_id = await _register_worker(client, c, "av1-node", ["hevc", "av1"])
        r = await c.post("/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers)
        claimed = r.json()["job"]
        assert claimed is not None
        assert claimed["id"] == job.id
        assert claimed["target_codec"] == "av1"


async def test_no_capable_worker_leaves_job_pending(client: AsyncClient, app):
    """D7: with only hevc-capable workers, a queued AV1 job stays PENDING (never fails)."""
    job = await _seed_av1_job(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        headers, worker_id = await _register_worker(client, c, "hevc-only", ["hevc"])
        for _ in range(3):
            r = await c.post(
                "/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers
            )
            assert r.json()["job"] is None
    current = await job_repo.get_job(app.state.db, job.id)
    assert current.status == JobStatus.PENDING


async def test_worker_without_supported_codecs_defaults_to_hevc(client: AsyncClient, app):
    """D7/D10: an old worker that doesn't send supported_codecs still claims HEVC jobs
    (rolling update is safe) but never AV1 ones."""
    await _seed_av1_job(app)
    hevc_job = Job(
        source_path="/m/hevc-target.mkv",
        library="movies",
        source_codec="h264",
        quality_value=21,
        status=JobStatus.PENDING,
    )
    await job_repo.create_job(app.state.db, hevc_job)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        headers, worker_id = await _register_worker(client, c, "old-worker", None)
        r = await c.post("/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers)
        claimed = r.json()["job"]
        assert claimed is not None
        assert claimed["id"] == hevc_job.id


# ── D5 — VMAF quality gate (the never-degrade guarantee) ─────────────────────────────


_mock_probe = make_probe  # shared in tests/helpers.py


async def _mock_encode_ok(cmd, total_duration, progress_callback=None):
    from transcode_forge.worker.encoder import EncodeResult

    output = Path(cmd[-1])
    output.write_bytes(b"y" * 5000)
    return EncodeResult(success=True, output_path=str(output), output_size=5000, returncode=0)


async def test_vmaf_below_floor_skips_and_keeps_original(tmp_path):
    """D5: if measured VMAF lands below the safety floors, the pipeline
    raises VmafGateError — original file untouched, no swap, nothing left
    behind. Floors are absolute (90/85 defaults), not derived from target."""
    from transcode_forge.worker.pipeline import VmafGateError, run_pipeline
    from transcode_forge.worker.vmaf import VmafScore

    source = tmp_path / "test.mkv"
    source.write_bytes(b"x" * 10000)

    async def damaged_vmaf(*args, **kwargs):
        return VmafScore(mean=88.0, perc5=80.0, min=70.0)

    with (
        patch("transcode_forge.worker.pipeline.run_encode", side_effect=_mock_encode_ok),
        patch("transcode_forge.worker.pipeline.ffprobe", return_value=_mock_probe()),
        patch("transcode_forge.worker.pipeline._decode_check"),
        patch("transcode_forge.worker.pipeline.has_libvmaf", AsyncMock(return_value=True)),
        patch("transcode_forge.worker.pipeline.measure_vmaf", side_effect=damaged_vmaf),
    ):
        with pytest.raises(VmafGateError) as exc_info:
            await run_pipeline(
                source_path=str(source),
                codec="hevc",
                backend="cpu",
                quality=21,
                source_duration=3600.0,
                job_id="j1",
                worker_id="w1",
                target_vmaf=97.0,
                vmaf_safety_mean=90.0,
                vmaf_safety_perc5=85.0,
            )
    assert exc_info.value.vmaf_perc5 == 80.0
    # Original untouched, no droppings.
    assert source.read_bytes() == b"x" * 10000
    assert not (tmp_path / "test.tf_tmp.mkv").exists()
    assert not (tmp_path / "test.tf_bak.mkv").exists()
    assert not (tmp_path / "test.mkv.tf_lock").exists()


async def test_vmaf_at_or_above_floor_completes_and_swaps(tmp_path):
    """D5: perc5 >= floor → pipeline completes and the atomic swap happens."""
    from transcode_forge.worker.pipeline import run_pipeline
    from transcode_forge.worker.vmaf import VmafScore

    source = tmp_path / "test.mkv"
    source.write_bytes(b"x" * 10000)

    async def good_vmaf(*args, **kwargs):
        return VmafScore(mean=98.0, perc5=97.0, min=96.0)

    with (
        patch("transcode_forge.worker.pipeline.run_encode", side_effect=_mock_encode_ok),
        patch("transcode_forge.worker.pipeline.ffprobe", return_value=_mock_probe()),
        patch("transcode_forge.worker.pipeline._decode_check"),
        patch("transcode_forge.worker.pipeline.has_libvmaf", AsyncMock(return_value=True)),
        patch("transcode_forge.worker.pipeline.measure_vmaf", side_effect=good_vmaf),
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
            vmaf_safety_mean=90.0,
            vmaf_safety_perc5=85.0,
        )
    assert result["vmaf_mean"] == 98.0
    assert result["vmaf_perc5"] == 97.0
    assert source.read_bytes() == b"y" * 5000  # swapped


async def test_av1_output_verifies_as_av1(tmp_path):
    """D5/D2: the VERIFY step checks for the job's target codec, not hardcoded hevc."""
    from transcode_forge.worker.pipeline import run_pipeline

    source = tmp_path / "test.mkv"
    source.write_bytes(b"x" * 10000)

    with (
        patch("transcode_forge.worker.pipeline.run_encode", side_effect=_mock_encode_ok),
        patch("transcode_forge.worker.pipeline.ffprobe", return_value=_mock_probe("av1")),
        patch("transcode_forge.worker.pipeline._decode_check"),
    ):
        result = await run_pipeline(
            source_path=str(source),
            codec="av1",
            backend="cpu",
            quality=21,
            source_duration=3600.0,
            job_id="j1",
            worker_id="w1",
        )
    assert result["output_size"] == 5000


async def test_worker_reports_vmaf_skip_job_ends_skipped(client: AsyncClient, app):
    """D5: the worker-reported below-floor outcome lands the job in SKIPPED
    (not FAILED) with the score recorded, and the file shows up in skipped_files."""
    job = Job(
        source_path="/m/grainy.mkv",
        library="movies",
        source_codec="h264",
        quality_value=21,
        status=JobStatus.PENDING,
    )
    await job_repo.create_job(app.state.db, job)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        headers, worker_id = await _register_worker(client, c, "w", ["hevc", "av1"])
        r = await c.post("/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers)
        assert r.json()["job"]["id"] == job.id
        r = await c.post(
            f"/api/worker/job/{job.id}/skipped",
            json={
                "reason": "below_vmaf_floor",
                "error_message": "VMAF perc5 90.0 below floor 95.0",
                "achieved_vmaf": 94.0,
            },
            headers=headers,
        )
        assert r.status_code == 204

    final = await job_repo.get_job(app.state.db, job.id)
    assert final.status == JobStatus.SKIPPED
    assert final.achieved_vmaf == 94.0

    from transcode_forge.repos import skipped as skip_repo

    files, total = await skip_repo.list_skipped(app.state.db, reason="below_vmaf_floor")
    assert total == 1
    assert files[0].file_path == "/m/grainy.mkv"


async def test_complete_records_vmaf_and_backend(client: AsyncClient, app):
    """D5/D10: completion persists achieved_vmaf, resolved_crf, backend_used."""
    job = Job(
        source_path="/m/good.mkv",
        library="movies",
        source_codec="h264",
        quality_value=21,
        status=JobStatus.PENDING,
    )
    await job_repo.create_job(app.state.db, job)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        headers, worker_id = await _register_worker(client, c, "w", ["hevc"])
        await c.post("/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers)
        r = await c.post(
            f"/api/worker/job/{job.id}/complete",
            json={
                "output_size": 1000,
                "space_saved": 9000,
                "source_size": 10000,
                "achieved_vmaf": 97.8,
                "resolved_crf": 22,
                "backend_used": "cpu",
            },
            headers=headers,
        )
        assert r.status_code == 204

    final = await job_repo.get_job(app.state.db, job.id)
    assert final.status == JobStatus.COMPLETE
    assert final.achieved_vmaf == 97.8
    assert final.resolved_crf == 22
    assert final.backend_used == "cpu"


# ── D9 — settings-override layer (env default → DB override; secrets stay env-only) ──


async def test_effective_prefers_db_override_then_env(db):
    """D9: effective(key) returns the DB override if set, else the env default."""
    from transcode_forge.repos import settings as settings_repo

    assert await settings_repo.effective(db, "default_codec") == "hevc"  # env default (unset)
    await settings_repo.set_override(db, "default_codec", "av1")
    assert await settings_repo.effective(db, "default_codec") == "av1"


async def test_clear_override_restores_env_default(db):
    """D9: removing the override falls back to the env default."""
    from transcode_forge.repos import settings as settings_repo

    await settings_repo.set_override(db, "target_vmaf", "93")
    assert await settings_repo.effective(db, "target_vmaf") == "93"
    await settings_repo.clear_override(db, "target_vmaf")
    assert float(await settings_repo.effective(db, "target_vmaf")) == 98.0


async def test_secret_keys_are_not_overridable(db):
    """D9: infra/secret keys (db_url, auth_secret, ...) must be rejected by set_override."""
    from transcode_forge.repos import settings as settings_repo

    for key in ("db_url", "auth_secret", "token_pepper", "redis_url", "s3_secret_access_key"):
        with pytest.raises(ValueError):
            await settings_repo.set_override(db, key, "x")


async def test_invalid_override_values_rejected(db):
    """D9: values are validated per key — codec must be a known codec, VMAF a number."""
    from transcode_forge.repos import settings as settings_repo

    with pytest.raises(ValueError):
        await settings_repo.set_override(db, "default_codec", "vp8")
    with pytest.raises(ValueError):
        await settings_repo.set_override(db, "target_vmaf", "not-a-number")


# ── D9 — tuning API (the editable-settings surface) ─────────────────────────────────


async def test_tuning_api_roundtrip(client: AsyncClient):
    """D9: GET returns effective values; PUT sets overrides; empty clears them."""
    r = await client.get("/api/settings/tuning")
    assert r.status_code == 200
    assert r.json()["data"]["default_codec"] == "hevc"

    r = await client.put(
        "/api/settings/tuning",
        json={"values": {"default_codec": "av1", "target_vmaf": "95"}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["data"]["default_codec"] == "av1"
    assert body["overrides"]["target_vmaf"] == "95"

    r = await client.put("/api/settings/tuning", json={"values": {"default_codec": ""}})
    assert r.json()["data"]["default_codec"] == "hevc"  # env default restored


async def test_tuning_api_rejects_invalid_and_non_editable(client: AsyncClient):
    """D9: bad values and non-allowlisted keys are 400; nothing partially applies."""
    r = await client.put("/api/settings/tuning", json={"values": {"default_codec": "vp8"}})
    assert r.status_code == 400
    r = await client.put(
        "/api/settings/tuning",
        json={"values": {"target_vmaf": "93", "auth_secret": "x"}},
    )
    assert r.status_code == 400
    # The valid half of the rejected request must NOT have been applied.
    r = await client.get("/api/settings/tuning")
    assert "target_vmaf" not in r.json()["overrides"]


async def test_tuning_api_requires_auth(unauthed_client: AsyncClient):
    r = await unauthed_client.get("/api/settings/tuning")
    assert r.status_code == 401


# ── D10 — env back-compat: TF_PREFERRED_ENCODER alias ───────────────────────────────


def test_preferred_encoder_env_is_honored_as_backend_alias(monkeypatch):
    """D10: the old TF_PREFERRED_ENCODER still sets the (renamed) preferred_backend."""
    monkeypatch.delenv("TF_PREFERRED_BACKEND", raising=False)
    monkeypatch.setenv("TF_PREFERRED_ENCODER", "qsv")
    from transcode_forge.config import Settings

    assert Settings().preferred_backend == "qsv"


def test_preferred_backend_env_wins_over_alias(monkeypatch):
    """D10: when both are set, the new name wins."""
    monkeypatch.setenv("TF_PREFERRED_BACKEND", "nvenc")
    monkeypatch.setenv("TF_PREFERRED_ENCODER", "qsv")
    from transcode_forge.config import Settings

    assert Settings().preferred_backend == "nvenc"

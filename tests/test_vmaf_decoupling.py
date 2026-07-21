"""VMAF gate decoupling tests (plans/vmaf-decoupling-spec.md, locked 2026-07-05).

The flaw (found live 2026-07-04): the CRF search converges to "samples
barely clear target", samples overestimate the full file by ~+3 mean /
+7 perc5, so a target-derived gate rejected class-typical encodes
wholesale — 93% skip on a real batch. The fix: the gate becomes two
ABSOLUTE safety floors (mean ≥ 90, perc5 ≥ 85 by default), never derived
from the target, while the search keeps its historical bars so CRF picks
don't shift. Predictions + full-file perc5 are persisted on every
terminal path so the sample-vs-full-file gap stays measurable.
"""

from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from tests.helpers import make_probe, register_worker
from transcode_forge.models.job import Job, JobStatus
from transcode_forge.repos import jobs as job_repo
from transcode_forge.worker.pipeline import (
    SizeRegressionError,
    VmafGateError,
    run_pipeline,
)
from transcode_forge.worker.vmaf import QualitySearchResult, VmafScore


def _mock_encode(output_bytes: int):
    async def encode(cmd, total_duration, progress_callback=None):
        from transcode_forge.worker.encoder import EncodeResult

        output = Path(cmd[-1])
        output.write_bytes(b"y" * output_bytes)
        return EncodeResult(
            success=True, output_path=str(output), output_size=output_bytes, returncode=0
        )

    return encode


def _vmaf(mean: float, perc5: float):
    async def measure(*args, **kwargs):
        return VmafScore(mean=mean, perc5=perc5, min=perc5 - 10.0)

    return measure


@contextmanager
def _pipeline_patches(
    *,
    encode_bytes: int = 5000,
    measured: tuple[float, float] | None = None,
    search: AsyncMock | None = None,
):
    """Patch the pipeline's externals (encode, probe, decode check, libvmaf,
    optionally measurement + CRF search) for the duration of a test."""
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "transcode_forge.worker.pipeline.run_encode", side_effect=_mock_encode(encode_bytes)
            )
        )
        stack.enter_context(
            patch("transcode_forge.worker.pipeline.ffprobe", return_value=make_probe())
        )
        stack.enter_context(patch("transcode_forge.worker.pipeline._decode_check"))
        stack.enter_context(
            patch("transcode_forge.worker.pipeline.has_libvmaf", AsyncMock(return_value=True))
        )
        if measured is not None:
            stack.enter_context(
                patch("transcode_forge.worker.pipeline.measure_vmaf", side_effect=_vmaf(*measured))
            )
        if search is not None:
            stack.enter_context(
                patch("transcode_forge.worker.pipeline.find_quality_for_target", search)
            )
        yield stack


async def _run(source: Path, **kwargs):
    defaults = dict(
        source_path=str(source),
        codec="hevc",
        backend="cpu",
        quality=21,
        source_duration=3600.0,
        job_id="j1",
        worker_id="w1",
    )
    defaults.update(kwargs)
    return await run_pipeline(**defaults)


def _search_mock() -> AsyncMock:
    return AsyncMock(
        return_value=QualitySearchResult(quality=18, predicted_mean=97.1, predicted_perc5=95.3)
    )


# ── The gate is decoupled from the target ────────────────────────────────


class TestFloorsOnlyGate:
    async def test_class_typical_encode_completes_despite_missing_target(self, tmp_path):
        """THE flaw regression. Live numbers from the 2026-07-04 batch:
        full file measured 93.3 mean / 88.1 perc5 against target 97 —
        skipped by the old target-derived gate (97/95), a good encode
        discarded. Under the absolute floors (90/85) it must complete."""
        source = tmp_path / "ep.mkv"
        source.write_bytes(b"x" * 10000)

        with _pipeline_patches(measured=(93.3, 88.1)):
            result = await _run(source, target_vmaf=97.0)

        assert result["vmaf_mean"] == 93.3
        assert result["vmaf_perc5"] == 88.1
        assert source.read_bytes() == b"y" * 5000  # swap happened

    async def test_damaged_mean_skips_at_default_floor(self, tmp_path):
        """Mean below the 90 default → VmafGateError (skip, original kept)."""
        source = tmp_path / "ep.mkv"
        source.write_bytes(b"x" * 10000)

        with _pipeline_patches(measured=(89.0, 88.0)):
            with pytest.raises(VmafGateError) as exc_info:
                await _run(source, target_vmaf=97.0)

        assert exc_info.value.mean_floor == 91.5
        assert source.read_bytes() == b"x" * 10000

    async def test_damaged_perc5_skips_at_default_floor(self, tmp_path):
        """Mean fine but worst scenes below the 85 default → skip."""
        source = tmp_path / "ep.mkv"
        source.write_bytes(b"x" * 10000)

        with _pipeline_patches(measured=(96.0, 84.0)):
            with pytest.raises(VmafGateError) as exc_info:
                await _run(source, target_vmaf=97.0)

        assert exc_info.value.perc5_floor == 86.0

    async def test_null_target_means_no_measurement_no_gate(self, tmp_path):
        """target_vmaf=None stays byte-identical to the pre-gate path —
        no VMAF measurement at all (load-bearing for gateless batches)."""
        source = tmp_path / "ep.mkv"
        source.write_bytes(b"x" * 10000)

        with _pipeline_patches() as stack:
            measure = stack.enter_context(
                patch("transcode_forge.worker.pipeline.measure_vmaf", AsyncMock())
            )
            result = await _run(source, target_vmaf=None)

        measure.assert_not_awaited()
        assert result["vmaf_mean"] is None
        assert result["predicted_vmaf_mean"] is None


# ── The search is untouched; predictions are captured ───────────────────


class TestSearchUnchangedAndPredictionsPersist:
    async def test_search_keeps_target_bars_and_predictions_flow_to_result(self, tmp_path):
        """The search still aims samples at (target, target-2) — its CRF
        distribution must not shift — and its winning predictions land in
        the pipeline result for persistence."""
        source = tmp_path / "ep.mkv"
        source.write_bytes(b"x" * 10000)
        search = _search_mock()

        with _pipeline_patches(measured=(93.3, 88.1), search=search):
            result = await _run(source, target_vmaf=97.0, crf_search=True)

        kwargs = search.await_args.kwargs
        assert kwargs["target_vmaf"] == 97.0
        assert kwargs["perc5_floor"] == 95.0  # target - 2, the historical search bar
        assert result["predicted_vmaf_mean"] == 97.1
        assert result["predicted_vmaf_perc5"] == 95.3
        assert result["resolved_crf"] == 18

    async def test_gate_error_carries_full_diagnostics(self, tmp_path):
        """A gate skip must be self-explaining: achieved scores, floors,
        resolved CRF, backend, and the predictions that led there."""
        source = tmp_path / "ep.mkv"
        source.write_bytes(b"x" * 10000)

        with _pipeline_patches(measured=(88.0, 80.0), search=_search_mock()):
            with pytest.raises(VmafGateError) as exc_info:
                await _run(source, target_vmaf=97.0, crf_search=True)

        e = exc_info.value
        assert e.predicted_vmaf_mean == 97.1
        assert e.predicted_vmaf_perc5 == 95.3
        assert e.resolved_crf == 18
        assert e.backend == "cpu"

    async def test_size_regression_carries_diagnostics(self, tmp_path):
        """Size-regression skips must also keep CRF/backend/predictions —
        pre-fix they persisted nothing and were indistinguishable from
        VMAF skips in analysis."""
        source = tmp_path / "ep.mkv"
        source.write_bytes(b"x" * 10000)

        with _pipeline_patches(encode_bytes=20000, search=_search_mock()):  # bigger than source
            with pytest.raises(SizeRegressionError) as exc_info:
                await _run(source, target_vmaf=97.0, crf_search=True)

        e = exc_info.value
        assert e.predicted_vmaf_mean == 97.1
        assert e.resolved_crf == 18
        assert e.backend == "cpu"


# ── API round-trip: the measurement loop persists ────────────────────────


async def _seed_pending_job(app, source_path: str = "/m/loop.mkv") -> Job:
    job = Job(
        source_path=source_path,
        library="movies",
        source_codec="h264",
        quality_value=21,
        status=JobStatus.PENDING,
        target_vmaf=97.0,
    )
    await job_repo.create_job(app.state.db, job)
    return job


class TestMeasurementLoopPersistence:
    async def test_complete_persists_predictions_and_perc5(self, client: AsyncClient, app):
        job = await _seed_pending_job(app)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            headers, worker_id = await register_worker(client, c, "loop-w")
            r = await c.post(
                "/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers
            )
            claimed = r.json()["job"]
            assert claimed["id"] == job.id

            r = await c.post(
                f"/api/worker/job/{job.id}/complete",
                json={
                    "output_size": 4000,
                    "space_saved": 6000,
                    "source_size": 10000,
                    "achieved_vmaf": 93.3,
                    "achieved_vmaf_perc5": 88.1,
                    "predicted_vmaf_mean": 97.1,
                    "predicted_vmaf_perc5": 95.3,
                    "resolved_crf": 18,
                    "backend_used": "cpu",
                },
                headers=headers,
            )
            assert r.status_code == 204

        row = await job_repo.get_job(app.state.db, job.id)
        assert row.status == JobStatus.COMPLETE
        assert row.achieved_vmaf == 93.3
        assert row.achieved_vmaf_perc5 == 88.1
        assert row.predicted_vmaf_mean == 97.1
        assert row.predicted_vmaf_perc5 == 95.3

    async def test_skip_persists_full_diagnostics(self, client: AsyncClient, app):
        job = await _seed_pending_job(app, "/m/skip.mkv")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            headers, worker_id = await register_worker(client, c, "skip-w")
            await c.post("/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers)

            r = await c.post(
                f"/api/worker/job/{job.id}/skipped",
                json={
                    "reason": "below_vmaf_floor",
                    "error_message": "VMAF below floor",
                    "achieved_vmaf": 88.0,
                    "achieved_vmaf_perc5": 80.0,
                    "predicted_vmaf_mean": 97.1,
                    "predicted_vmaf_perc5": 95.3,
                    "resolved_crf": 18,
                    "backend_used": "cpu",
                },
                headers=headers,
            )
            assert r.status_code == 204

        row = await job_repo.get_job(app.state.db, job.id)
        assert row.status == JobStatus.SKIPPED
        assert row.achieved_vmaf_perc5 == 80.0
        assert row.predicted_vmaf_mean == 97.1
        assert row.resolved_crf == 18
        assert row.backend_used == "cpu"

    async def test_pre_decoupling_worker_payloads_still_accepted(self, client: AsyncClient, app):
        """A lagging v0.9.x worker sends none of the new fields — both
        terminal reports must stay 204 (additive API, spec §6)."""
        job = await _seed_pending_job(app, "/m/old.mkv")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            headers, worker_id = await register_worker(client, c, "old-w")
            await c.post("/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers)

            r = await c.post(
                f"/api/worker/job/{job.id}/complete",
                json={"output_size": 4000, "space_saved": 6000, "source_size": 10000},
                headers=headers,
            )
            assert r.status_code == 204

        row = await job_repo.get_job(app.state.db, job.id)
        assert row.predicted_vmaf_mean is None
        assert row.achieved_vmaf_perc5 is None

    async def test_claim_stamps_safety_floors_and_legacy_alias(self, client: AsyncClient, app):
        """Claim carries the scheduler-owned safety floors; the legacy
        _vmaf_min_floor stamp aliases the perc5 floor so a pre-decoupling
        worker's perc5 bar is sane mid-deploy (spec §6)."""
        from transcode_forge.repos import settings as settings_repo

        await _seed_pending_job(app, "/m/stamp.mkv")
        await settings_repo.set_override(app.state.db, "vmaf_safety_perc5", "87")

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            headers, worker_id = await register_worker(client, c, "stamp-w")
            r = await c.post(
                "/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers
            )
        claimed = r.json()["job"]
        assert claimed["_vmaf_safety_mean"] == 91.5  # env default (v1 scale)
        assert claimed["_vmaf_safety_perc5"] == 87.0  # DB override
        assert claimed["_vmaf_min_floor"] == 87.0  # legacy alias


# ── Settings validation (spec §4.6) ──────────────────────────────────────


class TestSettingsValidation:
    async def test_perc5_floor_above_mean_floor_rejected(self, client: AsyncClient):
        r = await client.put(
            "/api/settings/tuning",
            json={"values": {"vmaf_safety_mean": "90", "vmaf_safety_perc5": "95"}},
        )
        assert r.status_code == 400
        assert "cannot exceed" in r.json()["detail"]

    async def test_perc5_check_uses_effective_values_across_requests(self, client: AsyncClient):
        """The cross-check must consider the OTHER knob's stored override,
        not just the values in this request."""
        r = await client.put("/api/settings/tuning", json={"values": {"vmaf_safety_mean": "87"}})
        assert r.status_code == 200
        r = await client.put("/api/settings/tuning", json={"values": {"vmaf_safety_perc5": "88"}})
        assert r.status_code == 400

    async def test_unrelated_save_not_blocked_by_preexisting_floor_state(
        self, client: AsyncClient, app
    ):
        """Review finding: the cross-check must only gate saves that touch
        the floor keys — a codec-only save must succeed even if stored
        floor state is incoherent (that's a boot/env problem, not this
        request's)."""
        from transcode_forge.repos import settings as settings_repo

        # Force incoherent stored state via the repo (per-key validation
        # alone can't see the pair).
        await settings_repo.set_override(app.state.db, "vmaf_safety_perc5", "95")

        r = await client.put("/api/settings/tuning", json={"values": {"default_codec": "av1"}})
        assert r.status_code == 200
        assert r.json()["overrides"]["default_codec"] == "av1"

    async def test_target_below_safety_mean_warns_but_saves(self, client: AsyncClient):
        r = await client.put("/api/settings/tuning", json={"values": {"target_vmaf": "85"}})
        assert r.status_code == 200
        body = r.json()
        assert "warning" in body
        assert body["overrides"]["target_vmaf"] == "85"

    async def test_sane_values_produce_no_warning(self, client: AsyncClient):
        r = await client.put("/api/settings/tuning", json={"values": {"target_vmaf": "97"}})
        assert r.status_code == 200
        assert "warning" not in r.json()

    async def test_retired_min_floor_knob_not_editable(self, client: AsyncClient):
        r = await client.put("/api/settings/tuning", json={"values": {"vmaf_min_floor": "95"}})
        assert r.status_code == 400


class TestConfigPairValidation:
    def test_incoherent_env_floor_pair_fails_at_boot(self):
        """Review finding: TF_VMAF_SAFETY_PERC5 > TF_VMAF_SAFETY_MEAN must
        fail fast at boot — the likeliest source is porting the retired
        TF_VMAF_MIN_FLOOR=95 onto the new perc5 knob, which would silently
        re-create the mass-skip storm the decoupling fixed."""
        from pydantic import ValidationError

        from transcode_forge.config import Settings

        with pytest.raises(ValidationError, match="cannot exceed"):
            Settings(vmaf_safety_perc5=95.0)  # mean stays at the 90 default

    def test_coherent_pair_boots(self):
        from transcode_forge.config import Settings

        s = Settings(vmaf_safety_mean=95.0, vmaf_safety_perc5=95.0)
        assert s.vmaf_safety_perc5 == 95.0

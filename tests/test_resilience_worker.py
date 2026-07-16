"""Worker-resilience train, worker side — the hostile-scheduler contract.

The spec's success criteria in one sentence: no single network fault,
scheduler outage, or worker restart can crash a worker loop, lose a
finished encode's report, or mark a successful job failed. Each test
here is one clause of it (plans/worker-resilience-spec.md D4), running
the REAL agent against the REAL app through a fault-injecting wrapper.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from tests.helpers import seed_media_file
from tests.hostile_scheduler import HostileScheduler
from transcode_forge.config import Settings
from transcode_forge.models.job import JobStatus
from transcode_forge.repos import jobs as job_repo
from transcode_forge.worker.hardware import HardwareCapabilities
from transcode_forge.worker.http_agent import DrainResult, HttpWorkerAgent
from transcode_forge.worker.http_client import WorkerHttpClient
from transcode_forge.worker.outbox import Outbox
from transcode_forge.worker.reliability import Backoff, ErrorClass, classify_error

# ── Harness plumbing ────────────────────────────────────────────────────────


async def _make_agent(
    client: AsyncClient, app, tmp_path: Path, label: str
) -> tuple[HttpWorkerAgent, HostileScheduler]:
    """A real agent wired to the real app through the hostile wrapper."""
    issue = await client.post("/api/worker-tokens", json={"label": label})
    token = issue.json()["token"]
    hostile = HostileScheduler(app)
    settings = Settings(
        worker_name=label,
        scratch_dir=str(tmp_path / "scratch"),
        worker_state_dir=str(tmp_path / "state"),
    )
    agent = HttpWorkerAgent(settings, "http://test", token)
    await agent._client.aclose()  # replace the real transport with the wrapper
    agent._client = WorkerHttpClient("http://test", token, transport=ASGITransport(app=hostile))
    # start() would probe real hardware; these tests pin a cpu-only node.
    agent.capabilities = HardwareCapabilities(
        encoders=["cpu"], pairs=[("hevc", "cpu")], ffmpeg_version="7.1", os_platform="Linux"
    )
    # Match the machine identity registered below — the token-reuse guard
    # 409s a re-registration whose name/host differ from the live binding.
    agent.host = "h"
    # Tests drive loops directly — keep backoffs near-zero so injected
    # failures don't stretch the suite's wall clock.
    agent._claim_backoff = Backoff(base=0.001, cap=0.005)
    reg = await agent._client.register(
        name=label,
        host="h",
        capabilities=["cpu"],
        supported_codecs=["hevc"],
        supports_downscale=True,
        ffmpeg_version="7.1",
        max_concurrent=1,
    )
    agent.worker_id = reg["worker_id"]
    return agent, hostile


async def _queue_and_claim(client: AsyncClient, app, agent: HttpWorkerAgent, name: str):
    """Seed one file, queue it, claim it as the agent; returns the Job."""
    from transcode_forge.models.job import Job

    await seed_media_file(app.state.db, f"/media/movies/{name}.mkv", width=1920, height=1080)
    file_id_resp = await client.post(
        "/api/media/queue",
        json={"file_ids": [await _file_id(app.state.db, f"/media/movies/{name}.mkv")]},
    )
    assert file_id_resp.status_code == 200
    job_dict = await agent._client.claim_job(worker_id=agent.worker_id)
    assert job_dict is not None
    return Job.model_validate(job_dict)


async def _file_id(db, path: str) -> str:
    async with db.execute("SELECT id FROM media_files WHERE file_path = ?", (path,)) as cur:
        row = await cur.fetchone()
    return str(row["id"])


_PIPELINE_OK = {
    "source_size": 10_000,
    "space_saved": 5_000,
    "output_size": 5_000,
    "vmaf_mean": 97.0,
    "vmaf_perc5": 95.0,
    "resolved_crf": 22,
    "backend": "cpu",
}


class _StubStorage:
    """Storage stub — the storage layer has its own tests; these tests
    are about the report path."""

    async def fetch(self, ref):
        return Path(str(ref))

    async def commit(self, *, local_output, source, job, space_saved):
        return SimpleNamespace(output_size=5_000, space_saved=space_saved)

    async def cleanup(self, job):
        return None


def _pipeline_patches(agent: HttpWorkerAgent):
    """Patch the pipeline to succeed instantly and storage to a stub."""
    return (
        patch(
            "transcode_forge.worker.http_agent.run_pipeline",
            AsyncMock(return_value=dict(_PIPELINE_OK)),
        ),
        patch("transcode_forge.worker.http_agent.recover_source_path", return_value="none"),
        patch.object(agent, "_get_backend_for_job", AsyncMock(return_value=_StubStorage())),
    )


# ── Contract 1 — claim survives consecutive timeouts ────────────────────────


async def test_claim_survives_consecutive_timeouts(client: AsyncClient, app, tmp_path, caplog):
    """K transient claim failures → the worker keeps polling, never exits,
    and the log carries repr (not the empty str of an httpx timeout)."""
    agent, hostile = await _make_agent(client, app, tmp_path, "timeout-node")
    hostile.inject("claim-job", "timeout", "timeout", "timeout")

    loop_task = asyncio.create_task(agent._job_loop())
    try:
        async with asyncio.timeout(10):
            while hostile.hit_count("claim-job") < 4:
                await asyncio.sleep(0.01)
    finally:
        agent._shutting_down = True
        await loop_task  # must exit cleanly — a raise here is the old bug

    assert hostile.hit_count("claim-job") >= 4  # kept polling past 3 failures
    assert "ConnectTimeout('injected timeout')" in caplog.text  # repr, not blank


# ── Contract 2 — a completion report outlives a scheduler flap ──────────────


async def test_completion_outlives_scheduler_flap(client: AsyncClient, app, tmp_path):
    """/complete fails repeatedly after a SUCCESSFUL pipeline → the job is
    never marked failed; once the scheduler recovers the drain lands the
    completion. Exactly the finding-2 scenario, survived."""
    agent, hostile = await _make_agent(client, app, tmp_path, "flap-node")
    job = await _queue_and_claim(client, app, agent, "flap")
    hostile.inject("complete", "500", "timeout", "500")

    p1, p2, p3 = _pipeline_patches(agent)
    with p1, p2, p3:
        await agent._process_job(job)

    mid = await job_repo.get_job(app.state.db, job.id)
    assert mid.status == JobStatus.TRANSCODING  # not COMPLETE yet — and NOT FAILED
    assert agent.outbox.pending_job_ids() == {job.id}

    # Scheduler "recovers": the drain (as the job loop's fence runs it)
    # retries until the outbox is settled.
    for _ in range(5):
        if await agent._drain_outbox() is DrainResult.EMPTY:
            break
    final = await job_repo.get_job(app.state.db, job.id)
    assert final.status == JobStatus.COMPLETE
    assert final.output_size == 5_000
    assert agent.outbox.pending_job_ids() == set()


# ── Contract 3 — restart between success and delivery ───────────────────────


async def test_restart_drains_before_register_and_claim(client: AsyncClient, app, tmp_path):
    """Kill the agent between pipeline-success and delivery; a new agent
    on the same state dir delivers the report BEFORE re-registering (which
    would requeue the job) and before any claim → job ends COMPLETE."""
    agent1, hostile1 = await _make_agent(client, app, tmp_path, "mortal-node")
    job = await _queue_and_claim(client, app, agent1, "mortal")
    hostile1.inject("complete", "timeout")  # delivery lost; entry journaled

    p1, p2, p3 = _pipeline_patches(agent1)
    with p1, p2, p3:
        await agent1._process_job(job)
    assert agent1.outbox.pending_job_ids() == {job.id}
    # agent1 "dies" here (no cleanup — that's the point).

    # New life, same state dir, same token — same worker identity. Emulate
    # start()'s exact order: drain FIRST (needs only the bound token)...
    issue_headers_client = agent1._client  # same token client works
    agent2 = HttpWorkerAgent(
        Settings(
            worker_name="mortal-node",
            scratch_dir=str(tmp_path / "scratch"),
            worker_state_dir=str(tmp_path / "state"),
        ),
        "http://test",
        "unused-placeholder-token",
    )
    await agent2._client.aclose()
    agent2._client = issue_headers_client
    agent2.worker_id = agent1.worker_id

    hostile1.watch("claim-job")
    claims_before = hostile1.hit_count("claim-job")
    assert await agent2._drain_outbox() is DrainResult.EMPTY

    final = await job_repo.get_job(app.state.db, job.id)
    assert final.status == JobStatus.COMPLETE  # finished work survived the crash
    assert hostile1.hit_count("claim-job") == claims_before  # drained before any claim
    assert agent2.outbox.pending_job_ids() == set()


# ── Contract 4 — duplicate delivery is settled by idempotent receipt ────────


async def test_duplicate_delivery_settles_cleanly(client: AsyncClient, app, tmp_path):
    """A delivered-but-unacknowledged report is retried by design; the
    scheduler's idempotent receipt (PR A) answers 204 and the entry
    settles — no error, no double side effects."""
    agent, _hostile = await _make_agent(client, app, tmp_path, "dup-node")
    job = await _queue_and_claim(client, app, agent, "dup")

    p1, p2, p3 = _pipeline_patches(agent)
    with p1, p2, p3:
        await agent._process_job(job)
    assert (await job_repo.get_job(app.state.db, job.id)).status == JobStatus.COMPLETE

    # The ack was "lost": the same report is journaled and drained again.
    agent.outbox.append(
        job.id,
        "complete",
        {"output_size": 5_000, "space_saved": 5_000, "source_size": 10_000},
    )
    assert await agent._drain_outbox() is DrainResult.EMPTY  # 204 → settled, no exception
    assert (await job_repo.get_job(app.state.db, job.id)).status == JobStatus.COMPLETE


# ── Contract 5 — ownership moved during the outage ──────────────────────────


async def test_ownership_moved_discards_with_warn(client: AsyncClient, app, tmp_path, caplog):
    """The job was requeued (and possibly re-claimed elsewhere) while our
    report waited out an outage → the drain's delivery is refused (403),
    the entry is discarded with a WARN, and the agent stays healthy."""
    agent, _hostile = await _make_agent(client, app, tmp_path, "moved-node")
    job = await _queue_and_claim(client, app, agent, "moved")
    agent.outbox.append(
        job.id,
        "complete",
        {"output_size": 5_000, "space_saved": 5_000, "source_size": 10_000},
    )
    # The reconciliation sweep (or an operator) requeues the job.
    await job_repo.update_job(app.state.db, job.id, status=JobStatus.QUEUED, worker_id=None)

    assert await agent._drain_outbox() is DrainResult.EMPTY
    assert agent.outbox.pending_job_ids() == set()
    assert "its copy of reality wins" in caplog.text
    final = await job_repo.get_job(app.state.db, job.id)
    assert final.status == JobStatus.QUEUED  # stale report never applied


# ── Contract 6 — S3 per-job ordering: complete never overtakes register ─────


async def test_s3_complete_never_overtakes_register(client: AsyncClient, app, tmp_path):
    """register_derivative failing (retryably) blocks the SAME job's
    complete; the chain delivers in order once the scheduler recovers."""
    agent, hostile = await _make_agent(client, app, tmp_path, "order-node")
    job = await _queue_and_claim(client, app, agent, "order")
    hostile.watch("complete")
    hostile.inject("register-derivative", "500", "500")

    agent.outbox.append(
        job.id,
        "register_derivative",
        {"derivative_key": "k1_hevc.mkv", "output_size": 5_000},
    )
    agent.outbox.append(
        job.id,
        "complete",
        {"output_size": 5_000, "space_saved": 0, "source_size": 10_000},
    )

    assert await agent._drain_outbox() is DrainResult.BLOCKED  # register blocked (fault 1)
    assert await agent._drain_outbox() is DrainResult.BLOCKED  # still blocked (fault 2)
    assert hostile.hit_count("complete") == 0  # complete never attempted
    assert len(agent.outbox.entries()) == 2

    assert await agent._drain_outbox() is DrainResult.EMPTY  # recovered: both land, in order
    assert hostile.hit_count("complete") == 1
    final = await job_repo.get_job(app.state.db, job.id)
    assert final.status == JobStatus.COMPLETE


# ── Contract 7 — scratch cleanup never touches the outbox ───────────────────


async def test_scratch_cleanup_spares_the_outbox(tmp_path):
    from transcode_forge.worker.storage.scratch import ScratchManager

    scratch = ScratchManager(tmp_path)
    outbox = Outbox(tmp_path / "state" / "outbox")
    entry = outbox.append("job-1", "complete", {"output_size": 1})
    (tmp_path / "deadjob_abc123").mkdir()

    await scratch.cleanup_orphans(max_age_hours=0)
    await scratch.cleanup_on_shutdown()

    assert entry.path.exists()  # the journal survived both cleanup paths
    assert not (tmp_path / "deadjob_abc123").exists()  # job dirs did not


# ── Contract 9 (review) — stale entry never lands on a re-claimed job ───────


async def test_stale_entry_never_lands_on_reclaimed_job(client: AsyncClient, app, tmp_path):
    """retry_job reuses job ids: a stale attempt-1 report must resolve
    (here: 403-discarded — ownership was cleared by the retry) BEFORE the
    worker may claim again, so it can never land on attempt 2."""
    agent, _hostile = await _make_agent(client, app, tmp_path, "fence-node")
    job = await _queue_and_claim(client, app, agent, "fence")
    # Stale attempt-1 outcome, delivery lost:
    agent.outbox.append(job.id, "failed", {"error_message": "attempt-1 stale", "retry_count": 1})
    # Operator retries the job — same id, ownership cleared:
    await job_repo.update_job(
        app.state.db, job.id, status=JobStatus.PENDING, worker_id=None, error_message=None
    )

    # The job loop's fence: drain MUST settle before any claim.
    assert await agent._drain_outbox() is DrainResult.EMPTY  # 403 → discarded

    reclaimed = await agent._client.claim_job(worker_id=agent.worker_id)
    assert reclaimed is not None and reclaimed["id"] == job.id
    current = await job_repo.get_job(app.state.db, job.id)
    assert current.status == JobStatus.TRANSCODING  # attempt 2 lives
    assert current.error_message is None  # stale attempt-1 report never applied


# ── Contract 10 (review CRITICAL) — startup drain BLOCKS until settled ──────


async def test_startup_drain_retries_until_settled_before_register(
    client: AsyncClient, app, tmp_path
):
    """A scheduler blip at exactly worker-restart must not lose finished
    work: registration's job-release would requeue the finished job and
    the late delivery would be 403-discarded. The pre-register drain
    therefore RETRIES until the outbox settles — one best-effort pass
    (the reviewed bug) is not a guarantee."""
    agent, hostile = await _make_agent(client, app, tmp_path, "blip-node")
    job = await _queue_and_claim(client, app, agent, "blip")
    hostile.inject("complete", "timeout")  # life 1: delivery lost
    p1, p2, p3 = _pipeline_patches(agent)
    with p1, p2, p3:
        await agent._process_job(job)
    assert agent.outbox.pending_job_ids() == {job.id}

    # Life 2 startup: the scheduler blips for the FIRST drain attempt too,
    # then recovers. The drain must absorb the blip and settle before
    # returning — with near-zero backoff so the test stays fast.
    hostile.inject("complete", "timeout")
    with patch(
        "transcode_forge.worker.http_agent.Backoff",
        lambda **_kw: Backoff(base=0.001, cap=0.002),
    ):
        await agent._drain_before_register()

    assert agent.outbox.pending_job_ids() == set()
    assert (await job_repo.get_job(app.state.db, job.id)).status == JobStatus.COMPLETE

    # NOW registration's release can fire — a COMPLETE job is untouchable.
    await agent._client.register(
        name="blip-node",
        host="h",
        capabilities=["cpu"],
        supported_codecs=["hevc"],
        supports_downscale=True,
        ffmpeg_version="7.1",
        max_concurrent=1,
    )
    final = await job_repo.get_job(app.state.db, job.id)
    assert final.status == JobStatus.COMPLETE  # never requeued, never re-run


# ── Contract 11 (review HIGH) — a refused credential never eats a report ────


async def test_auth_refusal_keeps_the_entry_and_screams(client: AsyncClient, app, tmp_path, caplog):
    """401 says the CREDENTIAL is bad, nothing about the job's outcome —
    the entry survives (loudly) instead of being discarded like an
    ownership move, and the pre-register drain doesn't deadlock on it
    (registration surfaces the same auth failure and exits loudly)."""
    agent, hostile = await _make_agent(client, app, tmp_path, "revoked-node")
    job = await _queue_and_claim(client, app, agent, "revoked")
    agent.outbox.append(
        job.id,
        "complete",
        {"output_size": 5_000, "space_saved": 5_000, "source_size": 10_000},
    )
    # The operator rotates the token mid-flight:
    await agent._client.aclose()
    agent._client = WorkerHttpClient(
        "http://test", "revoked-token", transport=ASGITransport(app=hostile)
    )

    assert await agent._drain_outbox() is DrainResult.AUTH_BLOCKED
    assert agent.outbox.pending_job_ids() == {job.id}  # kept, not eaten
    assert "fix TF_WORKER_TOKEN" in caplog.text

    # The pre-register drain must NOT loop forever behind the bad token.
    with patch(
        "transcode_forge.worker.http_agent.Backoff",
        lambda **_kw: Backoff(base=0.001, cap=0.002),
    ):
        await agent._drain_before_register()  # returns via the AUTH path
    assert agent.outbox.pending_job_ids() == {job.id}  # journaled for next boot


# ── Contract 12 (verify regression) — a revoked token dies LOUDLY ───────────


async def test_registration_exits_loudly_on_revoked_token(client: AsyncClient, app, tmp_path):
    """The verify pass caught this regression: carving AUTH out of
    TERMINAL made the registration loop retry a 401 forever — an
    invisible zombie instead of a loud death. Any non-RETRYABLE refusal
    must raise out of _register_with_retry (Restart=always is the second
    belt, and an operator revoking a token deserves the 'it visibly
    died' signal)."""
    import httpx

    agent, hostile = await _make_agent(client, app, tmp_path, "zombie-node")
    await agent._client.aclose()
    agent._client = WorkerHttpClient(
        "http://test", "revoked-token", transport=ASGITransport(app=hostile)
    )

    with (
        patch(
            "transcode_forge.worker.http_agent.Backoff",
            lambda **_kw: Backoff(base=0.001, cap=0.002),
        ),
        pytest.raises(httpx.HTTPStatusError) as exc_info,
    ):
        async with asyncio.timeout(5):  # loop-forever would trip this
            await agent._register_with_retry()
    assert exc_info.value.response.status_code == 401


async def test_registration_survives_transport_faults(client: AsyncClient, app, tmp_path):
    """The other half of the registration policy: transport/5xx retry
    forever — a scheduler restart must never kill fleet nodes."""
    agent, hostile = await _make_agent(client, app, tmp_path, "patient-node")
    hostile.inject("register", "timeout", "500")

    with patch(
        "transcode_forge.worker.http_agent.Backoff",
        lambda **_kw: Backoff(base=0.001, cap=0.002),
    ):
        reg = await agent._register_with_retry()
    assert reg["worker_id"] == agent.worker_id  # same identity, re-registered
    assert hostile.hit_count("register") == 3  # 2 faults absorbed + 1 success


# ── Unit: reliability primitives ────────────────────────────────────────────


def test_classify_error():
    import httpx

    def _status_error(code: int) -> httpx.HTTPStatusError:
        resp = httpx.Response(code, request=httpx.Request("POST", "http://t/x"))
        return httpx.HTTPStatusError("x", request=resp.request, response=resp)

    assert classify_error(_status_error(500)) is ErrorClass.RETRYABLE
    assert classify_error(_status_error(403)) is ErrorClass.TERMINAL
    assert classify_error(_status_error(409)) is ErrorClass.TERMINAL
    assert classify_error(_status_error(422)) is ErrorClass.TERMINAL
    assert classify_error(_status_error(401)) is ErrorClass.AUTH  # credential ≠ outcome
    assert classify_error(httpx.ConnectTimeout("t")) is ErrorClass.RETRYABLE
    assert classify_error(OSError("boom")) is ErrorClass.RETRYABLE


def test_backoff_caps_and_resets():
    b = Backoff(base=1.0, cap=8.0)
    delays = [b.next_delay() for _ in range(20)]
    assert all(0 <= d <= 8.0 for d in delays)
    b.reset()
    assert b.attempt == 0


# ── Unit: outbox mechanics ──────────────────────────────────────────────────


def test_outbox_orders_and_settles(tmp_path):
    ob = Outbox(tmp_path)
    e1 = ob.append("job-a", "register_derivative", {"derivative_key": "k", "output_size": 1})
    e2 = ob.append("job-a", "complete", {"output_size": 1})
    e3 = ob.append("job-b", "failed", {"error_message": "x", "retry_count": 0})
    got = ob.entries()
    assert [(e.job_id, e.kind) for e in got] == [
        ("job-a", "register_derivative"),
        ("job-a", "complete"),
        ("job-b", "failed"),
    ]
    assert ob.pending_job_ids() == {"job-a", "job-b"}
    assert ob.oldest_pending_job_id() == "job-a"
    ob.delete(e1)
    ob.delete(e2)
    assert ob.pending_job_ids() == {"job-b"}
    ob.delete(e3)
    assert ob.entries() == []
    assert ob.oldest_pending_job_id() is None


def test_outbox_survives_reopen(tmp_path):
    """The whole point: entries persist across a process restart."""
    Outbox(tmp_path).append("job-a", "complete", {"output_size": 1})
    reopened = Outbox(tmp_path)
    assert reopened.pending_job_ids() == {"job-a"}
    # And seq keeps rising — a new entry never collides with an old one.
    e = reopened.append("job-b", "failed", {"error_message": "x", "retry_count": 0})
    assert e.seq >= 2


def test_outbox_quarantines_torn_entries(tmp_path):
    ob = Outbox(tmp_path)
    ob.append("job-a", "complete", {"output_size": 1})
    (tmp_path / "0000000099-job-x-complete.json").write_text("{torn", encoding="utf-8")
    entries = ob.entries()
    assert [e.job_id for e in entries] == ["job-a"]  # torn entry didn't block the rest
    assert list(tmp_path.glob("*.corrupt"))  # ...and was quarantined, not deleted


def test_outbox_rejects_unknown_kind(tmp_path):
    with pytest.raises(ValueError):
        Outbox(tmp_path).append("job-a", "progress", {})

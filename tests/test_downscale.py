"""Resolution downscale + same-codec shrink — contract tests (PR A: scheduler + contract).

Written against plans/downscale-shrink-spec.md before the implementation.
Each block maps to a locked spec decision:

  M — queue-time validity matrix (replaces the hard-coded h264-only filter)
  H — height semantics: fixed option list (1080/720), strictly below the
      source height, never upscale, never a no-op scale
  C — per-file target-codec resolution: same-codec shrink rides the
      downscale (hevc→hevc, av1→av1 when no codec is picked), an explicit
      selection wins, av1 never converts to hevc, h264→h264 stays out
  G — claim gating: downscale jobs only go to workers that advertise
      supports_downscale (the supported_codecs pattern) — an old worker
      would silently encode at source resolution, ignoring the field
  P — contract pass-through: target_height rides the job row and the
      claim response unchanged
"""

from httpx import ASGITransport, AsyncClient

from tests.helpers import register_worker, seed_media_file
from transcode_forge.models.job import Job, JobStatus
from transcode_forge.repos import jobs as job_repo


async def _queue(client: AsyncClient, file_ids: list[str], **body) -> dict:
    resp = await client.post("/api/media/queue", json={"file_ids": file_ids, **body})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _only_job(db) -> Job:
    jobs, total = await job_repo.list_jobs(db)
    assert total == 1
    return jobs[0]


# ── M / H — the validity matrix and height semantics ─────────────────────────


async def test_queue_h264_with_downscale_stamps_target_height(client: AsyncClient, app):
    """M/P: a 4K h264 file queued with target_height=1080 → job carries it."""
    fid = await seed_media_file(app.state.db, "/media/movies/4k.mkv")
    out = await _queue(client, [fid], target_height=1080)
    assert out == {"queued": 1, "skipped": 0}
    job = await _only_job(app.state.db)
    assert job.target_height == 1080
    assert job.target_codec == "hevc"  # settings default, unchanged


async def test_queue_without_height_keeps_h264_only_rule(client: AsyncClient, app):
    """M: no downscale → today's behavior exactly (hevc skipped, h264 queued,
    job's target_height stays NULL like every pre-feature job)."""
    h264 = await seed_media_file(app.state.db, "/media/movies/a.mkv")
    hevc = await seed_media_file(app.state.db, "/media/movies/b.mkv", codec="hevc")
    out = await _queue(client, [h264, hevc])
    assert out == {"queued": 1, "skipped": 1}
    job = await _only_job(app.state.db)
    assert job.source_path == "/media/movies/a.mkv"
    assert job.target_height is None


async def test_hevc_queueable_only_with_downscale(client: AsyncClient, app):
    """M/C: an already-HEVC file (catalog status 'complete') is skipped
    without a downscale but queues WITH one — as a same-codec shrink."""
    fid = await seed_media_file(app.state.db, "/media/movies/big-hevc.mkv", codec="hevc")
    assert await _queue(client, [fid]) == {"queued": 0, "skipped": 1}
    assert await _queue(client, [fid], target_height=1080) == {"queued": 1, "skipped": 0}
    job = await _only_job(app.state.db)
    assert job.target_codec == "hevc"
    assert job.target_height == 1080


async def test_av1_downscale_defaults_to_same_codec(client: AsyncClient, app):
    """C: an av1 source (catalog status 'skipped') with no explicit codec
    shrinks as av1 — never silently converted to the global default."""
    fid = await seed_media_file(app.state.db, "/media/movies/av1.mkv", codec="av1")
    assert await _queue(client, [fid], target_height=720) == {"queued": 1, "skipped": 0}
    job = await _only_job(app.state.db)
    assert job.target_codec == "av1"
    assert job.target_height == 720


async def test_av1_to_hevc_is_never_queued(client: AsyncClient, app):
    """C: av1 → hevc is a codec downgrade; an explicit hevc pick skips the file."""
    fid = await seed_media_file(app.state.db, "/media/movies/av1.mkv", codec="av1")
    assert await _queue(client, [fid], target_height=1080, codec="hevc") == {
        "queued": 0,
        "skipped": 1,
    }


async def test_hevc_downscale_with_explicit_av1(client: AsyncClient, app):
    """C: hevc → av1 with a downscale is a valid re-encode."""
    fid = await seed_media_file(app.state.db, "/media/movies/h.mkv", codec="hevc")
    await _queue(client, [fid], target_height=1080, codec="av1")
    assert (await _only_job(app.state.db)).target_codec == "av1"


async def test_downscale_default_codec_setting_applies_to_h264_only(client: AsyncClient, app):
    """C: with default_codec=av1 set, a downscaled h264 follows the setting
    but a downscaled hevc still shrinks same-codec (least surprise)."""
    from transcode_forge.repos import settings as settings_repo

    await settings_repo.set_override(app.state.db, "default_codec", "av1")
    h264 = await seed_media_file(app.state.db, "/media/movies/a.mkv")
    hevc = await seed_media_file(app.state.db, "/media/movies/b.mkv", codec="hevc")
    assert await _queue(client, [h264, hevc], target_height=1080) == {"queued": 2, "skipped": 0}
    jobs, _ = await job_repo.list_jobs(app.state.db)
    by_path = {j.source_path: j.target_codec for j in jobs}
    assert by_path["/media/movies/a.mkv"] == "av1"
    assert by_path["/media/movies/b.mkv"] == "hevc"


async def test_never_upscale_or_noop_scale(client: AsyncClient, app):
    """H: sources at or below the requested height are skip-counted —
    a downscale must be strictly downward."""
    at_720 = await seed_media_file(app.state.db, "/media/movies/sd.mkv", width=1280, height=720)
    at_1080 = await seed_media_file(app.state.db, "/media/movies/hd.mkv", width=1920, height=1080)
    assert await _queue(client, [at_720, at_1080], target_height=1080) == {
        "queued": 0,
        "skipped": 2,
    }
    assert await _queue(client, [at_1080], target_height=720) == {"queued": 1, "skipped": 0}


async def test_missing_height_metadata_skips_downscale(client: AsyncClient, app):
    """H: a row without probed dimensions can't prove the scale is downward — skip."""
    fid = await seed_media_file(app.state.db, "/media/movies/x.mkv", width=None, height=None)
    assert await _queue(client, [fid], target_height=720) == {"queued": 0, "skipped": 1}
    # Without a downscale the same file queues fine (today's rule needs no height).
    assert await _queue(client, [fid]) == {"queued": 1, "skipped": 0}


async def test_unsupported_height_is_rejected(client: AsyncClient, app):
    """H: the option list is fixed (1080/720) — anything else fails validation."""
    fid = await seed_media_file(app.state.db, "/media/movies/a.mkv")
    for bad in (480, 2160, 0, -1080):
        resp = await client.post("/api/media/queue", json={"file_ids": [fid], "target_height": bad})
        assert resp.status_code == 422, f"target_height={bad} must be rejected"


async def test_other_source_codecs_stay_unqueueable(client: AsyncClient, app):
    """M: the matrix covers h264/hevc/av1 — a vc1/mpeg2 source stays out
    even with a downscale (same conservatism as today)."""
    fid = await seed_media_file(app.state.db, "/media/movies/old.mkv", codec="mpeg2video")
    assert await _queue(client, [fid], target_height=1080) == {"queued": 0, "skipped": 1}


async def test_in_flight_files_still_skipped_with_downscale(client: AsyncClient, app):
    """M: queued/transcoding stay blocking in both modes — 'complete' is the
    only status the downscale path unlocks."""
    fid = await seed_media_file(app.state.db, "/media/movies/a.mkv")
    assert await _queue(client, [fid], target_height=1080) == {"queued": 1, "skipped": 0}
    # File is now transcode_status='queued' with an active job — both modes skip it.
    assert await _queue(client, [fid], target_height=720) == {"queued": 0, "skipped": 1}
    assert await _queue(client, [fid]) == {"queued": 0, "skipped": 1}


# ── G — claim gating on supports_downscale ───────────────────────────────────


async def _seed_downscale_job(app, path: str = "/m/downscale.mkv") -> Job:
    job = Job(
        source_path=path,
        library="movies",
        source_codec="hevc",
        target_codec="hevc",
        target_height=1080,
        quality_value=21,
        status=JobStatus.PENDING,
    )
    await job_repo.create_job(app.state.db, job)
    return job


async def test_old_worker_never_claims_downscale_job(client: AsyncClient, app):
    """G: a worker that doesn't advertise supports_downscale skips the older
    downscale job and claims the plain conversion behind it."""
    await _seed_downscale_job(app)
    plain = Job(
        source_path="/m/plain.mkv",
        library="movies",
        source_codec="h264",
        quality_value=21,
        status=JobStatus.PENDING,
    )
    await job_repo.create_job(app.state.db, plain)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        headers, worker_id = await register_worker(client, c, "old-worker", ["hevc"])
        r = await c.post("/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers)
        claimed = r.json()["job"]
        assert claimed is not None
        assert claimed["id"] == plain.id


async def test_downscale_capable_worker_claims_and_gets_height(client: AsyncClient, app):
    """G/P: an advertising worker claims the downscale job; the claim
    response carries target_height for the pipeline."""
    job = await _seed_downscale_job(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        headers, worker_id = await register_worker(
            client, c, "new-worker", ["hevc"], supports_downscale=True
        )
        r = await c.post("/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers)
        claimed = r.json()["job"]
        assert claimed is not None
        assert claimed["id"] == job.id
        assert claimed["target_height"] == 1080


async def test_downscale_job_stays_pending_without_capable_workers(client: AsyncClient, app):
    """G: no capable worker → the job pends, it never fails at claim time
    (exactly how codec-filtered claims behave)."""
    job = await _seed_downscale_job(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        headers, worker_id = await register_worker(client, c, "old-worker", ["hevc", "av1"])
        for _ in range(3):
            r = await c.post(
                "/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers
            )
            assert r.json()["job"] is None
    current = await job_repo.get_job(app.state.db, job.id)
    assert current.status == JobStatus.PENDING


async def test_register_persists_supports_downscale(client: AsyncClient, app):
    """G: the advertised flag lands on the worker row; omitting it (an old
    worker) defaults to False."""
    from transcode_forge.repos import workers as worker_repo

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        _, new_id = await register_worker(client, c, "new", ["hevc"], supports_downscale=True)
        _, old_id = await register_worker(client, c, "old", ["hevc"])
    assert (await worker_repo.get_worker(app.state.db, new_id)).supports_downscale is True
    assert (await worker_repo.get_worker(app.state.db, old_id)).supports_downscale is False


async def test_pending_downscale_flags_missing_capable_worker(client: AsyncClient, app):
    """G: a pending downscale job with no advertising worker online says so
    on the queue page (the expected state between the scheduler deploy and
    the fleet's worker upgrade) — and the hint clears once one joins."""
    await _seed_downscale_job(app)
    html = (await client.get("/partials/jobs")).text
    assert "downscale-capable" in html

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        await register_worker(client, c, "upgraded", ["hevc"], supports_downscale=True)
    html = (await client.get("/partials/jobs")).text
    assert "downscale-capable" not in html


# ── P — the job row round-trips the field ────────────────────────────────────


async def test_job_row_roundtrips_target_height(app):
    """P: create_job persists target_height; get_job restores it (and NULL
    stays None for pre-feature jobs)."""
    db = app.state.db
    with_height = Job(
        source_path="/m/a.mkv",
        library="movies",
        source_codec="hevc",
        quality_value=21,
        target_height=720,
    )
    without = Job(source_path="/m/b.mkv", library="movies", source_codec="h264", quality_value=21)
    await job_repo.create_job(db, with_height)
    await job_repo.create_job(db, without)
    assert (await job_repo.get_job(db, with_height.id)).target_height == 720
    assert (await job_repo.get_job(db, without.id)).target_height is None

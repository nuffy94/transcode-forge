"""End-to-end tests for the HTTP worker API + token issuance."""

from httpx import AsyncClient

from tests.helpers import register_worker
from transcode_forge.repos import worker_tokens as token_repo


class TestTokenIssuance:
    async def test_issue_returns_raw_value_once(self, client: AsyncClient):
        resp = await client.post("/api/worker-tokens", json={"label": "gpu-node"})
        assert resp.status_code == 200
        data = resp.json()
        assert "token" in data
        assert len(data["token"]) >= 32
        assert data["fingerprint"] == data["token"][:6] + "…"

    async def test_list_masks_token_value(self, client: AsyncClient):
        await client.post("/api/worker-tokens", json={"label": "a"})
        resp = await client.get("/api/worker-tokens")
        rows = resp.json()["data"]
        assert len(rows) == 1
        assert "token" not in rows[0]
        assert rows[0]["fingerprint"].endswith("…")

    async def test_revoke_by_fingerprint(self, client: AsyncClient):
        issue = await client.post("/api/worker-tokens", json={"label": "a"})
        fp = issue.json()["fingerprint"]
        resp = await client.request("DELETE", "/api/worker-tokens", json={"token": fp})
        assert resp.status_code == 200
        assert resp.json()["revoked"] is True

    async def test_admin_endpoints_require_auth(self, unauthed_client: AsyncClient):
        resp = await unauthed_client.get("/api/worker-tokens")
        assert resp.status_code == 401

    async def test_revoke_cascades_to_dead_worker(self, client: AsyncClient, app):
        """Revoking a token whose worker is dead AND has a stale heartbeat
        should also delete its registration row — the user's intent
        ('this worker is retired') is unambiguous."""
        issue = await client.post("/api/worker-tokens", json={"label": "old-laptop"})
        token = issue.json()["token"]
        reg = await client.post(
            "/api/worker/register",
            json={"name": "old-laptop", "host": "host", "capabilities": ["cpu"]},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert reg.status_code == 200
        worker_id = reg.json()["worker_id"]

        # Age the heartbeat so the cascade's staleness check fires.
        old_iso = "2020-01-01T00:00:00+00:00"
        await app.state.db.execute(
            "UPDATE workers SET last_heartbeat = ? WHERE id = ?", (old_iso, worker_id)
        )
        await app.state.db.commit()

        resp = await client.request("DELETE", "/api/worker-tokens", json={"token": token})
        assert resp.status_code == 200
        body = resp.json()
        assert body["revoked"] is True
        assert body["worker_cleaned"] is True

        list_resp = await client.get("/api/workers")
        ids = {w["id"] for w in list_resp.json()["data"]}
        assert worker_id not in ids

    async def test_revoke_leaves_live_worker_alone(self, client: AsyncClient):
        """A worker that's still online when its token is revoked should
        NOT be deleted — let it die naturally via heartbeat timeout."""
        issue = await client.post("/api/worker-tokens", json={"label": "live"})
        token = issue.json()["token"]
        reg = await client.post(
            "/api/worker/register",
            json={"name": "live", "host": "host", "capabilities": ["cpu"]},
            headers={"Authorization": f"Bearer {token}"},
        )
        worker_id = reg.json()["worker_id"]

        resp = await client.request("DELETE", "/api/worker-tokens", json={"token": token})
        assert resp.json()["worker_cleaned"] is False

        list_resp = await client.get("/api/workers")
        ids = {w["id"] for w in list_resp.json()["data"]}
        assert worker_id in ids


class TestWorkerEndpointAuth:
    """Worker-side endpoints reject missing or revoked tokens."""

    async def test_no_token_rejected(self, unauthed_client: AsyncClient):
        resp = await unauthed_client.post(
            "/api/worker/heartbeat",
            json={"worker_id": "x", "status": "online"},
        )
        assert resp.status_code == 401

    async def test_invalid_token_rejected(self, unauthed_client: AsyncClient):
        resp = await unauthed_client.post(
            "/api/worker/heartbeat",
            json={"worker_id": "x", "status": "online"},
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert resp.status_code == 401

    async def test_revoked_token_rejected(self, client: AsyncClient, app):
        # Issue and immediately revoke
        issue = await client.post("/api/worker-tokens", json={"label": "tmp"})
        token = issue.json()["token"]
        await client.request("DELETE", "/api/worker-tokens", json={"token": token})

        # Now try to use it
        from httpx import ASGITransport
        from httpx import AsyncClient as RawClient

        transport = ASGITransport(app=app)
        async with RawClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/api/worker/heartbeat",
                json={"worker_id": "x", "status": "online"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 401


class TestRegisterFlow:
    async def test_register_creates_worker(self, client: AsyncClient, app):
        issue = await client.post("/api/worker-tokens", json={"label": "ws-1"})
        token = issue.json()["token"]

        from httpx import ASGITransport
        from httpx import AsyncClient as RawClient

        transport = ASGITransport(app=app)
        async with RawClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/api/worker/register",
                json={
                    "name": "remote-1",
                    "host": "remote-host",
                    "capabilities": ["nvenc", "cpu"],
                    "ffmpeg_version": "ffmpeg 6.0",
                    "max_concurrent": 1,
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200
            worker_id = resp.json()["worker_id"]
            assert worker_id

            # Re-register: same token returns the same worker_id
            resp2 = await c.post(
                "/api/worker/register",
                json={
                    "name": "remote-1",
                    "host": "remote-host",
                    "capabilities": ["nvenc", "cpu"],
                    "ffmpeg_version": "ffmpeg 6.0",
                    "max_concurrent": 1,
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp2.json()["worker_id"] == worker_id

        # Token should now be linked to a worker
        row = await token_repo.find_active(app.state.db, token)
        assert row is not None
        assert row["worker_id"] == worker_id


class TestJobLifecycleViaHttp:
    """Full register → claim → progress → complete flow over HTTP."""

    async def test_full_lifecycle(self, client: AsyncClient, app):
        from transcode_forge.models.job import Job, JobStatus
        from transcode_forge.repos import jobs as job_repo

        # Seed a pending job in the DB
        job = Job(
            source_path="/m/some.mkv",
            library="movies",
            source_codec="h264",
            quality_value=21,
            status=JobStatus.PENDING,
        )
        await job_repo.create_job(app.state.db, job)

        # Issue a token
        issue = await client.post("/api/worker-tokens", json={"label": "w"})
        token = issue.json()["token"]

        from httpx import ASGITransport
        from httpx import AsyncClient as RawClient

        transport = ASGITransport(app=app)
        async with RawClient(transport=transport, base_url="http://test") as c:
            headers = {"Authorization": f"Bearer {token}"}

            reg = await c.post(
                "/api/worker/register",
                json={
                    "name": "w",
                    "host": "h",
                    "capabilities": ["cpu"],
                    "ffmpeg_version": "x",
                    "max_concurrent": 1,
                },
                headers=headers,
            )
            worker_id = reg.json()["worker_id"]

            # Claim
            r = await c.post(
                "/api/worker/claim-job",
                json={"worker_id": worker_id},
                headers=headers,
            )
            claimed = r.json()["job"]
            assert claimed is not None
            assert claimed["id"] == job.id
            # HTTP claim endpoint bumps from ASSIGNED to TRANSCODING so the
            # status field reflects 'actually running' for UI filters.
            assert claimed["status"] == "transcoding"

            # Progress
            r = await c.post(
                f"/api/worker/job/{job.id}/progress",
                json={"progress": 0.42, "speed": 1.5},
                headers=headers,
            )
            assert r.status_code == 204

            # Complete
            r = await c.post(
                f"/api/worker/job/{job.id}/complete",
                json={
                    "output_size": 500_000_000,
                    "space_saved": 500_000_000,
                    "source_size": 1_000_000_000,
                },
                headers=headers,
            )
            assert r.status_code == 204

        # Final state in DB
        final = await job_repo.get_job(app.state.db, job.id)
        assert final is not None
        assert final.status == JobStatus.COMPLETE
        assert final.space_saved == 500_000_000

    async def test_claim_carries_s3_backend_fields(self, client: AsyncClient, app):
        """Jobs store the library NAME (since migration 0008), so the claim
        endpoint must resolve the library row by name to attach the S3
        coordinates. Regression (found live 2026-07-06): resolving by id
        returned None for name-keyed jobs, the backend fields were dropped,
        and workers processed S3 jobs as filesystem — every S3 job failed
        with 'Source file not found'."""
        from transcode_forge.models.job import Job, JobStatus
        from transcode_forge.models.library import StorageBackendType
        from transcode_forge.repos import jobs as job_repo
        from transcode_forge.repos import libraries as lib_repo

        await lib_repo.create_library(
            app.state.db,
            name="Movies (S3)",
            media_type="movies",
            path="s3://forge-media/masters/movies/",
            backend=StorageBackendType.S3,
            s3_bucket="forge-media",
            s3_prefix="masters/movies/",
        )
        job = Job(
            source_path="masters/movies/film.mov",
            library="Movies (S3)",  # the NAME, exactly as /api/media/queue stamps it
            source_codec="h264",
            quality_value=21,
            status=JobStatus.PENDING,
        )
        await job_repo.create_job(app.state.db, job)

        issue = await client.post("/api/worker-tokens", json={"label": "s3-w"})
        token = issue.json()["token"]

        from httpx import ASGITransport
        from httpx import AsyncClient as RawClient

        transport = ASGITransport(app=app)
        async with RawClient(transport=transport, base_url="http://test") as c:
            headers = {"Authorization": f"Bearer {token}"}
            reg = await c.post(
                "/api/worker/register",
                json={
                    "name": "s3-w",
                    "host": "h",
                    "capabilities": ["cpu"],
                    "ffmpeg_version": "x",
                    "max_concurrent": 1,
                },
                headers=headers,
            )
            worker_id = reg.json()["worker_id"]

            r = await c.post(
                "/api/worker/claim-job",
                json={"worker_id": worker_id},
                headers=headers,
            )
            claimed = r.json()["job"]
            assert claimed is not None
            assert claimed["id"] == job.id
            assert claimed["_backend_type"] == "s3"
            assert claimed["_s3_bucket"] == "forge-media"
            assert claimed["_s3_prefix"] == "masters/movies/"

    async def test_claim_resolves_stray_id_keyed_job(self, client: AsyncClient, app):
        """A stray pre-0008 job may still carry the library UUID — the
        claim endpoint's id fallback must resolve it."""
        from transcode_forge.models.job import Job, JobStatus
        from transcode_forge.models.library import StorageBackendType
        from transcode_forge.repos import jobs as job_repo
        from transcode_forge.repos import libraries as lib_repo

        lib_id = await lib_repo.create_library(
            app.state.db,
            name="Movies (S3)",
            media_type="movies",
            path="s3://forge-media/masters/movies/",
            backend=StorageBackendType.S3,
            s3_bucket="forge-media",
            s3_prefix="masters/movies/",
        )
        job = Job(
            source_path="masters/movies/old.mkv",
            library=lib_id,  # pre-backfill style: the UUID
            source_codec="h264",
            quality_value=21,
            status=JobStatus.PENDING,
        )
        await job_repo.create_job(app.state.db, job)

        issue = await client.post("/api/worker-tokens", json={"label": "s3-w2"})
        token = issue.json()["token"]

        from httpx import ASGITransport
        from httpx import AsyncClient as RawClient

        transport = ASGITransport(app=app)
        async with RawClient(transport=transport, base_url="http://test") as c:
            headers = {"Authorization": f"Bearer {token}"}
            reg = await c.post(
                "/api/worker/register",
                json={
                    "name": "s3-w2",
                    "host": "h",
                    "capabilities": ["cpu"],
                    "ffmpeg_version": "x",
                    "max_concurrent": 1,
                },
                headers=headers,
            )
            r = await c.post(
                "/api/worker/claim-job",
                json={"worker_id": reg.json()["worker_id"]},
                headers=headers,
            )
            claimed = r.json()["job"]
            assert claimed is not None
            assert claimed["_backend_type"] == "s3"
            assert claimed["_s3_bucket"] == "forge-media"

    async def test_register_derivative_persists_with_real_library_id(
        self, client: AsyncClient, app
    ):
        """Regression (review of PR #34): register-derivative passed
        job.library (the NAME) into derivatives.library_id — a real FK to
        libraries(id) with PRAGMA foreign_keys=ON. The FK violation message
        contains 'constraint', which the broad dedup-race handler swallowed,
        so the endpoint returned 204 while persisting nothing: the entire
        derivative cache silently never populated. The row must exist after
        the call, keyed by the library's actual UUID."""
        from transcode_forge.models.job import Job, JobStatus
        from transcode_forge.models.library import StorageBackendType
        from transcode_forge.repos import derivatives as deriv_repo
        from transcode_forge.repos import jobs as job_repo
        from transcode_forge.repos import libraries as lib_repo

        lib_id = await lib_repo.create_library(
            app.state.db,
            name="Movies (S3)",
            media_type="movies",
            path="s3://forge-media/masters/movies/",
            backend=StorageBackendType.S3,
            s3_bucket="forge-media",
            s3_prefix="masters/movies/",
        )
        job = Job(
            source_path="masters/movies/film.mov",
            library="Movies (S3)",  # the NAME, as /api/media/queue stamps it
            source_codec="h264",
            quality_value=21,
            status=JobStatus.PENDING,
        )
        await job_repo.create_job(app.state.db, job)

        issue = await client.post("/api/worker-tokens", json={"label": "s3-w3"})
        token = issue.json()["token"]

        from httpx import ASGITransport
        from httpx import AsyncClient as RawClient

        transport = ASGITransport(app=app)
        async with RawClient(transport=transport, base_url="http://test") as c:
            headers = {"Authorization": f"Bearer {token}"}
            reg = await c.post(
                "/api/worker/register",
                json={
                    "name": "s3-w3",
                    "host": "h",
                    "capabilities": ["cpu"],
                    "ffmpeg_version": "x",
                    "max_concurrent": 1,
                },
                headers=headers,
            )
            await c.post(
                "/api/worker/claim-job",
                json={"worker_id": reg.json()["worker_id"]},
                headers=headers,
            )

            r = await c.post(
                f"/api/worker/job/{job.id}/register-derivative",
                json={"derivative_key": "derivatives/abc123.mkv", "output_size": 1000},
                headers=headers,
            )
            assert r.status_code == 204

            # Registering the same key again is a benign dedup no-op.
            r2 = await c.post(
                f"/api/worker/job/{job.id}/register-derivative",
                json={"derivative_key": "derivatives/abc123.mkv", "output_size": 1000},
                headers=headers,
            )
            assert r2.status_code == 204

        row = await deriv_repo.lookup_by_key(app.state.db, "derivatives/abc123.mkv")
        assert row is not None, "derivative row was silently dropped (FK violation swallowed)"
        assert row["library_id"] == lib_id
        assert row["output_size"] == 1000

    async def test_claim_returns_null_when_paused(self, client: AsyncClient, app):
        from transcode_forge.repos import system as system_repo

        await system_repo.set_queue_paused(app.state.db, True)

        issue = await client.post("/api/worker-tokens", json={"label": "w"})
        token = issue.json()["token"]

        from httpx import ASGITransport
        from httpx import AsyncClient as RawClient

        transport = ASGITransport(app=app)
        async with RawClient(transport=transport, base_url="http://test") as c:
            headers = {"Authorization": f"Bearer {token}"}
            reg = await c.post(
                "/api/worker/register",
                json={
                    "name": "w",
                    "host": "h",
                    "capabilities": ["cpu"],
                    "ffmpeg_version": "x",
                    "max_concurrent": 1,
                },
                headers=headers,
            )

            r = await c.post(
                "/api/worker/claim-job",
                json={"worker_id": reg.json()["worker_id"]},
                headers=headers,
            )
            data = r.json()
            assert data["job"] is None
            assert data["reason"] == "queue_paused"


async def _seed_pending_job(app, source_path: str = "/m/own.mkv"):
    """Insert a PENDING job directly in the DB; returns the Job model."""
    from transcode_forge.models.job import Job, JobStatus
    from transcode_forge.repos import jobs as job_repo

    job = Job(
        source_path=source_path,
        library="movies",
        source_codec="h264",
        quality_value=21,
        status=JobStatus.PENDING,
    )
    await job_repo.create_job(app.state.db, job)
    return job


# Shared across test modules — one home in tests/helpers.py.
_register_worker = register_worker


class TestWorkerIdentityEnforcement:
    """claim-job and heartbeat must use the worker_id bound to the caller's token."""

    async def test_claim_with_foreign_worker_id_rejected(self, client: AsyncClient, app):
        from httpx import ASGITransport
        from httpx import AsyncClient as RawClient

        transport = ASGITransport(app=app)
        async with RawClient(transport=transport, base_url="http://test") as c:
            headers, _worker_id = await _register_worker(client, c, "honest")
            r = await c.post(
                "/api/worker/claim-job",
                json={"worker_id": "someone-else"},
                headers=headers,
            )
            assert r.status_code == 403

    async def test_heartbeat_with_foreign_worker_id_rejected(self, client: AsyncClient, app):
        from httpx import ASGITransport
        from httpx import AsyncClient as RawClient

        transport = ASGITransport(app=app)
        async with RawClient(transport=transport, base_url="http://test") as c:
            headers_a, worker_a = await _register_worker(client, c, "a")
            _headers_b, worker_b = await _register_worker(client, c, "b")
            r = await c.post(
                "/api/worker/heartbeat",
                json={"worker_id": worker_b, "status": "online"},
                headers=headers_a,
            )
            assert r.status_code == 403
            r = await c.post(
                "/api/worker/heartbeat",
                json={"worker_id": worker_a, "status": "online"},
                headers=headers_a,
            )
            assert r.status_code == 204

    async def test_unregistered_token_cannot_claim(self, client: AsyncClient, app):
        from httpx import ASGITransport
        from httpx import AsyncClient as RawClient

        issue = await client.post("/api/worker-tokens", json={"label": "never-registered"})
        headers = {"Authorization": f"Bearer {issue.json()['token']}"}
        transport = ASGITransport(app=app)
        async with RawClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/worker/claim-job",
                json={"worker_id": "anything"},
                headers=headers,
            )
            assert r.status_code == 403


class TestJobOwnership:
    """Job mutation endpoints reject tokens whose worker doesn't own the job."""

    async def test_non_owner_progress_rejected(self, client: AsyncClient, app):
        from httpx import ASGITransport
        from httpx import AsyncClient as RawClient

        job = await _seed_pending_job(app)
        transport = ASGITransport(app=app)
        async with RawClient(transport=transport, base_url="http://test") as c:
            headers_a, worker_a = await _register_worker(client, c, "owner")
            headers_b, _worker_b = await _register_worker(client, c, "intruder")
            claim = await c.post(
                "/api/worker/claim-job", json={"worker_id": worker_a}, headers=headers_a
            )
            assert claim.json()["job"]["id"] == job.id

            r = await c.post(
                f"/api/worker/job/{job.id}/progress",
                json={"progress": 0.5},
                headers=headers_b,
            )
            assert r.status_code == 403
            r = await c.post(
                f"/api/worker/job/{job.id}/progress",
                json={"progress": 0.5},
                headers=headers_a,
            )
            assert r.status_code == 204

    async def test_non_owner_complete_rejected_and_state_unchanged(self, client: AsyncClient, app):
        from httpx import ASGITransport
        from httpx import AsyncClient as RawClient

        from transcode_forge.models.job import JobStatus
        from transcode_forge.repos import jobs as job_repo

        job = await _seed_pending_job(app)
        transport = ASGITransport(app=app)
        async with RawClient(transport=transport, base_url="http://test") as c:
            headers_a, worker_a = await _register_worker(client, c, "owner")
            headers_b, _worker_b = await _register_worker(client, c, "intruder")
            await c.post("/api/worker/claim-job", json={"worker_id": worker_a}, headers=headers_a)

            r = await c.post(
                f"/api/worker/job/{job.id}/complete",
                json={"output_size": 1, "space_saved": 1, "source_size": 2},
                headers=headers_b,
            )
            assert r.status_code == 403

        current = await job_repo.get_job(app.state.db, job.id)
        assert current.status == JobStatus.TRANSCODING
        assert current.worker_id == worker_a

    async def test_non_owner_failed_rejected(self, client: AsyncClient, app):
        from httpx import ASGITransport
        from httpx import AsyncClient as RawClient

        job = await _seed_pending_job(app)
        transport = ASGITransport(app=app)
        async with RawClient(transport=transport, base_url="http://test") as c:
            headers_a, worker_a = await _register_worker(client, c, "owner")
            headers_b, _worker_b = await _register_worker(client, c, "intruder")
            await c.post("/api/worker/claim-job", json={"worker_id": worker_a}, headers=headers_a)

            r = await c.post(
                f"/api/worker/job/{job.id}/failed",
                json={"error_message": "nope", "retry_count": 1},
                headers=headers_b,
            )
            assert r.status_code == 403

    async def test_non_owner_derivative_endpoints_rejected(self, client: AsyncClient, app):
        from httpx import ASGITransport
        from httpx import AsyncClient as RawClient

        job = await _seed_pending_job(app)
        transport = ASGITransport(app=app)
        async with RawClient(transport=transport, base_url="http://test") as c:
            headers_a, worker_a = await _register_worker(client, c, "owner")
            headers_b, _worker_b = await _register_worker(client, c, "intruder")
            await c.post("/api/worker/claim-job", json={"worker_id": worker_a}, headers=headers_a)

            r = await c.post(
                f"/api/worker/job/{job.id}/check-derivative",
                json={"job_id": job.id, "derivative_key": "abc"},
                headers=headers_b,
            )
            assert r.status_code == 403
            r = await c.post(
                f"/api/worker/job/{job.id}/register-derivative",
                json={"derivative_key": "abc", "output_size": 1},
                headers=headers_b,
            )
            assert r.status_code == 403

    async def test_complete_unknown_job_is_404(self, client: AsyncClient, app):
        from httpx import ASGITransport
        from httpx import AsyncClient as RawClient

        transport = ASGITransport(app=app)
        async with RawClient(transport=transport, base_url="http://test") as c:
            headers, _worker_id = await _register_worker(client, c, "w")
            r = await c.post(
                "/api/worker/job/no-such-job/complete",
                json={"output_size": 1, "space_saved": 1, "source_size": 2},
                headers=headers,
            )
            assert r.status_code == 404

    async def test_stale_worker_cannot_complete_requeued_job(self, client: AsyncClient, app):
        """The double-completion race: worker A claims, crashes, and re-registers
        (releasing the job); worker B claims it. A's stale in-flight /complete
        must be rejected and B's job state preserved."""
        from httpx import ASGITransport
        from httpx import AsyncClient as RawClient

        from transcode_forge.models.job import JobStatus
        from transcode_forge.repos import jobs as job_repo

        job = await _seed_pending_job(app)
        transport = ASGITransport(app=app)
        async with RawClient(transport=transport, base_url="http://test") as c:
            headers_a, worker_a = await _register_worker(client, c, "stale")
            claim = await c.post(
                "/api/worker/claim-job", json={"worker_id": worker_a}, headers=headers_a
            )
            assert claim.json()["job"]["id"] == job.id

            # A "crashes" and re-registers with the SAME token — the register
            # endpoint releases its orphan job back to the queue.
            rereg = await c.post(
                "/api/worker/register",
                json={"name": "stale", "host": "h", "capabilities": ["cpu"]},
                headers=headers_a,
            )
            assert rereg.json()["worker_id"] == worker_a

            # B picks the job up.
            headers_b, worker_b = await _register_worker(client, c, "fresh")
            claim_b = await c.post(
                "/api/worker/claim-job", json={"worker_id": worker_b}, headers=headers_b
            )
            assert claim_b.json()["job"]["id"] == job.id

            # A's stale completion arrives late.
            r = await c.post(
                f"/api/worker/job/{job.id}/complete",
                json={"output_size": 1, "space_saved": 1, "source_size": 2},
                headers=headers_a,
            )
            assert r.status_code == 403

            # B can still finish its job normally.
            r = await c.post(
                f"/api/worker/job/{job.id}/complete",
                json={"output_size": 1, "space_saved": 1, "source_size": 2},
                headers=headers_b,
            )
            assert r.status_code == 204

        final = await job_repo.get_job(app.state.db, job.id)
        assert final.status == JobStatus.COMPLETE
        assert final.worker_id == worker_b


class TestWorkerCrashRecovery:
    """A worker restart (re-register with the same token) must release the
    jobs it owned back to the queue (review item 12) — a crashed worker
    has no in-memory pipeline state, so its jobs would otherwise sit in
    an active status forever."""

    async def test_reregister_requeues_assigned_job(self, client: AsyncClient, app):
        from httpx import ASGITransport
        from httpx import AsyncClient as RawClient

        from transcode_forge.models.job import JobStatus
        from transcode_forge.repos import jobs as job_repo

        job = await _seed_pending_job(app)
        transport = ASGITransport(app=app)
        async with RawClient(transport=transport, base_url="http://test") as c:
            headers, worker_id = await _register_worker(client, c, "crashy")

            # Claim at the repo level so the job stays ASSIGNED — the HTTP
            # claim endpoint immediately bumps to TRANSCODING.
            claimed = await job_repo.claim_next_job(app.state.db, worker_id, ["hevc"])
            assert claimed is not None and claimed.id == job.id
            assert claimed.status == JobStatus.ASSIGNED

            # Worker "crashes" and comes back: re-register with the same token.
            rereg = await c.post(
                "/api/worker/register",
                json={"name": "crashy", "host": "h", "capabilities": ["cpu"]},
                headers=headers,
            )
            assert rereg.status_code == 200
            assert rereg.json()["worker_id"] == worker_id

        requeued = await job_repo.get_job(app.state.db, job.id)
        assert requeued is not None
        assert requeued.status == JobStatus.QUEUED
        assert requeued.worker_id is None
        assert requeued.started_at is None
        assert requeued.progress == 0.0

    async def test_reregister_requeues_transcoding_job(self, client: AsyncClient, app):
        """Late orphan: the job was already mid-encode (TRANSCODING, progress
        reported) when the worker died."""
        from httpx import ASGITransport
        from httpx import AsyncClient as RawClient

        from transcode_forge.models.job import JobStatus
        from transcode_forge.repos import jobs as job_repo

        job = await _seed_pending_job(app)
        transport = ASGITransport(app=app)
        async with RawClient(transport=transport, base_url="http://test") as c:
            headers, worker_id = await _register_worker(client, c, "crashy2")
            claim = await c.post(
                "/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers
            )
            assert claim.json()["job"]["id"] == job.id
            r = await c.post(
                f"/api/worker/job/{job.id}/progress",
                json={"progress": 0.42},
                headers=headers,
            )
            assert r.status_code == 204

            rereg = await c.post(
                "/api/worker/register",
                json={"name": "crashy2", "host": "h", "capabilities": ["cpu"]},
                headers=headers,
            )
            assert rereg.json()["worker_id"] == worker_id

        requeued = await job_repo.get_job(app.state.db, job.id)
        assert requeued is not None
        assert requeued.status == JobStatus.QUEUED
        assert requeued.worker_id is None
        assert requeued.started_at is None
        assert requeued.progress == 0.0

    async def test_reregister_only_releases_own_active_jobs(self, client: AsyncClient, app):
        """The release must be scoped: another worker's in-flight job and the
        restarting worker's own finished jobs stay untouched."""
        from httpx import ASGITransport
        from httpx import AsyncClient as RawClient

        from transcode_forge.models.job import JobStatus
        from transcode_forge.repos import jobs as job_repo

        job_a = await _seed_pending_job(app, "/m/a.mkv")
        job_b = await _seed_pending_job(app, "/m/b.mkv")
        job_done = await _seed_pending_job(app, "/m/done.mkv")
        transport = ASGITransport(app=app)
        async with RawClient(transport=transport, base_url="http://test") as c:
            headers_a, worker_a = await _register_worker(client, c, "restarts")
            headers_b, worker_b = await _register_worker(client, c, "steady")

            # A finished job owned by the restarting worker.
            await job_repo.update_job(
                app.state.db, job_done.id, status=JobStatus.COMPLETE, worker_id=worker_a
            )

            claim_a = await c.post(
                "/api/worker/claim-job", json={"worker_id": worker_a}, headers=headers_a
            )
            assert claim_a.json()["job"]["id"] == job_a.id
            claim_b = await c.post(
                "/api/worker/claim-job", json={"worker_id": worker_b}, headers=headers_b
            )
            assert claim_b.json()["job"]["id"] == job_b.id

            await c.post(
                "/api/worker/register",
                json={"name": "restarts", "host": "h", "capabilities": ["cpu"]},
                headers=headers_a,
            )

        released = await job_repo.get_job(app.state.db, job_a.id)
        assert released.status == JobStatus.QUEUED
        assert released.worker_id is None

        untouched = await job_repo.get_job(app.state.db, job_b.id)
        assert untouched.status == JobStatus.TRANSCODING
        assert untouched.worker_id == worker_b

        finished = await job_repo.get_job(app.state.db, job_done.id)
        assert finished.status == JobStatus.COMPLETE
        assert finished.worker_id == worker_a


class TestJobFailureLifecycle:
    """POST /api/worker/job/{id}/failed (review item 13)."""

    async def test_failed_persists_error_and_ends_job(self, client: AsyncClient, app):
        from httpx import ASGITransport
        from httpx import AsyncClient as RawClient

        from transcode_forge.models.job import JobStatus
        from transcode_forge.repos import jobs as job_repo

        job = await _seed_pending_job(app)
        transport = ASGITransport(app=app)
        async with RawClient(transport=transport, base_url="http://test") as c:
            headers, worker_id = await _register_worker(client, c, "w")
            await c.post("/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers)

            r = await c.post(
                f"/api/worker/job/{job.id}/failed",
                json={"error_message": "ffmpeg exited with code 1", "retry_count": 1},
                headers=headers,
            )
            assert r.status_code == 204

            # No longer active: nothing left for a worker to claim.
            reclaim = await c.post(
                "/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers
            )
            assert reclaim.json()["job"] is None

        failed = await job_repo.get_job(app.state.db, job.id)
        assert failed is not None
        assert failed.status == JobStatus.FAILED
        assert failed.retry_count == 1
        assert failed.error_message == "ffmpeg exited with code 1"
        assert failed.completed_at is not None
        # FAILED is terminal — the path no longer has an active job.
        assert await job_repo.job_exists_for_path(app.state.db, job.source_path) is False

    async def test_retry_count_increments_across_failures(self, client: AsyncClient, app):
        """Full fail → admin retry → fail-again loop: retry_count only grows
        and the last error message wins."""
        from httpx import ASGITransport
        from httpx import AsyncClient as RawClient

        from transcode_forge.models.job import JobStatus
        from transcode_forge.repos import jobs as job_repo

        job = await _seed_pending_job(app)
        transport = ASGITransport(app=app)
        async with RawClient(transport=transport, base_url="http://test") as c:
            headers, worker_id = await _register_worker(client, c, "w")

            # Attempt 1: claim and fail (the worker sends retry_count + 1).
            claim = await c.post(
                "/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers
            )
            first_retry = claim.json()["job"]["retry_count"] + 1
            r = await c.post(
                f"/api/worker/job/{job.id}/failed",
                json={"error_message": "attempt 1 boom", "retry_count": first_retry},
                headers=headers,
            )
            assert r.status_code == 204

            # Admin retries via the real retry endpoint (re-queues + increments).
            retry = await client.post(f"/api/jobs/{job.id}/retry")
            assert retry.status_code == 200
            requeued = await job_repo.get_job(app.state.db, job.id)
            assert requeued.status == JobStatus.PENDING
            assert requeued.retry_count > first_retry
            assert requeued.error_message is None

            # Attempt 2: claim and fail again.
            claim2 = await c.post(
                "/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers
            )
            assert claim2.json()["job"]["id"] == job.id
            second_retry = claim2.json()["job"]["retry_count"] + 1
            r = await c.post(
                f"/api/worker/job/{job.id}/failed",
                json={"error_message": "attempt 2 boom", "retry_count": second_retry},
                headers=headers,
            )
            assert r.status_code == 204

        final = await job_repo.get_job(app.state.db, job.id)
        assert final.status == JobStatus.FAILED
        assert final.retry_count == second_retry
        assert final.retry_count > first_retry
        assert final.error_message == "attempt 2 boom"


class TestProgressChannelPrefix:
    """The progress publish channel must derive from the configurable
    redis_prefix — hardcoding it silently breaks live updates for any
    deployment that sets TF_REDIS_PREFIX (websocket.py subscribes by prefix)."""

    async def test_progress_publishes_to_prefix_derived_channel(self, client: AsyncClient, app):
        import json

        from httpx import ASGITransport
        from httpx import AsyncClient as RawClient

        app.state.settings.redis_prefix = "custom"
        job = await _seed_pending_job(app)
        transport = ASGITransport(app=app)
        async with RawClient(transport=transport, base_url="http://test") as c:
            headers, worker_id = await _register_worker(client, c, "w")
            await c.post("/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers)
            r = await c.post(
                f"/api/worker/job/{job.id}/progress",
                json={"progress": 0.25, "speed": 2.0},
                headers=headers,
            )
            assert r.status_code == 204

        channel, payload = app.state.redis.publish.call_args.args
        assert channel == "custom:pub:progress"
        assert json.loads(payload)["job_id"] == job.id

    async def test_register_reports_prefix_derived_channel(self, client: AsyncClient, app):
        from httpx import ASGITransport
        from httpx import AsyncClient as RawClient

        app.state.settings.redis_prefix = "custom2"
        issue = await client.post("/api/worker-tokens", json={"label": "w"})
        headers = {"Authorization": f"Bearer {issue.json()['token']}"}
        transport = ASGITransport(app=app)
        async with RawClient(transport=transport, base_url="http://test") as c:
            reg = await c.post(
                "/api/worker/register",
                json={"name": "w", "host": "h", "capabilities": ["cpu"]},
                headers=headers,
            )
            assert reg.json()["redis_progress_channel"] == "custom2:pub:progress"


class TestWorkerInputBounds:
    """Worker-supplied numbers and strings are bounded at the boundary."""

    async def test_negative_sizes_rejected(self, client: AsyncClient, app):
        from httpx import ASGITransport
        from httpx import AsyncClient as RawClient

        job = await _seed_pending_job(app)
        transport = ASGITransport(app=app)
        async with RawClient(transport=transport, base_url="http://test") as c:
            headers, worker_id = await _register_worker(client, c, "w")
            await c.post("/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers)
            r = await c.post(
                f"/api/worker/job/{job.id}/complete",
                json={"output_size": -1, "space_saved": 0, "source_size": 0},
                headers=headers,
            )
            assert r.status_code == 422
            r = await c.post(
                f"/api/worker/job/{job.id}/register-derivative",
                json={"derivative_key": "k", "output_size": -5},
                headers=headers,
            )
            assert r.status_code == 422

    async def test_error_message_truncated_not_rejected(self, client: AsyncClient, app):
        """A worker carrying a huge ffmpeg stderr dump must still be able to
        mark its job failed — the message is truncated server-side, never
        422'd (a lagging v0.9.x worker has no client-side truncation)."""
        from httpx import ASGITransport
        from httpx import AsyncClient as RawClient

        from transcode_forge.repos import jobs as job_repo

        job = await _seed_pending_job(app)
        transport = ASGITransport(app=app)
        async with RawClient(transport=transport, base_url="http://test") as c:
            headers, worker_id = await _register_worker(client, c, "w")
            await c.post("/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers)
            r = await c.post(
                f"/api/worker/job/{job.id}/failed",
                json={"error_message": "x" * 50_000, "retry_count": 1},
                headers=headers,
            )
            assert r.status_code == 204

        failed = await job_repo.get_job(app.state.db, job.id)
        assert len(failed.error_message) == 10_000

    async def test_error_message_at_limit_accepted(self, client: AsyncClient, app):
        """A message exactly at the bound (what a truncating worker sends)
        still marks the job failed."""
        from httpx import ASGITransport
        from httpx import AsyncClient as RawClient

        from transcode_forge.repos import jobs as job_repo

        job = await _seed_pending_job(app)
        transport = ASGITransport(app=app)
        async with RawClient(transport=transport, base_url="http://test") as c:
            headers, worker_id = await _register_worker(client, c, "w")
            await c.post("/api/worker/claim-job", json={"worker_id": worker_id}, headers=headers)
            r = await c.post(
                f"/api/worker/job/{job.id}/failed",
                json={"error_message": "x" * 10_000, "retry_count": 1},
                headers=headers,
            )
            assert r.status_code == 204

        failed = await job_repo.get_job(app.state.db, job.id)
        assert len(failed.error_message) == 10_000


class TestTokenRebindGuard:
    """A leaked token can't silently rebind to a second machine (review
    item 16) — while the bound worker is live, a different machine identity
    is rejected with 409. Crash recovery (same identity) and legitimate
    re-provisioning (bound worker gone silent) still work."""

    async def test_second_machine_gets_409_while_worker_live(self, client: AsyncClient, app):
        from transcode_forge.repos import workers as worker_repo

        issue = await client.post("/api/worker-tokens", json={"label": "shared"})
        token = issue.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        reg = await client.post(
            "/api/worker/register",
            json={"name": "legit", "host": "host-a", "capabilities": ["cpu"]},
            headers=headers,
        )
        assert reg.status_code == 200
        worker_id = reg.json()["worker_id"]

        # A different machine presents the same token while 'legit' is live.
        hijack = await client.post(
            "/api/worker/register",
            json={"name": "evil", "host": "host-b", "capabilities": ["cpu"]},
            headers=headers,
        )
        assert hijack.status_code == 409

        # The legitimate worker's row and the token binding are untouched.
        worker = await worker_repo.get_worker(app.state.db, worker_id)
        assert (worker.name, worker.host) == ("legit", "host-a")
        row = await token_repo.find_active(app.state.db, token)
        assert row["worker_id"] == worker_id

    async def test_same_machine_re_register_still_works(self, client: AsyncClient):
        """Crash recovery: the same identity re-registers and keeps its
        worker_id (the existing orphan-job release semantics depend on it)."""
        issue = await client.post("/api/worker-tokens", json={"label": "cr"})
        headers = {"Authorization": f"Bearer {issue.json()['token']}"}
        body = {"name": "node", "host": "host-a", "capabilities": ["cpu"]}
        first = await client.post("/api/worker/register", json=body, headers=headers)
        second = await client.post("/api/worker/register", json=body, headers=headers)
        assert second.status_code == 200
        assert second.json()["worker_id"] == first.json()["worker_id"]

    async def test_rebind_allowed_once_bound_worker_goes_silent(self, client: AsyncClient, app):
        """A re-provisioned machine (new name/host, same token) may take
        over the binding once the previous worker's heartbeat is stale —
        otherwise recreating a Docker worker would brick its token."""
        from transcode_forge.repos import workers as worker_repo

        issue = await client.post("/api/worker-tokens", json={"label": "mv"})
        headers = {"Authorization": f"Bearer {issue.json()['token']}"}
        reg = await client.post(
            "/api/worker/register",
            json={"name": "old-box", "host": "host-a", "capabilities": ["cpu"]},
            headers=headers,
        )
        worker_id = reg.json()["worker_id"]

        # The old worker goes silent (stale heartbeat).
        old_iso = "2020-01-01T00:00:00+00:00"
        await app.state.db.execute(
            "UPDATE workers SET last_heartbeat = ? WHERE id = ?", (old_iso, worker_id)
        )
        await app.state.db.commit()

        takeover = await client.post(
            "/api/worker/register",
            json={"name": "new-box", "host": "host-b", "capabilities": ["cpu"]},
            headers=headers,
        )
        assert takeover.status_code == 200
        assert takeover.json()["worker_id"] == worker_id  # binding continuity
        worker = await worker_repo.get_worker(app.state.db, worker_id)
        assert (worker.name, worker.host) == ("new-box", "host-b")

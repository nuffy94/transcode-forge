"""End-to-end tests for the HTTP worker API + token issuance."""

from httpx import AsyncClient

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
            await c.post(
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
                json={"worker_id": "any"},
                headers=headers,
            )
            data = r.json()
            assert data["job"] is None
            assert data["reason"] == "queue_paused"

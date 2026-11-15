"""The two write tools, ``cancel_job`` and ``queue_file`` (docs/MCP.md,
"Scopes"). Both call the functions the REST handlers call, so this file
checks the tool surface and the scope gate; the queue rule itself is
covered where the REST route is.
"""

from __future__ import annotations

from typing import Any

from httpx import ASGITransport, AsyncClient

from transcode_forge.models.job import Job, JobStatus
from transcode_forge.repos import jobs as job_repo
from transcode_forge.repos import media as media_repo
from transcode_forge.repos import oauth as oauth_repo

from .helpers import seed_media_file

PV = "2026-07-28"
RESOURCE = "http://test/mcp"
META = {
    "io.modelcontextprotocol/protocolVersion": PV,
    "io.modelcontextprotocol/clientCapabilities": {},
}


async def _grant(db: Any, *scopes: str) -> str:
    _, raw = await oauth_repo.create_manual_grant(
        db, label="t", scopes=set(scopes), resource=RESOURCE, user_id="admin-1"
    )
    return raw


async def _call(c: AsyncClient, token: str, tool: str, arguments: dict[str, Any]) -> Any:
    return await c.post(
        "/mcp",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PV,
            "Mcp-Method": "tools/call",
            "Mcp-Name": tool,
            "Authorization": f"Bearer {token}",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments, "_meta": META},
        },
    )


def _result(resp: Any) -> dict[str, Any]:
    assert resp.status_code == 200, resp.text
    return dict(resp.json()["result"])


async def _queued_job(db: Any, name: str = "a") -> Job:
    job = Job(
        source_path=f"/movies/{name}.mkv",
        library="movies",
        source_codec="h264",
        quality_value=21,
        status=JobStatus.QUEUED,
    )
    await job_repo.create_job(db, job)
    return job


async def test_write_tools_need_the_write_scope(app: Any, mcp_running: Any) -> None:
    read_only = await _grant(app.state.db, "catalog:read", "jobs:read")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for tool, args in (("cancel_job", {"job_id": "x"}), ("queue_file", {"file_id": "x"})):
            resp = await _call(c, read_only, tool, args)
            assert resp.status_code == 403, tool
            assert 'scope="jobs:write"' in resp.headers["www-authenticate"]


async def test_cancel_job(app: Any, mcp_running: Any) -> None:
    db = app.state.db
    token = await _grant(db, "jobs:write")
    waiting = await _queued_job(db, "waiting")
    running = Job(
        source_path="/movies/running.mkv",
        library="movies",
        source_codec="h264",
        quality_value=21,
        status=JobStatus.TRANSCODING,
    )
    await job_repo.create_job(db, running)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        result = _result(await _call(c, token, "cancel_job", {"job_id": waiting.id}))
        assert not result.get("isError"), result
        assert result["structuredContent"]["job"]["status"] == "cancelled"
        cancelled = await job_repo.get_job(db, waiting.id)
        assert cancelled is not None and cancelled.status == JobStatus.CANCELLED

        held = _result(await _call(c, token, "cancel_job", {"job_id": running.id}))
        assert held["isError"] is True
        assert "transcoding" in held["content"][0]["text"]
        still = await job_repo.get_job(db, running.id)
        assert still is not None and still.status == JobStatus.TRANSCODING

        missing = _result(await _call(c, token, "cancel_job", {"job_id": "no-such-job"}))
        assert missing["isError"] is True
        assert "Job not found" in missing["content"][0]["text"]


async def test_queue_file(app: Any, mcp_running: Any) -> None:
    db = app.state.db
    token = await _grant(db, "jobs:write")
    file_id = await seed_media_file(db, "/movies/film.mkv")
    hevc_id = await seed_media_file(db, "/movies/done.mkv", codec="hevc")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        result = _result(await _call(c, token, "queue_file", {"file_id": file_id}))
        assert result["structuredContent"] == {"queued": 1, "skipped": 0}
        row = await media_repo.get_media_file(db, file_id)
        assert row is not None and row["transcode_status"] == "queued"
        jobs, total = await job_repo.list_jobs(db, limit=10, offset=0)
        assert total == 1 and jobs[0].source_path == "/movies/film.mkv"

        # Already in flight: the same rule the queue page applies.
        again = _result(await _call(c, token, "queue_file", {"file_id": file_id}))
        assert again["structuredContent"] == {"queued": 0, "skipped": 1}
        # An HEVC file is not eligible without a downscale.
        done = _result(await _call(c, token, "queue_file", {"file_id": hevc_id}))
        assert done["structuredContent"] == {"queued": 0, "skipped": 1}
        # Unknown file, bad codec, bad height.
        unknown = _result(await _call(c, token, "queue_file", {"file_id": "nope"}))
        assert unknown["structuredContent"] == {"queued": 0, "skipped": 1}
        bad_codec = _result(
            await _call(c, token, "queue_file", {"file_id": file_id, "codec": "vp9"})
        )
        assert bad_codec["isError"] is True
        bad_height = _result(
            await _call(c, token, "queue_file", {"file_id": file_id, "target_height": 480})
        )
        assert bad_height["isError"] is True


async def test_rest_cancel_route_is_unchanged(client: AsyncClient, app: Any) -> None:
    db = app.state.db
    waiting = await _queued_job(db, "rest")
    resp = await client.post(f"/api/jobs/{waiting.id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "cancelled"
    assert (await client.post(f"/api/jobs/{waiting.id}/cancel")).status_code == 400
    assert (await client.post("/api/jobs/no-such-job/cancel")).status_code == 404


async def test_cancel_refuses_a_job_a_worker_already_holds(app: Any, mcp_running: Any) -> None:
    """The waiting check and the write are one statement: a claim that landed
    first keeps its job, its worker and its claim token."""
    db = app.state.db
    token = await _grant(db, "jobs:write")
    job = await _queued_job(db, "claimed")
    await job_repo.update_job(
        db, job.id, status=JobStatus.ASSIGNED, worker_id="w-1", claim_token="t-1"
    )
    assert await job_repo.cancel_if_waiting(db, job.id) is False
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        result = _result(await _call(c, token, "cancel_job", {"job_id": job.id}))
        assert result["isError"] is True
        assert "assigned" in result["content"][0]["text"]
    held = await job_repo.get_job(db, job.id)
    assert held is not None
    assert held.status == JobStatus.ASSIGNED
    assert held.worker_id == "w-1"
    assert held.claim_token == "t-1"

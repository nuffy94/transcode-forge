"""Release gate (M4 Step 17): the headline end-to-end onboarding path.

Drives the whole customer flow through the HTTP layer in one test:
first-run setup -> add a library -> issue a worker token -> a worker
registers with that token -> it appears under Workers and the token links
to it. Registering with the issued token also exercises the hash-at-rest
auth round-trip (Step 13) through the real request path.

This lives in the main suite (not tests/e2e/) on purpose: it needs no
browser or real server, so CI enforces it on every push as a true gate.
"""

from typing import Any

from httpx import AsyncClient

ADMIN_PW = "release-gate-password-123"


async def test_install_to_add_worker_flow(unauthed_client: AsyncClient, app: Any) -> None:
    # 1. First-run setup creates the admin and logs us in.
    r = await unauthed_client.post("/api/auth/setup", json={"password": ADMIN_PW})
    assert r.status_code == 200

    # 2. Preflight is healthy (no critical library/ffmpeg issues block us).
    r = await unauthed_client.get("/api/health/preflight")
    assert r.status_code == 200

    # 3. Configure a library.
    r = await unauthed_client.post(
        "/api/libraries",
        json={"name": "Gate Movies", "media_type": "movies", "path": "/media/movies"},
    )
    assert r.status_code == 201

    # 4. Issue a worker token — the raw value is returned exactly once and is
    #    what the UI embeds in the copy-paste worker command.
    r = await unauthed_client.post("/api/worker-tokens", json={"label": "gate-node"})
    assert r.status_code == 200
    issued = r.json()
    token = issued["token"]
    fingerprint = issued["fingerprint"]
    assert token and fingerprint == token[:6] + "…"

    # 5. A worker registers using that token (bearer auth + hash lookup).
    r = await unauthed_client.post(
        "/api/worker/register",
        json={"name": "gate-worker", "host": "10.0.0.5", "capabilities": ["cpu"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    worker_id = r.json()["worker_id"]
    assert worker_id

    # 6. The worker now shows up under Workers...
    r = await unauthed_client.get("/api/workers")
    assert r.status_code == 200
    assert "gate-worker" in r.text

    # 7. ...and the issued token is linked to it (by its fingerprint).
    r = await unauthed_client.get("/api/worker-tokens")
    assert r.status_code == 200
    entry = next(t for t in r.json()["data"] if t["fingerprint"] == fingerprint)
    assert entry["worker_id"] == worker_id


async def test_revoked_token_cannot_register(unauthed_client: AsyncClient) -> None:
    """A revoked token is rejected at registration — the offboarding side of
    the gate."""
    await unauthed_client.post("/api/auth/setup", json={"password": ADMIN_PW})
    token = (await unauthed_client.post("/api/worker-tokens", json={"label": "doomed"})).json()[
        "token"
    ]
    # Revoke by fingerprint (what the UI sends).
    revoke = await unauthed_client.request(
        "DELETE", "/api/worker-tokens", json={"token": token[:6] + "…"}
    )
    assert revoke.status_code == 200

    r = await unauthed_client.post(
        "/api/worker/register",
        json={"name": "ghost", "host": "10.0.0.9", "capabilities": ["cpu"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 401

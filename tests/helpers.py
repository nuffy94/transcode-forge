"""Shared test helpers used across API/pipeline test modules.

One home for the worker-registration dance and the ffprobe stub — the
worker-token/register API shape has changed once already; a single copy
means the next change is a one-file fix.
"""

from transcode_forge.scanner.probe import ProbeResult


def make_probe(codec: str = "hevc") -> ProbeResult:
    """A 1080p hevc-ish probe result for pipeline tests."""
    return ProbeResult(
        video_codec=codec,
        width=1920,
        height=1080,
        bitrate=5_000_000,
        duration=3600.0,
        file_size=5000,
    )


async def register_worker(client, worker_client, label: str, supported_codecs=None):
    """Issue a token (admin client) and register a worker with it
    (worker client); returns (auth headers, worker_id)."""
    issue = await client.post("/api/worker-tokens", json={"label": label})
    headers = {"Authorization": f"Bearer {issue.json()['token']}"}
    body = {"name": label, "host": "h", "capabilities": ["cpu"]}
    if supported_codecs is not None:
        body["supported_codecs"] = supported_codecs
    reg = await worker_client.post("/api/worker/register", json=body, headers=headers)
    assert reg.status_code == 200
    return headers, reg.json()["worker_id"]

"""Thin HTTP client for the scheduler — used by remote workers.

Wraps httpx.AsyncClient with the bearer-token header pre-set and a
small handful of typed methods covering the worker→scheduler API.
Single responsibility: marshalling requests; no retry, no circuit
breaking — those live in the agent loop.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _raise_for_status(r: httpx.Response) -> None:
    """Like httpx's ``raise_for_status``, but fold the server's error
    detail into the message so worker logs say *why* (e.g. "401: Invalid
    or revoked token") instead of a bare status code. Keeps the
    ``HTTPStatusError`` type so the agent's existing handlers still match.
    """
    if r.is_success:
        return
    detail = ""
    try:
        body = r.json()
        if isinstance(body, dict):
            detail = str(body.get("detail", ""))
    except Exception:
        detail = r.text[:200]
    msg = f"{r.request.method} {r.request.url.path} -> {r.status_code}"
    if detail:
        msg += f": {detail}"
    raise httpx.HTTPStatusError(msg, request=r.request, response=r)


class WorkerHttpClient:
    def __init__(self, base_url: str, token: str, timeout: float = 30.0) -> None:
        if not token:
            raise ValueError("worker token is required (set TF_WORKER_TOKEN)")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def register(
        self,
        *,
        name: str,
        host: str,
        capabilities: list[str],
        ffmpeg_version: str | None,
        max_concurrent: int,
    ) -> dict[str, Any]:
        r = await self._client.post(
            "/api/worker/register",
            json={
                "name": name,
                "host": host,
                "capabilities": capabilities,
                "ffmpeg_version": ffmpeg_version,
                "max_concurrent": max_concurrent,
            },
        )
        _raise_for_status(r)
        return r.json()

    async def heartbeat(
        self,
        *,
        worker_id: str,
        status: str,
        current_job_id: str | None = None,
    ) -> None:
        r = await self._client.post(
            "/api/worker/heartbeat",
            json={
                "worker_id": worker_id,
                "status": status,
                "current_job_id": current_job_id,
            },
        )
        _raise_for_status(r)

    async def claim_job(self, *, worker_id: str) -> dict[str, Any] | None:
        r = await self._client.post(
            "/api/worker/claim-job",
            json={"worker_id": worker_id},
        )
        _raise_for_status(r)
        return r.json().get("job")

    async def progress(self, *, job_id: str, progress: float, speed: float | None) -> None:
        r = await self._client.post(
            f"/api/worker/job/{job_id}/progress",
            json={"progress": progress, "speed": speed},
        )
        _raise_for_status(r)

    async def complete(
        self,
        *,
        job_id: str,
        output_size: int,
        space_saved: int,
        source_size: int,
    ) -> None:
        r = await self._client.post(
            f"/api/worker/job/{job_id}/complete",
            json={
                "output_size": output_size,
                "space_saved": space_saved,
                "source_size": source_size,
            },
        )
        _raise_for_status(r)

    async def failed(self, *, job_id: str, error_message: str, retry_count: int = 0) -> None:
        r = await self._client.post(
            f"/api/worker/job/{job_id}/failed",
            json={"error_message": error_message, "retry_count": retry_count},
        )
        _raise_for_status(r)

    async def check_derivative(self, *, job_id: str, derivative_key: str) -> dict[str, Any]:
        """Check if a derivative already exists (S3 dedup/reuse).

        Args:
            job_id: The job ID.
            derivative_key: The content-addressed derivative key.

        Returns:
            {"found": bool, "output_size": int, "derivative_key": str} if found.
        """
        r = await self._client.post(
            f"/api/worker/job/{job_id}/check-derivative",
            json={"job_id": job_id, "derivative_key": derivative_key},
        )
        _raise_for_status(r)
        data: dict[str, Any] = r.json()
        return data

    async def register_derivative(
        self, *, job_id: str, derivative_key: str, output_size: int
    ) -> None:
        """Register a derivative after S3 upload.

        Called by the worker after uploading a transcoded file to S3.
        The scheduler inserts the row in the derivatives table.

        Args:
            job_id: The job ID.
            derivative_key: The content-addressed derivative key.
            output_size: Size of the derivative in bytes.
        """
        r = await self._client.post(
            f"/api/worker/job/{job_id}/register-derivative",
            json={"derivative_key": derivative_key, "output_size": output_size},
        )
        _raise_for_status(r)

"""Fault-injecting ASGI wrapper — the hostile-scheduler test tier.

Wraps the REAL FastAPI app so the REAL worker agent (via ASGITransport)
exercises real endpoints, with per-endpoint fault schedules driven from
each test (worker-resilience spec D4): the missing regime where nothing
used to exercise worker↔scheduler failure modes.

Faults are queued per path-substring and consumed one per matching
request, in order:

    hostile = HostileScheduler(app)
    hostile.inject("complete", "500", "timeout")   # 1st 500s, 2nd times out
    hostile.hit_count("complete")                  # requests seen (any outcome)
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import httpx


class HostileScheduler:
    """ASGI middleware with a per-endpoint fault schedule."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self._faults: dict[str, list[str]] = {}
        self._hits: Counter[str] = Counter()
        # A list, not a set: if two registered parts match one request,
        # first-registered wins deterministically (a set's iteration
        # order would make that flaky).
        self._watched: list[str] = []

    def inject(self, path_part: str, *faults: str) -> None:
        """Queue faults ('500' or 'timeout') for requests matching path_part."""
        if path_part not in self._watched:
            self._watched.append(path_part)
        self._faults.setdefault(path_part, []).extend(faults)

    def watch(self, path_part: str) -> None:
        """Count requests matching path_part without injecting faults."""
        if path_part not in self._watched:
            self._watched.append(path_part)

    def hit_count(self, path_part: str) -> int:
        return self._hits[path_part]

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        fault: str | None = None
        for part in self._watched:
            if part in path:
                self._hits[part] += 1
                queue = self._faults.get(part)
                if queue and fault is None:
                    fault = queue.pop(0)
        if fault == "timeout":
            # ASGITransport propagates app exceptions to the client call
            # site — the agent sees the same exception type a real network
            # timeout raises.
            raise httpx.ConnectTimeout("injected timeout")
        if fault == "500":
            await send(
                {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": b'{"detail": "injected 500"}'})
            return
        await self.app(scope, receive, send)

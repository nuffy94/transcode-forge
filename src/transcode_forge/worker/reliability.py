"""Retry policy for the worker's scheduler traffic — one owner.

Every loop that talks to the scheduler (outbox drain, claim, heartbeat,
registration) uses this module for two decisions (worker-resilience spec
D2): is this error worth retrying, and how long to wait before trying
again. Keeping both in one place means a new call site can't quietly
invent a different policy.

Classification:
- RETRYABLE — transport-level failures (timeouts, connection errors,
  protocol hiccups) and 5xx: the scheduler may recover; the request may
  never have arrived.
- TERMINAL — 4xx: the scheduler received and REFUSED the request
  (auth, ownership moved, validation). Retrying the same request can
  never succeed; the caller's context decides what refusal means.
"""

from __future__ import annotations

import random
from enum import StrEnum

import httpx


class ErrorClass(StrEnum):
    RETRYABLE = "retryable"
    TERMINAL = "terminal"


def classify_error(exc: BaseException) -> ErrorClass:
    """Classify a scheduler-communication failure.

    Anything that isn't a definitive server refusal is retryable —
    at-least-once delivery would rather retry a hopeless request with
    capped backoff than drop a report that had a chance.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        if 400 <= exc.response.status_code < 500:
            return ErrorClass.TERMINAL
        return ErrorClass.RETRYABLE
    return ErrorClass.RETRYABLE


class Backoff:
    """Exponential backoff with full jitter, capped.

    delay(attempt N) is uniform in [0, min(cap, base * 2**N)] — full
    jitter beats equal-jitter for thundering-herd behavior when a whole
    fleet loses the same scheduler. Attempt budgets are the CALLER's
    concern (milestone delivery is unbounded — the outbox is the budget;
    claim/heartbeat/registration loop forever with capped interval).
    """

    def __init__(self, base: float = 1.0, cap: float = 60.0) -> None:
        self.base = base
        self.cap = cap
        self.attempt = 0

    def next_delay(self) -> float:
        ceiling = min(self.cap, self.base * (2**self.attempt))
        # Grow the exponent only while it still moves the ceiling —
        # avoids a silent overflow-sized exponent on very long outages.
        if self.base * (2**self.attempt) < self.cap:
            self.attempt += 1
        return random.uniform(0, ceiling)

    def reset(self) -> None:
        self.attempt = 0

"""On-disk milestone outbox — at-least-once delivery of terminal reports.

The centerpiece of the worker-resilience train (spec D1). Once the
pipeline decides an outcome, the report is written HERE before the first
POST attempt; an entry is deleted only on acknowledged receipt. A crash,
scheduler outage, or lost response can therefore delay a report but
never lose it: the next drain (startup, or before any claim) finishes
the delivery. The scheduler's idempotent receipts (PR A) make duplicate
delivery safe, so "attempt, maybe twice" is the whole protocol.

Covers ONLY milestone reports: register_derivative, complete, skipped,
failed. Progress/phase/heartbeat stay lossy by design.

Entries are one file each — `{seq:010d}-{job_id}-{kind}.json` — so the
journal needs no index, survives partial writes (tmp + atomic replace),
and sorts into delivery order by filename. Per-job ordering is a
contract: an S3 job's register_derivative must deliver before its
complete, so a drain stops a JOB's chain on a retryable failure and
never reorders past it (other jobs' chains continue).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Milestone kinds, in the only cross-kind order that matters: an S3 job
# appends register_derivative strictly before complete.
MILESTONE_KINDS = ("register_derivative", "complete", "skipped", "failed")


@dataclass(frozen=True)
class OutboxEntry:
    """One undelivered milestone report."""

    seq: int
    job_id: str
    kind: str
    payload: dict[str, object]
    attempts: int
    path: Path
    # Epoch of the last failed delivery attempt (0.0 = never attempted)
    # and its error class ("retryable" | "auth"; "" = pre-#89 entry).
    # The drain's poison-parking gate reads both — see http_agent: only
    # RETRYABLE-classed failures may park; AUTH must keep screaming.
    last_attempt_at: float = 0.0
    last_class: str = ""


class Outbox:
    """A directory of undelivered milestone reports.

    Single-writer by construction (one agent process, one job at a time);
    no locking. File I/O is deliberately synchronous on the event loop:
    entries are tiny JSON and the claim fence keeps the journal to a
    handful of files, so the simplicity of never reasoning about
    interleaved async writes beats the microseconds a thread hop would
    save. Durability is honest, not heroic: entries survive process
    restarts and scheduler outages on any worker whose state dir is real
    storage; a Docker worker additionally survives container recreation
    only if the compose file mounts the state dir (release note says so).
    """

    def __init__(self, state_dir: Path | str) -> None:
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def append(self, job_id: str, kind: str, payload: dict[str, object]) -> OutboxEntry:
        """Journal a report BEFORE its first delivery attempt.

        tmp + os.replace so a crash mid-write can't leave a torn entry —
        the drain either sees a complete report or nothing.
        """
        if kind not in MILESTONE_KINDS:
            raise ValueError(f"Unknown milestone kind: {kind!r}")
        seq = self._next_seq()
        entry_path = self.dir / f"{seq:010d}-{job_id}-{kind}.json"
        tmp_path = entry_path.with_suffix(".tmp")
        body = {"job_id": job_id, "kind": kind, "payload": payload, "attempts": 0}
        tmp_path.write_text(json.dumps(body), encoding="utf-8")
        os.replace(tmp_path, entry_path)
        return OutboxEntry(
            seq=seq, job_id=job_id, kind=kind, payload=payload, attempts=0, path=entry_path
        )

    def entries(self) -> list[OutboxEntry]:
        """All undelivered entries in delivery (seq) order.

        A torn or unparseable file is quarantined (renamed .corrupt) with
        an ERROR rather than crashing the drain — one bad entry must not
        block every other job's chain forever.
        """
        out: list[OutboxEntry] = []
        for path in sorted(self.dir.glob("*.json")):
            try:
                seq = int(path.name.split("-", 1)[0])
                body = json.loads(path.read_text(encoding="utf-8"))
                out.append(
                    OutboxEntry(
                        seq=seq,
                        job_id=str(body["job_id"]),
                        kind=str(body["kind"]),
                        payload=dict(body["payload"]),
                        attempts=int(body.get("attempts", 0)),
                        path=path,
                        last_attempt_at=float(body.get("last_attempt_at", 0.0)),
                        last_class=str(body.get("last_class", "")),
                    )
                )
            except (ValueError, KeyError, json.JSONDecodeError, OSError):
                logger.error("Quarantining unreadable outbox entry: %s", path)
                try:
                    os.replace(path, path.with_suffix(".corrupt"))
                except OSError:
                    logger.exception("Could not quarantine %s", path)
        return out

    def delete(self, entry: OutboxEntry) -> None:
        """Acknowledge a delivered (or terminally refused) report."""
        try:
            entry.path.unlink()
        except FileNotFoundError:
            pass

    def bump_attempts(self, entry: OutboxEntry, error_class: str = "retryable") -> None:
        """Record a failed delivery attempt, its time, and its error
        class (feeds the drain's poison-parking gate)."""
        body = {
            "job_id": entry.job_id,
            "kind": entry.kind,
            "payload": entry.payload,
            "attempts": entry.attempts + 1,
            "last_attempt_at": time.time(),
            "last_class": error_class,
        }
        tmp_path = entry.path.with_suffix(".tmp")
        try:
            tmp_path.write_text(json.dumps(body), encoding="utf-8")
            os.replace(tmp_path, entry.path)
        except OSError:
            logger.warning("Could not bump attempt count on %s", entry.path)

    def pending_job_ids(self) -> set[str]:
        """Jobs with undelivered reports — the claim fence reads this.

        A worker must never claim while it holds a pending LIVE entry
        (spec + PR A review): retry_job reuses job ids, so a stale
        attempt-1 report landing on a re-claimed attempt-2 of the same
        job is the exact 'successful job marked failed' lie this train
        closes. Poison-PARKED entries (#89) are the one exception for
        the run-loop's claim gate — but never for registration, which
        requires true emptiness (see _drain_before_register).
        """
        return {e.job_id for e in self.entries()}

    def oldest_pending_job_id(self) -> str | None:
        """The job whose undelivered report is first in line.

        The heartbeat keeps NAMING this job so the scheduler's
        reconciliation sweep reads a delayed delivery as "still mine",
        not abandonment.
        """
        entries = self.entries()
        return entries[0].job_id if entries else None

    def _next_seq(self) -> int:
        top = 0
        for path in self.dir.glob("*.json"):
            try:
                top = max(top, int(path.name.split("-", 1)[0]))
            except ValueError:
                continue
        return top + 1

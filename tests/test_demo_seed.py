"""Coherence guards for the demo seed (QA redesign P2, spec D3).

The seed is the deterministic substrate every QA layer renders. Incoherent
job lifecycles (created_at stamped now() while started/completed sit hours
in the past, or a whole queue sharing one identical timestamp) made
time-ordered views unassertable and fed the AI sweep a recurring class of
false leads. These tests pin the invariants:

  * every job: created_at <= started_at <= completed_at (where present)
  * waiting jobs (pending/queued): distinct, strictly-past created_at
  * the demo-static heartbeat touch freshens live workers only
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from transcode_forge.demo.heartbeat import touch_live_worker_heartbeats
from transcode_forge.demo.seed import seed_demo_data
from transcode_forge.models.worker import WorkerStatus


def _dt(value: Any) -> datetime | None:
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def _rows(db: Any, sql: str) -> list[Any]:
    async with db.execute(sql, ()) as cur:
        return list(await cur.fetchall())


async def test_seeded_job_lifecycles_are_coherent(db: Any) -> None:
    await seed_demo_data(db)
    rows = await _rows(db, "SELECT id, status, created_at, started_at, completed_at FROM jobs")
    assert rows, "seed produced no jobs"

    for row in rows:
        created, started, completed = (
            _dt(row["created_at"]),
            _dt(row["started_at"]),
            _dt(row["completed_at"]),
        )
        assert created is not None, f"job {row['id']} has no created_at"
        if started is not None:
            assert created <= started, (
                f"job {row['id']} ({row['status']}) created {created} AFTER started {started}"
            )
        if completed is not None:
            assert started is not None and started <= completed, (
                f"job {row['id']} ({row['status']}) started {started} after completed {completed}"
            )


async def test_waiting_jobs_have_distinct_past_created_at(db: Any) -> None:
    await seed_demo_data(db)
    rows = await _rows(db, "SELECT id, created_at FROM jobs WHERE status IN ('pending', 'queued')")
    assert len(rows) == 20, "seed shape changed — update this test with it"

    stamps = [_dt(row["created_at"]) for row in rows]
    now = datetime.now(UTC)
    for stamp in stamps:
        assert stamp is not None and stamp < now - timedelta(minutes=1), (
            "waiting job stamped 'just now' — queue age reads as zero"
        )
    assert len(set(stamps)) == len(stamps), (
        "identical created_at stamps make every queue order legal (sort is unassertable)"
    )


async def test_heartbeat_touch_freshens_live_workers_only(db: Any) -> None:
    await seed_demo_data(db)
    # Age every heartbeat far past the UI's 300s alarm tier.
    aged = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    await db.execute("UPDATE workers SET last_heartbeat = ?", (aged,))
    await db.commit()

    await touch_live_worker_heartbeats(db)

    rows = await _rows(db, "SELECT name, status, last_heartbeat FROM workers")
    assert rows, "seed produced no workers"
    fresh_cutoff = datetime.now(UTC) - timedelta(seconds=30)
    live = {WorkerStatus.ONLINE, WorkerStatus.BUSY}
    saw_live = saw_offline = False
    for row in rows:
        beat = _dt(row["last_heartbeat"])
        assert beat is not None
        if row["status"] in live:
            saw_live = True
            assert beat > fresh_cutoff, f"live worker {row['name']} not touched"
        else:
            saw_offline = True
            assert beat < fresh_cutoff, (
                f"non-live worker {row['name']} was touched — alarm rendering untestable"
            )
    assert saw_live and saw_offline, "seed no longer mixes live and offline workers"

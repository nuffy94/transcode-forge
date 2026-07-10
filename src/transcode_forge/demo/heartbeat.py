"""Keep seeded demo workers' heartbeats fresh under demo-static.

demo-static deliberately runs no simulator, so seeded heartbeats age past
the UI's staleness tiers mid-run — a long-lived QA instance decays into
"HEARTBEAT LOST" cards that contradict the seeded worker statuses (the
false-lead factory behind recurring AI-sweep reports). Liveness is a
since-now quantity; a frozen seed cannot stay coherent with it, so this
minimal loop re-touches ``last_heartbeat`` for workers whose stored status
is online/busy. Offline workers keep their aged heartbeats on purpose —
the alarm rendering must stay testable.
"""

import asyncio
import logging
from datetime import UTC, datetime

from transcode_forge.db import DBConnection
from transcode_forge.models.worker import WorkerStatus

logger = logging.getLogger(__name__)

# The workers UI alarms on heartbeats older than 300s; touching every 60s
# keeps live cards comfortably fresh without meaningful load.
TOUCH_INTERVAL_S = 60


async def touch_live_worker_heartbeats(db: DBConnection) -> None:
    """One pass: freshen ``last_heartbeat`` for online/busy workers only."""
    await db.execute(
        "UPDATE workers SET last_heartbeat = ? WHERE status IN (?, ?)",
        (datetime.now(UTC).isoformat(), WorkerStatus.ONLINE, WorkerStatus.BUSY),
    )
    await db.commit()


async def run_static_heartbeat(db: DBConnection) -> None:
    """Background loop for demo-static instances (started from the lifespan)."""
    while True:
        try:
            await touch_live_worker_heartbeats(db)
        except Exception:
            logger.exception("Demo heartbeat touch failed")
        await asyncio.sleep(TOUCH_INTERVAL_S)

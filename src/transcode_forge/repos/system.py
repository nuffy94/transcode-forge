"""System state repository — global flags like queue pause."""

from datetime import UTC, datetime

from transcode_forge.db import DBConnection


async def get_state(db: DBConnection, key: str, default: str = "") -> str:
    async with db.execute("SELECT value FROM system_state WHERE key = ?", (key,)) as cur:
        row = await cur.fetchone()
        return row["value"] if row else default


async def set_state(db: DBConnection, key: str, value: str) -> None:
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET"
        " value = excluded.value, updated_at = excluded.updated_at",
        (key, value, now),
    )
    await db.commit()


async def is_queue_paused(db: DBConnection) -> bool:
    """The queue is paused if either the manual flag is set OR we're outside
    every active schedule window. Imported here to avoid a circular import.
    """
    if (await get_state(db, "queue_paused", "0")) == "1":
        return True
    from transcode_forge.repos import schedules as sched_repo

    return not await sched_repo.is_within_active_window(db)


async def set_queue_paused(db: DBConnection, paused: bool) -> None:
    await set_state(db, "queue_paused", "1" if paused else "0")

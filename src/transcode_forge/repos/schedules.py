"""Schedule repository — time-of-day windows that gate the worker queue.

The model:
- If NO enabled schedules exist, the queue is always active (current behavior
  preserved).
- If 1+ enabled schedules exist, the queue is active only when the current
  time falls inside at least one of them.

A window with start_hour < end_hour (e.g. 09 → 17) is active during the day.
A window with start_hour > end_hour (e.g. 22 → 06) wraps midnight.
A window with start_hour == end_hour is treated as "always" if days match —
useful for "Saturdays the system can run anytime."

days_mask: 7-bit integer, Monday=bit 0, Sunday=bit 6. Default 127 = every day.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from transcode_forge.db import DBConnection

DAY_MASK_ALL = 127
HOUR_RANGE = range(24)


def is_active_now(schedule: dict[str, Any], now: datetime | None = None) -> bool:
    """Return True if this schedule says the queue should be active right now.

    Disabled schedules never count.
    """
    if not schedule.get("enabled"):
        return False

    n = now or datetime.now(UTC)
    weekday_bit = 1 << n.weekday()  # Mon=0 → bit 0; Sun=6 → bit 6
    if not (schedule["days_mask"] & weekday_bit):
        return False

    start: int = schedule["start_hour"]
    end: int = schedule["end_hour"]
    hour = n.hour

    if start == end:
        # Whole-day window for the matching day.
        return True
    if start < end:
        # Same-day window, e.g. 09:00 → 17:00.
        return start <= hour < end
    # Wraps midnight, e.g. 22:00 → 06:00.
    return hour >= start or hour < end


async def list_schedules(db: DBConnection) -> list[dict[str, Any]]:
    async with db.execute(
        "SELECT id, name, enabled, start_hour, end_hour, days_mask, "
        "       created_at, updated_at "
        "FROM schedules ORDER BY created_at"
    ) as cur:
        rows = await cur.fetchall()
        return [_normalize(dict(r)) for r in rows]


async def get_schedule(db: DBConnection, schedule_id: str) -> dict[str, Any] | None:
    async with db.execute(
        "SELECT id, name, enabled, start_hour, end_hour, days_mask, "
        "       created_at, updated_at "
        "FROM schedules WHERE id = ?",
        (schedule_id,),
    ) as cur:
        row = await cur.fetchone()
        return _normalize(dict(row)) if row else None


async def create_schedule(
    db: DBConnection,
    *,
    name: str,
    start_hour: int,
    end_hour: int,
    days_mask: int = DAY_MASK_ALL,
    enabled: bool = True,
) -> str:
    _validate_window(start_hour, end_hour, days_mask)
    sched_id = str(uuid4())
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT INTO schedules (id, name, enabled, start_hour, end_hour, "
        "    days_mask, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (sched_id, name, int(enabled), start_hour, end_hour, days_mask, now, now),
    )
    await db.commit()
    return sched_id


async def update_schedule(db: DBConnection, schedule_id: str, **fields: Any) -> bool:
    if not fields:
        return False
    allowed = {"name", "enabled", "start_hour", "end_hour", "days_mask"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"Invalid schedule fields: {bad}")
    if {"start_hour", "end_hour", "days_mask"} & fields.keys():
        existing = await get_schedule(db, schedule_id)
        if existing is None:
            return False
        merged = {**existing, **fields}
        _validate_window(merged["start_hour"], merged["end_hour"], merged["days_mask"])

    fields["updated_at"] = datetime.now(UTC).isoformat()
    if "enabled" in fields:
        fields["enabled"] = int(bool(fields["enabled"]))

    set_clause = ", ".join(f"{k} = ?" for k in fields)
    cursor = await db.execute(
        f"UPDATE schedules SET {set_clause} WHERE id = ?",
        [*fields.values(), schedule_id],
    )
    await db.commit()
    return bool(cursor.rowcount)


async def delete_schedule(db: DBConnection, schedule_id: str) -> bool:
    cursor = await db.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
    await db.commit()
    return bool(cursor.rowcount)


async def is_within_active_window(db: DBConnection, now: datetime | None = None) -> bool:
    """Return True if the current time is inside any enabled schedule window.

    Returns True when no enabled schedules exist (always-active default).
    """
    schedules = await list_schedules(db)
    enabled = [s for s in schedules if s["enabled"]]
    if not enabled:
        return True
    return any(is_active_now(s, now) for s in enabled)


def _validate_window(start_hour: int, end_hour: int, days_mask: int) -> None:
    if start_hour not in HOUR_RANGE or end_hour not in HOUR_RANGE:
        raise ValueError("hours must be in 0..23")
    if not (0 <= days_mask <= DAY_MASK_ALL):
        raise ValueError(f"days_mask must be 0..{DAY_MASK_ALL}")


def _normalize(row: dict[str, Any]) -> dict[str, Any]:
    """SQLite stores enabled as INTEGER — surface a real bool to callers."""
    row["enabled"] = bool(row.get("enabled"))
    return row

"""Tests for time-of-day scheduling windows."""

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient

from transcode_forge.repos import schedules as sched_repo
from transcode_forge.repos import system as system_repo


def _sched(start: int, end: int, days: int = sched_repo.DAY_MASK_ALL, enabled: bool = True):
    return {
        "start_hour": start,
        "end_hour": end,
        "days_mask": days,
        "enabled": enabled,
    }


# 2026-05-04 is a Monday; weekday() = 0; bit 0 = 1
MONDAY = datetime(2026, 5, 4, tzinfo=UTC)
SUNDAY = datetime(2026, 5, 10, tzinfo=UTC)  # weekday()=6, bit 6 = 64


class TestIsActiveNow:
    def test_disabled_schedule_is_never_active(self):
        sched = _sched(0, 23, enabled=False)
        assert sched_repo.is_active_now(sched, MONDAY.replace(hour=12)) is False

    def test_within_same_day_window(self):
        sched = _sched(9, 17)
        assert sched_repo.is_active_now(sched, MONDAY.replace(hour=10)) is True
        assert sched_repo.is_active_now(sched, MONDAY.replace(hour=8)) is False
        # Edge: end_hour is exclusive
        assert sched_repo.is_active_now(sched, MONDAY.replace(hour=17)) is False
        # Edge: start_hour is inclusive
        assert sched_repo.is_active_now(sched, MONDAY.replace(hour=9)) is True

    def test_window_wraps_midnight(self):
        # 22:00 → 06:00 — typical "only at night"
        sched = _sched(22, 6)
        assert sched_repo.is_active_now(sched, MONDAY.replace(hour=23)) is True
        assert sched_repo.is_active_now(sched, MONDAY.replace(hour=2)) is True
        assert sched_repo.is_active_now(sched, MONDAY.replace(hour=21)) is False
        assert sched_repo.is_active_now(sched, MONDAY.replace(hour=12)) is False
        # Edge: end is exclusive even when wrapping
        assert sched_repo.is_active_now(sched, MONDAY.replace(hour=6)) is False
        # Edge: start is inclusive
        assert sched_repo.is_active_now(sched, MONDAY.replace(hour=22)) is True

    def test_equal_hours_means_full_day(self):
        sched = _sched(0, 0)
        assert sched_repo.is_active_now(sched, MONDAY.replace(hour=3)) is True
        assert sched_repo.is_active_now(sched, MONDAY.replace(hour=23)) is True

    def test_days_mask_filter(self):
        # Saturday-only: bit 5 = 32
        sched = _sched(0, 24 - 1, days=32)  # any hour, Saturday only
        sat = datetime(2026, 5, 9, hour=12, tzinfo=UTC)  # weekday()=5
        sun = datetime(2026, 5, 10, hour=12, tzinfo=UTC)  # weekday()=6
        assert sched.copy(), "(sanity)"
        assert sched_repo.is_active_now(sched, sat) is True
        assert sched_repo.is_active_now(sched, sun) is False


class TestRepo:
    async def test_create_then_get(self, db):
        sched_id = await sched_repo.create_schedule(db, name="overnight", start_hour=22, end_hour=6)
        s = await sched_repo.get_schedule(db, sched_id)
        assert s is not None
        assert s["name"] == "overnight"
        assert s["start_hour"] == 22
        assert s["enabled"] is True

    async def test_invalid_hours_rejected(self, db):
        with pytest.raises(ValueError, match="hours must be"):
            await sched_repo.create_schedule(db, name="bad", start_hour=24, end_hour=10)

    async def test_update_field(self, db):
        sched_id = await sched_repo.create_schedule(db, name="x", start_hour=9, end_hour=17)
        ok = await sched_repo.update_schedule(db, sched_id, enabled=False)
        assert ok is True
        s = await sched_repo.get_schedule(db, sched_id)
        assert s is not None and s["enabled"] is False

    async def test_delete(self, db):
        sched_id = await sched_repo.create_schedule(db, name="x", start_hour=9, end_hour=17)
        assert await sched_repo.delete_schedule(db, sched_id) is True
        assert await sched_repo.get_schedule(db, sched_id) is None

    async def test_no_schedules_means_active(self, db):
        assert await sched_repo.is_within_active_window(db) is True


class TestQueuePauseGate:
    """The system-level is_queue_paused must respect schedules."""

    async def test_no_schedules_not_paused(self, db):
        assert await system_repo.is_queue_paused(db) is False

    async def test_manual_pause_overrides_active_schedule(self, db):
        await sched_repo.create_schedule(
            db,
            name="always",
            start_hour=0,
            end_hour=0,  # full day
        )
        await system_repo.set_queue_paused(db, True)
        assert await system_repo.is_queue_paused(db) is True

    async def test_paused_when_outside_all_windows(self, db, monkeypatch):
        # 09→17 window only; pretend we're at 03:00
        await sched_repo.create_schedule(db, name="day", start_hour=9, end_hour=17)

        from transcode_forge.repos import schedules as sched_module

        original = sched_module.is_active_now

        def stub(sched, now=None):
            return original(sched, MONDAY.replace(hour=3))

        monkeypatch.setattr(sched_module, "is_active_now", stub)
        assert await system_repo.is_queue_paused(db) is True

    async def test_active_when_inside_a_window(self, db, monkeypatch):
        await sched_repo.create_schedule(db, name="day", start_hour=9, end_hour=17)

        from transcode_forge.repos import schedules as sched_module

        original = sched_module.is_active_now

        def stub(sched, now=None):
            return original(sched, MONDAY.replace(hour=12))

        monkeypatch.setattr(sched_module, "is_active_now", stub)
        assert await system_repo.is_queue_paused(db) is False


class TestApi:
    async def test_create_then_list(self, client: AsyncClient):
        resp = await client.post(
            "/api/schedules",
            json={"name": "overnight", "start_hour": 22, "end_hour": 6},
        )
        assert resp.status_code == 200
        sid = resp.json()["data"]["id"]

        resp = await client.get("/api/schedules")
        body = resp.json()
        assert body["meta"]["total"] == 1
        assert body["data"][0]["id"] == sid
        assert "queue_active_now" in body["meta"]

    async def test_patch_disables(self, client: AsyncClient):
        resp = await client.post(
            "/api/schedules",
            json={"name": "x", "start_hour": 9, "end_hour": 17},
        )
        sid = resp.json()["data"]["id"]
        resp = await client.patch(f"/api/schedules/{sid}", json={"enabled": False})
        assert resp.status_code == 200
        assert resp.json()["data"]["enabled"] is False

    async def test_delete(self, client: AsyncClient):
        resp = await client.post(
            "/api/schedules",
            json={"name": "x", "start_hour": 9, "end_hour": 17},
        )
        sid = resp.json()["data"]["id"]
        resp = await client.delete(f"/api/schedules/{sid}")
        assert resp.status_code == 200
        # Now 404
        resp = await client.delete(f"/api/schedules/{sid}")
        assert resp.status_code == 404

    async def test_invalid_hour_rejected_by_pydantic(self, client: AsyncClient):
        resp = await client.post(
            "/api/schedules",
            json={"name": "x", "start_hour": 99, "end_hour": 17},
        )
        assert resp.status_code == 422

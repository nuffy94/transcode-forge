"""Schedule endpoints — control when the queue is allowed to run."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from transcode_forge.api.deps import get_db
from transcode_forge.db import DBConnection
from transcode_forge.repos import schedules as sched_repo

router = APIRouter(tags=["schedules"])


class ScheduleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    start_hour: int = Field(ge=0, le=23)
    end_hour: int = Field(ge=0, le=23)
    days_mask: int = Field(default=sched_repo.DAY_MASK_ALL, ge=0, le=sched_repo.DAY_MASK_ALL)
    enabled: bool = True


class ScheduleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    start_hour: int | None = Field(default=None, ge=0, le=23)
    end_hour: int | None = Field(default=None, ge=0, le=23)
    days_mask: int | None = Field(default=None, ge=0, le=sched_repo.DAY_MASK_ALL)
    enabled: bool | None = None


@router.get("/schedules")
async def list_all(db: DBConnection = Depends(get_db)) -> dict[str, Any]:
    rows = await sched_repo.list_schedules(db)
    active = await sched_repo.is_within_active_window(db)
    return {"data": rows, "meta": {"total": len(rows), "queue_active_now": active}}


@router.post("/schedules")
async def create(body: ScheduleCreate, db: DBConnection = Depends(get_db)) -> dict[str, Any]:
    sched_id = await sched_repo.create_schedule(
        db,
        name=body.name,
        start_hour=body.start_hour,
        end_hour=body.end_hour,
        days_mask=body.days_mask,
        enabled=body.enabled,
    )
    sched = await sched_repo.get_schedule(db, sched_id)
    return {"data": sched}


@router.patch("/schedules/{schedule_id}")
async def update(
    schedule_id: str,
    body: ScheduleUpdate,
    db: DBConnection = Depends(get_db),
) -> dict[str, Any]:
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    updated = await sched_repo.update_schedule(db, schedule_id, **fields)
    if not updated:
        raise HTTPException(status_code=404, detail="Schedule not found")
    sched = await sched_repo.get_schedule(db, schedule_id)
    return {"data": sched}


@router.delete("/schedules/{schedule_id}")
async def delete(schedule_id: str, db: DBConnection = Depends(get_db)) -> dict[str, Any]:
    deleted = await sched_repo.delete_schedule(db, schedule_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return {"deleted": True}

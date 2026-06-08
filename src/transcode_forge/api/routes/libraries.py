"""Library CRUD endpoints — manage media storage locations."""

import json
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from transcode_forge.api.deps import get_db
from transcode_forge.db import DBConnection
from transcode_forge.repos import libraries as lib_repo
from transcode_forge.scanner.scanner import scan_library

router = APIRouter(tags=["libraries"])


class LibraryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    media_type: str = Field(pattern=r"^(movies|tv|anime)$")
    path: str = Field(min_length=1)
    quality_preset: int = Field(default=21, ge=1, le=51)
    auto_scan: bool = False
    scan_interval_hours: int = Field(default=24, ge=1)


class LibraryUpdate(BaseModel):
    name: str | None = None
    quality_preset: int | None = Field(default=None, ge=1, le=51)
    enabled: bool | None = None
    auto_scan: bool | None = None
    scan_interval_hours: int | None = Field(default=None, ge=1)


@router.get("/libraries")
async def list_libraries(
    media_type: str | None = None,
    db: DBConnection = Depends(get_db),
) -> dict[str, Any]:
    libs = await lib_repo.list_libraries(db, media_type=media_type)
    return {"data": libs}


@router.post("/libraries", status_code=201)
async def create_library(
    body: LibraryCreate,
    response: Response,
    db: DBConnection = Depends(get_db),
) -> dict[str, Any]:
    if await lib_repo.path_in_use(db, body.path):
        raise HTTPException(409, "A library with this path already exists")
    lib_id = await lib_repo.create_library(
        db,
        name=body.name,
        media_type=body.media_type,
        path=body.path,
        quality_preset=body.quality_preset,
        auto_scan=body.auto_scan,
        scan_interval_hours=body.scan_interval_hours,
    )
    lib = await lib_repo.get_library(db, lib_id)
    trigger = {"showToast": {"message": "Library added", "type": "success"}}
    response.headers["HX-Trigger"] = json.dumps(trigger)
    return {"data": lib}


@router.put("/libraries/{lib_id}")
async def update_library(
    lib_id: str,
    body: LibraryUpdate,
    db: DBConnection = Depends(get_db),
) -> dict[str, Any]:
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(400, "No fields to update")
    lib = await lib_repo.update_library(db, lib_id, **updates)
    if not lib:
        raise HTTPException(404, "Library not found")
    return {"data": lib}


@router.delete("/libraries/{lib_id}")
async def delete_library(
    lib_id: str,
    response: Response,
    db: DBConnection = Depends(get_db),
) -> dict[str, Any]:
    removed = await lib_repo.delete_library(db, lib_id)
    if not removed:
        raise HTTPException(404, "Library not found")
    trigger = {"showToast": {"message": "Library removed", "type": "info"}}
    response.headers["HX-Trigger"] = json.dumps(trigger)
    return {"status": "deleted"}


@router.post("/libraries/{lib_id}/scan", status_code=202)
async def trigger_library_scan(
    lib_id: str,
    background_tasks: BackgroundTasks,
    max_files: int = 0,
    db: DBConnection = Depends(get_db),
) -> dict[str, Any]:
    lib = await lib_repo.get_library(db, lib_id)
    if not lib:
        raise HTTPException(404, "Library not found")

    background_tasks.add_task(
        scan_library,
        library_id=lib["id"],
        library_name=lib["name"],
        library_path=lib["path"],
        media_type=lib["media_type"],
        db=db,
        max_files=max_files,
    )
    return {"status": "scanning", "library": lib["name"]}

"""Library CRUD endpoints — manage media storage locations."""

import json
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from pydantic import BaseModel, Field, model_validator

from transcode_forge.api.deps import get_db, get_settings
from transcode_forge.api.routes.scan import run_scan
from transcode_forge.config import Settings
from transcode_forge.db import DBConnection
from transcode_forge.models.library import StorageBackendType
from transcode_forge.repos import libraries as lib_repo

router = APIRouter(tags=["libraries"])


class LibraryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    media_type: str = Field(pattern=r"^(movies|tv|anime)$")
    # Required for filesystem libraries; derived (s3://bucket/prefix) for S3.
    path: str = ""
    quality_preset: int = Field(default=21, ge=1, le=51)
    auto_scan: bool = False
    scan_interval_hours: int = Field(default=24, ge=1)
    backend: StorageBackendType = StorageBackendType.FILESYSTEM
    s3_bucket: str | None = Field(default=None, max_length=63)
    s3_prefix: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def _validate_backend_fields(self) -> "LibraryCreate":
        if self.backend == StorageBackendType.S3:
            if not self.s3_bucket:
                raise ValueError("s3_bucket is required for an S3 library")
            if not self.path:
                self.path = f"s3://{self.s3_bucket}/{self.s3_prefix or ''}"
        else:
            if not self.path:
                raise ValueError("path is required for a filesystem library")
            self.s3_bucket = None
            self.s3_prefix = None
        return self


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
        backend=body.backend,
        s3_bucket=body.s3_bucket,
        s3_prefix=body.s3_prefix,
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
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    lib = await lib_repo.get_library(db, lib_id)
    if not lib:
        raise HTTPException(404, "Library not found")

    # run_scan dispatches on the library backend (filesystem vs S3).
    background_tasks.add_task(
        run_scan,
        lib["id"],
        lib["name"],
        lib["path"],
        lib["media_type"],
        max_files,
        db,
        settings,
    )
    return {"status": "scanning", "library": lib["name"]}

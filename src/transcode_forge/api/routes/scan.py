"""Scan endpoints — trigger and monitor library scans."""

import json
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from transcode_forge.api.deps import get_db, get_settings
from transcode_forge.config import Settings
from transcode_forge.db import DBConnection
from transcode_forge.repos import libraries as lib_repo
from transcode_forge.repos import scans as scan_repo
from transcode_forge.scanner import runner

logger = logging.getLogger(__name__)

router = APIRouter(tags=["scans"])


class ScanRequest(BaseModel):
    library: str | None = None  # None = scan all libraries
    limit: int = Field(
        default=0, ge=0, le=1_000_000, description="Max files to queue (0 = unlimited)"
    )


class ScanResponse(BaseModel):
    scan_ids: list[str]
    status: str = "running"
    # Libraries not started because a scan of them is already running.
    skipped: list[str] = []


@router.post("/scan", status_code=202)
async def trigger_scan(
    body: ScanRequest,
    background_tasks: BackgroundTasks,
    response: Response,
    db: DBConnection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ScanResponse:
    """Trigger a library scan. Runs in background, returns immediately."""
    # Get libraries from DB, falling back to config if DB is empty
    db_libs = await lib_repo.list_libraries(db, enabled_only=True)

    if not db_libs:
        # Seed libraries from config on first scan. Scheduled scans on by
        # default: without them the catalog only ever reflects this one
        # manual scan (live: no scan since 2026-05-05, both rows auto_scan=0).
        config_libs = settings.libraries
        for name, (path, quality) in config_libs.items():
            media_type = name  # movies, tv, anime map directly
            await lib_repo.create_library(
                db,
                name=name,
                media_type=media_type,
                path=path,
                quality_preset=quality,
                auto_scan=True,
            )
        db_libs = await lib_repo.list_libraries(db, enabled_only=True)

    if body.library:
        targets = [lib for lib in db_libs if lib["name"] == body.library]
        if not targets:
            valid = [lib["name"] for lib in db_libs]
            raise HTTPException(
                status_code=400,
                detail=f"Unknown library '{body.library}'. Valid: {valid}",
            )
    else:
        targets = db_libs

    started: list[str] = []
    busy: list[str] = []
    for lib in targets:
        if settings.demo_mode:
            from transcode_forge.demo.simulator import simulate_scan

            background_tasks.add_task(
                simulate_scan,
                lib["id"],
                lib["name"],
                lib["media_type"],
                body.limit,
                db,
            )
            started.append(lib["name"])
            continue
        task = runner.start_scan(
            library_id=lib["id"],
            library_name=lib["name"],
            library_path=lib["path"],
            media_type=lib["media_type"],
            limit=body.limit,
            db=db,
            settings=settings,
        )
        (started if task is not None else busy).append(lib["name"])

    if not started:
        raise HTTPException(
            status_code=409,
            detail=f"A scan is already running for: {', '.join(busy)}",
        )
    trigger = {"showToast": {"message": "Library scan started", "type": "info"}}
    response.headers["HX-Trigger"] = json.dumps(trigger)
    return ScanResponse(scan_ids=started, skipped=busy)


@router.get("/scans")
async def list_scans(
    limit: int = Query(20, ge=1, le=200),
    page: int = Query(1, ge=1),
    db: DBConnection = Depends(get_db),
) -> dict[str, Any]:
    """List scan history with pagination."""
    offset = (page - 1) * limit
    scans, total = await scan_repo.list_scans(db, limit=limit, offset=offset)
    return {
        "data": [s.model_dump(mode="json") for s in scans],
        "meta": {"total": total, "page": page, "per_page": limit},
    }


@router.get("/scans/{scan_id}")
async def get_scan(
    scan_id: str,
    db: DBConnection = Depends(get_db),
) -> dict[str, Any]:
    """Get a single scan by ID."""
    scan = await scan_repo.get_scan(db, scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="Scan not found")
    return {"data": scan.model_dump(mode="json")}

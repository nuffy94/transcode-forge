"""Tuning-settings endpoints — the editable slice of configuration.

Only the allowlisted keys in repos/settings.py are readable/writable here.
Secret and infra settings (db_url, auth_secret, Redis URL, S3 credentials,
library paths, worker-side settings) are env-only by design and never
appear on this surface.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from transcode_forge.api.deps import get_db
from transcode_forge.db import DBConnection
from transcode_forge.repos import settings as settings_repo

router = APIRouter(tags=["settings"])


class TuningUpdate(BaseModel):
    # key → new value (as string). None/empty clears the override so the
    # env default applies again.
    values: dict[str, str | None]


@router.get("/settings/tuning")
async def get_tuning(
    request: Request,
    db: DBConnection = Depends(get_db),
) -> dict[str, Any]:
    """Effective tuning settings plus which of them are DB overrides."""
    settings = getattr(request.app.state, "settings", None)
    return {
        "data": await settings_repo.all_effective(db, settings),
        "overrides": await settings_repo.all_overrides(db),
    }


@router.put("/settings/tuning")
async def update_tuning(
    body: TuningUpdate,
    request: Request,
    db: DBConnection = Depends(get_db),
) -> dict[str, Any]:
    """Set (or clear) DB overrides for allowlisted tuning settings.

    Validation happens per key in the settings repo; the first invalid
    entry rejects the whole request so partial writes don't happen silently.
    """
    # Validate everything first, then write — no partial application.
    for key, value in body.values.items():
        if key not in settings_repo.TUNABLE_KEYS:
            raise HTTPException(status_code=400, detail=f"Setting {key!r} is not editable")
        if value is not None and value != "":
            try:
                settings_repo.TUNABLE_KEYS[key](value)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    for key, value in body.values.items():
        if value is None or value == "":
            await settings_repo.clear_override(db, key)
        else:
            await settings_repo.set_override(db, key, value)

    settings = getattr(request.app.state, "settings", None)
    return {
        "data": await settings_repo.all_effective(db, settings),
        "overrides": await settings_repo.all_overrides(db),
    }

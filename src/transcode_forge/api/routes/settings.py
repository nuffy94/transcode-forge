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

    Cross-field rules for the VMAF knobs (vmaf-decoupling spec §4.6),
    enforced ONLY when the request touches the involved keys — a save
    that edits an unrelated setting must never be blocked by floor state
    it didn't change (pre-existing incoherence is a boot/env problem,
    caught by config.py's validator):
    - HARD: safety perc5 > safety mean is rejected — per-frame perc5 can
      never exceed the mean, so that gate would be incoherent.
    - SOFT: target below the safety mean is allowed but flagged in the
      response ("warning") — the search would aim below the refuse bar.
    Never silently clamped: silent mutation is how the incoherent 91.5/95
    combo shipped unnoticed pre-redesign.
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

    settings = getattr(request.app.state, "settings", None)

    async def prospective(key: str) -> float:
        """The value a key WOULD have after this update lands."""
        if key in body.values:
            value = body.values[key]
            if value is not None and value != "":
                return float(value)
            # Clearing the override → the env default applies.
            from transcode_forge.config import get_settings

            return float(getattr(settings or get_settings(), key))
        return float(await settings_repo.effective(db, key, settings))

    touched = body.values.keys()
    if {"vmaf_safety_mean", "vmaf_safety_perc5"} & touched:
        safety_mean = await prospective("vmaf_safety_mean")
        safety_perc5 = await prospective("vmaf_safety_perc5")
        if safety_perc5 > safety_mean:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"VMAF safety perc5 floor ({safety_perc5:g}) cannot exceed the "
                    f"safety mean floor ({safety_mean:g}) — worst-scenes scores are "
                    "always at or below the mean."
                ),
            )
    warning: str | None = None
    if {"target_vmaf", "vmaf_safety_mean"} & touched:
        safety_mean = await prospective("vmaf_safety_mean")
        target = await prospective("target_vmaf")
        if target < safety_mean:
            warning = (
                f"Target VMAF ({target:g}) is below the safety mean floor "
                f"({safety_mean:g}) — the CRF search will aim below the gate's "
                "refuse bar and most encodes will be skipped."
            )

    for key, value in body.values.items():
        if value is None or value == "":
            await settings_repo.clear_override(db, key)
        else:
            await settings_repo.set_override(db, key, value)

    response: dict[str, Any] = {
        "data": await settings_repo.all_effective(db, settings),
        "overrides": await settings_repo.all_overrides(db),
    }
    if warning:
        response["warning"] = warning
    return response

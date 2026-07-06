"""Settings-override repository — DB-backed runtime overrides for tuning knobs.

Resolution order is always `effective(key)` = DB override if set, else the
env-var default from config.py. Only the allowlisted TUNABLE_KEYS below can
ever be overridden; secret/infra settings (db_url, auth_secret, token_pepper,
redis_url, S3 credentials, library paths, worker-side settings) are
env-only by design — they're the security and bootstrap boundary.
"""

from collections.abc import Callable
from datetime import UTC, datetime

from transcode_forge.config import Settings, get_settings
from transcode_forge.db import DBConnection
from transcode_forge.models.job import TargetCodec


def _validate_codec(value: str) -> str:
    if value not in tuple(TargetCodec):
        raise ValueError(f"Invalid codec {value!r}. Valid: {[c.value for c in TargetCodec]}")
    return value


def _validate_vmaf(value: str) -> str:
    try:
        score = float(value)
    except ValueError as exc:
        raise ValueError(f"VMAF value must be a number, got {value!r}") from exc
    if not 0.0 <= score <= 100.0:
        raise ValueError(f"VMAF value must be 0-100, got {score}")
    return value


def _validate_quality(value: str) -> str:
    try:
        quality = int(value)
    except ValueError as exc:
        raise ValueError(f"Quality preset must be an integer, got {value!r}") from exc
    if not 1 <= quality <= 51:
        raise ValueError(f"Quality preset must be 1-51, got {quality}")
    return value


# key -> value validator. This is the complete set of override-able settings;
# set_override rejects anything else (secrets can never become DB-editable
# by accident — extending this list is an explicit, reviewed act).
TUNABLE_KEYS: dict[str, Callable[[str], str]] = {
    "default_codec": _validate_codec,
    "target_vmaf": _validate_vmaf,
    "vmaf_safety_mean": _validate_vmaf,
    "vmaf_safety_perc5": _validate_vmaf,
    "quality_movies": _validate_quality,
    "quality_tv": _validate_quality,
    "quality_anime": _validate_quality,
}
# vmaf_min_floor was retired by the gate decoupling (2026-07-05): the gate
# no longer derives from the target, so the old coupled floor is neither
# read nor editable. A stale app_settings row for it is ignored on purpose
# — deleting it would break rollback to a v0.9.x binary.


async def get_override(db: DBConnection, key: str) -> str | None:
    """Return the DB override for a key, or None if not set."""
    async with db.execute("SELECT value FROM app_settings WHERE key = ?", (key,)) as cur:
        row = await cur.fetchone()
        return row["value"] if row else None


async def set_override(db: DBConnection, key: str, value: str) -> None:
    """Set a DB override for an allowlisted tuning key.

    Raises:
        ValueError: If the key is not allowlisted or the value is invalid.
    """
    validator = TUNABLE_KEYS.get(key)
    if validator is None:
        raise ValueError(f"Setting {key!r} is not overridable. Tunable: {sorted(TUNABLE_KEYS)}")
    validated = validator(value)
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET"
        " value = excluded.value, updated_at = excluded.updated_at",
        (key, validated, now),
    )
    await db.commit()


async def clear_override(db: DBConnection, key: str) -> None:
    """Remove a DB override so the env default applies again."""
    await db.execute("DELETE FROM app_settings WHERE key = ?", (key,))
    await db.commit()


async def effective(db: DBConnection, key: str, settings: Settings | None = None) -> str:
    """Resolve a tuning setting: DB override if set, else the env default.

    Values are returned as strings (the storage format); callers coerce
    (e.g. float(await effective(db, "target_vmaf"))).

    Raises:
        ValueError: If the key is not a tunable setting.
    """
    if key not in TUNABLE_KEYS:
        raise ValueError(
            f"Setting {key!r} is not a tunable setting. Tunable: {sorted(TUNABLE_KEYS)}"
        )
    override = await get_override(db, key)
    if override is not None:
        return override
    if settings is None:
        settings = get_settings()
    return str(getattr(settings, key))


async def all_effective(db: DBConnection, settings: Settings | None = None) -> dict[str, str]:
    """Resolve every tunable setting at once (for the settings page)."""
    return {key: await effective(db, key, settings) for key in TUNABLE_KEYS}


async def all_overrides(db: DBConnection) -> dict[str, str]:
    """Return only the keys that currently have a DB override."""
    async with db.execute("SELECT key, value FROM app_settings") as cur:
        return {row["key"]: row["value"] for row in await cur.fetchall()}

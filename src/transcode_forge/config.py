"""Application configuration via environment variables."""

import hashlib
import secrets
from functools import lru_cache

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Transcode Forge configuration. All values overridable via TF_* env vars."""

    model_config = SettingsConfigDict(env_prefix="TF_", populate_by_name=True)

    # Demo mode — fake data, no Redis/ffmpeg required, for UI testing
    demo_mode: bool = False
    # Static demo — seeds data but does NOT run simulator (frozen state for testing)
    demo_static: bool = False

    # Redis — Docker compose default uses the in-network 'redis' hostname.
    # Override with TF_REDIS_URL=redis://host:port/db for non-Docker setups.
    redis_url: str = "redis://localhost:6379/0"
    redis_prefix: str = "tf"

    # Database — sqlite:///path or postgresql://user:pass@host/db
    db_url: str = "sqlite:///transcode_forge.db"
    # Kept for backwards compat with worker CLI --db-path flag
    db_path: str = ""

    # Library paths — set these to wherever your media lives. Empty
    # disables the corresponding library.
    library_movies: str = ""
    library_tv: str = ""
    library_anime: str = ""

    # Quality presets on the x265-CRF reference scale. The worker maps
    # them per encoder (nvenc cq ≈ crf+11, SVT-AV1 crf ≈ x265 crf+7, …) —
    # see worker/encoder.py. With a target VMAF set these are the CRF-search
    # fallback, not the primary knob.
    quality_movies: int = Field(default=21, ge=1, le=51)
    quality_tv: int = Field(default=21, ge=1, le=51)
    quality_anime: int = Field(default=19, ge=1, le=51)

    # Codec + quality-goal defaults. default_codec pre-fills the queue-time
    # selector (per-job job.target_codec stays the source of truth).
    # target_vmaf is what the CRF search AIMS for on samples; the safety
    # floors are what the full-file gate REFUSES to keep — deliberately
    # decoupled (plans/vmaf-decoupling-spec.md): samples overestimate the
    # full file, so gating at the target rejected good encodes wholesale.
    # The old TF_VMAF_MIN_FLOOR knob is retired and no longer read.
    # All four are DB-overridable via the settings page (repos/settings.py).
    default_codec: str = Field(default="hevc", pattern=r"^(hevc|av1)$")
    target_vmaf: float = Field(default=97.0, ge=0.0, le=100.0)
    vmaf_safety_mean: float = Field(default=90.0, ge=0.0, le=100.0)
    vmaf_safety_perc5: float = Field(default=85.0, ge=0.0, le=100.0)

    # Per-file target-VMAF CRF search (ab-av1 style, worker-side). Disable
    # to always encode at the fixed quality preset; the VMAF gate still runs.
    crf_search_enabled: bool = True

    # Worker coordination
    heartbeat_interval: int = Field(default=10, ge=1, description="Seconds between heartbeats")
    heartbeat_timeout: int = Field(
        default=30, ge=5, description="Seconds before worker marked dead"
    )
    max_retries: int = Field(default=3, ge=0)

    # Worker-specific (set per node, not on scheduler). The hardware axis
    # was renamed encoder → backend (D2); TF_PREFERRED_ENCODER remains a
    # deprecated alias for one release so live workers keep working.
    worker_name: str = ""
    worker_max_concurrent: int = Field(default=1, ge=1, le=4)
    preferred_backend: str = Field(
        default="auto",
        pattern=r"^(auto|qsv|nvenc|cpu|quadra)$",
        validation_alias=AliasChoices("tf_preferred_backend", "tf_preferred_encoder"),
    )
    path_map: dict[str, str] = Field(default_factory=dict)

    # Auth — admin session cookie signing.
    # If unset, a fresh random key is generated per boot. Persisting it
    # across restarts (set TF_AUTH_SECRET in your .env) keeps active
    # sessions alive when the scheduler reboots.
    auth_secret: str = Field(default_factory=lambda: secrets.token_urlsafe(48))

    # HMAC key for hashing worker tokens at rest. Empty → derived from
    # auth_secret (so single-host deploys need no new env). Pin TF_AUTH_SECRET
    # or TF_TOKEN_PEPPER in production so issued worker tokens survive restarts.
    token_pepper: str = ""

    # Logging verbosity for the scheduler + workers.
    log_level: str = Field(default="info", pattern=r"^(debug|info|warning|error|critical)$")

    # Set true when serving over HTTPS (behind a TLS reverse proxy) so the
    # admin session cookie carries the Secure flag.
    session_secure: bool = False

    # S3-compatible object storage — optional, for S3-library backend.
    # Credentials injected via environment (1Password on production).
    s3_endpoint_url: str = ""
    s3_region: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""

    # Scratch directory for S3 backend downloads/uploads.
    # Defaults to a temp directory; should be fast local storage with ample space.
    scratch_dir: str = ""

    @model_validator(mode="after")
    def _validate_vmaf_floor_pair(self) -> "Settings":
        """Fail fast on an impossible gate: per-frame perc5 can never exceed
        the mean, so a perc5 floor above the mean floor would skip encodes
        the mean floor accepts — the mass-skip storm the decoupling fixed.
        Likeliest cause: porting the retired TF_VMAF_MIN_FLOOR value (95)
        onto TF_VMAF_SAFETY_PERC5 without also raising TF_VMAF_SAFETY_MEAN."""
        if self.vmaf_safety_perc5 > self.vmaf_safety_mean:
            raise ValueError(
                f"TF_VMAF_SAFETY_PERC5 ({self.vmaf_safety_perc5:g}) cannot exceed "
                f"TF_VMAF_SAFETY_MEAN ({self.vmaf_safety_mean:g}). If you are "
                "porting the retired TF_VMAF_MIN_FLOOR, note the new floors are "
                'absolute "refuse to keep" bars (defaults 90/85), not the old '
                "target-coupled floor."
            )
        return self

    @model_validator(mode="after")
    def _resolve_db_url(self) -> "Settings":
        """If db_path is set (from --db-path CLI), promote it to db_url."""
        if self.db_path:
            if self.db_path.startswith(("postgresql://", "postgres://", "sqlite://")):
                self.db_url = self.db_path
            else:
                self.db_url = f"sqlite:///{self.db_path}"
        return self

    @model_validator(mode="after")
    def _derive_token_pepper(self) -> "Settings":
        """Default the worker-token pepper to a value derived from auth_secret
        (domain-separated so it isn't the literal cookie key)."""
        if not self.token_pepper:
            self.token_pepper = hashlib.sha256(
                f"tf-worker-token-pepper:{self.auth_secret}".encode()
            ).hexdigest()
        return self

    @property
    def libraries(self) -> dict[str, tuple[str, int]]:
        """Return mapping of library name -> (path, quality_preset)."""
        all_libs = {
            "movies": (self.library_movies, self.quality_movies),
            "tv": (self.library_tv, self.quality_tv),
            "anime": (self.library_anime, self.quality_anime),
        }
        return {k: v for k, v in all_libs.items() if v[0]}


def get_settings() -> Settings:
    """Create settings instance, loading from environment."""
    return Settings()


@lru_cache(maxsize=1)
def get_token_pepper() -> str:
    """Process-stable HMAC key for worker-token hashing.

    Cached so every hash/verify in a process uses the same key even when
    auth_secret is randomly generated (unpinned) — otherwise a fresh
    Settings() per call would produce a different pepper each time and no
    token would ever verify. Pin TF_AUTH_SECRET or TF_TOKEN_PEPPER in
    production so tokens also survive across restarts.
    """
    return get_settings().token_pepper

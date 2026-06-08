"""FastAPI dependency injection helpers."""

from fastapi import Request
from redis.asyncio import Redis

from transcode_forge.config import Settings
from transcode_forge.db import DBConnection


def get_db(request: Request) -> DBConnection:
    """Get the SQLite database connection from app state."""
    return request.app.state.db  # type: ignore[no-any-return]


def get_redis(request: Request) -> Redis | None:
    """Get the Redis connection pool from app state (None in demo mode)."""
    return request.app.state.redis  # type: ignore[no-any-return]


def get_settings(request: Request) -> Settings:
    """Get application settings from app state."""
    return request.app.state.settings  # type: ignore[no-any-return]

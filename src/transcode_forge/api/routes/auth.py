"""Authentication — single-admin login + first-run setup.

Workers do NOT use these endpoints; they have a separate token flow.

POST /api/auth/setup — only works while no admin exists. Creates the
                        admin user and logs the caller in.
POST /api/auth/login  — username + password → sets session cookie.
POST /api/auth/logout — clears the session.
GET  /api/auth/status — { authenticated, setup_required } — useful
                        for the UI to decide where to send the user.
"""

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from transcode_forge.api.deps import get_db
from transcode_forge.db import DBConnection
from transcode_forge.repos import users as user_repo

router = APIRouter(tags=["auth"])

SESSION_KEY = "user"

# Brute-force throttle: max failed logins per client IP per window, after
# which we 429. State lives on app.state.login_attempts (per-process).
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 60


class LoginRequest(BaseModel):
    password: str = Field(min_length=1)
    username: str = user_repo.ADMIN_USERNAME


class SetupRequest(BaseModel):
    password: str = Field(min_length=8, max_length=200)


@router.get("/auth/status")
async def status(request: Request, db: DBConnection = Depends(get_db)) -> dict[str, Any]:
    setup_required = not await user_repo.has_admin(db)
    authed = bool(request.session.get(SESSION_KEY)) if hasattr(request, "session") else False
    return {
        "authenticated": authed,
        "setup_required": setup_required,
    }


@router.post("/auth/setup")
async def setup(
    body: SetupRequest, request: Request, db: DBConnection = Depends(get_db)
) -> dict[str, Any]:
    if await user_repo.has_admin(db):
        raise HTTPException(status_code=409, detail="Admin already exists")
    await user_repo.create_admin(db, body.password)
    request.session[SESSION_KEY] = user_repo.ADMIN_USERNAME
    return {"ok": True}


@router.post("/auth/login")
async def login(
    body: LoginRequest, request: Request, db: DBConnection = Depends(get_db)
) -> dict[str, Any]:
    attempts = getattr(request.app.state, "login_attempts", None)
    if attempts is None:
        attempts = {}
        request.app.state.login_attempts = attempts
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    recent = [t for t in attempts.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
    if len(recent) >= LOGIN_MAX_FAILURES:
        attempts[ip] = recent
        raise HTTPException(status_code=429, detail="Too many login attempts; try again shortly")

    if not await user_repo.authenticate(db, body.password):
        recent.append(now)
        attempts[ip] = recent
        raise HTTPException(status_code=401, detail="Invalid credentials")

    attempts.pop(ip, None)  # success clears the failure counter
    request.session[SESSION_KEY] = user_repo.ADMIN_USERNAME
    return {"ok": True}


@router.post("/auth/logout")
async def logout(request: Request) -> dict[str, Any]:
    request.session.clear()
    return {"ok": True}

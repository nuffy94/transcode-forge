"""Settings API for MCP grants (docs/MCP.md, "Grants and tokens").

GET    /api/mcp/grants             list grants; carries no token material
POST   /api/mcp/grants             issue a manual grant; the token is returned ONCE
DELETE /api/mcp/grants/{grant_id}  revoke a grant

Admin session only, enforced by the auth middleware. A manual grant is the
credential for a client that sends a bearer header instead of running the
browser flow. The browser flow's grants (Phase 2) appear in the same list
and are revoked the same way.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from transcode_forge.api.deps import get_db, get_settings
from transcode_forge.config import Settings
from transcode_forge.db import DBConnection
from transcode_forge.repos import oauth as oauth_repo
from transcode_forge.repos import users as user_repo

router = APIRouter(tags=["mcp"])


class IssueRequest(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    scopes: list[str] = Field(min_length=1)
    expires_in_days: int = Field(default=oauth_repo.MANUAL_GRANT_TTL_DAYS, ge=1, le=365)

    @field_validator("scopes")
    @classmethod
    def _known_scopes(cls, scopes: list[str]) -> list[str]:
        unknown = set(scopes) - oauth_repo.SCOPES
        if unknown:
            raise ValueError(f"unknown scopes: {sorted(unknown)}")
        return sorted(set(scopes))


@router.get("/mcp/grants")
async def list_grants(db: DBConnection = Depends(get_db)) -> dict[str, Any]:
    rows = await oauth_repo.list_grants(db)
    return {"data": rows, "meta": {"total": len(rows)}}


@router.post("/mcp/grants")
async def issue_grant(
    body: IssueRequest,
    db: DBConnection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Issue a manual grant. The raw token is in this response and nowhere
    else; the list shows the grant without it."""
    if not settings.public_url:
        raise HTTPException(
            status_code=409,
            detail="Set TF_PUBLIC_URL to enable the MCP endpoint before issuing a grant",
        )
    user_id = await user_repo.get_admin_id(db)
    if user_id is None:
        raise HTTPException(status_code=409, detail="No admin account exists yet")
    expires_at = (datetime.now(UTC) + timedelta(days=body.expires_in_days)).isoformat()
    grant_id, token = await oauth_repo.create_manual_grant(
        db,
        label=body.label,
        scopes=set(body.scopes),
        resource=settings.mcp_resource_url,
        user_id=user_id,
        expires_at=expires_at,
    )
    return {
        "token": token,
        "grant_id": grant_id,
        "client_id": oauth_repo.MANUAL_CLIENT_ID,
        "label": body.label,
        "scopes": body.scopes,
        "resource": settings.mcp_resource_url,
        "expires_at": expires_at,
    }


@router.delete("/mcp/grants/{grant_id}")
async def revoke_grant(grant_id: str, db: DBConnection = Depends(get_db)) -> dict[str, Any]:
    if not await oauth_repo.revoke_grant(db, grant_id):
        raise HTTPException(status_code=404, detail="Grant not found or already revoked")
    return {"revoked": True, "grant_id": grant_id}

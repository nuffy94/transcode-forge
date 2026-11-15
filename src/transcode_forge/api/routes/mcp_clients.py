"""Settings API for the OAuth clients of the MCP endpoint (docs/MCP.md,
"Authorization server").

GET    /api/mcp/clients               list clients
POST   /api/mcp/clients               allow a client
DELETE /api/mcp/clients/{client_id}   disable a client

Two kinds of client exist. A ``cimd`` client is identified by the HTTPS URL
of its metadata document; allowing the URL is the whole registration, and
the document is fetched at authorization time. An ``admin`` client is
created here with a name and its exact redirect URIs. Both are public
clients: PKCE always, no secret. Admin session only, enforced by the auth
middleware.
"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from transcode_forge.api.deps import get_db, get_settings
from transcode_forge.config import Settings
from transcode_forge.db import DBConnection
from transcode_forge.mcp import cimd
from transcode_forge.repos import oauth as oauth_repo

router = APIRouter(tags=["mcp"])

MAX_REDIRECT_URIS = 8


class ClientRequest(BaseModel):
    kind: Literal["cimd", "admin"]
    url: str = Field(default="", max_length=512)
    name: str = Field(default="", max_length=80)
    redirect_uris: list[str] = Field(default_factory=list, max_length=MAX_REDIRECT_URIS)


def _check_redirect_uri(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.fragment:
        raise HTTPException(
            status_code=422,
            detail=f"redirect URI {value!r} must be an absolute http(s) URL without a fragment",
        )
    return value


@router.get("/mcp/clients")
async def list_clients(db: DBConnection = Depends(get_db)) -> dict[str, Any]:
    rows = await oauth_repo.list_clients(db)
    return {"data": rows, "meta": {"total": len(rows)}}


@router.post("/mcp/clients")
async def allow_client(
    body: ClientRequest,
    db: DBConnection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    if not settings.public_url:
        raise HTTPException(
            status_code=409,
            detail="Set TF_PUBLIC_URL to enable the MCP endpoint before allowing a client",
        )
    if body.kind == "cimd":
        try:
            cimd.check_url(body.url)
        except cimd.DocumentError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        await oauth_repo.allow_cimd_client(db, body.url, name=body.name.strip())
        return {"id": body.url, "kind": "cimd"}

    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    uris = [_check_redirect_uri(u.strip()) for u in body.redirect_uris if u.strip()]
    if not uris:
        raise HTTPException(status_code=422, detail="at least one redirect URI is required")
    client_id = await oauth_repo.create_admin_client(db, name=name, redirect_uris=uris)
    return {"id": client_id, "kind": "admin"}


@router.delete("/mcp/clients/{client_id:path}")
async def disable_client(client_id: str, db: DBConnection = Depends(get_db)) -> dict[str, Any]:
    if client_id == oauth_repo.MANUAL_CLIENT_ID:
        raise HTTPException(status_code=404, detail="Client not found")
    if not await oauth_repo.disable_client(db, client_id):
        raise HTTPException(status_code=404, detail="Client not found or already disabled")
    return {"disabled": True, "id": client_id}

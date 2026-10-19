"""Assembly of the /mcp resource server (docs/MCP.md, "Architecture").

The SDK's pieces are stacked in this order around its transport:

    bearer authentication (SDK)  -> 401 with the metadata challenge
    auth context (SDK)           -> get_access_token() inside tools
    require auth (SDK)           -> no anonymous path to the transport
    Gate (ours)                  -> protocol admission, budget, per-tool scopes
    transport (SDK)              -> header/body consistency, dispatch

The SDK's own ``streamable_http_app`` builds a similar stack with a single
global scope list; it is not used here because the per-tool gate has to sit
between its authentication middleware and its transport.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlsplit

from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.routes import build_resource_metadata_url, create_protected_resource_routes
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.routing import BaseRoute, Route

from transcode_forge import __version__
from transcode_forge.config import Settings
from transcode_forge.db import DBConnection
from transcode_forge.mcp.auth import Gate, GrantTokenVerifier
from transcode_forge.mcp.tools import build_tools
from transcode_forge.repos.oauth import INITIAL_SCOPES

MAX_BODY_BYTES = 1024 * 1024

INSTRUCTIONS = (
    "Transcode Forge re-encodes a media library into HEVC or AV1. These tools "
    "read the catalog, the job queue and the statistics of one installation."
)


def build_mcp(
    settings: Settings, *, get_db: Callable[[], DBConnection]
) -> tuple[list[BaseRoute], StreamableHTTPSessionManager]:
    """Routes for /mcp and its metadata document, plus the session manager
    the app lifespan has to run."""
    issuer = settings.public_url.rstrip("/")
    # AuthSettings validates the URLs with pydantic's empty-path preservation,
    # so a path-less issuer stays "https://host" (RFC 8414 issuer comparison
    # is exact) instead of gaining a trailing slash.
    auth = AuthSettings(
        issuer_url=issuer,  # type: ignore[arg-type]
        resource_server_url=settings.mcp_resource_url,  # type: ignore[arg-type]
        validate_token_resource=True,
    )
    assert auth.resource_server_url is not None
    resource = auth.resource_server_url
    prm_url = str(build_resource_metadata_url(resource))

    server = MCPServer("Transcode Forge", instructions=INSTRUCTIONS, version=__version__)
    for spec in build_tools(get_db):
        server.add_tool(spec.fn, name=spec.name, description=spec.description)

    # Creates the session manager with the transport settings; the Starlette
    # app it returns is discarded in favour of the stack below.
    server.streamable_http_app(
        json_response=True,
        stateless_http=True,
        max_request_body_size=MAX_BODY_BYTES,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host=urlsplit(issuer).hostname or "",
    )
    session_manager = server.session_manager
    transport = StreamableHTTPASGIApp(session_manager)

    gate = Gate(transport, settings=settings, resource_metadata_url=prm_url)
    protected = RequireAuthMiddleware(
        gate, required_scopes=[], resource_metadata_url=AnyHttpUrl(prm_url)
    )
    with_context = AuthContextMiddleware(protected)
    verifier = GrantTokenVerifier(get_db, str(resource))
    endpoint = AuthenticationMiddleware(
        with_context,
        backend=BearerAuthBackend(verifier, resource_server_url=resource),
    )

    routes: list[BaseRoute] = [Route("/mcp", endpoint=endpoint)]
    routes.extend(
        create_protected_resource_routes(
            resource_url=resource,
            authorization_servers=[auth.issuer_url],
            scopes_supported=list(INITIAL_SCOPES),
        )
    )
    return routes, session_manager

"""Client ID Metadata Documents: fetch and validate a client's self-published
registration (docs/MCP.md, "Authorization server").

A client whose id is an HTTPS URL is one the operator allowed in Settings;
the document behind that URL supplies its redirect URIs. Fetching it is the
only outbound request the authorization server ever makes, so the fetch is
guarded (docs/MCP-THREAT-MODEL.md row 11): HTTPS only, the host has to
resolve to public addresses only, the connection is pinned to the vetted
address so a second lookup cannot land somewhere else, redirects are not
followed, and size and time are bounded. The document's own ``client_id``
has to equal the URL it was fetched from.

This module does no database work. The caller decides when a cached copy
is fresh enough (``CACHE_SECONDS``) and stores what it gets back.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

import httpx

MAX_DOCUMENT_BYTES = 64 * 1024
TIMEOUT_SECONDS = 5.0
CACHE_SECONDS = 3600
MAX_NAME = 80

Resolver = Callable[[str], Awaitable[list[str]]]
"""Hostname to the addresses it resolves to, as strings."""


class DocumentError(Exception):
    """The document could not be fetched or does not describe a usable client."""


@dataclass(frozen=True)
class ClientDocument:
    client_id: str
    client_name: str
    redirect_uris: tuple[str, ...]


async def resolve(host: str) -> list[str]:
    """Every address the host resolves to, via the system resolver."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def check_url(url: str) -> SplitResult:
    """The URL a client id must be: HTTPS, a host, no credentials, no fragment."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise DocumentError("client id must be an https URL")
    if not parts.hostname:
        raise DocumentError("client id has no host")
    if parts.username or parts.password:
        raise DocumentError("client id must not carry credentials")
    if parts.fragment:
        raise DocumentError("client id must not carry a fragment")
    return parts


def is_public_address(text: str) -> bool:
    """True for an address on the public internet. Private, loopback,
    link-local, multicast and reserved ranges are all refused, including
    an IPv4 range reached through an IPv4-mapped IPv6 address."""
    try:
        ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(text)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def fetch_document(
    url: str,
    *,
    resolver: Resolver = resolve,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ClientDocument:
    """Fetch and validate the document at ``url``.

    One resolution, every address checked, one connection to the first
    vetted address with TLS and the Host header still naming the hostname.
    Anything other than a 200 with a valid document is a ``DocumentError``.
    """
    parts = check_url(url)
    host = parts.hostname or ""
    try:
        addresses = await resolver(host)
    except OSError as exc:
        raise DocumentError("client id host does not resolve") from exc
    if not addresses:
        raise DocumentError("client id host does not resolve")
    for address in addresses:
        if not is_public_address(address):
            raise DocumentError("client id host resolves to a non-public address")

    pinned = addresses[0]
    port = parts.port or 443
    netloc = f"[{pinned}]:{port}" if ":" in pinned else f"{pinned}:{port}"
    pinned_url = urlunsplit(("https", netloc, parts.path or "/", parts.query, ""))
    host_header = host if port == 443 else f"{host}:{port}"

    try:
        async with httpx.AsyncClient(
            transport=transport, timeout=TIMEOUT_SECONDS, follow_redirects=False
        ) as client:
            request = client.build_request(
                "GET",
                pinned_url,
                headers={"Host": host_header, "Accept": "application/json"},
                extensions={"sni_hostname": host},
            )
            response = await client.send(request, stream=True)
            try:
                if response.status_code != 200:
                    raise DocumentError(f"document request answered {response.status_code}")
                body = b""
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_DOCUMENT_BYTES:
                        raise DocumentError("document is larger than the limit")
            finally:
                await response.aclose()
    except httpx.HTTPError as exc:
        raise DocumentError("document could not be fetched") from exc
    return parse_document(body, url)


def parse_document(body: bytes, url: str) -> ClientDocument:
    """Validate a fetched document against the URL it came from."""
    try:
        data: Any = json.loads(body)
    except ValueError as exc:
        raise DocumentError("document is not JSON") from exc
    if not isinstance(data, dict):
        raise DocumentError("document is not a JSON object")
    if data.get("client_id") != url:
        raise DocumentError("document client_id does not equal its URL")
    if data.get("token_endpoint_auth_method") != "none":
        raise DocumentError(
            "document must declare a public client: token_endpoint_auth_method none"
        )
    uris = data.get("redirect_uris")
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) and u for u in uris):
        raise DocumentError("document has no usable redirect_uris")
    grant_types = data.get("grant_types")
    if grant_types is not None and "authorization_code" not in grant_types:
        raise DocumentError("document does not allow the authorization code grant")
    name = data.get("client_name")
    client_name = name[:MAX_NAME] if isinstance(name, str) and name.strip() else ""
    return ClientDocument(client_id=url, client_name=client_name, redirect_uris=tuple(uris))

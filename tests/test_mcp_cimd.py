"""Client metadata documents: the guarded fetch (docs/MCP-THREAT-MODEL.md
row 11) and the document rules.

The resolver and the HTTP transport are both stubs, so every case below is
about what the guard refuses and how many requests leave the process.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from transcode_forge.mcp import cimd

URL = "https://claude.ai/oauth/claude-code-client-metadata"
PUBLIC_IP = "93.184.216.34"
DOCUMENT = {
    "client_id": URL,
    "client_name": "Claude Code",
    "client_uri": "https://claude.ai",
    "redirect_uris": ["http://localhost/callback", "http://127.0.0.1/callback"],
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
    "token_endpoint_auth_method": "none",
}


def _transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.MockTransport(record), seen


def _serve(body: bytes, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(status, content=body)


async def public_resolver(host: str) -> list[str]:
    return [PUBLIC_IP]


async def test_fetch_pins_the_vetted_address_and_keeps_the_hostname() -> None:
    transport, seen = _transport(_serve(json.dumps(DOCUMENT).encode()))
    document = await cimd.fetch_document(URL, resolver=public_resolver, transport=transport)
    assert document.client_id == URL
    assert document.client_name == "Claude Code"
    assert document.redirect_uris == ("http://localhost/callback", "http://127.0.0.1/callback")
    (request,) = seen
    assert request.url.host == PUBLIC_IP  # the connection goes to the address we checked
    assert request.url.scheme == "https"
    assert request.headers["host"] == "claude.ai"
    assert request.extensions["sni_hostname"] == "claude.ai"


@pytest.mark.parametrize(
    "addresses",
    [
        ["10.0.0.5"],
        [PUBLIC_IP, "10.0.0.5"],  # one private answer poisons the set
        ["127.0.0.1"],
        ["169.254.169.254"],
        ["::1"],
        ["fd00::1"],
        ["::ffff:10.0.0.1"],
        ["224.0.0.1"],
        [],
    ],
)
async def test_non_public_addresses_are_refused_before_any_request(addresses: list[str]) -> None:
    async def resolver(host: str) -> list[str]:
        return addresses

    transport, seen = _transport(_serve(json.dumps(DOCUMENT).encode()))
    with pytest.raises(cimd.DocumentError):
        await cimd.fetch_document(URL, resolver=resolver, transport=transport)
    assert seen == []


async def test_resolution_failure_is_refused() -> None:
    async def resolver(host: str) -> list[str]:
        raise OSError("no such host")

    transport, seen = _transport(_serve(b"{}"))
    with pytest.raises(cimd.DocumentError):
        await cimd.fetch_document(URL, resolver=resolver, transport=transport)
    assert seen == []


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://claude.ai/oauth/doc",
        "https:///oauth/doc",
        "https://user:pw@claude.ai/oauth/doc",
        "https://claude.ai/oauth/doc#frag",
        "ftp://claude.ai/doc",
    ],
)
async def test_only_plain_https_urls_are_client_ids(bad_url: str) -> None:
    transport, seen = _transport(_serve(b"{}"))
    with pytest.raises(cimd.DocumentError):
        await cimd.fetch_document(bad_url, resolver=public_resolver, transport=transport)
    assert seen == []


async def test_redirects_are_not_followed() -> None:
    transport, seen = _transport(
        lambda request: httpx.Response(302, headers={"location": "https://claude.ai/elsewhere"})
    )
    with pytest.raises(cimd.DocumentError):
        await cimd.fetch_document(URL, resolver=public_resolver, transport=transport)
    assert len(seen) == 1


async def test_non_200_is_refused() -> None:
    transport, _ = _transport(_serve(b"nope", status=404))
    with pytest.raises(cimd.DocumentError):
        await cimd.fetch_document(URL, resolver=public_resolver, transport=transport)


async def test_oversized_document_is_refused() -> None:
    transport, _ = _transport(_serve(b"x" * (cimd.MAX_DOCUMENT_BYTES + 1)))
    with pytest.raises(cimd.DocumentError):
        await cimd.fetch_document(URL, resolver=public_resolver, transport=transport)


async def test_transport_errors_are_refused() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    transport, _ = _transport(boom)
    with pytest.raises(cimd.DocumentError):
        await cimd.fetch_document(URL, resolver=public_resolver, transport=transport)


@pytest.mark.parametrize(
    "mutation",
    [
        {"client_id": "https://claude.ai/oauth/other"},
        {"token_endpoint_auth_method": "client_secret_post"},
        {"token_endpoint_auth_method": None},
        {"redirect_uris": []},
        {"redirect_uris": "http://localhost/callback"},
        {"redirect_uris": ["http://localhost/callback", 5]},
        {"grant_types": ["refresh_token"]},
    ],
)
def test_document_rules(mutation: dict[str, object]) -> None:
    document = {**DOCUMENT, **mutation}
    document = {k: v for k, v in document.items() if v is not None}
    with pytest.raises(cimd.DocumentError):
        cimd.parse_document(json.dumps(document).encode(), URL)


def test_document_must_be_a_json_object() -> None:
    with pytest.raises(cimd.DocumentError):
        cimd.parse_document(b"not json", URL)
    with pytest.raises(cimd.DocumentError):
        cimd.parse_document(b"[1, 2]", URL)


def test_document_name_is_optional_and_bounded() -> None:
    nameless = {k: v for k, v in DOCUMENT.items() if k != "client_name"}
    assert cimd.parse_document(json.dumps(nameless).encode(), URL).client_name == ""
    long = {**DOCUMENT, "client_name": "n" * 500}
    assert len(cimd.parse_document(json.dumps(long).encode(), URL).client_name) == cimd.MAX_NAME


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("8.8.8.8", True),
        ("2606:4700::1111", True),
        ("10.0.0.1", False),
        ("192.168.1.1", False),
        ("172.16.0.1", False),
        ("127.0.0.1", False),
        ("169.254.1.1", False),
        ("0.0.0.0", False),
        ("::1", False),
        ("fe80::1", False),
        ("fd00::1", False),
        ("::ffff:10.0.0.1", False),
        ("224.0.0.1", False),
        ("not-an-address", False),
    ],
)
def test_is_public_address(address: str, public: bool) -> None:
    assert cimd.is_public_address(address) is public

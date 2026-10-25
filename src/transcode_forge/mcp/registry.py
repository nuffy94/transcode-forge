"""One table of tools and the scopes each needs.

Tool registration (app.py) and the scope gate (auth.py) both read this
table, so a tool cannot exist without a policy and the policy cannot drift
from the code (docs/MCP.md, "Per-tool authorization"). An unknown name is
refused by the gate; it never means "no scope required".
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from transcode_forge.repos.oauth import SCOPES

TOOL_SCOPES: dict[str, frozenset[str]] = {
    "cancel_job": frozenset({"jobs:write"}),
    "get_job": frozenset({"jobs:read"}),
    "list_jobs": frozenset({"jobs:read"}),
    "list_libraries": frozenset({"catalog:read"}),
    "list_media": frozenset({"catalog:read"}),
    "queue_file": frozenset({"jobs:write"}),
    "queue_summary": frozenset({"jobs:read"}),
    "stats": frozenset({"jobs:read"}),
}
"""Tool name to the scopes a grant must hold to call it. Sorted by name;
``tools/list`` returns tools in this order so clients can cache the list."""

DISCOVERY_METHODS: frozenset[str] = frozenset(
    {"server/discover", "tools/list", "resources/list", "resources/templates/list", "prompts/list"}
)
"""Methods that need only a valid grant, no particular scope."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    fn: Callable[..., Awaitable[dict[str, Any]]]

    @property
    def scopes(self) -> frozenset[str]:
        return TOOL_SCOPES[self.name]


def required_scopes(name: str) -> frozenset[str] | None:
    """Scopes a tool needs, or None when no such tool exists."""
    return TOOL_SCOPES.get(name)


def check_policy(specs: list[ToolSpec]) -> None:
    """Fail at build time if a tool has no policy, a policy has no tool, or a
    policy names a scope the grant store does not know."""
    names = {spec.name for spec in specs}
    if names != set(TOOL_SCOPES):
        raise RuntimeError(
            "tool registry and scope table disagree: "
            f"tools={sorted(names)} policy={sorted(TOOL_SCOPES)}"
        )
    for name, scopes in TOOL_SCOPES.items():
        if not scopes or not scopes <= SCOPES:
            raise RuntimeError(f"tool {name!r} has an invalid scope policy: {sorted(scopes)}")

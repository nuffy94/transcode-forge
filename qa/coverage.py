"""QA coverage inventory + page-route gap gate (qa-redesign spec D5).

Derives the surface inventory from the code (routes from the live router,
templates + interactive surface from the template tree) and maps it against
what QA actually touches: the L2 crawl (tests/qa PAGES + structural anchors),
the explicit waiver registry below, and the L3 scenarios' `Routes:` lines in
qa/scenarios.md. Intent stays hand-curated in scenarios.md — only the
inventory and the gap report are derived.

The CI gate is scoped to top-level HTML page routes ONLY (12 today):
partials are covered transitively by the pages that load them, and /api/*
correctness is L1's job. A new page route with zero QA mapping fails the
qa-sweep job via tests/qa/test_coverage_gate.py.

CLI (also the L3 report's coverage table):

    uv run python qa/coverage.py            # markdown table, exit 1 on gaps
    uv run python qa/coverage.py --json     # machine-readable inventory
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_TEMPLATES = _REPO_ROOT / "src" / "transcode_forge" / "web" / "templates"
_SCENARIOS = Path(__file__).resolve().parent / "scenarios.md"

# Page routes deliberately not in the L2 authenticated crawl — each waiver
# names its actual coverage. A route in neither PAGES nor here fails the gate.
COVERED_ELSEWHERE: dict[str, str] = {
    "/login": "swept anonymously in tests/qa/test_sweep.py (fresh-context block)",
    "/setup": "tests/qa/test_setup_flow.py boots its own fresh instance",
    "/history": "301 redirect - tests/test_web.py::test_history_redirects_to_activity",
    "/skipped": "301 redirect - tests/test_web.py::test_skipped_redirects_to_activity",
}


def page_routes() -> list[str]:
    """Top-level HTML page routes from the live router (runtime truth)."""
    from transcode_forge.web.routes import router

    paths: set[str] = set()
    for route in router.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if "GET" in methods and not path.startswith("/partials/"):
            paths.add(path)
    return sorted(paths)


def l2_crawled_paths() -> set[str]:
    """Paths the authenticated L2 sweep visits (query variants normalized)."""
    from tests.qa.sweep_lib import PAGES

    return {page.split("?")[0] for page in PAGES}


def l2_anchor_counts() -> dict[str, int]:
    """Structural-anchor count per normalized path (all variants summed)."""
    from tests.qa.test_sweep import STRUCTURAL_ANCHORS

    counts: dict[str, int] = {}
    for page, anchors in STRUCTURAL_ANCHORS.items():
        path = page.split("?")[0]
        counts[path] = counts.get(path, 0) + len(anchors)
    return counts


def scenario_routes() -> dict[str, list[str]]:
    """Parse qa/scenarios.md: each `### S<n> - title` block declares a
    `Routes:` line (comma-separated paths). Scenarios without one are
    reported with an empty list — the gate test keeps that from rotting."""
    text = _SCENARIOS.read_text(encoding="utf-8")
    result: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        heading = re.match(r"^###\s+(S\d+)\b", line)
        if heading:
            current = heading.group(1)
            result[current] = []
            continue
        routes = re.match(r"^Routes:\s*(.+)$", line)
        if routes and current:
            result[current] = [r.strip() for r in routes.group(1).split(",") if r.strip()]
    return result


def interactive_surface() -> dict[str, dict[str, int]]:
    """Per-template counts of the interactive surface (inventory only)."""
    patterns = {
        "data_action": re.compile(r"data-action="),
        "hx": re.compile(r"hx-(get|post|delete|put)="),
        "dialogs": re.compile(r"<dialog\b"),
    }
    surface: dict[str, dict[str, int]] = {}
    for tpl in sorted(_TEMPLATES.rglob("*.html")):
        rel = tpl.relative_to(_TEMPLATES).as_posix()
        counts = {
            name: len(rx.findall(tpl.read_text(encoding="utf-8"))) for name, rx in patterns.items()
        }
        if any(counts.values()):
            surface[rel] = counts
    return surface


def page_route_gaps() -> list[str]:
    """Page routes with zero QA mapping — the CI gate fails on any."""
    crawled = l2_crawled_paths()
    return [r for r in page_routes() if r not in crawled and r not in COVERED_ELSEWHERE]


def scenarios_missing_routes() -> list[str]:
    """Scenario ids in scenarios.md that declare no Routes: line."""
    return sorted(sid for sid, routes in scenario_routes().items() if not routes)


def build_report() -> dict[str, Any]:
    crawled = l2_crawled_paths()
    anchors = l2_anchor_counts()
    by_scenario = scenario_routes()
    routes = page_routes()

    rows = []
    for route in routes:
        touching = sorted(
            (sid for sid, paths in by_scenario.items() if route in paths),
            key=lambda s: int(s[1:]),
        )
        rows.append(
            {
                "route": route,
                "l2_crawl": route in crawled,
                "waiver": COVERED_ELSEWHERE.get(route),
                "anchors": anchors.get(route, 0),
                "scenarios": touching,
            }
        )
    return {
        "page_routes": rows,
        "gaps": page_route_gaps(),
        "scenarios_missing_routes": scenarios_missing_routes(),
        "interactive_surface": interactive_surface(),
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = ["## QA coverage - page routes", ""]
    lines.append("| route | L2 crawl | anchors | scenarios |")
    lines.append("|---|---|---|---|")
    for row in report["page_routes"]:
        crawl = (
            "yes" if row["l2_crawl"] else f"waived: {row['waiver']}" if row["waiver"] else "**GAP**"
        )
        anchors = str(row["anchors"]) if row["anchors"] else "-"
        scenarios = ", ".join(row["scenarios"]) or "-"
        lines.append(f"| {row['route']} | {crawl} | {anchors} | {scenarios} |")
    lines.append("")
    if report["gaps"]:
        lines.append(f"**GAPS ({len(report['gaps'])})**: " + ", ".join(report["gaps"]))
    else:
        lines.append("No unmapped page routes.")
    if report["scenarios_missing_routes"]:
        lines.append(
            "Scenarios missing a Routes: line: " + ", ".join(report["scenarios_missing_routes"])
        )
    lines.append("")
    lines.append("## Interactive surface (inventory)")
    lines.append("")
    lines.append("| template | data-action | hx-* | dialogs |")
    lines.append("|---|---|---|---|")
    for tpl, counts in report["interactive_surface"].items():
        lines.append(
            f"| {tpl} | {counts['data_action'] or '-'} | {counts['hx'] or '-'} "
            f"| {counts['dialogs'] or '-'} |"
        )
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    report = build_report()
    if "--json" in argv:
        print(json.dumps(report, indent=2))
    else:
        print(render_markdown(report))
    return 1 if report["gaps"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

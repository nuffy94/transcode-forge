"""Coverage gap gate (qa-redesign spec D5) — pure inspection, no server.

Converts "QA forgot the new page" from a silent gap into a build failure:
every top-level HTML page route must be in the L2 crawl (PAGES) or carry an
explicit waiver naming its coverage (qa/coverage.py COVERED_ELSEWHERE).
Scoped to page routes only — partials are covered transitively by the pages
that load them, and /api/* correctness is L1's job.
"""

import pytest

from qa.coverage import page_route_gaps, scenarios_missing_routes


@pytest.mark.qa
def test_every_page_route_is_qa_mapped() -> None:
    gaps = page_route_gaps()
    assert not gaps, (
        f"unmapped page route(s): {', '.join(gaps)} — register this page: add it to "
        "PAGES + STRUCTURAL_ANCHORS in tests/qa (or waive with a reason in "
        "qa/coverage.py COVERED_ELSEWHERE)"
    )


@pytest.mark.qa
def test_gate_fires_on_an_unmapped_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate's whole value is failing loudly on a page QA forgot."""
    from qa import coverage

    real = coverage.page_routes()
    monkeypatch.setattr(coverage, "page_routes", lambda: [*real, "/brand-new-page"])
    assert coverage.page_route_gaps() == ["/brand-new-page"]


@pytest.mark.qa
def test_every_scenario_declares_its_routes() -> None:
    missing = scenarios_missing_routes()
    assert not missing, (
        f"scenarios without a Routes: line in qa/scenarios.md: {', '.join(missing)} — "
        "declare the routes each scenario drives so coverage stays derivable"
    )

"""Shell + brand/design-token sweep — the browser-only assertions absorbed
from the retired tests/e2e/ suite (QA redesign P1b, spec D2).

These need a real rendering engine (computed styles, fonts, visibility,
clicks) and don't care about seed state, so they run against the shared
seeded instance like every other L2 module. Empty-state and redirect
assertions from the old suite live in tests/test_web.py (L1) — see the
disposition checklist in PR #41.
"""

import re

import pytest
from playwright.sync_api import Page, expect

from tests.qa.sweep_lib import login

NAV_LABELS = [
    "Dashboard",
    "Movies",
    "TV Shows",
    "Queue",
    "Activity",
    "Workers",
    "Stats",
    "Settings",
]


@pytest.mark.qa
def test_sidebar_shell(qa_base_url: str, admin_pw: str, page: Page) -> None:
    """Sidebar nav: brand lockup, all destinations, sprite icons, no legacy
    Material font, no v1 ledger numbers, active-page highlighting."""
    login(page, qa_base_url, admin_pw)
    page.goto(f"{qa_base_url}/", wait_until="domcontentloaded")

    # aside.forge-sidebar specifically — the file-detail drawer is a second
    # <aside>, so a bare tag locator is ambiguous.
    expect(page.locator("aside.forge-sidebar")).to_be_visible()
    for label in NAV_LABELS:
        expect(page.locator(".forge-navlink-label", has_text=label)).to_be_visible()

    # The v2 brand lockup: mono eyebrow over the Big Shoulders wordmark.
    expect(page.locator(".forge-brand-word")).to_have_text("FORGE")
    expect(page.locator(".forge-brand-eyebrow")).to_have_text("Transcode")

    # Shell nav renders inline SVG sprite icons (not the Material font),
    # and v2 killed the 01/02 ledger numbers — nav order carries no meaning.
    assert page.locator("aside .forge-navlink svg.forge-icon").count() >= 9
    expect(page.locator("aside .material-symbols-outlined")).to_have_count(0)
    expect(page.locator(".forge-navlink-num")).to_have_count(0)

    page.goto(f"{qa_base_url}/movies", wait_until="domcontentloaded")
    expect(page.locator("aside a[href='/movies']")).to_have_class(re.compile("is-active"))


@pytest.mark.qa
def test_header_datum_strip(qa_base_url: str, admin_pw: str, page: Page) -> None:
    """Header carries the status strip, the live clock, and the section crumb
    (no search box / bell — those died with v1)."""
    login(page, qa_base_url, admin_pw)
    page.goto(f"{qa_base_url}/queue", wait_until="domcontentloaded")

    expect(page.locator("header >> text=Online")).to_be_visible()
    expect(page.locator("#forge-clock")).to_be_attached()
    expect(page.locator(".forge-crumb")).to_contain_text("Queue")


@pytest.mark.qa
def test_design_tokens(qa_base_url: str, admin_pw: str, page: Page) -> None:
    """The Forge Console design system is actually applied — computed styles,
    not class names (docs/design-system.md locks these)."""
    login(page, qa_base_url, admin_pw)
    page.goto(f"{qa_base_url}/", wait_until="domcontentloaded")

    # Warm graphite page field: #0d0b08 → rgb(13, 11, 8).
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert "13, 11, 8" in bg, f"body background drifted: {bg}"

    # The v2 signature — the molten seam along the top edge.
    expect(page.locator(".forge-seam")).to_be_attached()

    # Inline SVG sprite everywhere; Big Shoulders on display type.
    assert page.locator("svg.forge-icon").count() >= 10
    font = page.evaluate(
        "() => { const el = document.querySelector('.font-display');"
        " return el ? getComputedStyle(el).fontFamily : 'not found'; }"
    )
    assert "Big Shoulders" in font, f"display font not loaded: {font}"


@pytest.mark.qa
def test_htmx_shell_partials_populate(qa_base_url: str, admin_pw: str, page: Page) -> None:
    """The load-triggered shell partials actually replace their placeholders:
    dashboard stat cards render their labels (and carry no Material-font
    glyphs), and the sidebar health readout resolves past 'Checking'."""
    login(page, qa_base_url, admin_pw)
    page.goto(f"{qa_base_url}/", wait_until="domcontentloaded")

    stats = page.locator("#dashboard-stats")
    expect(stats.get_by_text("Space Reclaimed")).to_be_visible(timeout=15_000)
    expect(stats.get_by_text("Completed")).to_be_visible()
    expect(stats.get_by_text("Workers")).to_be_visible()
    expect(page.locator("#dashboard-stats .material-symbols-outlined")).to_have_count(0)

    # Demo instances may run Redis-less, so the resolved state is either
    # healthy or degraded — what matters is the partial loaded (a dead
    # partial stays on the 'Checking' placeholder forever).
    expect(page.locator("#health-indicator")).to_contain_text(
        re.compile("System OK|Degraded"), timeout=15_000
    )


@pytest.mark.qa
def test_activity_facet_switch(qa_base_url: str, admin_pw: str, page: Page) -> None:
    """Both facet-selection paths keep the views mutually exclusive: the
    click path (client JS) and the ?view=skips deep link (server-side Jinja
    conditional — a distinct render path the click test cannot cover)."""
    login(page, qa_base_url, admin_pw)
    page.goto(f"{qa_base_url}/activity", wait_until="domcontentloaded")

    expect(page.locator("#activity-tabs")).to_be_visible()
    page.click("#tab-skips")
    expect(page.locator("#skip-reason-filter")).to_be_visible()
    expect(page.locator("#skip-library-filter")).to_be_visible()
    expect(page.locator("#outcomes-view")).to_be_hidden()

    page.goto(f"{qa_base_url}/activity?view=skips", wait_until="domcontentloaded")
    expect(page.locator("#skips-view")).to_be_visible()
    expect(page.locator("#outcomes-view")).to_be_hidden()

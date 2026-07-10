"""Deterministic UX/QA sweep — the free, repeatable backbone (runs every CI
run). Drives every page of the seeded demo instance and fails on:

  * axe-core blocking violations (contrast, labels, interactive names)
  * any error toast present (errors are persistent by design — see base.html)
  * console errors / uncaught page errors
  * failed /api/ responses (>=400) during normal browsing
  * a load-bearing element missing from a page (dead partial detector)
  * Tab from the page body not reaching a focusable control

Findings are printed as JSON so a failure is self-explanatory, and a full-page
screenshot of each page is captured under tests/qa/shots/ for visual review.
Dialog states live in test_dialogs.py, the 390px pass in test_mobile.py, and
first-run /setup in test_setup_flow.py.
"""

import json

import pytest
from playwright.sync_api import Page

from tests.qa.sweep_lib import (
    PAGES,
    SHOTS,
    attach_error_capture,
    blocking_violations,
    error_toasts,
    horizontal_overflow,
    login,
    page_shot_name,
    tab_reaches_a_control,
)

# Load-bearing elements per page: if one is missing the partial died, even
# when nothing errored. Selectors are stable ids from the templates.
# Anchors check DOM PRESENCE only (deliberately — cheap, viewport-agnostic);
# assertions that need visibility semantics live in test_shell.py.
STRUCTURAL_ANCHORS: dict[str, list[str]] = {
    "/": [
        "#dashboard-stats",
        "#active-transcodes",
        "#recent-activity",
        "#scan-history",
        "#pause-btn",
    ],
    "/movies": [
        "#mv-status",
        "#mv-codec",
        "#mv-search",
        "#view-table",
        "#view-grid",
        "tr[data-file-id]",
    ],
    "/tv": ["#tab-shows", "#tab-files"],
    "/queue": [
        "#queue-sort",
        "#status-filter",
        "#library-filter",
        "#scan-library",
        "#scan-btn",
        "#pause-btn",
    ],
    "/activity": ["#tab-outcomes", "#tab-skips", "#activity-tabs"],
    "/activity?view=skips": ["#tab-skips.is-active", "#skip-reason-filter", "#skip-library-filter"],
    "/workers": ["#add-worker-toggle"],
    "/stats": ["#stats-container"],
    "/settings": ["#tab-libraries", "#tab-quality", "#tab-schedules", "#tab-general"],
}


@pytest.mark.qa
def test_ux_qa_sweep(qa_base_url: str, admin_pw: str, page: Page) -> None:
    console_errors, bad_api = attach_error_capture(page)
    login(page, qa_base_url, admin_pw)

    SHOTS.mkdir(exist_ok=True)
    axe_blocking: dict[str, list] = {}
    toast_hits: dict[str, list] = {}
    missing_anchors: dict[str, list[str]] = {}
    overflow_pages: dict[str, int] = {}
    focus_failures: list[str] = []

    for path in PAGES:
        page.goto(f"{qa_base_url}{path}", wait_until="domcontentloaded")
        page.wait_for_timeout(1200)
        page.screenshot(path=str(SHOTS / f"{page_shot_name(path)}.png"), full_page=True)

        if blocking := blocking_violations(page):
            axe_blocking[path] = blocking
        if toasts := error_toasts(page):
            toast_hits[path] = toasts

        missing = [
            sel for sel in STRUCTURAL_ANCHORS.get(path, []) if page.locator(sel).count() == 0
        ]
        if missing:
            missing_anchors[path] = missing

        if (overflow := horizontal_overflow(page)) > 1:
            overflow_pages[path] = overflow

        if not tab_reaches_a_control(page):
            focus_failures.append(path)

    # File-detail drawer: open a transcoded movie (complete + h264 source =
    # a seeded encode with VMAF + timeline), re-run axe on the open state,
    # and keep a screenshot of it.
    page.goto(f"{qa_base_url}/movies", wait_until="domcontentloaded")
    page.wait_for_selector("tr[data-file-id]", timeout=10_000)
    page.select_option("#mv-status", "complete")
    page.wait_for_selector("tr[data-file-id]:has(.codec-h264)", timeout=10_000)
    page.locator("tr[data-file-id]:has(.codec-h264)").first.click()
    page.wait_for_selector("#file-drawer.is-open", timeout=5_000)
    page.wait_for_timeout(600)
    page.screenshot(path=str(SHOTS / "movies_drawer.png"))
    if drawer_violations := blocking_violations(page):
        axe_blocking["/movies#drawer"] = drawer_violations
    if drawer_toasts := error_toasts(page):
        toast_hits["/movies#drawer"] = drawer_toasts

    # /login renders for anonymous visitors — sweep it in a FRESH context
    # (the main page object carries the admin session). First-run /setup is
    # covered by test_setup_flow.py against its own fresh instance.
    anon_ctx = page.context.browser.new_context(viewport=page.viewport_size)
    anon = anon_ctx.new_page()
    anon.goto(f"{qa_base_url}/login", wait_until="domcontentloaded")
    anon.wait_for_timeout(600)
    anon.screenshot(path=str(SHOTS / "login.png"), full_page=True)
    if login_violations := blocking_violations(anon):
        axe_blocking["/login"] = login_violations
    anon_ctx.close()

    report = json.dumps(
        {
            "axe_blocking": axe_blocking,
            "error_toasts": toast_hits,
            "console_errors": console_errors[:25],
            "bad_api_responses": bad_api[:25],
            "missing_anchors": missing_anchors,
            "horizontal_overflow_px": overflow_pages,
            "tab_focus_failures": focus_failures,
        },
        indent=2,
    )
    print("\n=== QA SWEEP REPORT ===\n" + report)

    assert not axe_blocking, f"axe blocking violations:\n{report}"
    assert not toast_hits, f"error toast(s) present on a page:\n{report}"
    assert not console_errors, f"console / page errors:\n{report}"
    assert not bad_api, f"failed /api/ responses during browsing:\n{report}"
    assert not missing_anchors, f"load-bearing element missing (dead partial?):\n{report}"
    assert not overflow_pages, f"page body scrolls horizontally at desktop width:\n{report}"
    assert not focus_failures, f"Tab does not reach a control:\n{report}"

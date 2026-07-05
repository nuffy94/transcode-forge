"""Mobile-viewport sweep (390x844) — every page must render without the
body scrolling horizontally, with the nav reachable and the usual error
capture (console / pageerror / failed /api/ / error toasts) active.

Wide content (tables, code blocks) is allowed to scroll INSIDE its own
container — the failure is document-level overflow.
"""

import json

import pytest
from playwright.sync_api import Browser

from tests.qa.sweep_lib import (
    PAGES,
    SHOTS,
    attach_error_capture,
    error_toasts,
    horizontal_overflow,
    login,
    page_shot_name,
)

MOBILE_VIEWPORT = {"width": 390, "height": 844}


@pytest.mark.qa
def test_mobile_sweep(qa_base_url: str, admin_pw: str, browser: Browser) -> None:
    ctx = browser.new_context(viewport=MOBILE_VIEWPORT)
    page = ctx.new_page()
    console_errors, bad_api = attach_error_capture(page)
    login(page, qa_base_url, admin_pw)

    mobile_shots = SHOTS / "mobile"
    mobile_shots.mkdir(parents=True, exist_ok=True)

    overflow_pages: dict[str, int] = {}
    toast_hits: dict[str, list] = {}
    nav_missing: list[str] = []

    for path in PAGES:
        page.goto(f"{qa_base_url}{path}", wait_until="domcontentloaded")
        page.wait_for_timeout(1200)
        page.screenshot(path=str(mobile_shots / f"{page_shot_name(path)}.png"), full_page=True)

        if (overflow := horizontal_overflow(page)) > 1:
            overflow_pages[path] = overflow
        if toasts := error_toasts(page):
            toast_hits[path] = toasts
        if page.locator("nav").count() == 0:
            nav_missing.append(path)

    ctx.close()

    report = json.dumps(
        {
            "horizontal_overflow_px": overflow_pages,
            "error_toasts": toast_hits,
            "console_errors": console_errors[:25],
            "bad_api_responses": bad_api[:25],
            "nav_missing": nav_missing,
        },
        indent=2,
    )
    print("\n=== MOBILE SWEEP REPORT ===\n" + report)

    assert not overflow_pages, f"page body scrolls horizontally at 390px:\n{report}"
    assert not toast_hits, f"error toast(s) on mobile:\n{report}"
    assert not console_errors, f"console / page errors on mobile:\n{report}"
    assert not bad_api, f"failed /api/ responses on mobile:\n{report}"
    assert not nav_missing, f"nav absent on mobile:\n{report}"

"""Deterministic UX/QA sweep — the free, repeatable backbone (runs every CI run
once wired). Drives every page of the seeded demo instance and fails on:

  * axe-core serious/critical violations (contrast, missing labels)
  * any error toast present (errors are persistent by design — see base.html)
  * console errors / uncaught page errors
  * failed /api/ responses (>=400) during normal browsing

Findings are printed as JSON so a failure is self-explanatory, and a full-page
screenshot of each page is captured under tests/qa/shots/ for visual review.
"""

import json
import pathlib

import pytest
from playwright.sync_api import Page

PAGES = ["/", "/movies", "/tv", "/queue", "/workers", "/history", "/skipped", "/stats", "/settings"]
AXE = pathlib.Path(__file__).parent / "vendor" / "axe.min.js"
SHOTS = pathlib.Path(__file__).parent / "shots"

# axe rule ids we treat as blocking (the classes that have actually shipped bugs)
BLOCKING_RULES = {
    "color-contrast",
    "label",
    "label-title-only",
    "select-name",
    "form-field-multiple-labels",
}


def _run_axe(page: Page) -> list[dict]:
    page.add_script_tag(path=str(AXE))
    page.wait_for_timeout(300)
    return page.evaluate(
        "async () => (await axe.run()).violations.map(v => "
        "({id: v.id, impact: v.impact, count: v.nodes.length, "
        "targets: v.nodes.map(n => n.target).slice(0, 4)}))"
    )


@pytest.mark.qa
def test_ux_qa_sweep(qa_base_url: str, admin_pw: str, page: Page) -> None:
    console_errors: list[str] = []
    bad_api: list[str] = []
    page.on(
        "console",
        lambda m: console_errors.append(f"{m.type}: {m.text}") if m.type == "error" else None,
    )
    page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))
    page.on(
        "response",
        lambda r: (
            bad_api.append(f"{r.status} {r.request.method} {r.url}")
            if r.status >= 400 and "/api/" in r.url
            else None
        ),
    )

    # Log in through the UI (the fixture already created the admin).
    page.goto(f"{qa_base_url}/login", wait_until="domcontentloaded")
    page.fill("input[type=password]", admin_pw)
    page.click("button[type=submit]")
    page.wait_for_timeout(1500)
    assert "/login" not in page.url, "login did not succeed"

    SHOTS.mkdir(exist_ok=True)
    axe_blocking: dict[str, list] = {}
    error_toasts: dict[str, list] = {}

    for path in PAGES:
        page.goto(f"{qa_base_url}{path}", wait_until="domcontentloaded")
        page.wait_for_timeout(1200)
        name = path.strip("/").replace("/", "_") or "dashboard"
        page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)

        violations = _run_axe(page)
        blocking = [v for v in violations if v["id"] in BLOCKING_RULES]
        if blocking:
            axe_blocking[path] = blocking

        toasts = page.evaluate(
            "Array.from(document.querySelectorAll('[data-toast-type=\"error\"]'))"
            ".map(e => e.innerText)"
        )
        if toasts:
            error_toasts[path] = toasts

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
    drawer_violations = [v for v in _run_axe(page) if v["id"] in BLOCKING_RULES]
    if drawer_violations:
        axe_blocking["/movies#drawer"] = drawer_violations
    drawer_toasts = page.evaluate(
        "Array.from(document.querySelectorAll('[data-toast-type=\"error\"]')).map(e => e.innerText)"
    )
    if drawer_toasts:
        error_toasts["/movies#drawer"] = drawer_toasts

    report = json.dumps(
        {
            "axe_blocking": axe_blocking,
            "error_toasts": error_toasts,
            "console_errors": console_errors[:25],
            "bad_api_responses": bad_api[:25],
        },
        indent=2,
    )
    print("\n=== QA SWEEP REPORT ===\n" + report)

    assert not axe_blocking, f"axe contrast/label violations:\n{report}"
    assert not error_toasts, f"error toast(s) present on a page:\n{report}"
    assert not console_errors, f"console / page errors:\n{report}"
    assert not bad_api, f"failed /api/ responses during browsing:\n{report}"

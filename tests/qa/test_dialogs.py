"""Dialog/expanded-state sweep — axe + error capture against every modal
and disclosure panel in its OPEN state (the base sweep only sees pages at
rest). Also asserts Escape closes real <dialog> elements.

Covered:
  * settings → add-library modal (filesystem AND S3 backend modes — the
    backend select must swap the path field for bucket/prefix fields)
  * settings → edit-library modal (opened from a seeded library row)
  * workers → "Add a worker" disclosure panel
"""

import json

import pytest
from playwright.sync_api import Browser, Page

from tests.qa.conftest import ADMIN_PW, launch_qa_app
from tests.qa.sweep_lib import (
    SHOTS,
    attach_error_capture,
    blocking_violations,
    error_toasts,
    login,
)

# Own port for the token-refresh guard's isolated instance (18799 = shared
# session instance, 18801 = setup flow).
TOKENS_PORT = 18802


def _dialog_open(page: Page, dialog_id: str) -> bool:
    return page.evaluate(f"document.getElementById('{dialog_id}').open")


def _dialog_is_centered(page: Page, dialog_id: str) -> bool:
    """True when the modal's box sits roughly at the viewport center — the
    guard against the top-left-corner regression (M1)."""
    return page.evaluate(
        f"""() => {{
            const r = document.getElementById('{dialog_id}').getBoundingClientRect();
            return Math.abs((r.x + r.width / 2) - window.innerWidth / 2) < 60
                && r.x > 100;
        }}"""
    )


@pytest.mark.qa
def test_dialog_states(qa_base_url: str, admin_pw: str, page: Page) -> None:
    console_errors, bad_api = attach_error_capture(page)
    login(page, qa_base_url, admin_pw)
    SHOTS.mkdir(exist_ok=True)

    axe_blocking: dict[str, list] = {}
    toast_hits: dict[str, list] = {}

    def check(state: str, shot: str | None = None) -> None:
        if shot:
            page.screenshot(path=str(SHOTS / shot))
        if blocking := blocking_violations(page):
            axe_blocking[state] = blocking
        if toasts := error_toasts(page):
            toast_hits[state] = toasts

    # --- settings: add-library modal, filesystem mode (default) ---
    page.goto(f"{qa_base_url}/settings", wait_until="domcontentloaded")
    page.wait_for_timeout(1000)
    page.click("#add-lib-btn")
    page.wait_for_timeout(300)
    assert _dialog_open(page, "add-lib-modal"), "add-library modal did not open"
    assert page.locator("#lib-path-field").is_visible(), "path field hidden in filesystem mode"
    assert not page.locator("#lib-s3-fields").is_visible(), "S3 fields visible in filesystem mode"
    # Regression guard (QA M1): Tailwind preflight zeroed dialog{margin:auto},
    # so modals rendered jammed in the top-left. They must be centered.
    assert _dialog_is_centered(page, "add-lib-modal"), "add-library modal not centered"
    check("settings#add-lib(filesystem)", "settings_add_lib.png")

    # --- flip to S3: the fields must swap ---
    page.select_option("#lib-backend", "s3")
    page.wait_for_timeout(200)
    assert not page.locator("#lib-path-field").is_visible(), "path field still visible in S3 mode"
    assert page.locator("#lib-s3-fields").is_visible(), "S3 fields not shown in S3 mode"
    assert page.locator("#lib-s3-bucket").is_visible(), "bucket input missing in S3 mode"
    check("settings#add-lib(s3)", "settings_add_lib_s3.png")

    # Escape closes the native dialog.
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    assert not _dialog_open(page, "add-lib-modal"), "Escape did not close the add-library modal"

    # --- settings: edit-library modal (needs a seeded library row) ---
    page.wait_for_selector("[data-edit-id]", timeout=10_000)
    page.locator("[data-edit-id]").first.click()
    page.wait_for_timeout(300)
    assert _dialog_open(page, "edit-lib-modal"), "edit-library modal did not open"
    assert _dialog_is_centered(page, "edit-lib-modal"), "edit-library modal not centered"
    # Regression guard (QA H2): the Tailwind forms-plugin's appearance:none
    # made accent-color inert, so checked boxes looked unchecked — a
    # disable-the-library footgun. The control must render its native state.
    assert (
        page.eval_on_selector("#edit-lib-enabled", "el => getComputedStyle(el).appearance")
        != "none"
    ), "library checkboxes have appearance:none — checked state is invisible"
    check("settings#edit-lib", "settings_edit_lib.png")
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    assert not _dialog_open(page, "edit-lib-modal"), "Escape did not close the edit-library modal"

    # --- workers: "Add a worker" disclosure panel ---
    page.goto(f"{qa_base_url}/workers", wait_until="domcontentloaded")
    page.wait_for_timeout(1000)
    page.click("#add-worker-toggle")
    page.wait_for_timeout(300)
    assert page.locator("#add-worker-panel").is_visible(), "add-worker panel did not expand"
    check("workers#add-worker", "workers_add_panel.png")

    report = json.dumps(
        {
            "axe_blocking": axe_blocking,
            "error_toasts": toast_hits,
            "console_errors": console_errors[:25],
            "bad_api_responses": bad_api[:25],
        },
        indent=2,
    )
    print("\n=== DIALOG SWEEP REPORT ===\n" + report)

    assert not axe_blocking, f"axe blocking violations in an open dialog:\n{report}"
    assert not toast_hits, f"error toast(s) during dialog interactions:\n{report}"
    assert not console_errors, f"console / page errors during dialog interactions:\n{report}"
    assert not bad_api, f"failed /api/ responses during dialog interactions:\n{report}"


@pytest.mark.qa
def test_error_toast_above_open_dialog(qa_base_url: str, admin_pw: str, page: Page) -> None:
    """Regression guard (ledger: settings-duplicate-path-409-behind-modal).

    showModal() puts the dialog in the browser top layer, where z-index is
    meaningless — an error toast fired while the add-library modal is open
    must still be the topmost element at its own center (visible AND
    clickable), not buried under the dialog backdrop where an invalid form
    submission looks like a silent failure."""
    login(page, qa_base_url, admin_pw)
    page.goto(f"{qa_base_url}/settings", wait_until="domcontentloaded")
    page.wait_for_timeout(1000)
    page.click("#add-lib-btn")
    page.wait_for_timeout(300)
    assert _dialog_open(page, "add-lib-modal"), "add-library modal did not open"

    # Empty name → 422 → persistent error toast, dialog stays open.
    page.fill("#lib-name", "")
    page.fill("#lib-path", "/tmp/qa-codify-toast")
    page.click("#add-lib-save")
    page.wait_for_selector('[data-toast-type="error"]', timeout=5_000)
    assert _dialog_open(page, "add-lib-modal"), "dialog should stay open for correction"
    # Let the slide-in animation settle — the toast enters from off-viewport
    # right, so an immediate hit test measures a mid-flight rect.
    page.wait_for_timeout(600)

    hit = page.evaluate(
        """() => {
            const t = document.querySelector('[data-toast-type="error"]');
            const r = t.getBoundingClientRect();
            const el = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
            return {
                topmost: el ? el.tagName + (el.id ? '#' + el.id : '') : null,
                inside_toast: el ? (t === el || t.contains(el)) : false,
                has_box: r.width > 0 && r.height > 0,
            };
        }"""
    )
    assert hit["has_box"], "error toast has no rendered box while the dialog is open"
    assert hit["inside_toast"], (
        f"error toast is buried under {hit['topmost']} while the dialog is open"
    )

    # The dismiss X must be genuinely clickable right now, dialog still open.
    page.click('[data-toast-type="error"] .forge-toast-x', timeout=3_000)
    page.wait_for_timeout(200)
    assert page.locator('[data-toast-type="error"]').count() == 0, (
        "dismiss click did not remove the toast"
    )
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)


@pytest.mark.qa
def test_schedule_list_live_refresh(qa_base_url: str, admin_pw: str, page: Page) -> None:
    """Regression guard (QA M2/M3): add and delete must update the schedule
    list WITHOUT a page reload. The old code re-fired the htmx `load`
    trigger, a no-op in htmx 2.x, so the list went stale behind a success
    toast."""
    login(page, qa_base_url, admin_pw)
    page.goto(f"{qa_base_url}/settings", wait_until="domcontentloaded")
    page.wait_for_selector("#schedules-list", timeout=10_000)
    page.wait_for_timeout(600)

    page.fill("#sched-name", "qa-live-refresh")
    page.click("#add-schedule-btn")
    # No reload: the row must appear from the live partial swap.
    page.wait_for_selector("#schedules-list:has-text('qa-live-refresh')", timeout=5_000)

    # Delete direction — accept the confirm(), row must vanish without reload.
    page.on("dialog", lambda d: d.accept())
    page.click("button[aria-label='Delete qa-live-refresh']")
    page.wait_for_function(
        "!document.querySelector('#schedules-list').innerText.includes('qa-live-refresh')",
        timeout=5_000,
    )


@pytest.mark.qa
def test_worker_token_panels_live_refresh(
    tmp_path_factory: pytest.TempPathFactory, browser: Browser
) -> None:
    """Regression guard (ledger: workers-token-panels-stale-after-issue-revoke):
    issuing and revoking a worker token must update the token panels WITHOUT
    a page reload — the schedules guard's sibling, same htmx `load` no-op
    root cause, fixed by the same custom `refresh` trigger (PR #30).

    The 4s assertion windows sit well inside the panels' 5s/10s polls, which
    is what masked the original bug. Runs on its OWN instance: a revoked
    token row is permanent, and the shared instance's /workers render is
    pixel-baselined (test_visual)."""
    qa_dir = tmp_path_factory.mktemp("qa-tokens")
    with launch_qa_app(qa_dir, TOKENS_PORT, create_admin=True) as base_url:
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.on("dialog", lambda d: d.accept())  # revoke uses confirm()
        login(page, base_url, ADMIN_PW)

        page.goto(f"{base_url}/workers", wait_until="domcontentloaded")
        # Let the tokens panel finish its initial load (fresh instance: empty
        # state), so the next content change must come from the custom
        # refresh, not the load trigger.
        page.wait_for_selector("#tokens-list:has-text('No worker tokens yet')", timeout=10_000)

        page.click("#add-worker-toggle")
        page.fill("#add-worker-label", "qa-token-refresh")
        page.click("#issue-token-btn")
        # Issue direction: both panels must show the new token live.
        page.wait_for_selector("#tokens-list tr:has-text('qa-token-refresh')", timeout=4_000)
        page.wait_for_selector(
            "#add-worker-tokens-list tr:has-text('qa-token-refresh')", timeout=4_000
        )

        # Ride past the panels' next poll tick so the revoke assertion window
        # is clean — a regressed no-op would leave the pill stale until the
        # 10s poll, far outside the 4s bound.
        page.wait_for_timeout(5_000)

        # Revoke from the canonical "Worker tokens" panel (the same button
        # sits in both panels — scope so the click is unambiguous). The row
        # must flip to the Revoked pill live; :has-text is case-insensitive,
        # so it matches the CSS-uppercased "REVOKED".
        page.click("#tokens-list button[aria-label='Revoke token qa-token-refresh']")
        page.wait_for_selector(
            "#tokens-list tr:has-text('qa-token-refresh'):has-text('Revoked')", timeout=4_000
        )
        ctx.close()


@pytest.mark.qa
def test_validation_toast_is_readable(qa_base_url: str, admin_pw: str, page: Page) -> None:
    """Regression guard (QA M4): a 422 validation error must show a readable
    message, never '[object Object]' (FastAPI's array-shaped `detail`)."""
    login(page, qa_base_url, admin_pw)
    page.goto(f"{qa_base_url}/settings", wait_until="domcontentloaded")
    page.wait_for_timeout(600)
    page.click("#add-lib-btn")
    page.wait_for_timeout(300)
    # Empty name + a path → 422.
    page.fill("#lib-path", "/media/qa-invalid")
    page.click("#add-lib-save")
    page.wait_for_timeout(600)
    toasts = error_toasts(page)
    assert toasts, "no error toast on an invalid add-library submit"
    joined = " ".join(toasts)
    assert "[object Object]" not in joined, f"unreadable validation toast: {joined!r}"

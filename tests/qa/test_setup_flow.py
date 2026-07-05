"""First-run /setup sweep — the one page the main sweep can never reach
(its session fixture creates the admin before any test runs, so /setup
302s there). This module boots its OWN fresh instance with no admin and:

  * sweeps /setup (axe blocking rules + screenshot + error capture)
  * completes the form through the real UI
  * asserts the redirect lands authenticated on the dashboard
"""

import json

import pytest
from playwright.sync_api import Browser

from tests.qa.conftest import launch_qa_app
from tests.qa.sweep_lib import (
    SHOTS,
    attach_error_capture,
    blocking_violations,
    error_toasts,
)

SETUP_PORT = 18801
SETUP_PW = "setup-flow-password-123"


@pytest.mark.qa
def test_first_run_setup_flow(tmp_path_factory: pytest.TempPathFactory, browser: Browser) -> None:
    qa_dir = tmp_path_factory.mktemp("qa-setup")
    with launch_qa_app(qa_dir, SETUP_PORT, create_admin=False) as base_url:
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        console_errors, bad_api = attach_error_capture(page)

        # A fresh instance routes to /setup from anywhere.
        page.goto(f"{base_url}/", wait_until="domcontentloaded")
        page.wait_for_timeout(800)
        assert "/setup" in page.url, f"fresh instance did not route to /setup (at {page.url})"

        SHOTS.mkdir(exist_ok=True)
        page.screenshot(path=str(SHOTS / "setup.png"), full_page=True)
        axe_hits = blocking_violations(page)
        toasts = error_toasts(page)

        # Mismatched confirm shows the inline error, no navigation.
        page.fill("#password", SETUP_PW)
        page.fill("#password2", "does-not-match-123")
        page.click("button[type=submit]")
        page.wait_for_timeout(400)
        assert page.locator("#err").is_visible(), "mismatch error not shown"
        assert "/setup" in page.url

        # Real setup through the form → redirected to the dashboard, signed in.
        page.fill("#password", SETUP_PW)
        page.fill("#password2", SETUP_PW)
        page.click("button[type=submit]")
        page.wait_for_url(f"{base_url}/", timeout=10_000)
        page.wait_for_timeout(800)
        assert page.locator("#dashboard-stats").count() == 1, "dashboard did not render after setup"

        ctx.close()

        report = json.dumps(
            {
                "axe_blocking": axe_hits,
                "error_toasts": toasts,
                "console_errors": console_errors[:25],
                "bad_api_responses": bad_api[:25],
            },
            indent=2,
        )
        print("\n=== SETUP FLOW REPORT ===\n" + report)

        assert not axe_hits, f"axe blocking violations on /setup:\n{report}"
        assert not toasts, f"error toast on /setup:\n{report}"
        assert not console_errors, f"console errors during setup flow:\n{report}"
        assert not bad_api, f"failed /api/ responses during setup flow:\n{report}"

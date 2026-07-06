"""Shared Playwright helper for the AI exploratory UX sweep (qa/scenarios.md).

Each scenario agent writes a short script that drives the seeded demo app
through one user task and judges what happened. This module removes the
boilerplate: launching the browser, logging in, and capturing the two signals
a reviewer cares most about — console/page errors and *persistent* error
toasts (errors stay until dismissed by design, so they can't be missed).

Usage from an agent's scenario script (run from the repo root):

    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path("qa").resolve()))
    from sweep_helpers import session, error_toasts, console_errors, snap

    with session(BASE_URL, PASSWORD) as page:
        page.goto(BASE_URL + "/settings", wait_until="domcontentloaded")
        page.wait_for_timeout(1000)
        snap(page, "settings")
        print({"toasts": error_toasts(page), "console": console_errors(page)})
"""

from __future__ import annotations

import contextlib
import os
import pathlib
from collections.abc import Iterator

from playwright.sync_api import Page, sync_playwright

# Screenshots land in the current run's directory when the sweep sets
# QA_RUN_DIR (qa/runs/<run>/shots); bare qa/shots otherwise (back-compat).
_run_dir = os.environ.get("QA_RUN_DIR")
SHOTS = pathlib.Path(_run_dir) / "shots" if _run_dir else pathlib.Path(__file__).parent / "shots"


@contextlib.contextmanager
def session(base_url: str, password: str, *, headless: bool = True) -> Iterator[Page]:
    """Launch a browser, log in (handling first-run /setup too), and yield the
    page. Console/page errors accumulate on ``page`` for the whole session."""
    SHOTS.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        errs: list[str] = []
        page.on(
            "console",
            lambda m: errs.append(f"{m.type}: {m.text}") if m.type == "error" else None,
        )
        page.on("pageerror", lambda e: errs.append(f"pageerror: {e}"))
        # Auto-accept confirm()/alert() dialogs. Playwright dismisses them by
        # default, which silently cancels destructive actions gated behind a
        # confirm — that produced false "silent failure" findings. The demo
        # data is disposable, so accepting is safe and exercises the real path.
        page.on("dialog", lambda d: d.accept())
        page._sweep_console_errors = errs  # type: ignore[attr-defined]

        page.goto(f"{base_url}/login", wait_until="domcontentloaded")
        page.wait_for_timeout(800)
        # Setup page has two password fields; login has one.
        fields = page.query_selector_all("input[type=password]")
        for f in fields:
            f.fill(password)
        if fields:
            page.click("button[type=submit]")
            page.wait_for_timeout(1500)
        try:
            yield page
        finally:
            browser.close()


def error_toasts(page: Page) -> list[str]:
    """Text of any error toasts currently on screen (persistent by design)."""
    return page.evaluate(
        "Array.from(document.querySelectorAll('[data-toast-type=\"error\"]')).map(e => e.innerText)"
    )


def console_errors(page: Page) -> list[str]:
    """Console/page errors seen so far this session."""
    return list(getattr(page, "_sweep_console_errors", []))


def snap(page: Page, name: str) -> str:
    """Full-page screenshot under qa/shots/, returns the path."""
    SHOTS.mkdir(exist_ok=True)
    path = SHOTS / f"sweep-{name}.png"
    page.screenshot(path=str(path), full_page=True)
    return str(path)

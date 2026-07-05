"""Shared helpers for the deterministic QA sweep modules.

One source of truth for the axe blocking set, error capture, login, and
the small assertions every sweep module repeats.
"""

import pathlib

from playwright.sync_api import Page

AXE = pathlib.Path(__file__).parent / "vendor" / "axe.min.js"
SHOTS = pathlib.Path(__file__).parent / "shots"

PAGES = [
    "/",
    "/movies",
    "/tv",
    "/queue",
    "/activity",
    "/activity?view=skips",
    "/workers",
    "/stats",
    "/settings",
]

# axe rule ids we treat as blocking (the classes that have actually shipped
# bugs, plus the interactive-name family added in the v2 sweep).
BLOCKING_RULES = {
    "color-contrast",
    "label",
    "label-title-only",
    "select-name",
    "form-field-multiple-labels",
    "button-name",
    "link-name",
    "aria-required-attr",
    "duplicate-id-aria",
    "image-alt",
}


def page_shot_name(path: str) -> str:
    return path.strip("/").replace("/", "_").replace("?", "_").replace("=", "_") or "dashboard"


def run_axe(page: Page) -> list[dict]:
    page.add_script_tag(path=str(AXE))
    page.wait_for_timeout(300)
    return page.evaluate(
        "async () => (await axe.run()).violations.map(v => "
        "({id: v.id, impact: v.impact, count: v.nodes.length, "
        "targets: v.nodes.map(n => n.target).slice(0, 4)}))"
    )


def blocking_violations(page: Page) -> list[dict]:
    return [v for v in run_axe(page) if v["id"] in BLOCKING_RULES]


def error_toasts(page: Page) -> list[str]:
    return page.evaluate(
        "Array.from(document.querySelectorAll('[data-toast-type=\"error\"]')).map(e => e.innerText)"
    )


def attach_error_capture(page: Page) -> tuple[list[str], list[str]]:
    """Wire console/pageerror/failed-API listeners; returns the live lists."""
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
    return console_errors, bad_api


def login(page: Page, base_url: str, admin_pw: str) -> None:
    page.goto(f"{base_url}/login", wait_until="domcontentloaded")
    page.fill("input[type=password]", admin_pw)
    page.click("button[type=submit]")
    page.wait_for_timeout(1500)
    assert "/login" not in page.url, "login did not succeed"


def horizontal_overflow(page: Page) -> int:
    """Pixels of horizontal overflow (0 = none). The body must never
    scroll sideways; wide content scrolls inside its own container."""
    return page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )


def tab_reaches_a_control(page: Page) -> bool:
    """Press Tab from the page and check focus lands on a real control —
    guards against focus traps and pages with no focusable content."""
    page.evaluate("document.activeElement && document.activeElement.blur()")
    page.keyboard.press("Tab")
    return page.evaluate(
        "['A','BUTTON','INPUT','SELECT','TEXTAREA','SUMMARY']"
        ".includes(document.activeElement && document.activeElement.tagName)"
        " || (document.activeElement && document.activeElement.tabIndex >= 0)"
    )

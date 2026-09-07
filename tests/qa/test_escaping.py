"""The one client-side escaper must be safe in every HTML context it is
pasted into: element text AND quoted attributes (ledger R-008).

catalog.js builds the Movies and TV tables from template strings and puts
esc(filename) inside aria-label, title and data-* attributes. Filenames
come from Sonarr, Radarr and release names, not from the admin, so a
double quote in one used to close the attribute and add a new one
(onfocus=...) in the logged-in admin tab. This runs esc() in the real
browser and asserts the attribute round-trips byte for byte with nothing
injected.
"""

import pytest
from playwright.sync_api import Page

from tests.qa.sweep_lib import login

PAYLOAD = 'x" autofocus onfocus="window.__pwned=1'


@pytest.mark.qa
def test_esc_is_safe_in_text_and_attributes(qa_base_url: str, admin_pw: str, page: Page) -> None:
    login(page, qa_base_url, admin_pw)
    page.goto(f"{qa_base_url}/movies")

    escaped = page.evaluate(
        "() => import('/static/js/toast.js').then((m) => m.esc('<a href=\"x\">O\\'Neil & co</a>'))"
    )
    assert escaped == "&lt;a href=&quot;x&quot;&gt;O&#39;Neil &amp; co&lt;/a&gt;"

    # The catalog.js pattern: esc() inside a double-quoted attribute of a
    # template string assigned via innerHTML.
    result = page.evaluate(
        """(payload) => import('/static/js/toast.js').then((m) => {
            const host = document.createElement('div');
            host.innerHTML = `<input aria-label="Select ${m.esc(payload)}">`;
            const input = host.firstElementChild;
            const label = input.getAttribute('aria-label');
            const names = input.getAttributeNames();
            host.innerHTML = `<td>${m.esc(payload)}</td>`;
            return {
                label,
                names,
                text: host.textContent,
                pwned: window.__pwned === 1,
            };
        })""",
        PAYLOAD,
    )
    assert result["label"] == f"Select {PAYLOAD}"
    assert result["names"] == ["aria-label"]
    assert result["text"] == PAYLOAD
    assert result["pwned"] is False

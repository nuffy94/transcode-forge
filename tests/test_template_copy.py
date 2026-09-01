"""The UI's voice register, enforced.

User-visible template copy never uses an em dash: plain sentences,
periods and colons (codified from the 2026-08-31 copy sweep, the same
register as the docs). The dash survives only where it is not prose:

- the standalone "no data" placeholder glyph in stat tiles and table
  cells (``>—<``, ``'—'``, ``"—"``),
- developer comments (Jinja ``{# #}`` and HTML ``<!-- -->``), which
  never render.
"""

import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "src" / "transcode_forge" / "web" / "templates"

_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_PLACEHOLDER = re.compile(r">\s*—\s*<|'—'|\"—\"|%\}\s*—\s*\{%")
_DASH = re.compile(r"—|&mdash;|&#8212;|&#x2014;", re.IGNORECASE)


def _blank_preserving_lines(match: re.Match[str]) -> str:
    return "\n" * match.group(0).count("\n")


def test_no_em_dashes_in_rendered_copy() -> None:
    assert TEMPLATES.is_dir(), f"template dir missing: {TEMPLATES}"
    offenders: list[str] = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        text = _JINJA_COMMENT.sub(_blank_preserving_lines, text)
        text = _HTML_COMMENT.sub(_blank_preserving_lines, text)
        text = _PLACEHOLDER.sub("", text)
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _DASH.search(line):
                rel = path.relative_to(TEMPLATES)
                offenders.append(f"{rel}:{lineno}: {line.strip()[:110]}")
    assert not offenders, (
        "Em dash in user-visible template copy (use a period or colon; "
        "see this test's docstring for the allowed placeholder forms):\n" + "\n".join(offenders)
    )

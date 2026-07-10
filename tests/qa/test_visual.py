"""Visual layer (qa-redesign spec D4): geometry invariants + pixel baselines.

Tier 1 — geometry invariants, run everywhere, flake-proof: per page, assert
layout *intent* (elements contained by the viewport, the nav rail's fixed
width, the pinned header, meter fills inside their troughs, top-level
sections not overlapping) rather than pixels. Dialog centering is guarded
separately in test_dialogs.py.

Tier 2 — pixel baselines, CI-only: committed PNGs in tests/qa/baselines/
(desktop 1440x900, one per swept page) compared with a Pillow diff ratio.
Comparison runs only when CI is set — baselines carry the ubuntu font
stack, so local Windows/mac runs skip instead of flaking (the May finding).
Every CI run also writes the current renders to tests/qa/shots/visual/
(ridden out by the existing qa-screenshots artifact); install them as new
baselines with scripts/update_qa_baselines.py after an intentional UI
change, and let the PNG diff ride the PR for eyeball review.
"""

import os
from pathlib import Path

import pytest
from PIL import Image, ImageChops
from playwright.sync_api import Page

from tests.qa.sweep_lib import PAGES, SHOTS, login, page_shot_name

BASELINES = Path(__file__).parent / "baselines"
CANDIDATES = SHOTS / "visual"

# Per-channel deltas at or below this are the same pixel (anti-aliasing
# wiggle); a page fails when more than MAX_DIFF_RATIO of pixels differ.
PIXEL_TOLERANCE = 24
MAX_DIFF_RATIO = 0.005

# Freeze everything time-driven before shooting: CSS animations (meter
# shimmer, pulse dots), the live header clock, and every element marked
# data-visual-volatile (server-rendered wall-clock timestamps — the seed is
# now()-anchored, so their digits differ between CI runs by design).
# visibility:hidden keeps each box in layout, so hiding never reflows.
_FREEZE_CSS = (
    "*, *::before, *::after { animation: none !important; transition: none !important; }\n"
    "#forge-clock, [data-visual-volatile] { visibility: hidden !important; }"
)

_GEOMETRY_JS = """
() => {
  const vw = window.innerWidth;
  const out = { overflow: [], meters: [], overlaps: [], sidebar: null, header: null };
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden';
  };
  const tag = (el) => el.tagName.toLowerCase()
    + (el.id ? '#' + el.id : '')
    + (el.className && typeof el.className === 'string'
        ? '.' + el.className.trim().split(/\\s+/).slice(0, 2).join('.') : '');

  // 1. Viewport containment: nothing in the content canvas pokes past the
  //    right edge or starts left of the page. Raw <table>s are exempt —
  //    they legitimately exceed their .forge-scroll wrapper (which scrolls
  //    them by design), so the wrapper is the element that must fit.
  const contained = 'main section, main .forge-scroll, main .forge-panel';
  for (const el of document.querySelectorAll(contained)) {
    if (!visible(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.right > vw + 1 || r.left < -1)
      out.overflow.push({ el: tag(el), left: r.left, right: r.right, vw });
  }

  // 2. Nav rail fixed width (w-60 = 240px at desktop).
  const aside = document.querySelector('aside.forge-sidebar');
  if (aside) {
    const w = aside.getBoundingClientRect().width;
    if (Math.abs(w - 240) > 2) out.sidebar = w;
  }

  // 3. Header pinned to the top edge.
  const header = document.querySelector('header.forge-header');
  if (header) {
    const t = header.getBoundingClientRect().top;
    if (Math.abs(t) > 1) out.header = t;
  }

  // 4. Meter fills stay inside their troughs.
  for (const fill of document.querySelectorAll('.forge-meter .forge-meter-fill')) {
    if (!visible(fill)) continue;
    const f = fill.getBoundingClientRect();
    const m = fill.closest('.forge-meter').getBoundingClientRect();
    if (f.right > m.right + 1 || f.left < m.left - 1) {
      out.meters.push({ fill: [f.left, f.right], trough: [m.left, m.right] });
    }
  }

  // 5. Sibling sections never overlap: group EVERY section in main by its
  //    parent and compare within each group — this reaches the nested grid
  //    wrappers (dashboard's 3-column row, queue's scan row) where an
  //    overlap regression would actually show, without flagging legitimate
  //    nesting.
  const byParent = new Map();
  for (const s of document.querySelectorAll('main section')) {
    if (!visible(s)) continue;
    const list = byParent.get(s.parentElement) || [];
    list.push(s);
    byParent.set(s.parentElement, list);
  }
  for (const group of byParent.values()) {
    for (let i = 0; i < group.length; i++) {
      for (let j = i + 1; j < group.length; j++) {
        const a = group[i].getBoundingClientRect();
        const b = group[j].getBoundingClientRect();
        const x = Math.min(a.right, b.right) - Math.max(a.left, b.left);
        const y = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
        if (x > 2 && y > 2) out.overlaps.push({ a: tag(group[i]), b: tag(group[j]), x, y });
      }
    }
  }
  return out;
}
"""


def _diff_ratio(a: Image.Image, b: Image.Image) -> float:
    """Fraction of pixels whose MAX per-channel delta exceeds the tolerance.

    Genuinely per-channel: the difference is reduced with a channel-wise max
    (never a luma conversion, which would let an 80% blue-channel swing read
    as "the same pixel").
    """
    diff = ImageChops.difference(a.convert("RGB"), b.convert("RGB"))
    r, g, b_ = diff.split()
    peak = ImageChops.lighter(ImageChops.lighter(r, g), b_)
    mask = peak.point(lambda v: 255 if v > PIXEL_TOLERANCE else 0)
    histogram = mask.histogram()
    return histogram[255] / (mask.width * mask.height)


@pytest.mark.qa
def test_geometry_invariants(qa_base_url: str, admin_pw: str, page: Page) -> None:
    login(page, qa_base_url, admin_pw)
    failures: dict[str, dict] = {}
    for path in PAGES:
        page.goto(f"{qa_base_url}{path}", wait_until="domcontentloaded")
        page.wait_for_timeout(1200)
        report = page.evaluate(_GEOMETRY_JS)
        if any((report["overflow"], report["meters"], report["overlaps"])) or (
            report["sidebar"] is not None or report["header"] is not None
        ):
            failures[path] = report
    assert not failures, f"geometry invariants violated:\n{failures}"


@pytest.mark.qa
@pytest.mark.skipif(
    not os.environ.get("CI"),
    reason="pixel baselines carry the ubuntu-CI font stack — local runs skip (D4)",
)
def test_pixel_baselines(qa_base_url: str, admin_pw: str, page: Page) -> None:
    login(page, qa_base_url, admin_pw)
    page.add_style_tag(content=_FREEZE_CSS)
    CANDIDATES.mkdir(parents=True, exist_ok=True)

    missing: list[str] = []
    diffs: dict[str, object] = {}
    for path in PAGES:
        name = page_shot_name(path)
        page.goto(f"{qa_base_url}{path}", wait_until="domcontentloaded")
        # Let the load-triggered partials swap their skeletons out before
        # shooting; the pollers leave >500ms of quiet between ticks, so
        # networkidle is reachable — but never block the run on it.
        try:
            page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            pass
        page.wait_for_timeout(700)
        page.add_style_tag(content=_FREEZE_CSS)
        candidate_path = CANDIDATES / f"{name}.png"
        page.screenshot(path=str(candidate_path))  # viewport-sized, 1440x900

        baseline_path = BASELINES / f"{name}.png"
        if not baseline_path.is_file():
            missing.append(name)
            continue
        baseline = Image.open(baseline_path)
        candidate = Image.open(candidate_path)
        if baseline.size != candidate.size:
            # Pillow would silently crop to the intersection — fail loudly
            # instead; a dimension change means the baselines need recapture.
            diffs[name] = f"size {baseline.size} vs {candidate.size} — recapture baselines"
            continue
        ratio = _diff_ratio(baseline, candidate)
        if ratio > MAX_DIFF_RATIO:
            diff_img = ImageChops.difference(baseline.convert("RGB"), candidate.convert("RGB"))
            diff_img.save(CANDIDATES / f"{name}.diff.png")
            diffs[name] = round(ratio, 5)

    assert not diffs, (
        f"pixel drift beyond {MAX_DIFF_RATIO:.1%}: {diffs} — if intentional, install the "
        "new baselines from this run's qa-screenshots artifact via "
        "scripts/update_qa_baselines.py and let the PNG diff ride the PR"
    )
    if missing:
        pytest.skip(
            f"no baselines yet for {missing} — candidates are in the qa-screenshots "
            "artifact; install with scripts/update_qa_baselines.py"
        )

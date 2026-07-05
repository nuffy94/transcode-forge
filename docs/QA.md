# UX / QA testing routine

How we catch UX bugs, broken flows, and visual problems **repeatably** without
paying for an AI on every run. The principle: *explore once with AI, codify the
findings, replay for free.*

Everything runs against a **seeded demo instance** — the app in demo-static
mode (realistic data, no Redis/ffmpeg, no live server). So the whole routine is
reproducible on any machine with one command, no Linode required.

```bash
# Launch the demo target (seeded, deterministic) on :18799:
TF_DEMO_STATIC=true TF_DB_URL="sqlite:///qa_sweep_live.db" TF_AUTH_SECRET=qa-sweep \
  uv run uvicorn transcode_forge.main:app --host 127.0.0.1 --port 18799
# First time, open http://127.0.0.1:18799 and set the admin password to
# 'qa-sweep-password-123' (the value the tooling logs in with).
```

## The layers (cheapest first)

| Layer | What | Cost | When |
|---|---|---|---|
| **L1 — unit/integration** | `pytest` (excl. e2e/qa) | free | every push (CI) |
| **L2 — deterministic UX sweep** | `pytest tests/qa/` — axe + error/console capture + screenshots over every page | free | every push (CI) |
| **L3 — AI exploratory sweep** | `qa/ux-sweep.workflow.js` — agents drive real user scenarios and judge them | tokens | on demand / before a release |
| **L4 — codify** | turn an L3 finding into an L2 assertion | free thereafter | whenever L3 finds something |

L1+L2 run forever for free and guard against regressions. L3 is the occasional
discovery pass; anything it finds becomes an L2 assertion (L4), so you never pay
to re-discover the same bug.

## L2 — deterministic sweep (`tests/qa/`)

```bash
uv run pytest tests/qa/        # excluded from the default suite
```

Four modules share one seeded demo instance (helpers in
`tests/qa/sweep_lib.py` — the `BLOCKING_RULES` axe set, error capture,
login, overflow/focus checks live there once):

**`test_sweep.py`** visits every page — the `PAGES` list covers dashboard,
movies, tv, queue, both Activity facets (`/activity` and
`/activity?view=skips`), workers, stats, and settings — and fails on:

- **axe-core** blocking violations — `color-contrast`, the missing-`label`
  family, and the interactive-name family (`button-name`, `link-name`,
  `aria-required-attr`, `duplicate-id-aria`, `image-alt`). Vendored
  `tests/qa/vendor/axe.min.js`, so it works offline.
- **console / page errors** and **failed `/api` responses** during browsing.
- **error toasts** present on any page. Error toasts are *persistent* by design
  (they require a click to dismiss — see `static/js/toast.js`), specifically so
  a transient error can never slip past a screenshot or an assertion.
- a **load-bearing element missing** (`STRUCTURAL_ANCHORS` — the dead-partial
  detector: a page can 200 with a dead HTMX section and no console error).
- **document-level horizontal overflow** at desktop width, and **Tab from the
  body not reaching a focusable control** (focus-trap guard).

After the page loop it also opens the **file-detail drawer** on a transcoded
movie (axe re-runs against the open state; `movies_drawer.png` is captured)
and sweeps **`/login` in a fresh unauthenticated context**.

**`test_dialogs.py`** re-runs axe + error capture against every dialog in its
OPEN state: the add-library modal in both storage modes (the backend select
must swap Path for Bucket/Prefix), the edit-library modal, and the Workers
"Add a worker" panel — and asserts `Escape` closes the real `<dialog>`s.

**`test_mobile.py`** re-sweeps every page at 390×844: the page body must not
scroll horizontally (wide tables scroll inside their own `forge-scroll`
container), nav must be present, error capture stays on. Screenshots land in
`tests/qa/shots/mobile/`.

**`test_setup_flow.py`** boots its own fresh instance (no admin) and sweeps
the one page the main sweep can never reach: `/setup` — axe, the
mismatched-confirm error path, and the real create-admin → dashboard flow.

Full-page screenshots of each page land in `tests/qa/shots/` (gitignored) for
visual review / diffing.

This layer alone has already caught: the broken Schedules partial, every input
rendering white (Tailwind forms-plugin cascade), unlabeled bulk-select
checkboxes, a demo-mode Redis startup crash, a misleading health 503, and —
on the mobile pass's first ever run — horizontal overflow on four pages
(queue, both Activity facets, stats: tables missing their `forge-scroll`
wrapper).

## L3 — AI exploratory sweep (`qa/ux-sweep.workflow.js`)

The on-demand discovery pass. One agent per scenario (see `qa/scenarios.md`)
**drives the running demo app** through a real user task — add a library and
scan it, queue a transcode, issue/revoke a worker token, create/delete a
schedule, probe invalid inputs — and *judges* what happened (did the right
thing happen, was it clear, did anything break or go silently missing), not just
whether it 200'd. Each "broke" finding is independently re-verified by a second
agent before it lands in the report.

```bash
# 1. Launch the demo target (above) and set the admin password.
# 2. Run the workflow (defaults to http://127.0.0.1:18799 + the demo password):
#    invoke the Workflow tool with scriptPath "qa/ux-sweep.workflow.js"
#    (override args {baseUrl, password} to point elsewhere).
```

It returns a prioritized report of verified issues plus, for each, a suggested
`tests/qa/` assertion to lock it (the L4 loop). `qa/sweep_helpers.py` gives the
agents login + error-capture so their scenario scripts stay short.

## The codify loop (L4)

When L3 surfaces a real issue: fix it, then add an assertion to `tests/qa/`
(another page/element check, or extend `BLOCKING_RULES` / the toast/console
checks). From then on L2 catches any regression for free. That's why the
deterministic suite keeps growing and the AI cost trends toward zero.

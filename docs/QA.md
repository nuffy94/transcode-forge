# UX / QA testing routine

How we catch UX bugs, broken flows, and visual problems **repeatably**
without paying for an AI on every run. The doctrine: *explore once with AI,
codify the findings, replay for free.* Proven in practice — the 2026-07-05
exploratory run confirmed 6 real product bugs; 5 are now free deterministic
guards and the 6th is fixed awaiting its guard (`/qa-codify
workers-token-panels-stale-after-issue-revoke` closes it). Every finding's
lifecycle is tracked in `qa/findings.yml`.

**One QA system, four layers, one boot substrate.** Every layer is either
free-in-CI or explicitly priced and human-triggered. Every finding has a
lifecycle (found → verified → ledgered → codified/fixed → guarded).

```
L1  unit/integration      pytest             CI      free
L2  deterministic sweep   pytest tests/qa/   CI      free
L3  AI exploratory        qa/ workflow       local   priced, release-gated
L4  codify loop           /qa-codify         local   the L3 → L1/L2 compiler
──────────────────────────────────────────────────────────────────────────
substrate: qa/instance.py (one launcher) · deterministic coherent seed ·
findings ledger (qa/findings.yml) · coverage gate · pixel baselines
```

## The boot substrate (`qa/instance.py`)

Every QA surface that needs a real HTTP server boots it through **one**
module, always in demo-static mode (seeded, deterministic, no Redis/ffmpeg
required):

- `launch()` — attached child process for pytest. `tests/qa/conftest.py`
  wraps it as `launch_qa_app` (session fixture + the fresh no-admin
  instance `test_setup_flow.py` uses).
- `start_detached()` / `stop_detached()` — pidfile-managed instances behind
  the `qa/launch_demo.py` CLI (the L3 sweep's per-agent instances). Their
  `READY`/`STOPPED` stdout lines are a contract the workflow's agent
  prompts parse; `tests/qa/test_instance.py` pins the exact wording.
- `bootstrap_admin()` — the single first-run auth bootstrap.

The seed itself is deterministic AND coherent: fixed RNG, every job's
lifecycle ordered (`created ≤ started ≤ completed`), waiting jobs at
distinct past timestamps, and a demo-static heartbeat keep-alive so worker
cards never decay into false "HEARTBEAT LOST" states mid-run
(`tests/test_demo_seed.py` guards all of it).

The old `tests/e2e/` suite is gone — absorbed into L1 + L2 (its threaded
in-process boot was the 4th boot mechanism and carried an asyncio-pollution
footgun). Empty-state and redirect assertions live in `tests/test_web.py`;
brand/computed-style/shell assertions live in `tests/qa/test_shell.py`.

## The layers (cheapest first)

| Layer | What | Cost (measured) | When |
|---|---|---|---|
| **L1 — unit/integration** | `pytest` | free · ~3m CI | every push (CI) |
| **L2 — deterministic sweep** | `pytest tests/qa/` | free · ~2m25s CI | every push (CI) |
| **L3 — AI exploratory** | `qa/ux-sweep.workflow.js` | 12–18 agents observed across full runs (verification agents scale with findings); each run logs its actual wall time + agent count into its `report.json` — that file is the authority, not this cell | before every release tag + after UI-heavy merges |
| **L4 — codify** | `/qa-codify <finding-id>` | free thereafter | whenever the ledger has something to close |

CI wall-times above are from real runs (2026-07-09: `test` ≈ 2m52–3m03s,
`qa-sweep` ≈ 1m37s before the visual tier, ≈ 2m24s with it). Re-measure
when the suite grows; the numbers live here so cost drift is visible.

## L2 — deterministic sweep (`tests/qa/`)

```bash
uv run pytest tests/qa/        # excluded from the default suite
```

Modules share one seeded instance (via the substrate) unless noted.
Shared helpers live in `tests/qa/sweep_lib.py` (the `BLOCKING_RULES` axe
set, `PAGES`, error capture, login, overflow/focus checks).

- **`test_sweep.py`** — visits every page in `PAGES` and fails on: axe
  blocking violations (vendored `axe.min.js`, offline), console/page
  errors, failed `/api` responses, error toasts (persistent by design),
  a missing **structural anchor** (`STRUCTURAL_ANCHORS` — the dead-partial
  detector; anchors check DOM presence deliberately, visibility semantics
  live in test_shell/test_visual), document-level horizontal overflow, and
  Tab-reaches-a-control. Also sweeps the file-detail drawer and `/login`
  in a fresh anonymous context.
- **`test_shell.py`** — the shell + brand contract in a real renderer:
  sidebar lockup/nav/sprite icons, header datum strip, design tokens by
  computed style (the `#0d0b08` field, `.forge-seam`, Big Shoulders),
  HTMX shell partials resolving past their placeholders, and Activity's
  facet exclusivity via BOTH render paths (client click and the
  server-side `?view=skips` conditional).
- **`test_dialogs.py`** — axe + error capture against every dialog OPEN
  (add-library in both storage modes, edit-library, add-worker), Escape
  closes real `<dialog>`s, the schedule list live-refresh guard, and the
  human-readable-422-toast guard.
- **`test_mobile.py`** — every page at 390×844: no horizontal body
  scroll, nav present, error capture on.
- **`test_setup_flow.py`** — its own fresh no-admin instance for `/setup`.
- **`test_visual.py`** — the visual layer (see below).
- **`test_coverage_gate.py`** — the coverage gap gate (see below).
- **`test_findings_ledger.py`** — schema guard for `qa/findings.yml`.
- **`test_instance.py`** — the substrate's detached CLI contract.

## Visual layer (`tests/qa/test_visual.py`)

Two tiers (design decision D4):

1. **Geometry invariants** — run everywhere, flake-proof. Per page:
   viewport containment (sections/panels/scroll-wrappers; raw tables are
   exempt — they scroll inside `.forge-scroll` by design), nav rail fixed
   at 240px, header pinned, meter fills inside their troughs, sibling
   sections never overlapping. These catch the layout-regression class
   structurally (a dialog pinned top-left, a panel escaping the grid).
2. **Pixel baselines** — CI-only. Committed PNGs in `tests/qa/baselines/`
   (desktop 1440×900, one per swept page) vs a per-channel Pillow diff
   (fail at >0.5% differing pixels). This is the only tier that sees the
   color/font/texture class — an ember gradient silently going gray passes
   every invariant. Local runs **skip**: baselines carry the ubuntu CI
   font stack; cross-OS pixel comparison is the flake we deliberately
   avoid. Animations are frozen and wall-clock-anchored text
   (`#forge-clock`, `[data-visual-volatile]`) is hidden before every shot.

**When you change the UI intentionally**: push, let qa-sweep run (it
uploads the current renders in the `qa-screenshots` artifact), then

```bash
uv run python scripts/update_qa_baselines.py --run <actions-run-id>
```

and commit — the PNG diff rides the PR for eyeball review. Never capture
baselines locally.

## Coverage model (`qa/coverage.py`)

The inventory and gap report are **derived**; intent stays hand-curated in
`qa/scenarios.md` (each scenario declares a `Routes:` line).

```bash
uv run python qa/coverage.py           # coverage table (markdown; exit 1 on gaps)
uv run python qa/coverage.py --json    # machine-readable
```

**When you add a page route** (the contract): the CI gate
(`test_coverage_gate.py`) fails qa-sweep for any top-level HTML page route
with zero QA mapping — register the page by adding it to `PAGES` +
`STRUCTURAL_ANCHORS` in `tests/qa/`, or waive it with a written reason in
`qa/coverage.py::COVERED_ELSEWHERE`. The gate is scoped to page routes
only (partials are covered transitively by their pages; `/api/*`
correctness is L1's job), which keeps its false-positive rate at ~zero.
New pages must land on the router in `web/routes.py` — that router is the
gate's derivation source.

## L3 — AI exploratory sweep (`qa/ux-sweep.workflow.js`)

The priced discovery pass. One agent per scenario (from `qa/scenarios.md`,
read at run time) drives a real app instance through a user task and
*judges* the result; every flagged finding is re-verified by an
independent agent on another fresh instance — clean-state reproduction or
it doesn't count.

- **Isolation** — every explorer/verifier gets its own detached instance
  (own port + throwaway sqlite) via the substrate's CLI.
- **Durability** — per-scenario JSON hits disk the moment it exists; a run
  that dies keeps everything completed, and re-running skips finished
  scenarios.
- **Bounded** — scenarios run in waves (default 3 at a time), and each
  scenario gets at most 3 verification agents (overflow is reported, never
  silently dropped). Observed costs: 18 agents on a run with findings
  to verify (1 prep + 10 explorers + 6 verify + 1 report); 12 on the
  2026-07-14 release-gate run, where nothing crossed the flag threshold
  so the verify wave never spawned. Each run's actual agent count and
  wall time land in its `report.json` — trust that over any number
  written here. `{serial: true}` is the
  rate-limit fallback (one at a time, one verifier per finding).
- **Ledgered output** — the synthesize step updates `qa/findings.yml`
  against the WHOLE ledger (semantic matching): recurrences of fixed
  entries re-surface as regressions on the same id, never duplicates; new
  verified findings enter as `new`, judged-but-unreproduced leads as
  `unverified`.
- **Cost-logged** — each run records wall time and agent count into
  `report.json` and states them in the report.

```bash
# Invoke the Workflow tool with scriptPath "qa/ux-sweep.workflow.js"
# optional args: {waveSize: 3, runDir: "qa/runs/latest", serial: false}
# Artifacts: qa/runs/latest/{report.md, report.json, S*.json, shots/}
```

**Cadence (policy)**: release-gated — run before every tag, plus after any
UI-heavy merge worth flagging. Not nightly: the drift-diff needs a stable
base, and unattended runs buy nothing.

## Findings ledger (`qa/findings.yml`) + the codify loop (L4)

Every L3 finding lives in the committed ledger with a stable semantic id
and a lifecycle:

```
unverified → new → verified → open → fixed → codified   (or wontfix)
```

`codified` means fixed AND guarded by a free deterministic test — the end
state everything should reach. The schema guard
(`test_findings_ledger.py`) enforces well-formed entries in CI: fixed and
codified entries must cite their PR; codified entries must cite an
existing guard test.

To close an entry:

```
/qa-codify <finding-id>
```

The command reproduces the finding (never guard what you can't observe),
routes the guard (browser-needing → `tests/qa/`, HTTP/template → L1 next
to the feature's tests), TDDs the fix, updates the ledger, and ships a PR.
That loop ran by hand six times in the week of 2026-07-05; the command
just names it.

## Staging smoke (the pre-release gate)

CI covers a synthetic real-ffmpeg encode on every push
(`tests/test_pipeline_integration.py`). The with-real-media version is
scripted but human-triggered (real ffmpeg time, a real file — deliberately
not CI):

```bash
./scripts/staging_smoke.sh /path/to/real-clip.mkv [hevc|av1]
```

It brings up the throwaway staging stack (`docker-compose.staging.yml`),
creates the admin, issues a worker token, starts the CPU worker, scans and
queues the file, polls the job to a terminal state, asserts the outcome
(**complete** = swapped and smaller; **skipped** = the size/VMAF gate kept
the original — also a PASS, that's the gate working), and tears everything
down. The manual walkthrough remains in [docs/STAGING.md](STAGING.md).
Green smoke → tag the release (after the release-gated L3 run).

## Reuse (what's designed to lift)

The kernel is deliberately portable to other projects (Thirsty, the bots):
the instance-manager pattern (`qa/instance.py`), `sweep_lib`'s gate set,
the workflow shape (isolate → persist → verify → synthesize), the ledger
schema, and the coverage-gate concept. **Don't extract yet** — the second
consumer drives the extraction into a shared template; until then it ships
in-forge only.

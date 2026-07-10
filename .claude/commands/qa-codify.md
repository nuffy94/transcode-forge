---
description: "L4 codify loop: turn a qa/findings.yml entry into a deterministic guard + fix"
argument-hint: "<finding-id>"
---

Codify the QA findings-ledger entry `$ARGUMENTS` — the loop that turns a
priced AI finding into a free deterministic guard (docs/QA.md, L4).

1. **Read the entry** in `qa/findings.yml` (match on `id`). If `$ARGUMENTS`
   is empty or matches nothing, list the entries whose status is
   `unverified`/`new`/`verified`/`open`/`fixed` and stop. Pull any extra
   evidence from `qa/runs/latest/report.md` (and `qa/runs/previous/`) if
   present — these are gitignored and may be absent; the ledger entry alone
   must be enough to proceed.

2. **Reproduce it first** if status is `unverified` or `new`: boot a demo
   instance (`uv run python qa/launch_demo.py --start --port 18871 --run-dir
   qa/runs/codify` — stop it when done) and follow the finding. If it does
   NOT reproduce, say so, set the entry's status to `wontfix` with a `note:`
   explaining why (or leave `unverified` with a dated note if inconclusive),
   and stop — never write a guard for a behavior you couldn't observe.

3. **Route the guard** (the routing rule the map showed we already use):
   - Needs a real browser (computed styles, visibility, clicks, dialogs,
     HTMX timing) → a `tests/qa/` module on the seeded instance.
   - HTTP/template/API-shape assertions (status codes, response text,
     redirects, empty states) → L1, next to its feature's tests in `tests/`.

4. **TDD the fix**: write the failing deterministic test first, then fix the
   product bug (smallest change; match existing style). If the fix is out of
   scope for one sitting, land the test as an xfail-marked guard, set status
   to `open`, and file the follow-up instead — never delete the evidence.

5. **Update the ledger entry**: status → `codified` (fix landed + guard test
   green: `refs: {pr: <n>, test: "<file>::<test>"}`) or `open` (xfail guard
   landed, fix pending: keep/point `refs.pr` at the follow-up). The `test`
   ref belongs to `codified` only; `fixed` and `codified` both require
   `refs.pr` — the schema guard (tests/qa/test_findings_ledger.py) enforces
   both rules. Bump `last_seen` if you re-observed the finding.

6. **Verify + ship**: `uv run ruff format src/ tests/` +
   `uv run ruff check src/ tests/` + `uv run mypy src/` + the relevant suite
   (`uv run pytest` and/or `uv run pytest tests/qa/`) all green; commit on a
   `qa-codify/<finding-id>` branch; open a PR whose body links the finding
   id and the run that surfaced it.

export const meta = {
  name: 'ux-qa-sweep',
  description: 'Task-driven AI UX sweep: agents drive the seeded demo app through real user scenarios, judge what works/breaks, verify, and synthesize a report',
  whenToUse: 'On-demand UX/QA discovery against a running demo-static instance. Launch the app first, then run with args {baseUrl, password}.',
  phases: [
    { title: 'Explore', detail: 'one agent per scenario drives the app + judges' },
    { title: 'Verify', detail: 'independently re-check each "broke" finding' },
    { title: 'Synthesize', detail: 'dedupe + prioritize + suggest deterministic tests' },
  ],
}

// --- inputs -----------------------------------------------------------------
// args may arrive as an object or a JSON string depending on the caller.
let A = args || {}
if (typeof A === 'string') {
  try { A = JSON.parse(A) } catch (e) { A = {} }
}
// Defaults match the documented launcher in docs/QA.md (demo-static on :18799
// with this throwaway admin password). Override via args to point elsewhere.
const BASE = A.baseUrl || 'http://127.0.0.1:18799'
const PW = A.password || 'qa-sweep-password-123'

// Scenario briefs mirror qa/scenarios.md (kept short here; full detail there).
const SCENARIOS = [
  { id: 'S1', title: 'Dashboard & navigation', brief: 'Load / then visit every nav item (Movies, TV, Queue, Workers, History, Skipped, Stats, Settings). Each should load with seeded content and no errors. Judge legibility and empty states.' },
  { id: 'S2', title: 'Library lifecycle', brief: 'Settings → Add Library (name "QA-S2 Movies", type movies, a path like /media/movies) → verify it appears → Scan it → check Movies reflects it → remove the library → verify it is gone.' },
  { id: 'S3', title: 'Queue a transcode', brief: 'Movies → select one file via its row checkbox → Queue Selected → go to Queue and confirm the job appears → pause then resume the queue.' },
  { id: 'S4', title: 'Settings persist', brief: 'Change a quality preset and another settings field. Save, reload, confirm values stuck. Judge UX: confusing defaults? unclear labels? should quality be a labelled slider?' },
  { id: 'S5', title: 'Worker onboarding', brief: 'Settings → Workers → Issue Token (label "qa-S5-node") → confirm the command block shows TF_SERVER_URL + TF_WORKER_TOKEN → revoke it → confirm it disappears.' },
  { id: 'S6', title: 'Schedules', brief: 'Settings → Schedules → New Window (name "qa-S6", start 23, end 7, pick weekdays) → Add → confirm it renders with the right day summary → delete it.' },
  { id: 'S7', title: 'Exclusions', brief: 'From History or Skipped, exercise the "Don\'t try again" / unexclude flow. Confirm the file moves to Skipped and can be lifted back.' },
  { id: 'S8', title: 'Error-path probes', brief: 'Do invalid things and confirm a CLEAR, PERSISTENT error toast appears each time: (a) add a library with empty name, (b) add a library with a duplicate path, (c) issue a worker token with empty label, (d) create a schedule with an out-of-range hour. Judge whether each message is specific and understandable.' },
  { id: 'S9', title: 'Visual & a11y judgement', brief: 'Visit /login (log out first via /api/auth/logout if needed), /settings, /movies, /queue. Judge text contrast (muted text, inputs, toast copy), label clarity, confusing wording, and layout/overflow.' },
]

const FINDINGS = {
  type: 'object',
  properties: {
    scenario: { type: 'string' },
    summary: { type: 'string' },
    steps: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          step: { type: 'string' },
          status: { type: 'string', enum: ['worked', 'broke', 'confusing'] },
          detail: { type: 'string' },
          severity: { type: 'string', enum: ['high', 'medium', 'low'] },
          evidence: { type: 'string' },
          suggested_test: { type: 'string' },
        },
        required: ['step', 'status', 'detail'],
      },
    },
  },
  required: ['scenario', 'steps', 'summary'],
}

const VERDICT = {
  type: 'object',
  properties: {
    real: { type: 'boolean' },
    reason: { type: 'string' },
  },
  required: ['real', 'reason'],
}

const REPORT = {
  type: 'object',
  properties: {
    markdown: { type: 'string' },
    confirmed_issues: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          title: { type: 'string' },
          severity: { type: 'string' },
          scenario: { type: 'string' },
          suggested_test: { type: 'string' },
        },
        required: ['title', 'severity'],
      },
    },
  },
  required: ['markdown', 'confirmed_issues'],
}

function explorePrompt(s) {
  return `You are a senior QA engineer testing a RUNNING demo of Transcode Forge (a self-hosted media transcoder web app) at ${BASE}. Admin password: ${JSON.stringify(PW)}.

SCENARIO ${s.id} — ${s.title}:
${s.brief}

Drive the REAL app with Playwright (Python, installed). Write a script to qa/.sweep_${s.id}.py and run it from the repo root with: uv run python qa/.sweep_${s.id}.py

Start the script with this exact preamble (a shared helper handles login + error capture):

    import sys, pathlib, json
    sys.path.insert(0, str(pathlib.Path("qa").resolve()))
    from sweep_helpers import session, error_toasts, console_errors, snap
    BASE = ${JSON.stringify(BASE)}; PW = ${JSON.stringify(PW)}
    with session(BASE, PW) as page:
        # ... your scenario steps: page.goto(BASE + "/settings"), page.click(...), page.fill(...),
        # page.wait_for_timeout(800), page.query_selector(...), etc.
        # After each meaningful action: snap(page, "S?_step"); and read error_toasts(page).
        print(json.dumps({"observations": ..., "console_errors": console_errors(page)}, default=str))

Find selectors by inspecting the page (you may add page.content()[:3000] dumps and re-run). Iterate until the script actually completes the scenario. The demo data is disposable — mutating it is fine; use the unique names in the brief.

Then JUDGE like a reviewer. For each step: did the RIGHT thing happen (not just "no crash")? Was it clear? Did an error toast or console error appear? Be honest — "worked" is a valid, common result; only use "broke" for a genuine failure (unexpected error toast, wrong/missing outcome, JS error, dead control). Use "confusing" for real UX problems. For each, give severity, evidence (toast text / console error / screenshot path), and a concrete suggested deterministic test (a tests/qa/ assertion) that would lock the behaviour.

Return findings matching the schema.`
}

function verifyPrompt(s, b) {
  return `A QA agent reported this as a problem in Transcode Forge scenario ${s.id} (${s.title}), app at ${BASE} (password ${JSON.stringify(PW)}):

  step: ${b.step}
  status: ${b.status}
  detail: ${b.detail}
  evidence: ${b.evidence || '(none)'}

Independently re-check it: write and run your own short Playwright script (same preamble as the sweep — sys.path.insert(0, str(pathlib.Path("qa").resolve())); from sweep_helpers import session, error_toasts, console_errors). Decide whether this is a REAL defect/UX problem or a test artifact (bad selector, timing, or actually-correct behaviour). Default to real=false if you cannot reproduce it. Return {real, reason}.`
}

// --- run --------------------------------------------------------------------
log(`UX sweep against ${BASE} — ${SCENARIOS.length} scenarios`)

const perScenario = await pipeline(
  SCENARIOS,
  (s) => agent(explorePrompt(s), { label: `explore:${s.id}`, phase: 'Explore', schema: FINDINGS }),
  (res, s) => {
    if (!res) return { scenario: s.id, findings: null, verified: [] }
    const flagged = (res.steps || []).filter((x) => x.status === 'broke' || (x.status === 'confusing' && x.severity === 'high'))
    if (!flagged.length) return { scenario: s.id, findings: res, verified: [] }
    return parallel(
      flagged.map((b) => () =>
        agent(verifyPrompt(s, b), { label: `verify:${s.id}`, phase: 'Verify', schema: VERDICT })
          .then((v) => ({ ...b, verdict: v }))
      )
    ).then((vs) => ({ scenario: s.id, findings: res, verified: vs.filter(Boolean) }))
  }
)

phase('Synthesize')
const confirmed = perScenario
  .filter(Boolean)
  .flatMap((r) =>
    (r.verified || [])
      .filter((v) => v.verdict && v.verdict.real)
      .map((v) => ({ scenario: r.scenario, ...v }))
  )

const report = await agent(
  `You are synthesizing a UX/QA sweep of Transcode Forge. Here are the per-scenario findings (raw) and the independently-VERIFIED issues.

VERIFIED ISSUES (real=true):
${JSON.stringify(confirmed, null, 2)}

ALL PER-SCENARIO FINDINGS (context, includes unverified + "worked"):
${JSON.stringify(perScenario, null, 2)}

Write a prioritized markdown report: group by severity, lead with the verified issues, note what worked, and for each real issue give a one-line repro + a concrete suggested tests/qa/ assertion to lock it (the "explore once, replay free" loop). Also return confirmed_issues as structured data.`,
  { schema: REPORT, phase: 'Synthesize' }
)

return report

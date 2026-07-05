export const meta = {
  name: 'ux-qa-sweep',
  description: 'AI UX sweep v2: isolated per-scenario demo instances, durable per-scenario results, bounded waves, fresh-instance verification, coverage-honest report',
  whenToUse: 'On-demand UX/QA discovery. Self-contained — provisions its own demo instances. Optional args: {waveSize, runDir}.',
  phases: [
    { title: 'Prep', detail: 'rotate run dirs + read scenarios.md (single source of truth)' },
    { title: 'Explore', detail: 'one agent + one FRESH instance per scenario, in bounded waves; findings hit disk per scenario' },
    { title: 'Verify', detail: 'each flagged finding re-checked on its own fresh instance' },
    { title: 'Synthesize', detail: 'coverage-honest report + diff vs the previous run' },
  ],
}

// --- inputs -----------------------------------------------------------------
let A = args || {}
if (typeof A === 'string') { try { A = JSON.parse(A) } catch (e) { A = {} } }
const WAVE = Math.max(1, Math.min(5, A.waveSize || 3))
const RUN_DIR = A.runDir || 'qa/runs/latest'
const PREV_DIR = 'qa/runs/previous'
const PW = 'qa-sweep-password-123'
// Port blocks: explorers 18811+i, verifiers 18841+3*i+n. Nothing else in
// this repo uses the 188xx range (tests/qa uses 18799/18801).
const EXPLORE_PORT = (i) => 18811 + i
const VERIFY_PORT = (i, n) => 18841 + i * 3 + n

// --- schemas ----------------------------------------------------------------
const SCENARIO_LIST = {
  type: 'object',
  properties: {
    rotated: { type: 'boolean' },
    scenarios: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          title: { type: 'string' },
          brief: { type: 'string' },
        },
        required: ['id', 'title', 'brief'],
      },
    },
  },
  required: ['rotated', 'scenarios'],
}

const FINDINGS = {
  type: 'object',
  properties: {
    scenario: { type: 'string' },
    summary: { type: 'string' },
    resumed_from_disk: { type: 'boolean' },
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
          repro: { type: 'array', items: { type: 'string' } },
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
          novelty: { type: 'string', enum: ['new', 'known'] },
          suggested_test: { type: 'string' },
        },
        required: ['title', 'severity', 'novelty'],
      },
    },
  },
  required: ['markdown', 'confirmed_issues'],
}

// --- prompts ----------------------------------------------------------------
function explorePrompt(s, i) {
  const port = EXPLORE_PORT(i)
  const base = `http://127.0.0.1:${port}`
  return `You are a senior QA engineer running scenario ${s.id} of the Transcode Forge UX sweep. Work from the repo root.

RESUME CHECK FIRST: if ${RUN_DIR}/${s.id}.json exists and parses as JSON, return its contents verbatim (set resumed_from_disk=true) and do NOTHING else.

SCENARIO ${s.id} — ${s.title}:
${s.brief}

You get your OWN fresh app instance — no other agent touches it, so every state change you observe is yours:
1. Launch it:  uv run python qa/launch_demo.py --start --port ${port} --run-dir ${RUN_DIR}
   (waits for READY; fresh empty-admin instance in demo-static mode, seeded data)
2. Drive the REAL app with Playwright (Python, installed). Write your script to ${RUN_DIR}/script_${s.id}.py and run it with: uv run python ${RUN_DIR}/script_${s.id}.py
   Set QA_RUN_DIR=${RUN_DIR} in the script's environment (or os.environ before importing sweep_helpers) so screenshots land in ${RUN_DIR}/shots/.
   Start from this preamble (the helper logs in AND completes first-run setup — your fresh instance has no admin yet, session() handles it):

       import os, sys, pathlib, json
       os.environ.setdefault("QA_RUN_DIR", ${JSON.stringify(RUN_DIR)})
       sys.path.insert(0, str(pathlib.Path("qa").resolve()))
       from sweep_helpers import session, error_toasts, console_errors, snap
       BASE = ${JSON.stringify(base)}; PW = ${JSON.stringify(PW)}
       with session(BASE, PW) as page:
           # scenario steps; after each meaningful action: snap(page, "${s.id}_<step>") and check error_toasts(page)
           print(json.dumps({"observations": "...", "console_errors": console_errors(page)}, default=str))

   Find selectors by inspecting the page; iterate until the script genuinely completes the scenario. Mutating this instance is fine — it is yours alone.
3. JUDGE like a reviewer. Per step: did the RIGHT thing happen (not just "no crash")? Was it clear? "worked" is a valid, common result; "broke" only for genuine failures (unexpected/persistent error toast, wrong or silently-missing outcome, JS error, dead control); "confusing" for real UX problems. For every broke/confusing step include: severity, evidence (exact toast text / console error / screenshot path), and repro — a SHORT numbered list of UI actions a verifier on a FRESH instance can follow exactly.
4. PERSIST BEFORE RETURNING (this is what makes the run survivable): write your findings JSON (matching the return schema) to ${RUN_DIR}/${s.id}.json.
5. Clean up:  uv run python qa/launch_demo.py --stop --port ${port} --run-dir ${RUN_DIR}
   (run this even if earlier steps failed)

Return the findings object.`
}

function verifyPrompt(s, b, port) {
  const base = `http://127.0.0.1:${port}`
  return `Independently verify a flagged QA finding for Transcode Forge (repo root). You get a FRESH instance — reproduce from scratch; the explorer's state is gone, which is the point.

FINDING (scenario ${s.id} — ${s.title}):
  step: ${b.step}
  status: ${b.status}
  detail: ${b.detail}
  evidence: ${b.evidence || '(none)'}
  repro: ${JSON.stringify(b.repro || [])}

1. Launch:  uv run python qa/launch_demo.py --start --port ${port} --run-dir ${RUN_DIR}
2. Write+run a short Playwright script (${RUN_DIR}/verify_${s.id}_${port}.py) using the sweep_helpers preamble (session/error_toasts/console_errors; BASE=${JSON.stringify(base)}, PW=${JSON.stringify(PW)}; session() completes first-run setup automatically). Follow the repro steps exactly.
3. Decide: REAL defect/UX problem, or artifact (bad selector, timing, actually-correct behavior)? Default real=false if you cannot reproduce it on clean state.
4. Write your verdict JSON to ${RUN_DIR}/${s.id}.verify-${port}.json, then stop the instance:  uv run python qa/launch_demo.py --stop --port ${port} --run-dir ${RUN_DIR}

Return {real, reason}.`
}

// --- run --------------------------------------------------------------------
phase('Prep')
const prep = await agent(
  `Prepare the Transcode Forge UX-sweep run (repo root, use Bash/file tools):
1. Rotate run dirs: if ${RUN_DIR} exists and contains any ${'S'}*.json or report files, delete ${PREV_DIR} (if present) and move ${RUN_DIR} to ${PREV_DIR}. Then create ${RUN_DIR} (and ${RUN_DIR}/shots). If ${RUN_DIR} is empty/missing, just create it. Also kill any stale instances: for every pidfile under ${RUN_DIR}/instances and ${PREV_DIR}/instances, run qa/launch_demo.py --stop for that port (ignore failures).
2. Read qa/scenarios.md — it is the SINGLE source of truth. Return every scenario as {id (e.g. "S1"), title (the heading text after the em dash), brief (the full body text of that scenario, verbatim)}. Set rotated=true if you moved a previous run into ${PREV_DIR}.`,
  { label: 'prep', phase: 'Prep', schema: SCENARIO_LIST }
)
if (!prep || !prep.scenarios || !prep.scenarios.length) {
  throw new Error('prep agent returned no scenarios — cannot sweep')
}
const SCENARIOS = prep.scenarios
log(`${SCENARIOS.length} scenarios from qa/scenarios.md · waves of ${WAVE} · run dir ${RUN_DIR}${prep.rotated ? ' (previous run rotated)' : ''}`)

// Explore in bounded waves: a rate-limit meltdown costs one wave, and
// per-scenario JSON on disk means a resumed run skips finished scenarios.
phase('Explore')
const perScenario = []
const failed = []
for (let w = 0; w < SCENARIOS.length; w += WAVE) {
  const wave = SCENARIOS.slice(w, w + WAVE)
  log(`wave ${1 + w / WAVE}: ${wave.map((s) => s.id).join(', ')}`)
  const results = await parallel(
    wave.map((s, j) => () =>
      agent(explorePrompt(s, w + j), { label: `explore:${s.id}`, phase: 'Explore', schema: FINDINGS })
    )
  )
  results.forEach((res, j) => {
    if (res) perScenario.push({ scenario: wave[j], findings: res })
    else failed.push(wave[j].id)
  })
}
if (failed.length) log(`explore incomplete — no result for: ${failed.join(', ')} (their ${RUN_DIR}/<id>.json may still exist from a partial run)`)

// Verify flagged findings, each on its own fresh instance. Max 3 verifier
// instances per scenario (port block size) — overflow is REPORTED, not
// silently dropped.
phase('Verify')
let flaggedTotal = 0
const unverifiedOverflow = []
const verified = await parallel(
  perScenario.flatMap((r, i) => {
    const flagged = (r.findings.steps || []).filter(
      (x) => x.status === 'broke' || (x.status === 'confusing' && x.severity === 'high')
    )
    flaggedTotal += flagged.length
    flagged.slice(3).forEach((b) => unverifiedOverflow.push({ scenario: r.scenario.id, step: b.step }))
    return flagged.slice(0, 3).map((b, n) => () =>
      agent(verifyPrompt(r.scenario, b, VERIFY_PORT(i, n)), {
        label: `verify:${r.scenario.id}`,
        phase: 'Verify',
        schema: VERDICT,
      }).then((v) => ({ scenario: r.scenario.id, finding: b, verdict: v }))
    )
  })
)
const confirmed = verified.filter(Boolean).filter((v) => v.verdict && v.verdict.real)
if (unverifiedOverflow.length) log(`verification cap hit — unverified overflow: ${JSON.stringify(unverifiedOverflow)}`)

phase('Synthesize')
const coverage = {
  scenarios_total: SCENARIOS.length,
  scenarios_completed: perScenario.length,
  scenarios_failed: failed,
  findings_flagged: flaggedTotal,
  findings_verified: verified.filter(Boolean).length,
  findings_confirmed: confirmed.length,
  unverified_overflow: unverifiedOverflow,
}
const report = await agent(
  `Synthesize the Transcode Forge UX-sweep run in ${RUN_DIR} (repo root).

COVERAGE (state this prominently and honestly in the report — unswept scenarios are unknowns, not passes):
${JSON.stringify(coverage, null, 2)}

CONFIRMED ISSUES (independently reproduced on fresh instances):
${JSON.stringify(confirmed, null, 2)}

ALL PER-SCENARIO FINDINGS:
${JSON.stringify(perScenario.map((r) => ({ id: r.scenario.id, findings: r.findings })), null, 2)}

Steps:
1. If ${PREV_DIR}/report.json exists, read it and mark each confirmed issue's novelty: "known" if the previous run confirmed substantially the same issue (same scenario + same failure), else "new". No previous report → everything is "new".
2. Write a prioritized markdown report: coverage first, then confirmed issues by severity (novelty-tagged), then notable "confusing" items that failed verification (say why), then what worked. Per confirmed issue: one-line repro + a concrete suggested tests/qa/ assertion (the codify loop).
3. Write the markdown to ${RUN_DIR}/report.md and the structured data (your full return value) to ${RUN_DIR}/report.json.
4. Return {markdown, confirmed_issues} matching the schema.`,
  { label: 'report', phase: 'Synthesize', schema: REPORT }
)

return { coverage, report }

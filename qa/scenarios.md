# UX sweep scenarios

Task scripts for the **AI exploratory sweep** (`qa/ux-sweep.workflow.js`). Each
scenario is handed to one agent, which drives the seeded demo instance through
the steps, watches what happens, and **judges** the result — not just "did it
200", but "did the right thing happen, was it clear, did anything break or go
silently missing." Persistent error toasts (`[data-toast-type="error"]`) and
console errors are always captured.

The deterministic, free-forever checks live in `tests/qa/`; this layer is the
on-demand discovery pass. When it finds something real, codify it as a new
assertion in `tests/qa/` (the "explore once, replay free" loop).

For each scenario the agent reports, per step: **worked / broke / confusing**,
any error toast or console error, a screenshot path, and a suggested
deterministic test to lock the behaviour.

Each scenario declares the page routes it drives on a `Routes:` line —
`qa/coverage.py` derives the coverage table (and the CI gap gate) from these,
so keep the line current when a scenario's journey changes.

---

### S1 — Dashboard & navigation
Routes: /, /movies, /tv, /queue, /activity, /workers, /stats, /settings

Load `/`, then visit every nav item (Movies, TV Shows, Queue, Activity,
Workers, Stats, Settings). On Activity, switch between the two facets
(Encode outcomes / Scan skips). Each should load with seeded content and no
errors. Judge: is the dashboard legible at a glance? Any dead links, empty
states that look broken, or numbers that don't add up?

### S2 — Library lifecycle (add → scan → browse → remove)
Routes: /settings, /movies

Settings → **Add Library** (name e.g. "QA Movies", type movies, a path).
Verify it appears in the library list. Trigger a **Scan**. Go to Movies and
confirm the catalog reflects it. Then **remove** the library and confirm it's
gone. Judge: is each outcome confirmed in the UI? Any stuck state?

### S3 — Queue a transcode
Routes: /movies, /queue

Movies → select one file (row checkbox) → **Queue Selected**. Go to Queue and
confirm the job appears. Try **pause** then **resume** the queue. Judge: is
selection obvious? Does the queue reflect state changes immediately?

### S4 — Settings changes persist
Routes: /settings

Change a quality preset and another settings field. Save, reload the page, and
confirm the values stuck. Judge the known UX pain points: does any field prefill
a confusing default? Are field labels clear? Should quality be a labelled
slider?

### S5 — Worker onboarding
Routes: /workers, /settings

Workers → **Add a worker** → issue a token (label e.g. "qa-node"). Confirm
the copy-paste command blocks show `TF_SERVER_URL` + `TF_WORKER_TOKEN`, then
**revoke** the token from the Worker tokens panel and confirm its status
flips. Also confirm Settings only links here rather than duplicating the
flow. Judge: is the one-command flow clear to a newcomer?

### S6 — Schedules (create → verify → delete)
Routes: /settings

Settings → Schedules → **New Window** (name, start/end hour, pick days) → Add.
Confirm it renders in the schedule list with the right day summary. Then
delete it. Judge: are the hour/day inputs clear? Does the summary read right?

### S7 — Exclusions ("don't try again")
Routes: /activity, /movies, /tv

From Activity (either facet), exclude a file via the row action, then open
that file's detail drawer (click the row from Movies/TV or Activity) and
**lift the exclusion** from there. Confirm both directions are reflected in
the UI. Judge clarity — does the drawer make the exclusion state obvious?

### S8 — Error-path probes (must show a clear, persistent error)
Routes: /settings, /workers

Deliberately do invalid things and confirm a **readable error toast** appears
(and stays — it must require a click to dismiss):
- Add a library with an empty name.
- Add a library with a path already in use (duplicate).
- Issue a worker token with an empty label.
- Create a schedule with an end hour out of range.
Judge: is each error message specific and understandable, or generic/cryptic?

### S9 — Visual & accessibility judgement
Routes: /, /movies, /tv, /queue, /activity, /workers, /stats, /settings

Across the captured screenshots, judge: text contrast (esp. muted text, inputs,
toast/popup copy), label clarity, confusing wording, and layout/overflow issues.
Flag anything a design-conscious reviewer would call out.

### S10 — S3 Object Storage library (add → scan → judge)
Routes: /settings, /activity

Settings → **Add Library** → switch Storage to **S3 Object Storage**. Confirm
the Path field is replaced by Bucket + Prefix inputs (and the hint about
TF_S3_* environment credentials). Add one (name e.g. "QA Cloud", bucket
"qa-bucket", prefix "masters/movies/"), confirm it lists with an
`s3://qa-bucket/masters/movies/` path, then trigger a **Scan** — the
scan-started toast must appear and no error toast (the demo instance has no
real bucket, so judge how the inevitable scan failure is surfaced in
Activity: is it honest and findable, or silent?). Then remove the library.
Judge: would a user who picked S3 by accident understand how to get back?

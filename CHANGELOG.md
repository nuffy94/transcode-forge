# Changelog

All notable changes to Transcode Forge are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.9.2] - 2026-07-05

### Fixed
- **S3 worker job-loop crash** — `S3Backend.cleanup()` assumed a dict but
  receives the Pydantic `Job` model; the resulting `AttributeError`
  escaped the pipeline's cleanup path on every S3 job and killed the
  worker's job loop. All S3-library deployments on 0.9.x should upgrade.
- Worker no longer stays `busy` with a dead job id after a fetch failure
  or a dedup early-complete.
- Oversized worker error messages are truncated server-side (never
  rejected), and the worker also truncates before sending.

### Added
- Token-rebind guard: a token bound to a live worker rejects a second
  machine with 409 (unique binding enforced by migration 0010); same-
  machine crash recovery and silent-worker replacement still work.
- Worker-startup SWAP recovery scan: originals hidden as `.tf_bak` by a
  power loss mid-swap are restored and stale locks cleared.
- Benchmark harness (`scripts/bench/`): throughput/economics reports and
  A/B gate tooling (`docs/BENCHMARKS.md`).
- QA sweep v2 (dialog states, 390px mobile pass, first-run /setup flow,
  structural anchors) plus mobile overflow fixes it caught on queue,
  Activity, and stats.
- Hardening test suites: worker crash recovery, failure lifecycle, S3
  error paths, swap recovery (repo now at ~690 tests).

## [0.9.1] - 2026-07-05

### Added
- **Linode Compute deploy path** (`deploy/linode/`) — scheduler and worker
  StackScripts (Caddy TLS edge with Cloudflare DNS-01 or HTTP-01, Object
  Storage media plane, Managed Database toggle, Block Storage handling,
  plan-aware concurrency auto-tuning), a CC-BY seed-media helper, and a
  full runbook with a Dedicated CPU plan table and Cloud Firewall guidance.
- **S3 library creation** — `POST /api/libraries` and the add-library
  modal now accept an S3 Object Storage backend (bucket + prefix; the
  library path is derived as `s3://bucket/prefix`). Scan, claim, and
  worker paths already supported S3 libraries; creation was the missing
  piece.
- Throwaway staging compose profile (`docker-compose.staging.yml`) for
  pre-release smoke tests (`docs/STAGING.md`).
- CI hardening: `mypy src/` gate and a real-ffmpeg pipeline integration
  test.

### Fixed
- The per-library **Scan** button now dispatches S3 libraries to the S3
  scanner; previously it always ran the filesystem scanner, so an S3
  library scan failed behind a success toast.

## [0.9.0] - 2026-07-03

### Added
- **Forge Console v2** — the web UI rebuilt as a dense ops console on a
  documented design system (`docs/design-system.md`): instrument numerals
  (tabular, slashed-zero), temperature-coded status language, heat-seam
  signature, honest zero states everywhere.
- **File-detail drawer** — click any file/job row for probe data, encode
  economics (savings, achieved VMAF vs. target, resolved CRF, encoder
  backend), the full attempt timeline, and queue/exclude actions.
  Deep-linkable via `#file=<id>`. Backed by a `jobs.source_path` index
  (migration 0007).
- **Activity** — History and Skipped merged into one ledger with two honest
  facets: encode outcomes (jobs that ran, incl. discarded-after-encode) and
  scan skips (never attempted). `/history` and `/skipped` return 301s.
- Real Tailwind v4 build pipeline (pinned standalone CLI, committed CSS,
  CI freshness gate) replacing the runtime Play CDN; fonts, HTMX, and a
  Lucide-derived inline SVG icon sprite are all vendored — the UI makes
  zero third-party requests.
- Queue status tiles double as filter shortcuts; live WebSocket progress
  on dashboard, queue, and worker cards; worker names (not UUIDs) in every
  attribution column.

### Changed
- Worker tokens live in one place — the Workers page (issue, live connect
  status, revoke). Settings keeps a pointer.
- `settings.html` split into four focused sub-templates; all inline page
  scripts replaced by ES modules under `static/js/`.
- mypy runs in CI; the E2E harness authenticates properly (the suite was
  previously red on main).

### Fixed
- Demo seed wrote outcome fields through `create_job`/`create_scan`, which
  silently drop them — the "0.0 GiB reclaimed", stuck-0% bars, dash worker
  columns, and "0000 found" scan rows all traced to this. Outcomes now land
  via the update paths, same as production.
- The workers page's token-revoke button called a function that only
  existed on Settings — it now works where the tokens live.
- Catalog stat tiles rendered an API field that never existed (always
  `0000`); Movies reads real per-status counts, TV derives its strip from
  the show rows.

## [0.8.1] - 2026-07-01

### Fixed
- Worker crash-loop on AV1 jobs: the encoder hardware axis and the storage
  backend shared a variable name in the job processor, so the pipeline
  received a storage object where the codec backend string belonged. Any
  AV1 job crashed the claiming worker, which restarted, released the job,
  and passed the bug to the next worker. Regression-tested.
- Hardening from the same incident: an unexpected exception while
  processing a job now fails that job instead of crashing the agent.

## [0.8.0] - 2026-07-01

### Added
- **AV1 output codec** (SVT-AV1 software, av1_nvenc, av1_qsv), selectable
  per job at queue time — HEVC stays the default; AV1 is opt-in with a
  compatibility warning. Workers advertise which codecs they can encode and
  only claim jobs they can handle; an AV1 job with no capable worker waits
  in the queue instead of failing.
- **VMAF quality gate**: after every encode the result is scored against the
  original (resolution-matched model, worst-scenes percentile pooling). An
  encode below the floor is discarded and the original kept — the job ends
  *skipped*, never silently degraded. New `TF_TARGET_VMAF` /
  `TF_VMAF_MIN_FLOOR` knobs; achieved VMAF shows in History.
- **Per-file CRF search** (`TF_CRF_SEARCH_ENABLED`, on by default): short
  samples are test-encoded to find the smallest file that still meets the
  VMAF target, instead of trusting one fixed CRF for every file.
- **Editable settings**: default codec and VMAF targets can now be changed
  from the Settings page (stored as DB overrides; env vars remain the
  defaults). Secrets and infra settings stay env-only.
- 10-bit output everywhere (kills banding, even from 8-bit sources), and
  per-encoder quality mapping — the same quality preset now produces
  comparable visual quality on x265, QSV, NVENC, and SVT-AV1.

### Changed
- `TF_PREFERRED_ENCODER` is renamed `TF_PREFERRED_BACKEND` (the old name
  still works as a deprecated alias for one release).
- Derivative cache keys are now goal-based (source + codec + quality goal),
  so the same rendition produced by different hardware deduplicates.
- Worker image bundles a static ffmpeg with libvmaf for measurement;
  hardware capability detection now probes with 10-bit input (Skylake-era
  QSV that can't encode 10-bit HEVC falls back to software x265).
- TV quality preset default corrected from CRF 24 to 21 ("don't degrade"
  research: 24 was too aggressive for replace-the-original encodes).

### Fixed
- Simplified PostgreSQL SSL handling — the connection URL's `sslmode` is now
  parsed natively by asyncpg instead of a redundant manual mapping. This also
  corrects `prefer`/`allow`, which were previously forced to no-TLS rather than
  best-effort TLS.

## [0.7.0] - 2026-06-04

Pluggable storage layer + multi-node data plane: libraries can now live in
S3-compatible object storage alongside the classic in-place filesystem mode,
the database can point at managed cloud Postgres, and onboarding a worker is
self-service from the UI. Workers are now HTTP-only.

### Added
- **Pluggable per-library storage backends.** Each library is now either a
  **filesystem** path (the default — in-place "shrink to save space", unchanged)
  or an **S3-compatible bucket** (Linode Object Storage reference). The S3 backend
  keeps the master object untouched and uploads a content-addressed **derivative**;
  a `derivatives` registry skips re-encoding when a matching output already exists.
  Workers pull → transcode on local scratch → upload, so no shared filesystem is
  required for multi-node / cloud setups. (migration 0005)
- **S3 library scanning** — lists objects and probes each via a presigned URL
  (with a head-bytes fallback) to build the catalog, so S3 libraries are
  browseable/queueable like filesystem ones.
- **Database local|cloud toggle** — point `TF_DB_URL` at self-hosted Postgres or
  **Linode Managed Postgres (DBaaS)** (`sslmode=require`); startup preflight
  validates the connection with clear errors.
- **Self-service "Add a worker"** flow on the Workers page — issue a token and get
  a ready-to-paste Docker or `uv` join command, with backend-aware storage guidance
  (a read-write media mount for filesystem libraries; bucket credentials for S3)
  and a live Pending → Connected indicator.
- **`docs/STORAGE.md`** — setup for the filesystem (NFS/SMB), S3/Linode,
  and DBaaS backends, plus the upgrade path.

### Changed
- **Workers are now HTTP-only.** `TF_SERVER_URL` + `TF_WORKER_TOKEN` are required,
  and a worker holds no database or Redis credentials. **Breaking:** the legacy
  DB-direct worker (selected when `TF_SERVER_URL` was unset) has been removed — a
  worker started without those two variables now exits with an error. Convert any
  DB-direct workers to HTTP mode before upgrading.

### Fixed
- Storage-layer hardening from a multi-perspective review: derivative registration
  moved to a bearer-authed scheduler endpoint (workers never touch the DB),
  asyncpg SSL-mode mapping for DBaaS, idempotent derivative inserts under races,
  non-blocking S3 scanner I/O, and a worker-singleton scratch manager with
  startup/shutdown cleanup.

## [0.6.1] - 2026-06-03

QA-hardening pass: a repeatable UX/QA testing routine, the bugs it found, and a
round of table/search polish.

### Added
- **Click-to-sort column headers** on every table (Movies, TV, History, Queue,
  Skipped) — click a header to sort, click again to reverse. Size/Date columns
  sort high-first, the rest A→Z; a caret marks the active column. The choice
  persists across pagination and the auto-refresh polls.
- **Deterministic UX/QA sweep** (`tests/qa/`) — runs axe-core + console/network
  /error-toast capture + screenshots over every page of a seeded demo instance;
  wired into CI as a `qa-sweep` job.
- **On-demand AI exploratory sweep** (`qa/ux-sweep.workflow.js`) — agents drive
  real user scenarios and judge them, with adversarial verification. See
  `docs/QA.md` for the full routine.
- Error toasts are now **persistent** (click-to-dismiss) so a transient error
  can't be missed.
- **Admin password reset CLI** — `python -m transcode_forge.admin reset-password`
  for server-side recovery of a forgotten admin login (no SQL, no re-setup);
  the same model as Nextcloud's `occ` / Django's `changepassword`.

### Fixed
- **Session expiry showed the login over the still-rendered app** — background
  HTMX polls got a `302 → /login` which the XHR followed and swapped into a
  widget; the middleware now returns `HX-Redirect` for HTMX requests. Also
  closes a minor info-leak (authed content stayed visible after expiry).
- **Pagination count and dead-end pager** on Movies/TV — the "Showing X–Y of Z"
  range is now page-aware (it read "1–50" on every page), and navigating past
  the last page snaps back instead of stranding an empty list.
- **Queue filters reset on every auto-refresh** — the status/library selects now
  survive the 5s poll (the polling container includes them), so the queue also
  correctly honors its default "Active" filter instead of showing everything.
- **Search magnifier overlapped the placeholder text** — the icon now sits
  inside the input's padding.
- Demo mode crashed at startup when Redis was absent (caught the wrong
  `ConnectionError`); `/api/health` returned 503 in demo mode (Redis is
  optional there).
- Unskipping a skipped file returned 422 (the button sent form-encoded data to
  a JSON endpoint); bulk-select checkboxes had no accessible label.

### Removed
- **Optional third-party integrations.** The app reads media off disk directly
  and never required external dependencies — integrations for library-refresh
  webhooks and notification services were optional only. Removed the code, the
  `/api/integrations` routes, the related config variables, and the Settings UI
  section, trimming surface area and config that confused setup.

## [0.6.0] - 2026-05-30

The "customer-deployable" release: a polished first-run, one-command worker
onboarding, a published image, and the security/operability work needed to
run this on an internet-exposed host.

### Added
- **One-command worker onboarding** — issue a token in the UI and copy a
  ready-to-paste worker config; the node appears under Workers within a
  heartbeat.
- **Published Docker image** to GHCR on tagged releases, with a
  `docker-compose.prod.yml` that pulls the pre-built image (no clone/build).
- **Health split** — `/api/health/live` (process up) and `/api/health/ready`
  (checks DB + Redis, returns 503 when degraded) for orchestrators; the
  compose healthcheck targets `/ready`.
- **Startup preflight** — validates library paths and ffmpeg presence on boot
  and surfaces clear errors in the UI instead of failing silently on scan.
- **Login rate-limiting** — per-IP throttle returns 429 after repeated
  failures to blunt brute-force.
- **Worker tokens hashed at rest** (HMAC-SHA256) with an optional expiry; the
  UI shows only a 6-char fingerprint. Existing tokens are backfilled in-place
  on upgrade.
- New config knobs: `TF_LOG_LEVEL` (wired into the scheduler + worker),
  `TF_SESSION_SECURE` (HTTPS-only session cookie), `TF_TOKEN_PEPPER`.
- **Documentation** — `GETTING-STARTED`, `TROUBLESHOOTING`, `BACKUP`,
  `UPGRADE`, and a README security section with a copy-paste Caddy reverse
  proxy for automatic HTTPS.

### Changed
- Single-sourced the version string (`__version__`) across the app, logs, and
  health output.
- Hardened the Compose stack — Postgres and Redis are no longer published to
  the host; they live only on the internal network.
- Batched the `/media/queue` lookups (removed an N+1) and added real
  transaction support in the DB layer so multi-statement writes are atomic.
- QSV / x265 default to the `fast` preset for better throughput.

### Fixed
- **Schedules** was completely broken — the partial used an invalid Jinja
  bitwise operator, so the template never compiled and `/partials/schedules`
  always 500'd. Rewritten with a `day_names` filter and a regression test.
- **Every form input rendered white** (light text on white) — the Tailwind
  CDN `forms` plugin overrode `.forge-input` at equal specificity; the
  form-control selectors are now element-qualified so the dark theme wins.
- Accessibility — associated labels on the schedule fields, `aria-label` on
  all filter selects and password fields, and bumped muted text to WCAG AA.
- Library create — duplicate path now returns 409 (was an unhandled 500) and
  an empty name returns 422.
- Scrubbed leaked internal IPs / hostnames from the demo seed data.
- Orphaned/stuck jobs are released when a worker re-registers after a restart.

### Security
- CSRF protection on state-changing routes (Origin / Sec-Fetch-Site checks).
- Worker tokens stored hashed, login throttled, and documented TLS-first
  exposure (never serve plain HTTP to an untrusted network).

## [0.5.0] - 2026-05-05
- HTTP-only worker API with server-issued bearer tokens (no DB/Redis
  credentials on workers), schema migrations, and single-admin authentication.

## [0.3.0] - 2026-05-04
- Worker stale-heartbeat alerts, history filtering, containerized scheduler.

## [0.2.0] - 2026-03-25
- Multi-node deploy groundwork and the Forge Console UI redesign.

## [0.1.0] - 2026-03-23
- Initial release: scanner, queue, 8-step transcode pipeline, dashboard.

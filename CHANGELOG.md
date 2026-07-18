# Changelog

All notable changes to Transcode Forge are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.12.0] - 2026-07-18

### Added
- **The queue table shows the station pipeline bar.** The five-station
  bar (#62/#63) had only ever shipped on the dashboard — the queue kept
  the classic meter, so a fleet mid-CRF-search read as rows stuck at
  "starting 0%". Both surfaces now render one shared macro; pre-phase
  workers keep the classic meter. (#84)
- **The timed stations show progress, not just presence.** Gauge streams
  a true percentage from the measurement ffmpeg (the pipeline's longest
  silent phase — a 40-minute gauge used to be an unchanging highlight)
  and Search shows its probe count ("q3/5"). Rendered as a suffix on
  the active station, live over the WebSocket, on the dashboard and the
  queue. Old workers render exactly as before (migration 0014 is
  additive; the fields ride the existing progress report). (#85)
- **Settings warns when the target VMAF sits below the perc5 safety
  floor** — that misconfiguration aims the CRF search at quality the
  gate then refuses to keep, discarding full encodes after the fact.
  (#86)

### Fixed
- **The dashboard no longer flickers.** Five polled panels carrying
  infinite pulse-dot animations (stats, scheduler-info on two pages,
  recent scans on three) still swapped with plain innerHTML, restarting
  their animations every poll — the same class of bug #39 fixed for the
  progress bars. All animated polls now morph, with a regression test
  pinning every animated poll target across all four pages. (#83)
- Recent scans (with their FAILED pills) now render on the Activity
  page's scan facet — a scan kicked off from Settings could previously
  fail invisibly. All three surfaces embed the same partial, pinned by
  a view-consistency test. (#82)
- The schedule editor's 422 toast shows the server's precise validation
  reason instead of a generic "Failed to add schedule". (#81)

### Changed
- **The image's measurement ffmpeg now carries the VMAF v1 models**
  (libvmaf 3.2): the static build is pinned to BtbN autobuild-2026-07-15
  and the image's build-time smoke verifies the v1 models on 1080p
  frames (v1's CAMBI banding feature rejects tiny probe frames). The
  quality gate still scores with the v0 models — nothing changes until
  the v1 migration's cohort recalibration lands deliberately. (#78)
- **GHCR release channels.** Every push to main publishes `:edge` (the
  integration/soak image); `:latest` moves only on `v*` release tags —
  fresh installs resolving `:latest` (compose default, StackScripts,
  README) can no longer receive an untagged build. Every PR now also
  builds the production image (no push) as a CI gate, so a broken
  Dockerfile surfaces before merge instead of at publish time. The
  unused `dev` integration branch is retired. (#79)

## [0.11.0] - 2026-07-16

### Added
- **Finished work can no longer be lost to a scheduler outage.** Workers
  write every terminal report (complete / skipped / failed) to a durable
  on-disk outbox before attempting delivery, retry with classified
  backoff, drain the outbox before registering or claiming new work, and
  keep naming the job in heartbeats until its report lands — so delayed
  delivery reads as "still mine," never abandonment. Durable state lives
  in `TF_WORKER_STATE_DIR` (Docker workers should mount it; the compose
  example does). A hostile-scheduler test tier pins the behavior against
  blips, 5xxs, timeouts, token revocation, and restarts mid-delivery.
  (#76)
- **Terminal reports are idempotent on the scheduler.** A duplicate
  report of the same outcome answers 204 and changes nothing; a
  conflicting terminal report answers 409 — first outcome wins,
  atomically, with every side effect inside one transaction. A
  reconciliation sweep requeues jobs whose live worker has stopped
  claiming them (migration 0013), and `/api/audit/integrity` gains
  `abandoned_active_jobs`. Deploy the scheduler before the workers.
  (#74)
- **NETINT Quadra VPU backend (`quadra`).** Encoder builders, hardware
  probe, and scheduler support for NETINT's HEVC/AV1 transcoding ASICs —
  probe-gated, so fleets without the hardware are unaffected. Terminal
  reports now accept any well-formed `backend_used` name instead of an
  allowlist, so a newer worker's report can never be rejected after an
  irreversible swap. (#73)

### Changed
- Worker systemd units (repo example and StackScript) run with
  `Restart=always` — a worker that dies on a transient error comes back
  on its own. (#76)
- The staging smoke accepts an optional downscale height, exercising the
  downscale path end-to-end during release gating. (#72)

## [0.10.0] - 2026-07-16

### Added
- **Resolution downscale — the third size dial** (codec → quality →
  resolution). The Movies/TV bulk bars gain a resolution selector
  (keep / 1080p / 720p; strictly below the source height — never an
  upscale or a no-op). The encode scales with `scale=-2:H` (aspect
  preserved, width always even) and VERIFY pins the output height
  exactly. The quality gate scores **at the target resolution**: the
  reference is the source downscaled with pinned lanczos inside the
  measurement graph and the VMAF model follows the target height, so the
  gate asks "did the encode add damage beyond the downscale you asked
  for?" — the absolute safety floors keep their meaning unchanged. The
  CRF search optimizes quality-at-target the same way. Downscale
  replaces the original on filesystem libraries (the UI says so
  plainly); S3 libraries keep their master untouched and derivative
  keys fork by height. (#69, #70)
- **Same-codec shrink.** Already-HEVC/AV1 files become queueable when —
  and only when — a downscale is selected: hevc→hevc / av1→av1 by
  default, an explicit codec pick wins, and av1 never converts to hevc.
  (#69)
- **Safe mixed-fleet rollout.** Workers advertise `supports_downscale`
  at registration (migration 0012) and only upgraded workers can claim
  downscale jobs; pending ones show "Needs downscale-capable worker"
  until one is online. Deploy the scheduler first, upgrade workers
  whenever — old workers keep converting h264 exactly as before. (#69,
  #70)

### Changed
- **The VMAF measurement graph is now a pinned contract.** The gauge's
  filter graph comes from one pure function guarded by byte-for-byte
  golden tests — a scoring-behavior change now fails CI instead of
  shipping silently. No-downscale scores are bit-identical to 0.9.8.
  (#70)

### Fixed
- Pre-release hardening from the adversarial review of #69/#70: downscale
  jobs fail closed when the source height can't be probed (the upscale
  guard never runs blind); a resolution pick can no longer survive its
  selector being hidden and silently ride other queue buttons (it resets
  whenever its surface hides); and the pending-downscale hint no longer
  misattributes a fully-offline fleet as a capability gap.

## [0.9.8] - 2026-07-14

### Fixed
- **The VMAF gauge pairs frames by index, not timestamp.** The quality
  gate compared encode and source with their original timestamps, letting
  libvmaf pair frame N against source frame N-1 whenever a source's muxer
  rounded its millisecond grid differently than ffmpeg's (1-2ms apart) —
  every cut and motion frame scored near zero, and genuinely good encodes
  were falsely skipped (a real 480p episode gauged mean 75.33 /
  perc5 2.67 against its true 97.25 / 95.98). Both inputs are now rebased
  onto one shared synthetic timeline so equal-index frames — always the
  same picture, since the encode path never resamples — are what gets
  compared. Scores on well-behaved sources are bit-identical to before;
  the CRF search inherits the same fix. (#66)

## [0.9.7] - 2026-07-14

### Added
- **Active transcodes now show where they are, not just a percentage.**
  Workers report the pipeline phase with every progress update (migration
  0011 adds `jobs.phase`), and the dashboard renders each active job as a
  five-station pipeline bar — Search → Encode → Verify → Gauge → Swap —
  with the sub-second protocol steps as tick marks. Only Encode shows a
  percentage, because only Encode has one: the CRF search and VMAF gauge
  phases used to masquerade as a meter stuck at 0% for up to half an hour.
  Gate-off jobs mark Search and Gauge "off" instead of pretending they
  happen. Jobs from pre-phase workers keep the classic meter row, so mixed
  fleets render coherently. (#62, #63)

### Changed
- **Type refresh for long console sessions.** The data face changes from
  IBM Plex Mono to Intel One Mono (OFL, designed for low-strain
  legibility), with a token pass to match: stamped labels grow to 11.5px
  with relaxed tracking, functional muted text is lifted for contrast, and
  the ember glow is removed from meter fills (halation on dark screens was
  part of the strain). (#60)
- The Docker image's static ffmpeg (the VMAF measurement binary) is pinned
  to a dated BtbN GPL release instead of rolling "latest" — image builds
  are reproducible and the GPL binary's corresponding source stays
  identifiable. `THIRD-PARTY-LICENSES.md` now documents the image's GPL
  source pointers. (#61)

### Fixed
- **The VMAF gauge now uses every core.** The filter graph pinned
  `n_threads=0` — libvmaf's "no threading" — so every quality measurement
  fleet-wide ran single-threaded: the pipeline's dominant cost on
  hardware-encode workers. It now defaults to the machine's CPU count.
  (#61)
- **Error toasts fired over an open modal are visible and dismissable.**
  `showModal()` makes everything outside the dialog subtree inert, so an
  invalid add/edit-library submission looked like a silent failure until
  the dialog closed. Toasts now mount in a dialog-local host while a modal
  is open and migrate back on close, so persistent errors are never lost.
  (#59)
- Stats library cards no longer render "−0 GiB" for libraries that reclaim
  nothing (S3 masters by design; sub-GiB savings that round to zero). (#61)
- Bench reports derive compression from source vs output sizes — S3 arms
  reported 0.0% saved despite 66% real compression. (#61)
- The staging smoke script seeds its compose env file up front — compose
  interpolates the whole file, profiles included, so the worker's required
  token variable used to kill the scheduler-only bring-up and teardown.
  (#58)

### Compatibility
- Migration 0011 (`jobs.phase`) is additive and applies automatically on
  scheduler boot.
- Old workers simply omit the phase field — their jobs render with the
  classic meter row. Upgrade workers to get station-level progress and the
  multithreaded VMAF gauge (the gauge fix is worker-side and is the big
  speedup of this release).

## [0.9.6] - 2026-07-12

### Fixed
- **Graceful shutdown orphaned the running ffmpeg encode.** A single SIGTERM
  ("finish current job, then exit") could tear the worker down around a
  still-running encode, leaving a detached ffmpeg writing `.tf_tmp` forever
  (observed live on two LXC workers). The worker now manages its whole
  ffmpeg process tree (`worker/proc.py`), escalates shutdown in three
  stages (drain → orderly abort → force), and never strands the job
  unreported. (#50)
- **The atomic swap could destroy a stranded backup.** With a leftover
  `.tf_bak` at a media path (a completed job whose backup delete failed),
  re-encoding that file silently renamed the new encode over the last copy
  of the true original — then cleanup deleted it. The swap now refuses to
  run while a backup exists. (#54)
- **Crash recovery only worked if the crashed worker came back.** The
  startup recovery scan can't run when nobody restarts: a mid-swap crash
  left the original hidden as `.tf_bak` indefinitely while every retry
  burned on the dead worker's lock. Workers now run single-path recovery at
  claim time — restoring hidden originals, clearing stale leftovers, and
  declining (without consuming a retry) when the path is genuinely busy or
  needs an operator. (#54)
- **Live long encodes looked abandoned to recovery.** Lock files carried
  only their creation timestamp, so any encode past the 2-hour staleness
  threshold could have its lock and in-progress output deleted by a
  restarting neighbor on shared storage. Pipelines now heartbeat their lock
  every 5 minutes for the full run (encode + VMAF + swap window). (#54)
- **Catalog rows no longer go stale after job outcomes.** Completed and
  skipped jobs now sync `media_files.transcode_status` (S3 rows can't
  self-heal on rescan — the master object never changes). (#53)
- **Scans no longer catalog pipeline sidecar files.** `movie.tf_bak.mkv` /
  `movie.tf_tmp.mkv` passed the extension check and became real — and
  queueable — catalog entries during any scan that raced a transcode. (#56)
- Aggregate SQL casts hardened against PostgreSQL `SUM(bigint)` overflow in
  the remaining stats queries; claim/registration races covered by true
  concurrency tests. (#51)
- Active-transcode progress bars no longer flicker on every poll — polled
  panels morph in place instead of being innerHTML-swapped. (#39)

### Added
- **Jobs orphaned by a dead worker requeue automatically.** The scheduler
  now requeues active jobs whose worker is dead, offline, or missing after
  10 minutes without signs of life — previously they sat in "transcoding"
  forever unless the same worker re-registered. The integrity audit
  endpoint reports the same condition. (#56)
- **Kubernetes deployment (LKE).** A Helm chart
  (`deploy/lke/transcode-forge/`) with golden-render tests and an
  operations runbook — scheduler + workers on Linode Kubernetes Engine,
  rolling updates drain in-flight encodes. (#52)
- Reproducible open-licensed benchmark corpus builder. (#38)
- CI now runs the full test suite against real PostgreSQL in addition to
  SQLite — the lane caught nine real dialect bugs on its first run. (#49)
- A four-layer QA system (deterministic browser sweep, visual baselines,
  coverage gate, findings ledger + AI exploratory workflow) documented as a
  contract in `docs/QA.md`, plus a scripted pre-release staging smoke
  (`scripts/staging_smoke.sh`). (#40–#48)

### Compatibility
- No schema changes (latest migration remains 0010).
- **Workers should be upgraded**: the shutdown-orphan fix (#50), the swap
  guard, lock heartbeat, and claim-time recovery (#54) are all worker-side.
  Old workers keep functioning against a 0.9.6 scheduler, but locks written
  by pre-0.9.6 workers are never refreshed — recovery may treat their
  long-running encodes as stale. Upgrade workers promptly after the
  scheduler.
- The scheduler's orphan-job auto-requeue changes steady-state behavior:
  jobs stuck on dead workers requeue after ~10 minutes instead of waiting
  for the worker to return. Reports from a worker whose job was requeued
  are rejected by the existing ownership checks.

## [0.9.5] - 2026-07-08

### Fixed
- **S3 library scans still dropped tail-moov files** after 0.9.4. With the
  presigned URL correctly awaited, it reached `ffprobe()` — which `Path()`-ified
  the input, mangling `https://` to `https:/`, and raised `FileNotFoundError`
  from its `exists()` check before the binary ever ran. Every presigned probe
  fell back to 64 KB head-bytes, so `.mov`/`.mp4` objects without faststart
  kept vanishing from the catalog. `ffprobe()` now passes URL inputs through
  to argv untouched. This was the third and final layer of the S3 probe
  failure found on the first live deploy; filesystem libraries were never
  affected.
- **Head-bytes fallback cataloged the wrong file size.** `_probe_s3_object`
  now overwrites `file_size` with the S3 listing size on both probe paths —
  previously the fallback recorded the 64 KB temp-file size as the media size.
- Probe failures log real ffmpeg stderr (`-v quiet` → `-v error`); an empty
  `ffprobe failed (exit 1):` message had slowed diagnosis.

### Compatibility
- Scheduler-side fixes only; no schema changes, no worker API changes.
  v0.9.x workers are unaffected. Any install using S3 libraries should
  upgrade the scheduler.

## [0.9.4] - 2026-07-06

### Fixed
- **S3 library scans dropped tail-moov files.** The scanner never awaited
  aioboto3's `generate_presigned_url` (it returns a coroutine, unlike
  sync boto3), so ffprobe received a coroutine object and every presigned
  probe silently fell back to 64 KB head-bytes. Matroska survived that;
  `.mov`/`.mp4` without faststart failed both paths and vanished from the
  catalog with no skip record. Found live on the first real S3 deploy.
- **Every S3 job failed with 'Source file not found'.** Jobs carry the
  library NAME (migration 0008), but claim-job still resolved the library
  by id — the lookup missed, the S3 backend fields were dropped, and
  workers processed S3 jobs as filesystem. Claim now resolves by name
  (id fallback for stray pre-backfill rows). Filesystem libraries were
  never affected.
- **Derivative registration silently failed-as-success.** The third
  name-as-id call site: register-derivative fed the library name into the
  `derivatives.library_id` FK, and the resulting FOREIGN KEY violation was
  swallowed by a dedup-race handler matching the bare word 'constraint' —
  204, nothing persisted, the dedup cache never populated. The library is
  now resolved before insert (409 if unresolvable), and both dedup
  handlers match UNIQUE violations only.
- `deploy/linode/seed-media.sh`: Big Buck Bunny source moved to the live
  peach h264 master (upstream turned the demo mp4 into zip-only).

### Compatibility
- Scheduler-side fixes only; no schema changes, no worker API changes.
  v0.9.x workers are unaffected. Filesystem-only installs can upgrade at
  leisure; any install using S3 libraries should upgrade the scheduler
  immediately.

## [0.9.3] - 2026-07-06

### Changed
- **VMAF gate decoupled from the CRF-search target.** The full-file gate
  is now two absolute safety floors — mean ≥ `TF_VMAF_SAFETY_MEAN` (90)
  AND worst-scenes perc5 ≥ `TF_VMAF_SAFETY_PERC5` (85) — instead of
  bars derived from `target_vmaf`. Sample-based CRF searches
  systematically overestimate full-file scores (+3 mean / +7 perc5
  measured on real content), so the old target-derived gate rejected
  good encodes wholesale (93% skip on a live batch). The search itself
  is unchanged; a job with no target VMAF still runs no search and no
  gate.
- **`TF_VMAF_MIN_FLOOR` is retired and no longer read.** If you had
  tightened it, set the new absolute floors instead — note they are
  "refuse to keep" bars, not quality goals, so port intent, not the
  number. An incoherent pair (perc5 floor above mean floor) now fails
  fast at boot, and the Settings page cross-validates (hard-rejects an
  impossible pair, warns when the target aims below the mean floor).

### Added
- Sample-vs-full-file measurement loop persisted on every terminal
  path (migration 0009, additive): the search's winning predictions
  (`predicted_vmaf_mean/perc5`) and the full-file `achieved_vmaf_perc5`
  are stored on completes AND skips; skips now also carry
  `resolved_crf`/`backend_used`. The file drawer shows predicted vs
  achieved so a skip explains itself.
- QA L3 exploratory sweep v2 (isolated instances, durable results) and
  six UX fixes it caught, locked into the L2 suite.

### Compatibility
- Additive worker API only: v0.9.x workers keep working against this
  scheduler (their jobs simply lack the new diagnostics, and their gate
  stays target-coupled until upgraded — upgrade workers promptly to get
  the fixed gate). Rollback-safe: v0.9.x binaries run against the
  migrated schema.

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

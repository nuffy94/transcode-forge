# CLAUDE.md

Guidance for Claude Code (or any AI coding assistant) working on this
repository. End-users don't need to read this — see `README.md`.

## Project overview

Transcode Forge is a self-hosted h264→HEVC/AV1 media transcoder. Scheduler +
workers, hardware-accelerated encoding on two axes — codec (HEVC, AV1) ×
backend (Intel QSV, NVIDIA NVENC, software x265/SVT-AV1 fallback) — an atomic
8-step pipeline that never loses an original file, and a VMAF quality gate so
a bad encode can never silently replace one either.

**Stack**: Python 3.12 · FastAPI · Redis (pub/sub + WebSocket relay) ·
PostgreSQL (prod) / SQLite (dev/test) · asyncpg + aiosqlite · ffmpeg/ffprobe ·
Jinja2 + HTMX + Tailwind v4 (standalone CLI build, committed CSS, all
assets vendored — zero runtime CDNs).

## Commands

```bash
uv sync --extra dev --dev                # install
uv run uvicorn transcode_forge.main:app --reload --port 8000
uv run python -m transcode_forge.worker  # worker (config via TF_* env vars)

uv run pytest                            # unit + integration (excludes tests/e2e/)
uv run pytest tests/test_pipeline.py     # single file
uv run pytest -k "test_swap"             # by name
uv run pytest --cov=transcode_forge      # with coverage
uv run pytest tests/e2e/                 # E2E (Playwright, real server)

uv run ruff check src/ tests/
uv run ruff format src/ tests/        # CI enforces --check; format before pushing
uv run mypy src/

uv run python scripts/build_css.py            # build served CSS from assets/css/forge.css
uv run python scripts/build_css.py --watch    # rebuild on change (dev CSS loop)
uv run python scripts/build_css.py --check    # fail if committed app.css is stale (CI gate)
```

CI (`.github/workflows/tests.yml`) runs `ruff check`, `ruff format --check`,
`mypy src/`, `pytest --cov`, and a `css-fresh` job (`build_css.py --check`)
on every push and PR. A formatting drift, type error, or CSS drift will
fail the build — always run `ruff format`, `mypy src/`, and rebuild the CSS
before committing.

The served `src/transcode_forge/web/static/css/app.css` is **generated** by the
pinned Tailwind v4 standalone CLI (no Node). Edit the source
`assets/css/forge.css` and rebuild — never hand-edit the built file.

## Architecture

### Two-process model

1. **Scheduler** (`main.py` → FastAPI): web UI, REST API, Prometheus metrics,
   scheduled library scans, the worker HTTP API.
2. **Worker** (`worker/http_agent.py` → `HttpWorkerAgent`): standalone process
   per machine. Connects to the scheduler via HTTP-only with a server-issued
   token. No DB or Redis credentials on the worker side.

Workers are HTTP-only: `TF_SERVER_URL` + `TF_WORKER_TOKEN` are required (the
worker exits with an error if either is missing). The old DB-direct worker was
removed — workers never hold DB or Redis credentials.

### The 8-step "never lose a file" pipeline

`worker/pipeline.py` is the safety protocol:

```
LOCK → TRANSCODE → VERIFY → COMPARE → SWAP → CONFIRM → CLEANUP → UNLOCK
```

VERIFY does an ffprobe AND a real decode of frames at three offsets — files
ffprobe accepts but that won't decode are caught here. COMPARE checks size
(larger than source → `SizeRegressionError`) and, when the job carries a
target VMAF, the quality gate: full-file VMAF (resolution-matched model,
worst-scenes perc5 pooling, `worker/vmaf.py`) must clear the **absolute
safety floors** (mean ≥ 90, perc5 ≥ 85 by default) or the encode is
discarded (`VmafGateError`) — both are skip outcomes (job ends SKIPPED,
original kept), not failures. The floors are deliberately NOT derived from
the target: the target is what the CRF search aims for on samples, the
floors are what the gate refuses to keep — samples overestimate the full
file, so a target-derived gate rejected good encodes wholesale.
TRANSCODE is optionally preceded by an ab-av1-style CRF search on short
samples; its winning sample predictions are persisted alongside the
full-file scores (predicted_* / achieved_* job columns) on completes AND
skips, keeping the sample-vs-full-file gap measurable. If post-swap verification
fails, the original is restored from `.tf_bak`. The lock file (`.tf_lock`)
prevents concurrent transcodes of the same path.

### Data flow

```
Scanner (ffprobe) → media_files (catalog)
                          ↓ user queues from UI
                       jobs (pending)
                          ↓ worker claims via /api/worker/claim-job
                  HttpWorkerAgent → pipeline.py → ffmpeg
                          ↓ on success
                  /api/worker/job/{id}/complete
```

The scanner never creates jobs — it builds a browseable catalog. Users
select files and queue them via the UI.

### Real-time updates

Workers report progress via `POST /api/worker/job/{id}/progress`. The
scheduler relays each event onto Redis pub/sub
(`tf:pub:progress`), and a WebSocket endpoint (`/ws/updates`) forwards
to the browser.

### Repository pattern

All DB access lives in `repos/` — one module per resource (jobs, workers,
media, libraries, scans, skipped, system, schedules, exclusions, users,
worker_tokens). Models in `models/` are Pydantic `BaseModel` with `StrEnum`
for statuses.

### Database abstraction

`db.py` exposes a `DBConnection` protocol with two implementations:

- `_SqliteConnection` wraps aiosqlite (dev/test).
- `_PgConnection` wraps an asyncpg pool (production).
- `_translate_placeholders()` rewrites `?` to `$1, $2, …` for asyncpg —
  every repo writes plain `?` placeholders.
- URL prefix decides: `sqlite:///path` vs `postgresql://…`.

### Schema migrations

`migrations/` holds numbered SQL files. `apply_sqlite()` /
`apply_postgres()` create the `schema_migrations` table on first boot
and apply pending migrations. Existing pre-migrations installs are
detected (the `jobs` table exists but `schema_migrations` doesn't) and
have version 1 stamped applied without re-running. **Never edit a
released migration; always add a new numbered file.**

### Auth

Single-admin model. First-run `/setup` creates the admin user; subsequent
boots route to `/login`. `AuthMiddleware` sits ahead of the routes and
short-circuits unauthenticated requests with 401 (API) or 302 to /login
(HTML). Worker-side endpoints (`/api/worker/*`) are exempt — they use
bearer-token auth via `require_worker_token` instead.

### Web UI

Jinja2 templates with HTMX for partials. "Forge Console" design — warm
graphite + hot-iron amber palette, Big Shoulders Display + IBM Plex
(design reference: docs/design-system.md). Pages: dashboard, movies, tv,
queue, activity (history + scan skips, two facets; /history and /skipped
301 there), workers, stats, settings. HTMX partials for the data-driven
sections; a file-detail drawer opens from any file/job row.

### Source layout

```
src/transcode_forge/
├── main.py              # FastAPI app factory + lifespan
├── config.py            # Settings via TF_* env vars
├── db.py, auth.py       # DB connections + auth middleware
├── api/routes/          # ~14 API routers (jobs, libraries, media, scan,
│                        # workers, audit, exclusions, schedules,
│                        # auth, worker_api, worker_tokens, …)
├── models/              # Pydantic DTOs
├── repos/               # Data access — one module per resource
├── worker/              # http_agent.py (worker agent), pipeline.py,
│                        # encoder.py, hardware.py, storage/
├── scanner/             # scanner.py, probe.py
├── migrations/          # numbered .sql files + runner
└── web/                 # routes.py, websocket.py, templates/, static/
```

## Configuration

All settings live in `config.py` (pydantic-settings, `env_prefix="TF_"`).
Common knobs:

- `TF_DB_URL` — `postgresql://…` (prod) or `sqlite:///path.db` (dev/test).
- `TF_REDIS_URL` — `redis://host:port/db`.
- `TF_AUTH_SECRET` — cookie-signing secret. Generated random per boot if
  not set; pin it in production so sessions survive restarts.
- `TF_LIBRARY_MOVIES`, `TF_LIBRARY_TV`, `TF_LIBRARY_ANIME` — library paths.
- `TF_QUALITY_*` — reference-scale CRF (lower = better quality, bigger
  file); mapped per encoder in `worker/encoder.py`.
- `TF_DEFAULT_CODEC` — pre-fills the queue-time codec selector (hevc).
- `TF_TARGET_VMAF` — the quality goal the CRF search aims for on samples
  (not a gate). `TF_VMAF_SAFETY_MEAN` / `TF_VMAF_SAFETY_PERC5` — absolute
  "refuse to keep" floors for the full-file gate (90/85 defaults), never
  derived from the target. `TF_CRF_SEARCH_ENABLED` toggles the per-file
  CRF search. These plus the quality presets are DB-overridable from the
  Settings page (`repos/settings.py`, `effective(key)` = DB override
  else env). The old `TF_VMAF_MIN_FLOOR` knob is retired and ignored.

Worker-side:
- `TF_SERVER_URL` — scheduler URL (presence selects HTTP mode).
- `TF_WORKER_TOKEN` — bearer token issued from the scheduler UI.
- `TF_WORKER_NAME`, `TF_PREFERRED_BACKEND` (old `TF_PREFERRED_ENCODER`
  is a deprecated alias), `TF_PATH_MAP` — per-worker.
- `TF_VMAF_FFMPEG` — ffmpeg binary used for VMAF measurement only (the
  Docker image bundles a static libvmaf build; distro ffmpeg lacks the
  filter). Missing libvmaf → gate skipped with a loud warning.

## Testing

- `asyncio_mode = "auto"` in pytest config — async tests run automatically.
- In-memory SQLite per test (no Postgres dependency).
- Redis is mocked via `conftest.py` fixtures. ffmpeg is not required.
- `db` fixture: temp SQLite with schema fully applied via the migrations.
- `app` fixture: full FastAPI app with mocked Redis.
- `client` fixture: `httpx.AsyncClient` over ASGI transport, **already
  authenticated** as admin. Tests verifying the auth boundary use
  `unauthed_client`.
- E2E tests are excluded by default (Playwright, real server) — run with
  `pytest tests/e2e/`. **Do not run E2E + unit together** (threaded
  uvicorn pollutes the asyncio loop).
- UX/QA sweep in `tests/qa/` (axe + error capture + screenshots vs. a seeded
  demo-static instance), also excluded by default — run with `pytest tests/qa/`.
  The full layered routine (incl. the on-demand AI exploratory sweep in `qa/`)
  is documented in [docs/QA.md](docs/QA.md).
- Coverage target: 80%+.

## Conventions

- Repos use `?` placeholders everywhere — `_translate_placeholders` in
  `db.py` handles Postgres at runtime.
- Statuses are `StrEnum`, never bare strings — `JobStatus.COMPLETE`, not
  `"complete"`. SQL string literals in repos are an exception (cheap and
  the enum values are stable).
- New schema changes go in a new numbered migration. Released migrations
  are immutable.
- Worker endpoints under `/api/worker/*` use bearer auth, not the cookie
  session — this split is enforced in `auth.PUBLIC_PREFIXES`.
- Cross-view consistency: when adding a new "view of the same data,"
  add a corresponding test in `tests/test_view_consistency.py`.
- "Don't try this again" → `repos/exclusions.py`. Queue endpoint and
  retry endpoint both consult it.

## Encoder selection

Hardware detection runs once at worker startup (`worker/hardware.py`) and
produces (codec, backend) pairs — which of libx265 / libsvtav1 / hevc_qsv /
av1_qsv / hevc_nvenc / av1_nvenc actually work. Hardware probes are real
10-bit test encodes (all pipeline output is 10-bit now), so Skylake-era
QSV that can't encode 10-bit HEVC is not advertised. Per-job backend:
`TF_PREFERRED_BACKEND` (if it supports the job's codec) > QSV > NVENC >
CPU; software is the universal per-codec fallback. Workers advertise
`supported_codecs` at registration and only claim jobs they can encode.

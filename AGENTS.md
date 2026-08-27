# AGENTS.md

Instructions for AI coding agents working in this repository. Humans:
start with [README.md](README.md). [CLAUDE.md](CLAUDE.md) goes deeper on
architecture — this file is the short version every agent should read
first. PR and branching rules live in [CONTRIBUTING.md](CONTRIBUTING.md).

## Project overview

Transcode Forge is a self-hosted media transcoder: a FastAPI scheduler
plus HTTP-only workers re-encode libraries into modern codecs (HEVC, AV1)
with hardware acceleration (Intel QSV, NVIDIA NVENC, software fallback).
An atomic 8-step pipeline guarantees no original file is ever lost, and a
VMAF quality gate keeps a bad encode from silently replacing a good one.

**Stack**: Python 3.12 · FastAPI · Redis · PostgreSQL (prod) / SQLite
(dev/test) · ffmpeg/ffprobe · Jinja2 + HTMX + Tailwind v4 (standalone CLI
build, all assets vendored — zero runtime CDNs).

## Setup and commands

```bash
uv sync --extra dev --dev                # install
uv run uvicorn transcode_forge.main:app --reload --port 8000
uv run python -m transcode_forge.worker  # worker (config via TF_* env vars)

uv run pytest                            # unit + integration
uv run pytest tests/test_pipeline.py     # single file
uv run pytest -k "test_swap"             # by name
uv run pytest --cov=transcode_forge      # with coverage

uv run ruff check src/ tests/
uv run ruff format src/ tests/           # CI enforces --check
uv run mypy src/

uv run python scripts/build_css.py            # rebuild served CSS
uv run python scripts/build_css.py --check    # fail if committed CSS is stale
```

## CI gates — what fails your PR

`.github/workflows/tests.yml` runs on every push and PR:

- `ruff check` and `ruff format --check` — format before pushing.
- `mypy src/` — type errors fail the build.
- `pytest --cov` — the full suite, plus a separate run against real
  PostgreSQL (`test-postgres`). SQLite passing is not enough; watch for
  dialect differences.
- `css-fresh` — `build_css.py --check`. The served
  `src/transcode_forge/web/static/css/app.css` is **generated**; edit the
  source `assets/css/forge.css` and rebuild. Never hand-edit the built file.
- `image-build` — the Docker image must build.

## Architecture essentials

- **Two processes.** Scheduler (`main.py` → FastAPI): web UI, REST API,
  scans, worker HTTP API. Worker (`worker/http_agent.py`): one per
  machine, connects HTTP-only with a server-issued bearer token
  (`TF_SERVER_URL` + `TF_WORKER_TOKEN`). Workers never hold DB or Redis
  credentials.
- **The 8-step pipeline** (`worker/pipeline.py`) is the safety protocol:
  LOCK → TRANSCODE → VERIFY → COMPARE → SWAP → CONFIRM → CLEANUP →
  UNLOCK. VERIFY decodes real frames, not just ffprobe. COMPARE enforces
  size and the absolute VMAF safety floors (a miss is a skip, not a
  failure — the original is kept). Post-swap failures restore from
  `.tf_bak`. Treat this file with care; it is why the project can promise
  "never lose a file".
- **Repository pattern.** All DB access lives in `repos/`, one module per
  resource. Models in `models/` are Pydantic with `StrEnum` statuses.
- **DB abstraction** (`db.py`): repos write plain `?` placeholders;
  `_translate_placeholders()` rewrites them for asyncpg. Code must work
  on both SQLite and PostgreSQL.
- **Auth split**: browser routes use the cookie session; `/api/worker/*`
  uses bearer tokens (`auth.PUBLIC_PREFIXES` enforces the split).
- The **scanner never creates jobs** — it builds a catalog; users queue
  from the UI.

## Hard rules

- Never edit a released migration. Schema changes go in a **new numbered
  file** in `migrations/`.
- Never hand-edit `web/static/css/app.css` — it's generated.
- Statuses are `StrEnum` members, never bare strings.
- New "view of the same data" → add a test in
  `tests/test_view_consistency.py`.
- All settings go through `config.py` (pydantic-settings, `TF_` prefix) —
  no hardcoded config.

## Testing notes

- `asyncio_mode = "auto"` — async tests just work, no decorator needed.
- Tests run on in-memory SQLite with migrations fully applied; Redis is
  mocked; **ffmpeg is not required locally** — the one real-encode test
  (`tests/test_pipeline_integration.py`) self-skips missing encoders. CI
  installs ffmpeg so it actually runs there.
- The `client` fixture is already authenticated as admin; use
  `unauthed_client` to test the auth boundary.
- The QA sweep in `tests/qa/` is excluded by default — run explicitly
  with `pytest tests/qa/`. See [docs/QA.md](docs/QA.md).
- Coverage target: 80%+.

## Commits and PRs

Conventional Commits (`feat:`, `fix:`, `docs:`, …), atomic commits,
feature branches into `main` via PR with green CI. Full flow in
[CONTRIBUTING.md](CONTRIBUTING.md).

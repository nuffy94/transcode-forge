# Third-Party Licenses

Transcode Forge is original work licensed under the MIT License (see `LICENSE`).
It builds on the following third-party components. None are bundled in the
**source** repository except where noted; runtime tools are installed or pulled
by the user.

## Python dependencies

All runtime and development dependencies are under permissive licenses
(MIT / BSD / Apache-2.0 / ISC) — see `pyproject.toml` and `uv.lock` for the
full resolved set. Highlights:

| Package | License |
|---------|---------|
| FastAPI, Pydantic, pydantic-settings, redis, aiosqlite, itsdangerous | MIT |
| Uvicorn, Starlette, Jinja2, httpx | BSD-3-Clause |
| asyncpg, bcrypt | Apache-2.0 |
| prometheus-fastapi-instrumentator | ISC |
| prometheus-client | Apache-2.0 / BSD-2-Clause |
| pytest, pytest-cov, ruff, mypy, pillow *(dev)* | MIT |
| pytest-asyncio, playwright, pytest-playwright *(dev)* | Apache-2.0 |

No copyleft (GPL/AGPL/LGPL) or proprietary Python packages are used.

## Front-end assets (loaded from CDNs at runtime, not bundled)

| Asset | License |
|-------|---------|
| Tailwind CSS | MIT |
| HTMX + `htmx-ext-ws` | BSD-2-Clause |
| Chart.js | MIT |
| Big Shoulders Display, IBM Plex Sans, IBM Plex Mono (Google Fonts) | SIL Open Font License 1.1 |
| Material Symbols (Google Fonts icons) | Apache-2.0 |

## Vendored file

| File | License | Notes |
|------|---------|-------|
| `tests/qa/vendor/axe.min.js` (axe-core) | MPL-2.0 | Accessibility testing only; not shipped in the running app. |

## Runtime tools (referenced, not redistributed in this repo)

These are installed by the `Dockerfile` / referenced by `docker-compose*.yml`.
Publishing **this source repository** (install instructions) carries no
redistribution obligation. Building or pulling a **pre-built image** that
*bundles* them is the user's responsibility, which is why no public pre-built
image is distributed from this repo:

| Tool | License | Note |
|------|---------|------|
| ffmpeg | LGPL-2.1+ (may include GPL components per build) | Installed via `apt`; called as an external tool. |
| `intel-media-va-driver-non-free`, `libmfx1` | Proprietary (Intel; Debian non-free) | Optional QSV hardware acceleration. |
| Redis 7 | RSALv2 + Commons Clause | Pulled as the official `redis:7-alpine` image. |
| Python 3.12 base image | PSF License | `python:3.12-slim-bookworm`. |
| uv | MIT | Pulled from `ghcr.io/astral-sh/uv`. |


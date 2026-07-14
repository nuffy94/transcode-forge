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

## Front-end assets (vendored — bundled in this repository; the UI loads
nothing from third-party hosts at runtime)

| Asset | License | Where |
|-------|---------|-------|
| Big Shoulders Display, IBM Plex Sans (latin woff2 subsets), Intel One Mono (woff2, © Intel) | SIL Open Font License 1.1 — text shipped alongside (`static/vendor/fonts/OFL.txt`) | `src/transcode_forge/web/static/vendor/fonts/` |
| HTMX 2.0.4 + `htmx-ext-ws` | BSD-2-Clause | `src/transcode_forge/web/static/vendor/` |
| Lucide icon path data (v1.23.0, curated subset) | ISC — attribution comment in the file | inlined in `src/transcode_forge/web/templates/partials/_icons.html` |
| Tailwind CSS v4 | MIT | build-time tool only (pinned standalone CLI, cached locally, not committed); its compiled output is `static/css/app.css` |

## Vendored file (dev/test only)

| File | License | Notes |
|------|---------|-------|
| `tests/qa/vendor/axe.min.js` (axe-core) | MPL-2.0 | Accessibility testing only; not shipped in the running app. |

## Runtime tools (bundled in the published Docker image)

These are installed by the `Dockerfile`. A pre-built image **is** published
publicly (`ghcr.io/nuffy94/transcode-forge`, on every release tag), so the
image redistributes these binaries. Transcode Forge invokes ffmpeg as an
external program (no linking), so its own MIT license is unaffected; the
GPL components' obligation is source availability, satisfied below:

| Tool | License | Corresponding source |
|------|---------|------|
| ffmpeg (Debian package) | GPL-2+ build (links GPL x264/x265) | Debian source packages for the pinned base image: <https://snapshot.debian.org/> (`apt source ffmpeg` inside the image resolves the exact version). |
| ffmpeg static build (VMAF measurement) | GPL-3.0 | Pinned BtbN release — sources published with the same release tag: <https://github.com/BtbN/FFmpeg-Builds/releases> (tag in the `VMAF_FFMPEG_URL` Dockerfile ARG). |
| `intel-media-va-driver-non-free`, `libmfx1` | Proprietary (Intel; Debian non-free) | Optional QSV hardware acceleration; redistributable per Intel's Debian packaging terms. |
| Redis 7 | RSALv2 + Commons Clause | Separate official `redis:7-alpine` image — referenced by compose, not bundled in ours. |
| PostgreSQL 16 | PostgreSQL License | Separate official image — referenced by compose, not bundled in ours. |
| Python 3.12 base image | PSF License | `python:3.12-slim-bookworm`. |
| uv | MIT | Build stage only (`ghcr.io/astral-sh/uv`). |


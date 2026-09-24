# Transcode Forge

[![CI](https://github.com/nuffy94/transcode-forge/actions/workflows/tests.yml/badge.svg)](https://github.com/nuffy94/transcode-forge/actions/workflows/tests.yml)
![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

Transcode Forge re-encodes your media library into HEVC or AV1 to save disk
space, and it never loses an original. You run one scheduler and as many
workers as you have machines. Each encode goes through an 8-step pipeline
that keeps the original until the new file has been checked, so a worker
dying mid-encode costs you time, not files.

- **Checked before it replaces anything.** Every output is probed, decoded at
  three points and matched stream for stream against the source. When the
  worker can measure VMAF (the Docker image can), it also has to clear a
  quality floor. If any check fails, the original stays.
- **Spread across your machines.** Each worker uses the best encoder it has:
  Intel QSV, then NVIDIA NVENC, then software (x265 for HEVC, SVT-AV1 for
  AV1). NETINT Quadra cards work too, when you ask for them.
- **Workers hold no database access.** A worker talks to the scheduler over
  HTTP with a token you can revoke. It never sees the database or Redis
  credentials.
- **Stuck jobs recover on their own.** If a worker dies or loses track of a
  job, the scheduler hands the job back to the queue.

> **Status**: actively developed and in production use.

## The console

The dashboard shows live encodes by worker, your total savings, and the VMAF
score of every finished encode. The fonts, icons and scripts are all bundled,
so the UI loads nothing from outside hosts.

![Dashboard: live transcodes, total savings, scan history](docs/img/dashboard.png)

Click any file for its full history: probe data, what the encode saved, the
VMAF it reached against its target, the CRF and encoder used, and every
attempt.

![File detail drawer: 47% saved, VMAF 96.4 against a 95 target](docs/img/file-drawer.png)

The Activity page keeps two lists. Encode outcomes are the jobs that ran,
including the ones thrown out for coming out bigger or missing the VMAF
floor. Scan skips are the files never attempted, for example because they are
already HEVC.

![Activity: encode outcomes with error traces and retry actions](docs/img/activity.png)

## Install

You need Docker (or Podman) with Compose. It is developed and run on Linux.
Docker Desktop on macOS or Windows should work too, but nobody tests it
regularly.

```bash
git clone https://github.com/nuffy94/transcode-forge.git
cd transcode-forge

# Writes random secrets to .env. Safe to run again.
./bootstrap.sh        # or: powershell -File bootstrap.ps1

# Point TF_LIBRARY_MOVIES and TF_LIBRARY_TV in .env at your library.
$EDITOR .env

docker compose up -d
```

`bootstrap.sh` puts your admin password in `.env` next to the other secrets.
Read it with `grep TF_ADMIN_PASSWORD .env`, open http://localhost:8000 and log
in with it. There is one admin and no username.

The scheduler scans your library and runs the queue, but it does not encode
anything itself. Nothing transcodes until you add at least one worker (see
[Adding workers](#adding-workers)). To run one on the same machine, uncomment
the `worker` service in `docker-compose.yml` and put its token in `.env` as
`TF_WORKER_TOKEN`. That service is set up for Intel QSV; on a host without
`/dev/dri`, drop its `devices` lines and it encodes in software.

### Pre-built image

Releases publish a public image to GHCR, so you can skip the build. No
registry login is needed.

```bash
./bootstrap.sh
docker compose -f docker-compose.prod.yml up -d   # pulls ghcr.io/nuffy94/transcode-forge
```

`:latest` is the newest release. To pin one, set `TF_VERSION=0.16.0` in
`.env`. Every push to `main` also publishes `:edge`, if you want to run ahead
of releases.

### Linode and Kubernetes

Two StackScripts deploy the whole stack on Linode: a Caddy TLS edge, media in
Object Storage, an optional Managed Database, and CPU workers that join with a
token. See [deploy/linode/README.md](deploy/linode/README.md). For Kubernetes,
there is a Helm chart in [deploy/lke/](deploy/lke/README.md).

## Adding workers

A worker is the process that actually encodes. Run one on every machine you
want to use: a box with an NVIDIA GPU, one with Intel QSV, or any spare
machine with ffmpeg.

The quickest way is the **Workers** page, under **Add a worker**. It issues a
token and gives you a Docker or `uv` command to paste, with notes for your
library type (a read-write media mount for filesystem libraries, bucket
credentials for S3). To set one up by hand instead:

1. On the **Workers** page, issue a token and name it, for example
   "gpu-node". Copy it now: it is shown once.
2. On the worker machine, either install [uv](https://docs.astral.sh/uv/)
   and an ffmpeg with the hardware encoder you want, or run the published
   image (it bundles ffmpeg): `ghcr.io/nuffy94/transcode-forge:latest` with
   the command `python -m transcode_forge.worker`.
3. Set these environment variables:

```bash
TF_SERVER_URL=http://<scheduler-host>:8000
TF_WORKER_TOKEN=<paste-the-token>
TF_WORKER_NAME=gpu-node            # defaults to worker-<hostname>
TF_PREFERRED_BACKEND=auto          # or qsv, nvenc, quadra, cpu
TF_PATH_MAP='{"/media/movies":"/mnt/media/movies"}'
```

`TF_PATH_MAP` turns the path the scheduler knows into the path this machine
can read. Leave it empty if both machines mount the library at the same path.
The old name `TF_PREFERRED_ENCODER` still works for now, but it is deprecated.

4. Start the worker:

```bash
uv run python -m transcode_forge.worker
```

It registers with the scheduler and starts taking jobs. It shows up on the
Workers page within one heartbeat (10 seconds).

To retire a worker, or if its token leaks, revoke the token under **Worker
tokens** on the Workers page. The scheduler refuses that token from then on,
so the worker gets no more jobs. The process itself keeps running and
retrying, so stop it on that machine too.

## Configuration

Settings come from `TF_*` environment variables. Libraries, schedules,
exclusions and worker tokens live in the admin UI, and the Settings page can
change some of the variables below without a restart.

| Setting | What it does |
|---|---|
| `TF_LIBRARY_MOVIES`, `TF_LIBRARY_TV`, `TF_LIBRARY_ANIME` | The library paths the scheduler scans |
| `TF_TARGET_VMAF` | The quality to aim for (default 98). When the CRF search is on (the default) and the worker's ffmpeg has libvmaf, the worker encodes short samples first to find the CRF that reaches it |
| `TF_QUALITY_MOVIES`, `TF_QUALITY_TV`, `TF_QUALITY_ANIME` | The CRF to use when that search is off or cannot find one (defaults 21, 21, 19; lower means bigger and better) |
| `TF_PORT`, `TF_BIND` | Where Compose publishes the web UI (defaults 8000 and 0.0.0.0) |
| `TF_LOG_LEVEL` | `debug`, `info`, `warning`, `error` or `critical` (default `info`) |
| `TF_SESSION_SECURE` | Set `true` behind HTTPS so the session cookie is only sent over HTTPS |
| `TF_AUTH_SECRET` | Signs sessions and protects stored worker tokens. `bootstrap.sh` sets it; if it is ever unset, the app makes a new one each start, which logs you out and breaks every issued worker token |

**Guides:** [Getting Started](docs/GETTING-STARTED.md) ·
[Troubleshooting](docs/TROUBLESHOOTING.md) · [Backup](docs/BACKUP.md) ·
[Upgrade](docs/UPGRADE.md) · [Staging](docs/STAGING.md) ·
[Changelog](CHANGELOG.md). The full list of settings and how the app is built
is in [CLAUDE.md](./CLAUDE.md).

## Security

It is built for one admin on a network you control.

- **Exposure.** The web UI listens on all interfaces by default, so your LAN
  can reach it. Postgres and Redis are never published to the host. They
  live only on the internal Docker network.
- **The internet.** Do not expose the UI directly. Set `TF_BIND=127.0.0.1`,
  put a TLS reverse proxy (Caddy, nginx) or a Cloudflare Tunnel in front, and
  set `TF_SESSION_SECURE=true`.
- **Secrets.** `bootstrap.sh` generates `TF_ADMIN_PASSWORD`, `TF_PG_PASSWORD`
  and `TF_AUTH_SECRET`, and `.env` is git-ignored. Requests that change
  anything are refused when they come from another site (the app checks the
  browser's `Origin` and `Sec-Fetch-Site` headers).
- **Workers** hold only the scheduler URL and a token you can revoke, never
  database or Redis credentials. Tokens are stored as HMAC-SHA256 hashes,
  not as the token itself.

**Automatic HTTPS with Caddy.** Put this `Caddyfile` in front of the app and
Caddy gets and renews the certificate for you:

```caddyfile
forge.example.com {
    reverse_proxy localhost:8000
}
```

Then set `TF_BIND=127.0.0.1` in `.env` so only the proxy can reach the app,
and `TF_SESSION_SECURE=true`. A Cloudflare Tunnel gives you the same TLS
without opening a port. **Never expose port 8000 over plain HTTP.** Your admin
password and worker tokens would cross the network unencrypted.

## What it does and doesn't do

**Does:**
- Scans your library and lists the files worth re-encoding. You pick what to
  queue.
- Spreads the work over one or more workers (QSV, NVENC, Quadra or software).
- Checks every output before it replaces the original.
- Hands stuck jobs back to the queue when a worker dies mid-encode.
- Shows live progress, history and savings.
- Runs on a schedule, for example only between 11pm and 7am on weekdays.
- Remembers "never try this file again" with one click.

**Doesn't, yet:**
- A plugin or flow editor. That is on the roadmap for v1.0.
- More than one output per file, like an H.264 copy next to the HEVC one.
- An arm64 image. It is amd64 only for now, because the Intel QSV packages
  in the image are x86-only.

## Hardware encoders

| Encoder | Codecs | What you need |
|---|---|---|
| Intel QSV | HEVC, AV1 | Linux, `intel-media-va-driver-non-free` plus `libmfx1` (gen 8 to 10) or `libmfx-gen` (gen 11 and up), and `/dev/dri` passed into the container. The GPU has to encode 10-bit, so Skylake does not qualify |
| NVIDIA NVENC | HEVC, AV1 | Linux, driver 470 or newer, and `nvidia-container-toolkit` in Docker. Windows should work but is untested |
| NETINT Quadra | HEVC, AV1 | A Quadra card and its ffmpeg build |
| Software (x265, SVT-AV1) | HEVC, AV1 | Nothing extra. Slow, but it always works. It is the only path on macOS, which is untested |

AV1 needs a GPU that can encode it, for example Intel Arc or an NVIDIA RTX 40
card. At startup each worker test-encodes every codec and backend pair and
only offers the ones that work. It then uses `TF_PREFERRED_BACKEND` if that
supports the job's codec, otherwise QSV, then NVENC, then software. Quadra is
never picked on its own: set `TF_PREFERRED_BACKEND=quadra` on the worker that
has the card. Linux is the tested platform; Windows and macOS are best effort.

## Pipeline safety

Every encode goes through eight steps:

```
LOCK → TRANSCODE → VERIFY → COMPARE → SWAP → CONFIRM → CLEANUP → UNLOCK
```

- **LOCK** puts a `.tf_lock` file next to the original, so two workers never
  encode the same file.
- **VERIFY** probes the output and decodes frames at three points. Any
  decoder or demuxer complaint fails it, even when ffmpeg exits 0.
- **COMPARE** checks that every stream in the source landed where the
  encoder planned it and that the output is smaller than the source. When
  the worker can measure VMAF, the full file also has to clear the safety
  floors (mean 91.5 and worst scenes 86 by default). Failing any of these
  ends the job as skipped, with the original kept.
- **SWAP** and **CONFIRM** move the new file into place and check it again.
  If that check fails, the original comes back from its `.tf_bak` copy.

If a step fails, the original is kept. The scheduler also sweeps for stuck
jobs every 30 seconds. A job whose worker died goes back to the queue after
10 minutes, and a job whose worker is alive but working on something else
goes back after 2 minutes. For a manual check, `GET /api/audit/integrity`
lists anything that looks stuck.

## Development

```bash
uv sync --extra dev --dev
uv run pytest                       # about 1,100 tests, a few minutes
uv run pytest tests/test_pipeline.py
uv run ruff check src/ tests/
uv run ruff format src/ tests/
uv run mypy src/
```

The suite covers the repositories, the HTTP handlers, the worker API, the
pipeline, schema migrations, and tests that keep every view of the same data
in agreement. Filter with `pytest -k <name>`. CI also runs the suite against
real Postgres, a UI sweep, and a production image build; see
[CONTRIBUTING.md](CONTRIBUTING.md) and [CLAUDE.md](CLAUDE.md).

## License

MIT. See [LICENSE](./LICENSE).

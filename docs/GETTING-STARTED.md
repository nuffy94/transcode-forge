# Getting Started — Transcode Forge

Welcome to Transcode Forge, a self-hosted transcoder that shrinks your media library into modern codecs (HEVC and AV1 today). This guide walks you from zero to your first transcode in about 5 minutes, then adds a second worker node on a different machine.

**System requirements:** Linux or macOS with Docker and Compose.

## 1. Install

Clone the repository and run the bootstrap script:

```bash
git clone https://github.com/nuffy94/transcode-forge.git
cd transcode-forge
./bootstrap.sh        # Generates .env with random secrets
```

Edit `.env` to point at your media libraries:

```bash
# Edit these to match your actual library paths
TF_LIBRARY_MOVIES=/path/to/your/movies
TF_LIBRARY_TV=/path/to/your/tv
```

Then bring up the containers:

```bash
docker compose up -d
```

Done — the scheduler, database, and Redis are now running. Open http://localhost:8000 in your browser.

### Pre-built image (skip building from source)

If you have a GitHub PAT with `read:packages` scope:

```bash
echo <your-PAT> | docker login ghcr.io -u <github-user> --password-stdin
docker compose -f docker-compose.prod.yml up -d
```

This pulls the pre-built image instead of building locally.

## 2. First-run setup

The first time you open http://localhost:8000, you'll land on a setup screen. Pick a strong admin password — this is the only login on the system. Click through and you're in.

## 3. Add a library and scan

1. Go to **Settings** (bottom left of the dashboard).
2. Under **Libraries** section, click "Add Library".
3. Fill in:
   - **Name:** e.g. "My Movies"
   - **Media Type:** `movies` / `tv` / `anime`
   - **Path:** The path inside the container where your media is mounted (from `TF_LIBRARY_MOVIES` or `TF_LIBRARY_TV` in `.env`). Inside Docker it's `/media/movies` or `/media/tv`.
   - **Quality Preset:** HEVC CRF (20–22 is typical; lower = better quality, bigger file).
   - **Auto Scan:** Enable if you want the scheduler to rescan periodically.

4. Click "Add Library". The library is now in the catalog.
5. To populate it with files, go to **Settings → Libraries**, find your library, and click "Scan". The dashboard will show scan progress on the **Dashboard** tab. Once done, browse **Movies** or **TV** pages to see what was found.

## 4. Queue your first transcode

1. Browse **Movies** or **TV** (depending on your library type).
2. Click on a file to select it (or use the select-all checkbox).
3. Click "Queue Selected" at the bottom of the page.
4. Go to **Dashboard** to watch the scheduler process it. When a worker (or the scheduler itself acting as a worker) claims it, you'll see live progress: frames encoded, ETA, output size.
5. When it finishes, the original is swapped for the HEVC version. During the swap the original is held as a `.tf_bak` and restored automatically if the new file fails verification; on success the `.tf_bak` is removed in the cleanup step.

## 5. Add a second worker

The scheduler can transcode on its own, but the killer feature is spreading work across machines. Here's how to add a worker on a different box (e.g. a desktop, a small server, any Linux machine with ffmpeg and an encoder).

### On the scheduler machine (Settings UI)

1. Go to **Settings → Workers** section → "Issue Token".
2. Enter a label like `gpu-node` or `node-2`.
3. Click "Issue".
4. A one-time token appears. Copy it (the UI shows a copy button). You won't see it again, so write it down if needed.
5. Below the token, there's a "How to use it on a remote machine" section. Copy that env-var block.

### On the worker machine

You'll need [uv](https://docs.astral.sh/uv/) (Python package manager) and ffmpeg with hardware encoder support (Intel QSV, NVIDIA NVENC, or fallback to CPU-based x265).

Install uv and ffmpeg:

```bash
# Ubuntu/Debian
sudo apt-get install ffmpeg

# macOS
brew install ffmpeg uv
```

For GPU acceleration, also install the right driver:

```bash
# NVIDIA (Linux)
# Driver 470+; nvidia-container-toolkit if running in Docker

# Intel QSV (Linux)
sudo apt-get install intel-media-va-driver-non-free libmfx1

# macOS
# Built into ffmpeg from Homebrew
```

Set environment variables from the token block the UI gave you:

```bash
export TF_SERVER_URL=http://<scheduler-machine>:8000
export TF_WORKER_TOKEN=<paste-token-here>
export TF_WORKER_NAME=gpu-node
export TF_PREFERRED_ENCODER=auto         # auto / qsv / nvenc / cpu
```

If the worker machine mounts the library at a different path than the scheduler, set `TF_PATH_MAP` to translate:

```bash
# Maps the path the scheduler stores -> the path the worker can read.
# Example: scheduler stores /mnt/media/movies, worker mounts it at /media/movies
export TF_PATH_MAP='{"/mnt/media/movies":"/media/movies"}'
```

If both machines mount identically, leave `TF_PATH_MAP` unset.

Start the worker:

```bash
uv run python -m transcode_forge.worker
```

It connects to the scheduler, registers itself, and starts pulling jobs. Check **Settings → Workers** on the scheduler — your new worker should appear within ~10 seconds.

### Removing a worker

Go to **Settings → Workers** on the scheduler and click "Revoke" next to the token. The worker exits cleanly on its next API call.

## 6. Next steps

- **Scheduling:** Set up transcoding windows (e.g. "only run between 11pm–7am") in **Settings → Schedules**.
- **Exclusions:** Queue up a file, see it fail, then click "Don't try this again" to skip it forever.
- **Hardware tuning:** Force a specific encoder with `TF_PREFERRED_ENCODER=qsv` (etc.) or read [README.md → Hardware encoder support](../README.md#hardware-encoder-support) for driver setup.

For troubleshooting and detailed configuration, see [README.md](../README.md) and [TROUBLESHOOTING.md](./TROUBLESHOOTING.md).

## Security note

The web UI binds to `0.0.0.0` by default (LAN-reachable on the local network). If you're on an untrusted network or the internet:
- Set `TF_BIND=127.0.0.1` in `.env` and front the app with a TLS reverse proxy (Caddy, nginx, or Cloudflare Tunnel).
- Set `TF_SESSION_SECURE=true` so the admin cookie is HTTPS-only.
- See [README.md → Security](../README.md#security) for a Caddy example.

Workers never need DB or Redis credentials — they use a revocable HTTP bearer token only.

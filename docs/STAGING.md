# Staging — pre-release smoke test

A throwaway stack for putting **one real file through the whole pipeline**
before you tag a release. It's the manual half of the release gate: CI
already runs a synthetic real-ffmpeg encode on every push
(`tests/test_pipeline_integration.py`), and this is the with-real-media
version you run by hand before cutting a version.

It's deliberately minimal and isolated from your dev/prod stacks:

- **SQLite**, not Postgres (single scheduler, workers never touch the DB).
- A **scratch library** (`./staging-media`), not your real drives.
- **One CPU (software) worker** — no GPU passthrough, so it uses libx265 /
  libsvtav1. The VMAF gate still runs (the VMAF ffmpeg is baked into the image).
- Its own Compose project name (`transcode-forge-staging`), so its
  containers and volumes never collide with anything else you're running.

**Requirements:** Linux or macOS with Docker and Compose. Everything below
uses `docker-compose.staging.yml`.

## 1. Bring up the scheduler

```bash
docker compose -f docker-compose.staging.yml up -d --build
```

This starts Redis and the scheduler (the worker is held back behind a
profile — you can't issue it a token until the scheduler exists). Open
**http://localhost:8001** and set the admin password.

> Port 8001 by default so it never clashes with a dev instance on 8000.
> Override with `TF_STAGING_PORT`. It binds `127.0.0.1` by default; set
> `TF_STAGING_BIND=0.0.0.0` to reach it from another machine.

## 2. Issue a worker token and start the worker

In the UI: **Settings → Workers → issue a token**. Drop it into an env file:

```bash
echo "TF_WORKER_TOKEN=<paste-the-token>" > .env.staging
```

Then bring the worker up with the `worker` profile:

```bash
docker compose -f docker-compose.staging.yml --profile worker \
    --env-file .env.staging up -d --build
```

Back in **Workers**, the `staging-cpu` worker should register within a few
seconds, advertising the `cpu` backend for hevc + av1.

## 3. Run the smoke test

Drop a real test file into the scratch library and let the pipeline chew on it:

```bash
mkdir -p staging-media/movies
cp "/path/to/some-real-clip.mkv" staging-media/movies/
```

Then in the UI:

1. **Movies → Scan** (or wait for the scheduled scan) — the file appears in
   the catalog.
2. Select it and **queue** it (pick hevc or av1).
3. Watch **Queue** — the job goes claimed → transcoding → complete, with live
   progress. On success the original is replaced in place and space-saved is
   reported. If the encode can't beat the source size or clears the VMAF
   floor, the job ends **SKIPPED** and the original is kept — that's the gate
   working, not a failure.

That end-to-end pass — real file, real ffmpeg, real swap — is the release
gate. If it's green, tag the release.

## 4. Tear down

```bash
docker compose -f docker-compose.staging.yml --profile worker down -v
```

`-v` drops the SQLite volume too, so the next run starts clean. The scratch
`./staging-media` directory is yours to delete whenever.

## Notes

- **Why pin `TF_AUTH_SECRET`?** The compose file pins a throwaway staging
  secret. Left unpinned it's random per boot, which changes the worker-token
  pepper and silently revokes every worker token on restart. The pinned value
  is staging-only — never reuse it in production.
- **Testing an already-built image** instead of source: swap `build: .` for
  `image: ghcr.io/nuffy94/transcode-forge:${TF_VERSION:-latest}` in both
  services. Building from source is the default because staging exists to test
  the code you're *about* to ship, not a published image.

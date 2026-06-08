# Transcode Forge Troubleshooting

## Where are the logs?

View scheduler logs:
```bash
docker compose logs scheduler
docker compose logs scheduler -f    # Follow in real-time
```

View worker logs (if running locally):
```bash
docker compose logs worker
docker compose logs worker -f
```

For standalone worker processes, logs output to stdout. Capture them:
```bash
uv run python -m transcode_forge.worker > worker.log 2>&1 &
```

## Worker won't connect to scheduler

### Symptoms
- Worker logs: "Failed to connect", "Connection refused", or "401 Invalid or revoked token"
- Worker never appears under Settings → Workers

### Causes & Fixes

**Network unreachable**
- Verify scheduler host and port from the worker machine:
  ```bash
  curl http://<scheduler-host>:8000/api/health/ready
  ```
  Should return `{"status":"ok",...}` with HTTP 200.
- Check firewall: the scheduler's `TF_PORT` (default 8000) must be reachable from the worker.
- If using Docker, check the worker's network: `docker network ls` and ensure both containers share the same network.

**Invalid or revoked token**
- Token was revoked in Settings → Workers → revoke.
- Token was pasted incorrectly (typo, extra spaces).
- Token expired (if `TF_AUTH_SECRET` was changed since the token was issued, all tokens are invalidated).

Fix: Issue a fresh token from Settings → Workers → Issue token and paste it exactly.

**Wrong `TF_SERVER_URL`**
- Verify the scheduler is reachable and running:
  ```bash
  curl http://<TF_SERVER_URL>/api/health/live
  ```
  Should return HTTP 200 even if the scheduler is degraded.

**Scheduler is degraded**
- Check scheduler health:
  ```bash
  curl http://localhost:8000/api/health/ready
  ```
  If HTTP 503 (degraded), Postgres or Redis is down. Check:
  ```bash
  docker compose logs postgres
  docker compose logs redis
  ```

## Jobs stuck in TRANSCODING or ASSIGNED

### Symptoms
- Queue shows jobs with status "transcoding" or "assigned" that aren't progressing.
- Worker crashed or was force-killed mid-transcode.

### Root Cause
Worker lost connection or crashed while it had claimed jobs. The scheduler's heartbeat timeout will eventually re-queue them, but you can force it immediately.

### Fixes

**Restart the worker**
```bash
docker compose restart worker
# or for standalone:
kill <worker-process-id>
uv run python -m transcode_forge.worker
```

When a worker re-registers, its `POST /worker/register` call atomically releases any jobs it had claimed. The scheduler logs will show:
```
Worker <id> came online — released <N> orphan job(s) back to the queue
```

**Retry a job that ended up failed**
- A job that errored shows a **Retry** button in **History**; it re-queues that file.
- Or use the API (failed jobs only):
  ```bash
  curl -X POST http://localhost:8000/api/jobs/<job-id>/retry \
    -b "session=<your-session-cookie>"
  ```
- A job genuinely stuck *in-progress* (status transcoding/assigned) is released
  by restarting its worker — see above; you don't re-queue it by hand.

**Check for more details**
- Dashboard → History to see error messages from failed jobs.
- Dashboard → Settings → Audit to see integrity issues (if jobs were in-progress and the worker disappeared).

## Encoder fell back to CPU (slow transcodes)

### Symptoms
- Worker logs show "Selected encoder: cpu (available: ['cpu'])".
- Transcode speed is ~10-20% of real-time (very slow).

### Root Cause
QSV or NVENC initialization failed; the worker fell back to software x265.

### Check which encoder failed

Look for these patterns in worker logs:

**QSV failed** — a line like:
```
Failed to set value 'qsv=hw' for option 'init_hw_device' ...
```

**NVENC failed** — a line like:
```
NVENC not available: ...
```

### Fixes

**For Intel QSV** (Linux)
1. Install Intel Media drivers:
   ```bash
   # Ubuntu/Debian
   sudo apt install intel-media-va-driver-non-free libmfx1
   # For 11th gen and newer:
   sudo apt install intel-media-va-driver-non-free libmfx-gen
   ```

2. Verify `/dev/dri/renderD128` exists and is readable:
   ```bash
   ls -la /dev/dri/renderD128
   ```

3. If using Docker, pass the device:
   ```yaml
   devices:
     - /dev/dri:/dev/dri
   ```
   Uncomment the `worker:` service in `docker-compose.yml`.

4. Restart the worker and check logs for "Selected encoder: qsv".

**For NVIDIA NVENC** (Linux / Windows)
1. Install the NVIDIA driver (470+):
   ```bash
   nvidia-smi  # Verify driver is installed
   ```

2. If using Docker, install `nvidia-container-toolkit`:
   ```bash
   # Ubuntu/Debian
   sudo apt install nvidia-container-toolkit
   sudo systemctl restart docker
   ```

3. In `docker-compose.yml`, add the GPU runtime to the worker service:
   ```yaml
   worker:
     runtime: nvidia
     environment:
       NVIDIA_VISIBLE_DEVICES: all
   ```

4. Restart the worker and check logs for "Selected encoder: nvenc".

**Force a specific encoder for testing**
```bash
export TF_PREFERRED_ENCODER=qsv  # or nvenc, cpu, auto
uv run python -m transcode_forge.worker
```

If the forced encoder fails to initialize, the worker will still fall back to CPU.

## Library path is not found or not readable

### Symptoms
- Dashboard shows "Library scanning disabled" or "critical" preflight issues.
- Scheduler logs show: "No such file or directory" or "Permission denied".

### Root Cause
`TF_LIBRARY_MOVIES` or `TF_LIBRARY_TV` path doesn't exist, isn't mounted, or doesn't have read permissions.

### Fixes

1. Check the scheduler's preflight status:
   ```bash
   curl http://localhost:8000/api/health/preflight \
     -H "Cookie: session=<your-session-cookie>"
   ```

2. Verify the path exists on the host:
   ```bash
   ls -la /path/to/library
   ```

3. In `docker-compose.yml`, ensure the volume mount is correct:
   ```yaml
   volumes:
     - "/path/to/movies:/media/movies:ro"
   ```
   The left side is the **host path**, the right side is the **container path** (must match `TF_LIBRARY_MOVIES`).

4. Update `.env`:
   ```bash
   TF_LIBRARY_MOVIES=/path/to/your/movies
   TF_LIBRARY_TV=/path/to/your/tv
   ```

5. Restart the scheduler:
   ```bash
   docker compose restart scheduler
   ```

6. Go back to the dashboard — the issue should clear within a few seconds.

## Lost admin password

### Symptoms
- Locked out of the UI; can't log in.

### Fix

Reset it from the server with the admin CLI — no SQL, no restart, and your
catalog/jobs/workers are untouched (only the login changes):

```bash
# Prompts for a new password:
docker compose exec scheduler python -m transcode_forge.admin reset-password

# Or non-interactively:
docker compose exec -T scheduler python -m transcode_forge.admin reset-password --password 'your-new-password'
```

Then log in with the new password. The command resets the admin if one exists,
or creates it if not (so it also covers a headless first-run). This is the same
server-side recovery model as Nextcloud's `occ user:resetpassword` or Django's
`changepassword` — shell access to the host is the trust boundary.

## Lost worker token

### Symptoms
- You issued a token but didn't copy it (it's shown once).
- Worker is running with an old token that no longer works.

### Fix

You cannot retrieve the original token. Issue a new one:

1. Go to Settings → Workers → Issue token, give it a label, copy the token.
2. On the worker machine, update the environment variable:
   ```bash
   export TF_WORKER_TOKEN=<new-token>
   ```
3. Restart the worker:
   ```bash
   docker compose restart worker
   # or standalone:
   kill <process-id>
   uv run python -m transcode_forge.worker
   ```

## Safe restart

Restarting the scheduler or any service is safe.

```bash
# Restart the scheduler
docker compose restart scheduler

# Restart the worker
docker compose restart worker

# Restart all services
docker compose restart
```

### Important: Pin `TF_AUTH_SECRET`

By default, if `TF_AUTH_SECRET` is not set in `.env`, the scheduler generates a random secret on every boot. This invalidates:
- All admin sessions (you'll be logged out).
- All worker tokens (workers will fail to auth).

To prevent this, pin a secret in `.env`:

```bash
# In .env, ensure this line exists (bootstrap.sh generates it once):
TF_AUTH_SECRET=<random-secret-from-bootstrap>
```

If `TF_AUTH_SECRET` is set, sessions and worker tokens survive restarts.

### Schema migrations

When the scheduler starts, it automatically applies any pending database migrations. This is idempotent — if a migration has already been applied, it's skipped. Safe to restart; the database will not be corrupted.

## FFmpeg or ffprobe missing

### Symptoms
- Scheduler or worker logs: "ffmpeg: command not found" or "ffprobe: command not found".

### Fix

**In the Docker container**
- The Dockerfile installs ffmpeg. If the image was built without it, rebuild:
  ```bash
  docker compose down
  docker compose build --no-cache scheduler
  docker compose up -d
  ```

**For a standalone worker**
- Install ffmpeg on the worker machine:
  ```bash
  # Ubuntu/Debian
  sudo apt install ffmpeg
  # macOS
  brew install ffmpeg
  # Windows (with Chocolatey)
  choco install ffmpeg
  ```

Verify:
```bash
ffmpeg -version
ffprobe -version
```

## Queue is paused

### Symptoms
- Jobs sit in the queue and never claim to a worker.
- Worker logs show "queue_paused" reason.

### Fix

Go to Settings → Queue and enable the queue (toggle off pause). Or use the API:

```bash
curl -X POST http://localhost:8000/api/queue/resume \
  -b "session=<your-session-cookie>"
```

## Scheduler or database is down

### Symptoms
- Dashboard returns HTTP 500 or "Bad Gateway".
- Curl `/api/health/ready` returns HTTP 503 or no response.

### Check service status

```bash
docker compose ps
```

All three should show "Up":
- `scheduler`
- `postgres`
- `redis`

### Restart

```bash
docker compose down
docker compose up -d
```

Check health:
```bash
docker compose logs scheduler
curl http://localhost:8000/api/health/ready
```

### Common issues

**Postgres won't start**
- Check disk space: `docker compose exec postgres df /var/lib/postgresql/data`
- Corrupted data directory: back up and delete the volume:
  ```bash
  docker compose down -v
  docker compose up -d
  # Data is lost; re-scan and re-queue jobs.
  ```

**Redis won't start**
- Check logs: `docker compose logs redis`
- If data is corrupted: `docker compose down -v && docker compose up -d`

---

**Still stuck?** Check the scheduler logs for error messages and share them in an issue.

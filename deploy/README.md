# Deployment recipes

For most users, the top-level `docker-compose.yml` + `bootstrap.sh` is
all you need. This directory holds optional artifacts for non-Docker
setups and for monitoring integration.

| File | When you'd use it |
|---|---|
| `transcode-forge-scheduler.service` | Run the scheduler under systemd on bare-metal Linux instead of Docker |
| `transcode-forge-worker.service` | Run a worker under systemd on a Linux host |
| `start-transcode-worker.bat`, `stop-transcode-worker.bat` | Run a worker on a Windows host (e.g. a Windows desktop with NVENC) |
| `worker.env.example` | Template for the worker config used by the Windows scripts (and useful as a reference for systemd's EnvironmentFile) |
| `alertmanager-rules.yml` | Drop-in Prometheus alerting rules — high-failure-rate, no-active-workers, queue-stalled |
| `grafana-dashboard.json` | Importable Grafana dashboard for the metrics exposed at `/metrics` |

## Bare-metal scheduler (Linux + systemd)

If you don't want Docker, run the scheduler directly:

```bash
# As root on the host:
mkdir -p /opt/transcode-forge
cd /opt/transcode-forge
git clone https://github.com/nuffy94/transcode-forge.git .
uv sync

cp deploy/transcode-forge-scheduler.service /etc/systemd/system/
# Create /opt/transcode-forge/.env with at minimum:
#   TF_DB_URL=postgresql://tf:<password>@localhost/transcode_forge
#   TF_REDIS_URL=redis://localhost:6379/0
#   TF_AUTH_SECRET=<random 32+ chars>
#   TF_LIBRARY_MOVIES=/path/to/movies
#   TF_LIBRARY_TV=/path/to/tv

systemctl daemon-reload
systemctl enable --now transcode-forge-scheduler
systemctl status transcode-forge-scheduler
```

You'll need a Postgres + Redis running somewhere. The compose file is the
easiest way to provide them.

## Bare-metal worker (Linux + systemd)

```bash
# On the worker host:
mkdir -p /opt/transcode-forge
cd /opt/transcode-forge
git clone https://github.com/nuffy94/transcode-forge.git .
uv sync

cp deploy/transcode-forge-worker.service /etc/systemd/system/

# Issue a token in the scheduler UI: Settings → Workers → Issue.
# Then write the worker config:
cat >/etc/default/transcode-forge-worker <<EOF
TF_SERVER_URL=http://your-scheduler:8000
TF_WORKER_TOKEN=<paste the token>
TF_WORKER_NAME=$(hostname)
TF_PREFERRED_ENCODER=auto
EOF

systemctl daemon-reload
systemctl enable --now transcode-forge-worker
journalctl -u transcode-forge-worker -f
```

## Windows worker (NVENC)

For a Windows desktop, see `start-transcode-worker.bat` — copy
`worker.env.example` to `worker.env`, fill in the values, and double-click
the bat file.

## Hardware encoder access

Whether under Docker, systemd, or .bat, the worker process needs:

- **Intel QSV (Linux)**: `/dev/dri/renderD128` accessible to the worker
  process. In Docker, pass `--device /dev/dri`. On bare metal the worker
  user needs to be in the `render` group.
- **NVIDIA NVENC (Linux)**: NVIDIA driver + `nvidia-container-toolkit`
  for Docker, or just the driver for bare metal.
- **Apple VideoToolbox (macOS)**: built into ffmpeg from Homebrew.
- **Software x265**: works anywhere, slow.

The worker's hardware-detection runs at startup and logs which encoders
it found. If it fell back to CPU when you expected hardware, check those
logs first.

## Monitoring

`alertmanager-rules.yml` defines four useful alerts (transcode failure
rate, queue stalled, no active workers, disk almost full). Copy into your
Alertmanager config or rule directory.

`grafana-dashboard.json` imports as a Grafana dashboard with panels for
queue depth, active workers, throughput, and savings. Point it at the
Prometheus that scrapes `/metrics` on the scheduler.

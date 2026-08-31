# Deploying Transcode Forge on Linode

Two StackScripts deploy the real, complete product on Linode Compute:

- **`stackscript-scheduler.sh`** — the scheduler stack: Redis + PostgreSQL +
  scheduler behind a Caddy TLS edge. Fully standalone: after boot you can
  join the instance's own spare CPU as a worker, so a one-node deploy is
  simply "don't add workers."
- **`stackscript-worker.sh`** — an HTTP-only CPU worker that joins via a
  server-issued token. Add as many as you want.

Linode Compute instances have no Intel iGPU or NVIDIA card, so encoding is
software (`libx265` / `libsvtav1`) — that's why the plan table below is
Dedicated CPU. The 8-step pipeline, VMAF quality gate, and CRF search all
work exactly as they do on hardware-accelerated nodes.

## Topology

```
              ┌───────────────────────────────┐
   HTTPS      │  scheduler Linode             │
  ──────────► │  Caddy ──► scheduler:8000     │
              │  Redis · PostgreSQL*          │        Object Storage
              │  [worker profile, optional]───┼──────► bucket (media)
              └───────────────▲───────────────┘              ▲
                              │ HTTPS + token                │
              ┌───────────────┴───────────────┐              │
              │  worker Linode(s), 0..N       ├──────────────┘
              │  outbound-only, CPU encode    │
              └───────────────────────────────┘
   * or a Linode Managed Database (UDF toggle)
```

Single node: media can live on a Block Storage volume on the scheduler
(filesystem library). **Multi-node requires an Object Storage library** —
remote workers can't see the scheduler's local disk; they fetch masters
from and upload derivatives to the bucket.

## Plan table (Dedicated CPU)

Shared CPU plans throttle sustained x265 encode — use Dedicated. Size the
plan for CPU; store media in Object Storage and scratch on a Block Storage
volume, not the small root disk.

| Tier   | Plan            | vCPU | ~$/mo |
| ------ | --------------- | ---- | ----- |
| Small  | Dedicated 8GB   | 4    | ~$72  |
| Medium | Dedicated 16GB  | 8    | ~$144 |
| Large  | Dedicated 32–64GB | 16–32 | ~$288–576 |

Every worker runs one job at a time; more vCPUs make that one job (and
its quality measurement) faster. Block Storage scratch: ~2× your largest
media file.

**For throughput, add worker Linodes — each with its own token.**
Measured (2026-07-15, RTX 4000 Ada GPU plan, 4 vCPU): two concurrent
encodes ran **17% slower** than one back-to-back — the VMAF quality gauge
is CPU-bound, so a second job starves both of CPU while the GPU encoder
sits ~13% utilized. Same guidance as the LKE chart (scale
`worker.replicas`, not concurrency).

## Prerequisites

Have these ready before you start. Everything else the StackScripts
handle on the instance.

1. **A Linode account** with Cloud Manager access and the ability to
   create paid resources.
2. **An SSH key pair**, public key handy. It's attached to the
   *scheduler* at creation — the localhost-only path and the local-worker
   join both use SSH. Workers never get one (see step 8).
3. **Optional but recommended — a domain**, if you want HTTPS on a real
   name: registered, with its nameservers pointed at a DNS provider
   where you can add an `A` record.
   - **Cloudflare DNS:** also create an API token scoped to
     `Zone / DNS / Edit` for that zone. Certificates issue via DNS-01 —
     no waiting on propagation, no port-80 race.
   - **Any other DNS provider (including Linode's DNS Manager):** leave
     the Cloudflare token UDF blank. Certificates fall back to HTTP-01,
     which works once your `A` record points at the instance and has
     propagated.
4. **Object Storage (required for multi-node):** a bucket + access keys
   (Cloud Manager → Object Storage). Create **limited access keys** scoped
   to just the media bucket — every worker holds these keys, so scoping
   caps the blast radius of a compromised node. Note the endpoint URL,
   e.g. `https://us-ord-1.linodeobjects.com`.
5. **Optional — Managed Databases:** a PostgreSQL Managed Database if you
   don't want the built-in Postgres container. Note its connection URL,
   including `?sslmode=require`.
6. **Something to transcode:** your own h264 media, or seed the bucket
   with ~2 GB of CC-BY test movies (step 2).

## The runbook

### 1. Add the StackScripts to your account

Cloud Manager → StackScripts → Create StackScript → paste
`stackscript-scheduler.sh` (target image: Ubuntu 24.04 LTS). Repeat for
`stackscript-worker.sh`. (Or `linode-cli stackscripts create …`.)

If you edit the scripts first, mind Linode's StackScript parser: script
content must be **ASCII-only**, and UDF `label` values can't contain
apostrophes or `>` (both read as malformed tags and the API rejects the
whole script with a confusing "mismatched quotes" error).

### 2. (Multi-node / Object Storage) create and seed the bucket

Cloud Manager → Object Storage → create a bucket + access keys. Seed it
with free CC-BY media so there's something to transcode on day one:

```bash
export S3_ENDPOINT=https://us-ord-1.linodeobjects.com
export S3_ACCESS_KEY=…
export S3_SECRET_KEY=…
./seed-media.sh forge-media        # ~2 GB of Blender open movies, h264
```

Or upload your own h264 media under `masters/movies/` with rclone/s3cmd.

> For the **reproducible benchmark corpus** (open-licensed, content-class
> partitioned, versioned with a manifest) used by `scripts/bench/`, use
> `build_corpus.py` instead — see [CORPUS.md](CORPUS.md).

### 3. Create a Cloud Firewall

Docker publishes ports past `ufw`, so the **Cloud Firewall is the
perimeter** — don't skip it.

- **forge-scheduler** policy: inbound DROP default; allow TCP 22 (ideally
  from your IP only), 80, 443.
- **forge-worker** policy: inbound DROP default, no allow rules. Workers
  are outbound-only and never need SSH — use the Lish console if you
  ever need a shell on one.

### 4. Create the scheduler Linode

Cloud Manager → Create → Linode → StackScripts tab → your scheduler script.

- Image Ubuntu 24.04 LTS, region near your Object Storage bucket, a
  Dedicated CPU plan from the table.
- Fill the UDFs: `domain` (blank = localhost-only), Cloudflare token
  (blank = HTTP-01), Object Storage endpoint/bucket/keys (blank = local
  media), Managed Database URL (blank = built-in Postgres).
- Attach the **forge-scheduler** Cloud Firewall and your SSH key (the
  scheduler is the only instance that gets a key).
- Add a **Block Storage volume** (media/scratch — the script formats and
  mounts an unformatted attached volume at `/mnt/data`).

First boot takes a few minutes (Docker install, image pulls; the DNS-01
Caddy build adds ~2–4 min). Progress: Lish console or
`tail -f /root/StackScript.out` over SSH. A `NEXT-STEPS.txt` recap is
written to `/opt/transcode-forge/`.

### 5. Point DNS

An `A` record for your domain → the instance's public IP. With the
Cloudflare DNS-01 token, certificates issue even before DNS propagates.

### 6. First-run setup

Open `https://<domain>/setup` (no domain: tunnel with
`ssh -L 8000:127.0.0.1:8000 root@<ip>` and use `http://localhost:8000`),
create the admin account, then **Settings → Add library**:

- **Object Storage:** storage "S3 Object Storage", bucket `forge-media`,
  prefix `masters/movies/`. End prefixes with a trailing slash — S3 prefix
  matching is raw string-prefix, so a bare `media` would also match a
  `media-archive/` tree.
- **Local:** path `/media/movies` (host `/mnt/data/media/movies`; upload
  via scp/rsync).

Scan the library, then queue files from the Movies/TV pages.

### 7. (Optional) join the scheduler's own CPU as a worker

Workers page → Issue token, then on the instance:

```bash
cd /opt/transcode-forge && ./join-local-worker.sh   # prompts for the token
```

### 8. Add worker nodes

For each worker: issue a fresh token in the UI (one per worker — they're
individually revocable), then Create → Linode with the worker StackScript.
UDFs: `server_url` (`https://<domain>`), the token, and the same Object
Storage endpoint/keys. Attach the **forge-worker** Cloud Firewall — no
SSH key (use Lish if you ever need a console). The worker appears on the
Workers page within a heartbeat of boot finishing.

### Smoke test

```bash
curl -fsS https://<domain>/api/health/ready   # → {"status":"ok"}
```

plus: the issued-token worker shows on the Workers page, and a queued file
completes (or lands in Activity → Skipped with a VMAF/size reason — that's
the quality gate doing its job).

## Managed Database notes

- Include `?sslmode=require` in the URL — Linode Managed Databases require
  TLS.
- If the generated password contains a literal `$`, escape it as `$$` in
  the UDF value (docker compose interpolates `$` in env files).
- The built-in Postgres container is skipped entirely when a URL is given
  (no idle container, no unused volume).

## No public exposure at all?

Leave `domain` blank — the scheduler binds `127.0.0.1` only. Documented
alternatives to the Caddy edge:

- **Cloudflare Tunnel:** run `cloudflared` on the instance with a tunnel
  token; no inbound ports at all (drop 80/443 from the firewall).
- **Tailscale:** install tailscaled, then reach `http://<tailscale-ip>:8000`
  privately. Point worker `server_url` at the Tailscale IP.

## Security model

- **Cloud Firewall is the perimeter.** `ufw` does not see Docker-published
  ports; the app itself binds loopback and only Caddy listens publicly.
- **No Linode API token on the instance** — control-plane actions (volumes,
  firewalls, DNS) happen in Cloud Manager, not in the scripts.
- Workers hold a revocable bearer token and Object Storage keys — never DB
  or Redis credentials. Revoke a worker's token in the UI and it's out.
  Use bucket-scoped limited access keys (prereq 3) so those keys can't
  reach anything but the media bucket.
- Secrets (`TF_PG_PASSWORD`, `TF_AUTH_SECRET`) are generated on the
  instance into `/opt/transcode-forge/.env` (mode 600) and never printed —
  StackScript output lands on disk at `/root/StackScript.out`.
- **`/metrics` stays inside the perimeter.** The endpoint is auth-exempt
  in the app so Prometheus can scrape it, so the Caddyfile answers 403
  for it at the public edge. Want the metrics? Scrape the instance from
  your monitoring network, not through the domain.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| StackScript seems stuck | `tail -f /root/StackScript.out` (or Lish). Docker install + pulls take a few minutes; the DNS-01 Caddy build adds ~2–4 more. |
| `https://` not answering | DNS A record propagated? Cloud Firewall allows 80/443? `docker compose logs caddy` in `/opt/transcode-forge`. |
| Certificate errors (HTTP-01) | Port 80 must be reachable and DNS must already point here — or use the Cloudflare DNS-01 token. |
| Worker not on Workers page | `docker compose logs worker` on the worker node. Usually a wrong/revoked token or a `server_url` typo. |
| S3 scan finds nothing | Bucket/prefix on the library must match where media was seeded (`masters/movies/`). |
| S3 uploads fail with 403 | The app pins botocore checksums to `when_required` (Linode Object Storage rejects the newer CRC32 default). If you see 403s anyway, re-check keys and endpoint region. |
| Jobs stay queued | No worker has claimed them: is a worker joined (step 7/8)? Multi-node with a filesystem library won't work — use an S3 library. |
| Disk filling up | Media/scratch belong on the Block Storage volume (`/mnt/data`), not the root disk. The script warns at boot if no volume was attached. |

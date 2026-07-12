# Deploying Transcode Forge on LKE (Kubernetes)

A Helm chart for Linode Kubernetes Engine — the Kubernetes sibling of the
[StackScript deploy](../linode/README.md). One release runs the scheduler
(with Redis and Postgres) plus N CPU workers.

**The media plane is Object Storage, non-negotiable.** LKE block storage
is RWO — a volume mounts on one node — so a filesystem library can never
be shared across worker pods. Libraries here are S3 libraries: workers
fetch masters from and upload derivatives to the bucket. That also means
the image must be **v0.9.5 or later** (earlier tags predate the S3
scan/claim/derivative fixes and break exactly this path); the chart pins
a working tag — don't override it backwards, and never to `latest`.

## Topology

```
   kubectl port-forward / optional Ingress
  ─────────────► Service transcode-forge:8000
                    │
      ┌─────────────▼──────────────┐
      │ scheduler Deployment       │       Object Storage
      │ redis Deployment           ├─────► bucket (media)
      │ postgres StatefulSet+PVC*  │            ▲
      └─────────────▲──────────────┘            │
                    │ token (in-cluster HTTP)   │
      ┌─────────────┴──────────────┐            │
      │ worker Deployment, 0..N    ├────────────┘
      │ outbound-only, CPU encode  │
      └────────────────────────────┘
   * or an external DBaaS URL (values toggle)
```

## Node pool sizing

Shared CPU pools throttle sustained x265 — fine for proving the deploy,
wrong for throughput. Overlays mirror the StackScript plan table:

| Tier   | Pool               | vCPU/node | Values                |
| ------ | ------------------ | --------- | --------------------- |
| Small  | 2× g6-standard-2   | 2 shared  | `values.yaml` (defaults) |
| Medium | g6-standard-4 / g6-dedicated-4 | 4 | `-f values-medium.yaml` |
| Large  | g6-dedicated-8     | 8 dedic.  | `-f values-large.yaml`  |

Expect a 1080p movie at x265 `-preset slow` to take **hours per shared
vCPU** — that's the archival-quality design, not a hang. Scale worker
replicas (one encode each), not `maxConcurrent`.

## Prerequisites

1. `kubectl` + `helm` (v3) and the cluster kubeconfig
   (`linode-cli lke kubeconfig-view <id>` or Cloud Manager → Download).
2. An Object Storage bucket + **limited access keys** scoped to it (every
   worker holds these keys). Seed it with the CC-BY corpus:
   `../linode/seed-media.sh <bucket>` (needs rclone).
3. Optional: a PostgreSQL DBaaS URL if you don't want the in-cluster
   Postgres.

## The runbook

### 1. Install (workers at 0)

Secrets are passed at install time and land in one Kubernetes Secret —
never in values files you commit. With 1Password, wrap the install in
`op run` and reference env vars; plain shell works too:

```bash
helm install transcode-forge deploy/lke/transcode-forge \
  -n transcode-forge --create-namespace \
  --set secrets.authSecret="$(openssl rand -base64 48)" \
  --set secrets.pgPassword="$(openssl rand -base64 24)" \
  --set secrets.s3AccessKeyId="$S3_ACCESS_KEY" \
  --set secrets.s3SecretAccessKey="$S3_SECRET_KEY" \
  --set s3.endpointUrl=https://us-ord-1.linodeobjects.com \
  --set s3.region=us-ord-1
```

Workers default to 0 replicas — they need a server-issued token, and the
chart refuses to render workers without one (no CrashLoopBackOff
guesswork). `kubectl -n transcode-forge get pods` until everything is
Running.

### 2. First-run setup

```bash
kubectl -n transcode-forge port-forward svc/transcode-forge 8000:8000
```

Open `http://localhost:8000` → `/setup` creates the admin account. Then
**Settings → Add library**: storage "S3 Object Storage", your bucket,
prefix `masters/movies/` (trailing slash — S3 prefix matching is raw
string-prefix). Scan it.

### 3. Join workers

Workers page → Issue token, then:

```bash
helm upgrade transcode-forge deploy/lke/transcode-forge \
  -n transcode-forge --reuse-values \
  --set secrets.workerToken=<token> --set worker.replicas=1
```

The worker registers within a heartbeat. Queue files from Movies/TV.
Note: changing the Secret rolls the scheduler pod too (checksum
annotation) — your port-forward dies with it; just re-run it.

### 4. Upgrades — zero downtime, proven

The scheduler rolls with `maxUnavailable: 0` plus a preStop drain. Live
proof on a real cluster: an in-cluster poller hitting readiness at 2/s
through an env-bump `helm upgrade` measured **0 non-200 in 117 samples**
(without the preStop drain, the same test dropped 1 — the terminating
pod racing endpoint removal). Workers use `Recreate` — an old encode's
job is re-queued on shutdown rather than doubling CPU on a small pool.

```bash
helm upgrade transcode-forge deploy/lke/transcode-forge \
  -n transcode-forge --reuse-values --set image.tag=<new released tag>
```

### External database (DBaaS)

```
--set postgres.enabled=false \
--set externalDb.url="postgresql://user:pass@host:5432/db?sslmode=require"
```

Drops the StatefulSet + PVC entirely. Linode Managed Databases require
`sslmode=require`.

### Gateless runs (acceptance/demo speed)

Full-file CPU VMAF adds hours per movie on small pools. Pointing the
measurement binary at the image's distro ffmpeg (built **without**
libvmaf) makes the worker skip CRF search + the VMAF gate — the
documented "pre-VMAF behavior" fallback, loudly logged:

```
--set worker.extraEnv.TF_VMAF_FFMPEG=/usr/bin/ffmpeg
```

Unset it (default) to restore the quality guarantee. Encodes then carry
the full gate: a replacement that can't prove its quality is discarded.

## Security model

- Workers are outbound-only (no Service, no listening port) and hold a
  revocable bearer token + bucket-scoped S3 keys — never DB or Redis
  credentials. Revoke in the UI and the worker is out.
- All secret material lives in one namespaced Secret, injected at
  install; nothing secret renders into Deployments (pinned by the
  golden-render tests).
- No public edge by default: Ingress is off, access is port-forward.
  `ingress.enabled=true` + `ingress.host` when you mean to expose it —
  set `scheduler.sessionSecure=true` behind TLS.
- No Linode API token lives in the cluster.

## Teardown

```bash
helm uninstall transcode-forge -n transcode-forge
kubectl delete namespace transcode-forge
```

LKE's default StorageClass is `linode-block-storage-retain`: the
Postgres volume **survives** uninstall (that's the point — a bad
`helm uninstall` can't eat the catalog). Delete the leftover PV/volume
in Cloud Manager when you're truly done, or install with
`--set postgres.storageClass=linode-block-storage` for delete-on-uninstall
semantics. The cluster itself is yours — the chart never touches it.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Render fails: "workerToken required" | By design — issue a token first (runbook step 3), or keep `worker.replicas=0`. |
| Worker pod up but not on Workers page | `kubectl logs deploy/transcode-forge-worker`. Usually a revoked/mistyped token in the Secret. |
| S3 scan finds nothing | Library bucket/prefix must match where media was seeded (`masters/movies/`, trailing slash). |
| Port-forward dies during `helm upgrade` | Expected — it pins a pod, and Secret changes roll the scheduler. Re-run it. |
| Job says transcoding, "no progress" | x265 `-preset slow` on shared vCPUs is slow by design; check `kubectl logs` for ffmpeg progress POSTs before assuming a hang. |
| Scheduler Pending | Node headroom: `kubectl describe nodes` — the postgres PVC also needs a node with the volume's attachment slot free. |
| Postgres pod Pending forever | Volume provisioning: `kubectl -n transcode-forge describe pvc`. Linode volumes have a 10Gi minimum. |

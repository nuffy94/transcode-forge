# Storage backends

Transcode Forge selects a storage backend **per library**. Each library is
either a **filesystem** path (the default) or an **S3-compatible bucket**. The
two backends serve different goals:

| Backend | Behavior | Use it for |
|---------|----------|-----------|
| **filesystem** | Transcodes the file **in place** — replaces the original with the smaller encode and reclaims disk. | A NAS library where the goal is to **shrink** your collection. |
| **s3** | Keeps the master object untouched, transcodes a copy, and uploads a **derivative** (a separate optimized object); repeat requests reuse the cached derivative. | Cloud / distributed setups where workers shouldn't share a filesystem. |

Pick a backend when you create the library (UI or API). Existing libraries
default to `filesystem` and behave exactly as before — this is additive.

---

## Filesystem backend (single node or a cluster)

A single-node install needs nothing special — the scheduler and worker share the
local disk. For **multiple worker nodes**, every node (scheduler included) must
see the same media at the same path.

**Recommended: NFS v4.1** (all-Linux). It saturates a gigabit link for
large-file sequential I/O and beats SMB on library scanning. Use SMB/CIFS only
if Windows nodes are involved.

1. **Export the library from the storage host** (`/etc/exports`):
   ```
   /srv/media  10.0.0.0/24(rw,no_root_squash,sync)
   ```
   `no_root_squash` is required so a worker container running as root can write
   the transcoded file back. It's safe on a trusted LAN; do not export to the
   open internet.

2. **Standardize the mount path on every node** — mount the export at the *same*
   path everywhere (e.g. `/mnt/transcode`). Identical paths mean no translation
   is needed. If a node must mount elsewhere, either symlink it or set
   `TF_PATH_MAP` (a JSON map of scheduler-path → local-path) on that worker.

3. **Mount inside the worker container** via the compose NFS volume driver
   (cleaner than bind-mounting a host mount):
   ```yaml
   volumes:
     media:
       driver: local
       driver_opts:
         type: nfs
         o: "addr=<storage-host>,nfsvers=4.1,hard,timeo=600"
         device: ":/srv/media"
   ```

**Cache coherency:** on a busy multi-node farm, NFS attribute caching can briefly
hide a just-written file from another node. If you see stale reads, mount with
`noac` (or `actimeo=0`) on the workers — at a small performance cost.

The 8-step "never lose a file" pipeline (lock → transcode → verify → compare →
atomic swap → confirm → cleanup → unlock, with rollback) is unchanged on this
backend.

---

## S3 object-storage backend

The media lives in an S3-compatible bucket. Any worker — on any host — pulls the
master to local scratch, transcodes it, uploads a derivative, and registers it;
no shared filesystem required. Workers just need bucket credentials.

**Scheduler + worker config** (env, `TF_` prefix — supply secrets via 1Password,
never commit them):

| Setting | Meaning |
|---------|---------|
| `TF_S3_ENDPOINT_URL` | S3 endpoint (e.g. a Linode Object Storage region endpoint). Leave blank for AWS. |
| `TF_S3_REGION` | Bucket region. |
| `TF_S3_ACCESS_KEY_ID` / `TF_S3_SECRET_ACCESS_KEY` | Bucket credentials. |
| `TF_SCRATCH_DIR` | Local scratch dir for downloads/transcodes (defaults to a temp dir). Needs free space ≥ your largest file. |

**Per-library** (set when creating the library): `backend = s3`, `s3_bucket`,
`s3_prefix`. The scheduler scans the prefix (listing objects + probing each via a
presigned URL) to build the catalog; workers fetch/transcode/upload.

**How derivatives + reuse work.** A derivative's key is content-addressed —
`blake2b(source_path + source/target resolution + audio codec + encoder + crf +
preset)` — so the same source + the same settings always map to the same object.
Before transcoding, the worker checks the `derivatives` registry; if a matching
derivative already exists, the job completes without re-encoding. The master is
never overwritten.

### Linode Object Storage notes

- S3-compatible — set `TF_S3_ENDPOINT_URL` to your region's endpoint and the
  standard S3 client works unchanged.
- Bucket names are **globally unique**; prefer a region's high-throughput
  endpoint.
- Linode does **not** offer S3 object versioning — and this design doesn't need
  it: the master object is never overwritten, so there's nothing to roll back.

---

## Database: self-hosted or managed (DBaaS)

The database target is also a local|cloud toggle, set via `TF_DB_URL`:

- **SQLite** (single-node dev): `sqlite:///transcode_forge.db`
- **Self-hosted Postgres**: `postgresql://tf:PASSWORD@host:5432/transcode_forge`
- **Linode Managed Postgres (DBaaS)** — TLS is mandatory, so include
  `?sslmode=require`:
  `postgresql://USER:PASSWORD@YOUR-LINODE-DBAAS-HOST:PORT/transcode_forge?sslmode=require`

Startup preflight attempts a real connection and reports a clear error
(malformed URL / unreachable / TLS failure / auth failure) instead of failing
silently.

---

## Upgrading an existing install

- **Staying single-node (SQLite + local filesystem)?** Nothing to do — the
  filesystem backend is the default and behaves exactly as before.
- **Moving to Postgres / DBaaS?** Dump and import, then point `TF_DB_URL` at the
  new database and restart:
  ```bash
  sqlite3 transcode_forge.db .dump > dump.sql
  # create the managed DB, then:
  psql "<your TF_DB_URL>" < dump.sql
  ```
- **Adding an S3 library?** Leave existing filesystem libraries as they are and
  create a new library with `backend = s3`. The two coexist.

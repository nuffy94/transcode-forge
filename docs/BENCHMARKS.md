# Benchmarks

How Transcode Forge measures encode throughput, compression, perceived
quality, and cost — and how to reproduce the numbers. The tooling lives in
`scripts/bench/` and is pure analysis over the `jobs` table: no ffmpeg, no
writes.

## What's measured

Every group in a report is a **(target codec, backend, resolution class)**
cell — e.g. `hevc / qsv / 1080p`. Only terminal jobs (`complete`,
`skipped`, `failed`) are counted; pending/running jobs never appear.

| Metric | Definition |
|---|---|
| jobs/hr | Completed jobs per encode-hour. Encode-hours = sum of per-job wall clock (`completed_at − started_at`) over completed jobs. |
| GB-in/hr | Decimal GB (10⁹ bytes) of *source* video processed per encode-hour, completed jobs. This is the number the economics model consumes. |
| saved % | Aggregate compression over completed jobs, size-weighted (one big movie counts more than one small episode). Derived from `source_size − output_size` when both exist; `space_saved` is the fallback. The distinction matters for S3 jobs, which record `space_saved = 0` — masters are never replaced, so nothing is "reclaimed" even though the encode compressed. |
| VMAF mean/min/p5 | Distribution of `achieved_vmaf` over every terminal job that has one — including gate-skipped jobs, whose scores are exactly the interesting ones. |
| skip % / fail % | Share of the group's terminal jobs that ended `skipped` (size regression or VMAF gate) / `failed`. |
| wall p50/p90/p95 | Per-job wall-clock percentiles in seconds, completed jobs. |

Resolution classes are width-based (cropped heights like 1920×800 are
common): ≥3000 px → `2160p`, ≥1700 → `1080p`, ≥1200 → `720p`, else `sd`.

**Honest framing of jobs/hr:** encode-hours sum *per-job* wall clock, so
jobs/hr is per-worker-slot throughput, not fleet calendar throughput. Five
workers each doing 1 job/hr is still reported as 1 job/hr — multiply by
your worker count for fleet capacity.

## Content classes

Numbers are only comparable within a content class. The fleet's libraries
map roughly to:

- **Movies** — long-form, mostly film grain; the friendliest content.
- **TV** — episodic broadcast/streaming masters; mid.
- **Anime** — flat regions + line art; compresses hardest of the three.
- **Reality/handheld TV** — sensor noise and fast cuts; the known
  worst case for both compression and the VMAF gate (see the `library`
  column on job rows if you need to split a report by class — the
  grouped report does not slice by library yet).

## Reproducing a report

```bash
# Grouped report (markdown to stdout; optionally write files)
uv run python -m scripts.bench.report --db-url "$TF_DB_URL" \
    --json report.json --markdown report.md

# $/100GB — feed a group's gb_in_per_hour into the economics model
uv run python -m scripts.bench.economics --plan dedicated-8gb \
    --gb-per-hour <gb_in_per_hour from the report> [--object-storage]
```

`--db-url` accepts the app's URL conventions (`sqlite:///path.db` or
`postgresql://…?sslmode=require`) and falls back to the `TF_DB_URL` env
var. Plan presets are Linode Dedicated CPU list prices as of **2026-07**;
update them in `scripts/bench/economics.py` if Linode reprices.

## A/B: VMAF gate on vs off

The gate's cost (extra VMAF passes + skipped encodes) is measured on a
reserved slice of jobs run twice:

1. Pick a reserved test slice (job IDs of already-terminal jobs).
2. `uv run python -m scripts.bench.ab_gate --db-url … --mode gate-on --ids ID1,ID2,…`
   — prints the current state and the `UPDATE` SQL to re-stamp the slice
   `target_vmaf = 97` and re-queue it. **The tool never executes the
   UPDATE** — review the SQL and run it yourself (psql / sqlite3).
3. Let the fleet drain the slice; note the IDs.
4. Repeat with `--mode gate-off` (stamps `target_vmaf = NULL`) on the
   twin slice.
5. Diff the two arms:

```bash
uv run python -m scripts.bench.report --db-url "$TF_DB_URL" \
    --compare gate_on:ID1,ID2 --compare gate_off:ID3,ID4
```

## Caveats

- **Linode-instance numbers are pending.** Everything measured so far
  comes from the homelab fleet (Skylake-era i5-6500T LXC workers + the
  Docker workers on the media server). Do not present those as cloud
  numbers; the placeholder table below is filled from real Linode runs.
- **CPU vs QSV timing is not comparable.** QSV rows measure fixed-function
  hardware; software x265/SVT-AV1 rows measure cores and preset choice. A
  Linode Dedicated CPU plan only ever produces software rows.
- **VMAF is only present where the gate ran** — jobs queued without a
  target (`target_vmaf` NULL) have no `achieved_vmaf`, so quality columns
  under-cover gate-off slices by construction.
- Wall clock includes the whole 8-step pipeline (probe, CRF search when
  enabled, encode, verify, VMAF, swap), not just ffmpeg.
- GB are decimal (10⁹ bytes) to match cloud pricing units.

## Results

Filled in from real runs — the harness never invents numbers.

### Linode Dedicated CPU (pending)

| plan | codec | class | jobs/hr | GB-in/hr | saved % | VMAF mean/min/p5 | $/100GB |
|---|---|---|---|---|---|---|---|
| dedicated-8gb | hevc | 1080p | TBD | TBD | TBD | TBD | TBD |
| dedicated-8gb | av1 | 1080p | TBD | TBD | TBD | TBD | TBD |
| dedicated-16gb | hevc | 1080p | TBD | TBD | TBD | TBD | TBD |
| dedicated-16gb | av1 | 1080p | TBD | TBD | TBD | TBD | TBD |

### VMAF gate A/B (pending)

| slice | jobs/hr | GB-in/hr | saved % | skip % | VMAF mean/min/p5 |
|---|---|---|---|---|---|
| gate_on (target 97) | TBD | TBD | TBD | TBD | TBD |
| gate_off | TBD | TBD | TBD | TBD | — |

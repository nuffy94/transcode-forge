"""Pure metric computation over job rows — no I/O.

All functions take plain mappings (one per jobs-table row) and return
numbers. Timestamps are the app's ISO-8601 TEXT columns; sizes are bytes.
"Decimal GB" (1e9 bytes) is used throughout so throughput feeds directly
into the $/100GB economics model.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

GB = 1_000_000_000  # decimal gigabyte (matches cloud pricing units)

JobRow = Mapping[str, Any]


@dataclass(frozen=True)
class GroupMetrics:
    """Aggregate metrics for one set of terminal job rows."""

    jobs_total: int
    jobs_complete: int
    jobs_skipped: int
    jobs_failed: int
    skip_rate: float
    fail_rate: float
    # Throughput — over completed jobs with parseable started/completed stamps.
    encode_hours: float | None
    jobs_per_hour: float | None
    gb_in_per_hour: float | None
    # Compression — aggregate (sum space_saved / sum source_size), completed jobs.
    compression_pct: float | None
    # Achieved-VMAF distribution — any terminal row where achieved_vmaf is set.
    vmaf_mean: float | None
    vmaf_min: float | None
    vmaf_p5: float | None
    # Wall-clock (completed_at - started_at) percentiles, seconds, completed jobs.
    wall_clock_p50_s: float | None
    wall_clock_p90_s: float | None
    wall_clock_p95_s: float | None


def resolution_class(source_resolution: str | None) -> str:
    """Bucket a WxH string into a resolution class.

    Width-based, because cropped heights (e.g. 1920x800 scope movies) are
    common — width is the stable axis.
    """
    if not source_resolution:
        return "unknown"
    try:
        width = int(source_resolution.lower().split("x")[0])
    except (ValueError, IndexError):
        return "unknown"
    if width >= 3000:
        return "2160p"
    if width >= 1700:
        return "1080p"
    if width >= 1200:
        return "720p"
    return "sd"


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse an ISO-8601 TEXT column; naive stamps are assumed UTC."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def wall_clock_seconds(row: JobRow) -> float | None:
    """completed_at - started_at in seconds, or None if unparseable."""
    started = parse_timestamp(row.get("started_at"))
    completed = parse_timestamp(row.get("completed_at"))
    if started is None or completed is None:
        return None
    seconds = (completed - started).total_seconds()
    return seconds if seconds >= 0 else None


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Linear-interpolation percentile (PERCENTILE.INC convention)."""
    if not values:
        return None
    xs = sorted(values)
    k = (len(xs) - 1) * pct / 100.0
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return xs[int(k)]
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def _throughput(
    completed: Sequence[JobRow],
) -> tuple[float | None, float | None, float | None, list[float]]:
    """(encode_hours, jobs/hr, GB-in/hr, per-job durations) over completed rows."""
    durations: list[float] = []
    gb_in = 0.0
    for row in completed:
        seconds = wall_clock_seconds(row)
        if seconds is None:
            continue
        durations.append(seconds)
        gb_in += (row.get("source_size") or 0) / GB
    hours = sum(durations) / 3600.0
    if hours <= 0:
        return None, None, None, durations
    return hours, len(durations) / hours, gb_in / hours, durations


def _compression_pct(completed: Sequence[JobRow]) -> float | None:
    """Aggregate space saved as a % of source bytes (size-weighted)."""
    measured = [row for row in completed if row.get("space_saved") is not None]
    source = float(sum(row["source_size"] for row in measured))
    saved = float(sum(row["space_saved"] for row in measured))
    if not source:
        return None
    return saved / source * 100.0


def compute_metrics(rows: Sequence[JobRow]) -> GroupMetrics:
    """Aggregate one group (or slice) of terminal job rows."""
    total = len(rows)
    completed = [r for r in rows if r["status"] == "complete"]
    skipped = sum(1 for r in rows if r["status"] == "skipped")
    failed = sum(1 for r in rows if r["status"] == "failed")

    encode_hours, jobs_per_hour, gb_in_per_hour, durations = _throughput(completed)
    vmaf = [r["achieved_vmaf"] for r in rows if r.get("achieved_vmaf") is not None]

    return GroupMetrics(
        jobs_total=total,
        jobs_complete=len(completed),
        jobs_skipped=skipped,
        jobs_failed=failed,
        skip_rate=skipped / total if total else 0.0,
        fail_rate=failed / total if total else 0.0,
        encode_hours=encode_hours,
        jobs_per_hour=jobs_per_hour,
        gb_in_per_hour=gb_in_per_hour,
        compression_pct=_compression_pct(completed),
        vmaf_mean=sum(vmaf) / len(vmaf) if vmaf else None,
        vmaf_min=min(vmaf) if vmaf else None,
        vmaf_p5=percentile(vmaf, 5),
        wall_clock_p50_s=percentile(durations, 50),
        wall_clock_p90_s=percentile(durations, 90),
        wall_clock_p95_s=percentile(durations, 95),
    )

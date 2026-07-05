"""Benchmark report over the jobs table — throughput, compression, quality.

Reads a Transcode Forge database (same URL conventions as TF_DB_URL:
``sqlite:///path.db`` or ``postgresql://...``) and emits a markdown and/or
JSON report grouped by (target_codec, backend_used, resolution class).
Pure analysis — no ffmpeg, no writes to the jobs table.

Usage:
    uv run python -m scripts.bench.report --db-url sqlite:///forge.db
    uv run python -m scripts.bench.report --json report.json --markdown report.md
    uv run python -m scripts.bench.report \
        --compare gate_on:JOBID1,JOBID2 --compare gate_off:JOBID3,JOBID4

--db-url falls back to the TF_DB_URL environment variable. With two
--compare slices the grouped report is replaced by a side-by-side diff
of the two labeled job-ID sets (the A/B gate protocol — see
scripts/bench/ab_gate.py and docs/BENCHMARKS.md).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.bench.metrics import GroupMetrics, JobRow, compute_metrics, resolution_class

TERMINAL_STATUSES = ("complete", "skipped", "failed")

_JOB_COLUMNS = (
    "id, status, library, source_resolution, source_size, output_size, "
    "space_saved, target_codec, quality_value, target_vmaf, resolved_crf, "
    "achieved_vmaf, backend_used, created_at, started_at, completed_at"
)


# ── Data access (read-only) ───────────────────────────────────────────


async def fetch_jobs(db_url: str, ids: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Fetch terminal job rows (optionally restricted to a set of IDs)."""
    from transcode_forge.db import init_db

    db = await init_db(db_url)
    try:
        placeholders = ", ".join("?" for _ in TERMINAL_STATUSES)
        sql = f"SELECT {_JOB_COLUMNS} FROM jobs WHERE status IN ({placeholders})"
        params: list[Any] = list(TERMINAL_STATUSES)
        if ids is not None:
            sql += f" AND id IN ({', '.join('?' for _ in ids)})"
            params.extend(ids)
        async with db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        await db.close()


# ── Grouped report ────────────────────────────────────────────────────


def group_rows(rows: Sequence[JobRow]) -> dict[tuple[str, str, str], list[JobRow]]:
    """Group rows by (target_codec, backend_used, resolution class)."""
    groups: dict[tuple[str, str, str], list[JobRow]] = {}
    for row in rows:
        key = (
            row.get("target_codec") or "unknown",
            row.get("backend_used") or "unknown",
            resolution_class(row.get("source_resolution")),
        )
        groups.setdefault(key, []).append(row)
    return dict(sorted(groups.items()))


def build_report(rows: Sequence[JobRow]) -> dict[str, Any]:
    """JSON-able grouped report."""
    groups = [
        {
            "target_codec": codec,
            "backend": backend,
            "resolution_class": res_class,
            "metrics": asdict(compute_metrics(group)),
        }
        for (codec, backend, res_class), group in group_rows(rows).items()
    ]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "terminal_jobs": len(rows),
        "groups": groups,
    }


def _fmt(value: float | int | None, nd: int = 2) -> str:
    if value is None:
        return "-"
    if isinstance(value, int):
        return str(value)
    return f"{value:.{nd}f}"


def _group_line(group: dict[str, Any]) -> str:
    m = group["metrics"]
    cells = (
        group["target_codec"],
        group["backend"],
        group["resolution_class"],
        _fmt(m["jobs_total"]),
        _fmt(m["jobs_complete"]),
        _fmt(m["skip_rate"] * 100, 1),
        _fmt(m["fail_rate"] * 100, 1),
        _fmt(m["jobs_per_hour"]),
        _fmt(m["gb_in_per_hour"]),
        _fmt(m["compression_pct"], 1),
        f"{_fmt(m['vmaf_mean'])}/{_fmt(m['vmaf_min'])}/{_fmt(m['vmaf_p5'])}",
        (
            f"{_fmt(m['wall_clock_p50_s'], 0)}/{_fmt(m['wall_clock_p90_s'], 0)}"
            f"/{_fmt(m['wall_clock_p95_s'], 0)}"
        ),
    )
    return "| " + " | ".join(cells) + " |"


def to_markdown(report: dict[str, Any]) -> str:
    """Render a grouped report as a markdown table."""
    lines = [
        "# Transcode Forge benchmark report",
        "",
        f"Generated: {report['generated_at']} — {report['terminal_jobs']} terminal job(s)",
        "",
        "| codec | backend | class | jobs | done | skip% | fail% | jobs/hr "
        "| GB-in/hr | saved% | VMAF mean/min/p5 | wall p50/p90/p95 (s) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
        *(_group_line(group) for group in report["groups"]),
    ]
    return "\n".join(lines) + "\n"


# ── A/B slice comparison ──────────────────────────────────────────────


def build_comparison(
    label_a: str,
    rows_a: Sequence[JobRow],
    label_b: str,
    rows_b: Sequence[JobRow],
) -> dict[str, Any]:
    """Side-by-side metrics for two labeled slices, with B-minus-A deltas."""
    metrics_a = asdict(compute_metrics(rows_a))
    metrics_b = asdict(compute_metrics(rows_b))
    deltas = {
        field.name: (
            metrics_b[field.name] - metrics_a[field.name]
            if metrics_a[field.name] is not None and metrics_b[field.name] is not None
            else None
        )
        for field in fields(GroupMetrics)
    }
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "slices": {label_a: metrics_a, label_b: metrics_b},
        "delta_direction": f"{label_b} - {label_a}",
        "deltas": deltas,
    }


def comparison_to_markdown(comparison: dict[str, Any]) -> str:
    """Render a slice comparison as a markdown table."""
    label_a, label_b = comparison["slices"].keys()
    metrics_a, metrics_b = comparison["slices"].values()
    lines = [
        f"# A/B comparison: {label_a} vs {label_b}",
        "",
        f"Generated: {comparison['generated_at']} — delta = {comparison['delta_direction']}",
        "",
        f"| metric | {label_a} | {label_b} | delta |",
        "|---|---|---|---|",
    ]
    for field in fields(GroupMetrics):
        name = field.name
        delta = comparison["deltas"][name]
        lines.append(
            f"| {name} | {_fmt(metrics_a[name])} | {_fmt(metrics_b[name])} | {_fmt(delta)} |"
        )
    return "\n".join(lines) + "\n"


# ── CLI ───────────────────────────────────────────────────────────────


def _parse_slice(spec: str) -> tuple[str, list[str]]:
    """Parse a ``label:id1,id2,...`` --compare argument."""
    label, sep, ids_part = spec.partition(":")
    ids = [i.strip() for i in ids_part.split(",") if i.strip()]
    if not sep or not label or not ids:
        raise argparse.ArgumentTypeError(f"--compare expects 'label:id1,id2,...', got {spec!r}")
    return label, ids


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts.bench.report",
        description="Benchmark report over the jobs table — throughput, compression, quality.",
    )
    parser.add_argument(
        "--db-url",
        default=os.environ.get("TF_DB_URL"),
        help="database URL (default: TF_DB_URL env var)",
    )
    parser.add_argument("--json", metavar="PATH", help="write the JSON report here")
    parser.add_argument("--markdown", metavar="PATH", help="write the markdown report here")
    parser.add_argument(
        "--compare",
        metavar="LABEL:IDS",
        type=_parse_slice,
        action="append",
        help="labeled job-ID slice ('gate_on:id1,id2'); give exactly twice to diff two slices",
    )
    return parser


async def _run(args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    """Build (report_dict, markdown) for either mode."""
    if args.compare:
        (label_a, ids_a), (label_b, ids_b) = args.compare
        rows_a = await fetch_jobs(args.db_url, ids_a)
        rows_b = await fetch_jobs(args.db_url, ids_b)
        for label, ids, rows in ((label_a, ids_a, rows_a), (label_b, ids_b, rows_b)):
            if len(rows) < len(ids):
                found = {row["id"] for row in rows}
                missing = [i for i in ids if i not in found]
                print(
                    f"warning: slice {label!r}: {len(missing)} id(s) not terminal "
                    f"or not found: {', '.join(missing)}",
                    file=sys.stderr,
                )
        comparison = build_comparison(label_a, rows_a, label_b, rows_b)
        return comparison, comparison_to_markdown(comparison)
    rows = await fetch_jobs(args.db_url)
    report = build_report(rows)
    return report, to_markdown(report)


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.db_url:
        print("error: no database URL (pass --db-url or set TF_DB_URL)", file=sys.stderr)
        return 2
    if args.compare and len(args.compare) != 2:
        print("error: --compare must be given exactly twice (two slices)", file=sys.stderr)
        return 2

    report, markdown = asyncio.run(_run(args))
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.markdown:
        Path(args.markdown).write_text(markdown, encoding="utf-8")
    print(markdown, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

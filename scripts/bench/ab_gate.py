"""A/B VMAF-gate re-stamp helper — SELECT/plan-only, never mutates.

Given a DB URL and a reserved slice of job IDs, this tool:

1. SELECTs the current state of those jobs and prints a preview.
2. Prints the UPDATE SQL that would re-stamp the slice gate-on
   (``target_vmaf = 97``) or gate-off (``target_vmaf = NULL``) and
   re-queue it (status back to pending, worker/outcome fields cleared —
   the same reset the app's retry endpoint performs, plus outcome
   columns so a re-run can't inherit stale results).

IT NEVER EXECUTES THE UPDATE. The operator reviews the SQL and runs it
by hand (psql / sqlite3). After both arms have run, diff them with:

    uv run python -m scripts.bench.report \
        --compare gate_on:ID,ID --compare gate_off:ID,ID

Usage:
    uv run python -m scripts.bench.ab_gate --db-url ... --mode gate-on --ids ID1,ID2
    uv run python -m scripts.bench.ab_gate --db-url ... --mode gate-off --ids ID1,ID2
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

GATE_ON_TARGET_VMAF = 97.0

# Job IDs are UUIDs; anything else is a typo (or an injection attempt).
_ID_RE = re.compile(r"^[A-Za-z0-9-]+$")


def _quoted_ids(job_ids: Sequence[str]) -> str:
    """Validate and single-quote job IDs for an IN (...) list."""
    if not job_ids:
        raise ValueError("no job IDs given")
    for job_id in job_ids:
        if not _ID_RE.match(job_id):
            raise ValueError(f"invalid job ID {job_id!r} (expected UUID-like)")
    return ", ".join(f"'{job_id}'" for job_id in job_ids)


def build_requeue_sql(
    job_ids: Sequence[str],
    mode: str,
    *,
    target_vmaf: float = GATE_ON_TARGET_VMAF,
    now: str | None = None,
) -> str:
    """Return the UPDATE statement (as text) for the operator to run.

    ``mode`` is ``gate-on`` (stamp target_vmaf) or ``gate-off`` (NULL it).
    Dialect-neutral SQL — valid on both SQLite and PostgreSQL.
    """
    if mode == "gate-on":
        vmaf_value = str(target_vmaf)
    elif mode == "gate-off":
        vmaf_value = "NULL"
    else:
        raise ValueError(f"mode must be 'gate-on' or 'gate-off', got {mode!r}")
    stamp = now or datetime.now(UTC).isoformat()
    return (
        f"-- transcode-forge A/B slice re-stamp ({mode}): review, then run by hand.\n"
        f"-- Generated {stamp}; {len(job_ids)} job(s). This tool never executes it.\n"
        "UPDATE jobs SET\n"
        f"    target_vmaf = {vmaf_value},\n"
        "    status = 'pending',\n"
        "    worker_id = NULL,\n"
        "    progress = 0,\n"
        "    error_message = NULL,\n"
        "    resolved_crf = NULL,\n"
        "    achieved_vmaf = NULL,\n"
        "    backend_used = NULL,\n"
        "    output_size = NULL,\n"
        "    space_saved = NULL,\n"
        "    started_at = NULL,\n"
        "    completed_at = NULL,\n"
        f"    updated_at = '{stamp}'\n"
        f"WHERE id IN ({_quoted_ids(job_ids)});\n"
    )


async def _fetch_preview(db_url: str, job_ids: Sequence[str]) -> list[dict[str, Any]]:
    """SELECT the slice's current state (the tool's only DB access)."""
    from transcode_forge.db import init_db

    db = await init_db(db_url)
    try:
        placeholders = ", ".join("?" for _ in job_ids)
        async with db.execute(
            "SELECT id, status, target_vmaf, achieved_vmaf, source_path "
            f"FROM jobs WHERE id IN ({placeholders})",
            list(job_ids),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        await db.close()


def _print_preview(rows: Sequence[dict[str, Any]]) -> None:
    print("Current state of the slice:")
    for row in rows:
        print(
            f"  {row['id']}  status={row['status']}  target_vmaf={row['target_vmaf']}  "
            f"achieved_vmaf={row['achieved_vmaf']}  {row['source_path']}"
        )
    print()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts.bench.ab_gate",
        description="Emit (never execute) the SQL to re-stamp an A/B job slice.",
    )
    parser.add_argument(
        "--db-url",
        default=os.environ.get("TF_DB_URL"),
        help="database URL (default: TF_DB_URL env var); used for a read-only preview",
    )
    parser.add_argument("--mode", required=True, choices=("gate-on", "gate-off"))
    parser.add_argument("--ids", required=True, help="comma-separated job IDs (the slice)")
    args = parser.parse_args(argv)

    if not args.db_url:
        print("error: no database URL (pass --db-url or set TF_DB_URL)", file=sys.stderr)
        return 2
    job_ids = [i.strip() for i in args.ids.split(",") if i.strip()]
    try:
        sql = build_requeue_sql(job_ids, args.mode)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    rows = asyncio.run(_fetch_preview(args.db_url, job_ids))
    found = {row["id"] for row in rows}
    missing = [i for i in job_ids if i not in found]
    if missing:
        print(
            f"error: {len(missing)} job ID(s) not found in this DB: {', '.join(missing)}",
            file=sys.stderr,
        )
        return 1

    _print_preview(rows)
    print(sql, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

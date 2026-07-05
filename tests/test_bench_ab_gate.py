"""A/B gate re-stamp helper (scripts/bench/ab_gate.py) — SQL generation only."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from scripts.bench.ab_gate import GATE_ON_TARGET_VMAF, build_requeue_sql, main
from transcode_forge.db import init_db

_NOW = "2026-07-04T12:00:00+00:00"


def test_gate_on_sql() -> None:
    sql = build_requeue_sql(["a1", "b2"], "gate-on", now=_NOW)
    assert sql.startswith("--")  # review banner
    assert f"target_vmaf = {GATE_ON_TARGET_VMAF}" in sql
    assert "status = 'pending'" in sql
    assert "worker_id = NULL" in sql
    assert "achieved_vmaf = NULL" in sql  # outcome cleared so re-runs are clean
    assert f"updated_at = '{_NOW}'" in sql
    assert "WHERE id IN ('a1', 'b2');" in sql


def test_gate_off_sql() -> None:
    sql = build_requeue_sql(["a1"], "gate-off", now=_NOW)
    assert "target_vmaf = NULL" in sql
    assert "WHERE id IN ('a1');" in sql


def test_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="invalid job ID"):
        build_requeue_sql(["x'; DROP TABLE jobs; --"], "gate-on")
    with pytest.raises(ValueError, match="no job IDs"):
        build_requeue_sql([], "gate-on")
    with pytest.raises(ValueError, match="mode"):
        build_requeue_sql(["a1"], "gate-sideways")


# ── CLI: previews via SELECT, prints SQL, never mutates ───────────────


async def _seed(db_url: str) -> None:
    db = await init_db(db_url)
    try:
        for job_id in ("j1", "j2"):
            await db.execute(
                """INSERT INTO jobs (id, source_path, library, source_codec, target_codec,
                       quality_value, status, target_vmaf, achieved_vmaf,
                       created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id,
                    f"/m/{job_id}.mkv",
                    "Movies",
                    "h264",
                    "hevc",
                    21,
                    "complete",
                    95.0,
                    96.2,
                    _NOW,
                    _NOW,
                ),
            )
        await db.commit()
    finally:
        await db.close()


async def _job_states(db_url: str) -> list[tuple[Any, ...]]:
    db = await init_db(db_url)
    try:
        async with db.execute(
            "SELECT id, status, target_vmaf, updated_at FROM jobs ORDER BY id"
        ) as cursor:
            rows = await cursor.fetchall()
        return [tuple(row) for row in rows]
    finally:
        await db.close()


@pytest.fixture
def seeded_db_url(tmp_path: Path) -> str:
    url = f"sqlite:///{tmp_path / 'ab.db'}"
    asyncio.run(_seed(url))
    return url


def test_cli_prints_sql_without_mutating(
    seeded_db_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    before = asyncio.run(_job_states(seeded_db_url))

    rc = main(["--db-url", seeded_db_url, "--mode", "gate-off", "--ids", "j1,j2"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Current state of the slice:" in out
    assert "j1" in out and "j2" in out
    assert "UPDATE jobs SET" in out
    assert "target_vmaf = NULL" in out

    # The one thing this tool must guarantee: the DB is untouched.
    assert asyncio.run(_job_states(seeded_db_url)) == before
    assert before[0][1] == "complete"
    assert before[0][2] == 95.0


def test_cli_errors_on_unknown_id(seeded_db_url: str, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--db-url", seeded_db_url, "--mode", "gate-on", "--ids", "j1,ghost"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "ghost" in captured.err
    assert "UPDATE jobs" not in captured.out  # no SQL for a bad slice


def test_cli_rejects_injection_id(seeded_db_url: str, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--db-url", seeded_db_url, "--mode", "gate-on", "--ids", "j1;DROP"])
    assert rc == 2
    assert "invalid job ID" in capsys.readouterr().err


def test_cli_requires_db_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TF_DB_URL", raising=False)
    assert main(["--mode", "gate-on", "--ids", "j1"]) == 2
    assert "no database URL" in capsys.readouterr().err

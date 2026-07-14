"""Benchmark report (scripts/bench) — metrics + grouped report + A/B compare."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.bench.metrics import (
    parse_timestamp,
    percentile,
    resolution_class,
)
from scripts.bench.report import (
    build_comparison,
    build_report,
    comparison_to_markdown,
    fetch_jobs,
    main,
    to_markdown,
)
from transcode_forge.db import init_db

_T0 = "2026-07-01T10:00:00+00:00"


def _end(seconds: int) -> str:
    return f"2026-07-01T{10 + seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:00+00:00"


async def _insert_job(db: Any, job_id: str, **overrides: Any) -> None:
    row = {
        "id": job_id,
        "source_path": f"/media/{job_id}.mkv",
        "library": "Movies",
        "source_codec": "h264",
        "source_resolution": "1920x1080",
        "source_size": 4_000_000_000,
        "target_codec": "hevc",
        "quality_value": 21,
        "status": "complete",
        "output_size": None,
        "space_saved": None,
        "target_vmaf": 95.0,
        "resolved_crf": 22,
        "achieved_vmaf": None,
        "backend_used": "qsv",
        "created_at": "2026-07-01T09:00:00+00:00",
        "started_at": _T0,
        "completed_at": None,
        "updated_at": _T0,
    }
    row.update(overrides)
    columns = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    await db.execute(f"INSERT INTO jobs ({columns}) VALUES ({placeholders})", list(row.values()))
    await db.commit()


async def _seed(db_url: str) -> None:
    """Schema via the app's migrations runner, then a small known dataset."""
    db = await init_db(db_url)
    try:
        # Group (hevc, qsv, 1080p): 3 complete + 1 skipped + 1 failed.
        for job_id, size, saved, vmaf, secs in (
            ("j1", 4_000_000_000, 2_000_000_000, 96.0, 1800),
            ("j2", 6_000_000_000, 3_000_000_000, 94.0, 3600),
            ("j3", 10_000_000_000, 5_000_000_000, 98.0, 5400),
        ):
            await _insert_job(
                db,
                job_id,
                source_size=size,
                output_size=size - saved,
                space_saved=saved,
                achieved_vmaf=vmaf,
                completed_at=_end(secs),
            )
        await _insert_job(db, "j4", status="skipped", achieved_vmaf=91.0, completed_at=_end(1200))
        await _insert_job(db, "j5", status="failed", started_at=None)
        # Group (av1, cpu, 2160p): 1 complete.
        await _insert_job(
            db,
            "j6",
            target_codec="av1",
            backend_used="cpu",
            source_resolution="3840x2160",
            source_size=8_000_000_000,
            output_size=2_000_000_000,
            space_saved=6_000_000_000,
            achieved_vmaf=95.5,
            completed_at=_end(7200),
        )
        # Non-terminal: must never appear in a report.
        await _insert_job(db, "j7", status="pending", started_at=None)
        # Failed before a backend was chosen: lands in the "unknown" backend group.
        await _insert_job(db, "j8", status="failed", backend_used=None, started_at=None)
    finally:
        await db.close()


@pytest.fixture
def seeded_db_url(tmp_path: Path) -> str:
    url = f"sqlite:///{tmp_path / 'bench.db'}"
    asyncio.run(_seed(url))
    return url


# ── metrics.py primitives ─────────────────────────────────────────────


def test_resolution_class_buckets() -> None:
    assert resolution_class("3840x2160") == "2160p"
    assert resolution_class("1920x1080") == "1080p"
    assert resolution_class("1920x800") == "1080p"  # cropped scope film
    assert resolution_class("1280x720") == "720p"
    assert resolution_class("720x480") == "sd"
    assert resolution_class(None) == "unknown"
    assert resolution_class("garbage") == "unknown"


def test_percentile_interpolates() -> None:
    assert percentile([], 50) is None
    assert percentile([7.0], 95) == 7.0
    assert percentile([1800.0, 3600.0, 5400.0], 50) == 3600.0
    assert percentile([1800.0, 3600.0, 5400.0], 90) == pytest.approx(5040.0)
    assert percentile([91.0, 94.0, 96.0, 98.0], 5) == pytest.approx(91.45)


def test_parse_timestamp_assumes_utc_when_naive() -> None:
    aware = parse_timestamp("2026-07-01T10:00:00+00:00")
    naive = parse_timestamp("2026-07-01T10:00:00")
    assert aware is not None and naive is not None
    assert aware == naive
    assert parse_timestamp(None) is None
    assert parse_timestamp("not-a-date") is None


# ── grouped report against the seeded DB ──────────────────────────────


async def test_fetch_jobs_excludes_non_terminal(seeded_db_url: str) -> None:
    rows = await fetch_jobs(seeded_db_url)
    ids = {row["id"] for row in rows}
    assert "j7" not in ids  # pending
    assert ids == {"j1", "j2", "j3", "j4", "j5", "j6", "j8"}


async def test_compression_derived_from_sizes_for_s3_rows(tmp_path: Path) -> None:
    """Regression (S4b bench): S3 jobs record space_saved=0 — masters are
    never replaced, nothing is 'reclaimed' — so a 66%-compression GPU arm
    reported saved% 0.0. Compression must derive from source vs output
    sizes whenever they exist; space_saved is only the fallback."""
    from transcode_forge.db import close_db

    db_url = f"sqlite:///{tmp_path / 'r.db'}"
    db = await init_db(db_url)
    try:
        await _insert_job(
            db,
            "s3a",
            backend_used="nvenc",
            source_size=1_000_000_000,
            output_size=400_000_000,
            space_saved=0,
            achieved_vmaf=95.0,
            completed_at=_end(600),
        )
    finally:
        await close_db(db)

    rows = await fetch_jobs(db_url)
    report = build_report(rows)
    m = report["groups"][0]["metrics"]
    assert m["compression_pct"] == pytest.approx(60.0)


async def test_group_metrics(seeded_db_url: str) -> None:
    rows = await fetch_jobs(seeded_db_url)
    report = build_report(rows)
    groups = {
        (g["target_codec"], g["backend"], g["resolution_class"]): g["metrics"]
        for g in report["groups"]
    }
    assert set(groups) == {
        ("hevc", "qsv", "1080p"),
        ("av1", "cpu", "2160p"),
        ("hevc", "unknown", "1080p"),
    }

    m = groups[("hevc", "qsv", "1080p")]
    assert m["jobs_total"] == 5
    assert m["jobs_complete"] == 3
    assert m["skip_rate"] == pytest.approx(0.2)
    assert m["fail_rate"] == pytest.approx(0.2)
    # 1800 + 3600 + 5400 s = 3.0 encode-hours; 20 GB in.
    assert m["encode_hours"] == pytest.approx(3.0)
    assert m["jobs_per_hour"] == pytest.approx(1.0)
    assert m["gb_in_per_hour"] == pytest.approx(20.0 / 3.0)
    assert m["compression_pct"] == pytest.approx(50.0)
    # VMAF over complete + gate-skipped rows: [96, 94, 98, 91].
    assert m["vmaf_mean"] == pytest.approx(94.75)
    assert m["vmaf_min"] == pytest.approx(91.0)
    assert m["vmaf_p5"] == pytest.approx(91.45)
    assert m["wall_clock_p50_s"] == pytest.approx(3600.0)
    assert m["wall_clock_p90_s"] == pytest.approx(5040.0)
    assert m["wall_clock_p95_s"] == pytest.approx(5220.0)

    av1 = groups[("av1", "cpu", "2160p")]
    assert av1["jobs_per_hour"] == pytest.approx(0.5)
    assert av1["gb_in_per_hour"] == pytest.approx(4.0)
    assert av1["compression_pct"] == pytest.approx(75.0)

    unknown = groups[("hevc", "unknown", "1080p")]
    assert unknown["jobs_total"] == 1
    assert unknown["fail_rate"] == pytest.approx(1.0)
    assert unknown["jobs_per_hour"] is None  # nothing completed


async def test_markdown_render(seeded_db_url: str) -> None:
    rows = await fetch_jobs(seeded_db_url)
    markdown = to_markdown(build_report(rows))
    assert "# Transcode Forge benchmark report" in markdown
    assert (
        "| hevc | qsv | 1080p | 5 | 3 | 20.0 | 20.0 | 1.00 | 6.67 | 50.0 "
        "| 94.75/91.00/91.45 | 3600/5040/5220 |" in markdown
    )


# ── A/B slice comparison ──────────────────────────────────────────────


async def test_compare_slices(seeded_db_url: str) -> None:
    rows_a = await fetch_jobs(seeded_db_url, ["j1", "j2"])
    rows_b = await fetch_jobs(seeded_db_url, ["j3", "j4"])
    comparison = build_comparison("gate_on", rows_a, "gate_off", rows_b)

    a = comparison["slices"]["gate_on"]
    b = comparison["slices"]["gate_off"]
    assert a["jobs_complete"] == 2
    assert a["jobs_per_hour"] == pytest.approx(2 / 1.5)
    assert b["jobs_complete"] == 1
    assert b["skip_rate"] == pytest.approx(0.5)
    assert comparison["deltas"]["jobs_per_hour"] == pytest.approx(1 / 1.5 - 2 / 1.5)
    assert comparison["deltas"]["jobs_total"] == 0

    markdown = comparison_to_markdown(comparison)
    assert "# A/B comparison: gate_on vs gate_off" in markdown
    assert "| metric | gate_on | gate_off | delta |" in markdown


# ── CLI ───────────────────────────────────────────────────────────────


def test_cli_writes_json_and_markdown(
    seeded_db_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    json_path = tmp_path / "report.json"
    md_path = tmp_path / "report.md"
    rc = main(["--db-url", seeded_db_url, "--json", str(json_path), "--markdown", str(md_path)])
    assert rc == 0
    assert "| hevc | qsv | 1080p |" in capsys.readouterr().out
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["terminal_jobs"] == 7
    assert md_path.read_text(encoding="utf-8").startswith("# Transcode Forge")


def test_cli_compare_mode(seeded_db_url: str, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(
        [
            "--db-url",
            seeded_db_url,
            "--compare",
            "gate_on:j1,j2",
            "--compare",
            "gate_off:j3,j4",
        ]
    )
    assert rc == 0
    assert "# A/B comparison: gate_on vs gate_off" in capsys.readouterr().out


def test_cli_compare_warns_on_missing_ids(
    seeded_db_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(
        [
            "--db-url",
            seeded_db_url,
            "--compare",
            "gate_on:j1,nope",
            "--compare",
            "gate_off:j3",
        ]
    )
    assert rc == 0
    assert "nope" in capsys.readouterr().err


def test_cli_requires_db_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TF_DB_URL", raising=False)
    assert main([]) == 2
    assert "no database URL" in capsys.readouterr().err


def test_cli_compare_needs_two_slices(
    seeded_db_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["--db-url", seeded_db_url, "--compare", "gate_on:j1"])
    assert rc == 2
    assert "exactly twice" in capsys.readouterr().err

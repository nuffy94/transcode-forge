"""One release rule, enforced (ledger R-020).

``jobs.claim_token`` is the identity of the attempt that owns a job: a
claim stamps a fresh value, and a report is accepted only while the token
it carries still matches the row. That is only true if EVERY release
clears the token along with the owner. A release that nulls ``worker_id``
and leaves the token behind hands the next attempt a token a stale report
could still match.

The obligation is machine-checked here, in the style of
``tests/test_job_status_vocabulary.py``: nothing under ``src/`` may
release a job's owner without releasing its attempt identity, whether it
writes the SQL itself or goes through ``update_job``.
"""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "transcode_forge"

# `worker_id = NULL` inside a SQL string, and `worker_id=None` as a
# keyword to the generic update_job. Both are releases.
_SQL_RELEASE = re.compile(r"worker_id\s*=\s*NULL", re.IGNORECASE)
_KWARG_RELEASE = re.compile(r"worker_id\s*=\s*None")
_SQL_CLEARS = re.compile(r"claim_token\s*=\s*NULL", re.IGNORECASE)
_KWARG_CLEARS = re.compile(r"claim_token\s*=\s*None")

# How far from the release line the matching clear may sit. A statement is
# built over a handful of adjacent lines; anything further apart is a
# different statement and would not be a clear at all.
_WINDOW = 6


def _python_sources() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _offenders(pattern: re.Pattern[str], clears: re.Pattern[str]) -> list[str]:
    found: list[str] = []
    for path in _python_sources():
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if not pattern.search(line):
                continue
            window = "\n".join(lines[max(0, i - _WINDOW) : i + _WINDOW + 1])
            if not clears.search(window):
                found.append(f"{path.relative_to(SRC)}:{i + 1}: {line.strip()}")
    return found


def test_every_release_clears_the_claim_token():
    offenders = _offenders(_SQL_RELEASE, _SQL_CLEARS) + _offenders(_KWARG_RELEASE, _KWARG_CLEARS)
    assert not offenders, (
        "These releases null worker_id but leave claim_token set, so a report "
        "from the released attempt can still match the next claim:\n  " + "\n  ".join(offenders)
    )


def test_the_guard_would_catch_a_regression(tmp_path: Path, monkeypatch):
    """The guard is only worth having if it fails on the shape it forbids."""
    bad = tmp_path / "transcode_forge"
    bad.mkdir()
    (bad / "leaky.py").write_text(
        'SQL = "UPDATE jobs SET status = ?, worker_id = NULL WHERE id = ?"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("tests.test_claim_token_invariant.SRC", bad)
    assert _offenders(_SQL_RELEASE, _SQL_CLEARS)

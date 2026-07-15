"""Schema guard for the QA findings ledger (qa-redesign spec D7).

qa/findings.yml is append-mostly and edited by both humans and the L3
synthesize agent — this keeps it structurally honest: parseable YAML,
stable kebab-case ids (unique, never run-dated), known statuses, and the
required lifecycle fields present on every entry.
"""

import re
from pathlib import Path

import pytest
import yaml

LEDGER = Path(__file__).resolve().parents[2] / "qa" / "findings.yml"

STATUSES = {"unverified", "new", "verified", "open", "fixed", "codified", "wontfix"}
SEVERITIES = {"high", "medium", "low"}
ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)+$")
# Provenance is a numbered QA scenario (S4) or a kebab op-label (concurrency-bench).
FIRST_SEEN_RE = re.compile(r"^\d{4}-\d{2}-\d{2} / (S\d+|[a-z][a-z0-9-]+)$")


def _entries() -> list[dict]:
    data = yaml.safe_load(LEDGER.read_text(encoding="utf-8"))
    assert isinstance(data, list) and data, "ledger must be a non-empty YAML list"
    return data


@pytest.mark.qa
def test_ledger_entries_are_well_formed() -> None:
    for entry in _entries():
        eid = entry.get("id", "<missing id>")
        assert ID_RE.match(str(entry.get("id", ""))), f"{eid}: id must be a kebab-case slug"
        assert not re.search(r"\d{4}-\d{2}-\d{2}", str(entry["id"])), f"{eid}: id is run-dated"
        assert entry.get("title"), f"{eid}: missing title"
        assert FIRST_SEEN_RE.match(str(entry.get("first_seen", ""))), (
            f"{eid}: first_seen must look like '2026-07-05 / S4' or '2026-07-14 / op-label'"
        )
        assert entry.get("severity") in SEVERITIES, f"{eid}: bad severity"
        assert entry.get("status") in STATUSES, f"{eid}: unknown status {entry.get('status')!r}"
        refs = entry.get("refs") or {}
        if entry["status"] in ("fixed", "codified"):
            # A shipped-fix claim must be traceable to its PR.
            assert isinstance(refs.get("pr"), int), f"{eid}: {entry['status']} needs refs.pr"
        if entry["status"] == "codified":
            assert refs.get("test"), f"{eid}: codified entries must ref their guard test"


@pytest.mark.qa
def test_ledger_ids_are_unique() -> None:
    ids = [e["id"] for e in _entries()]
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"duplicate ledger ids: {sorted(dupes)}"


@pytest.mark.qa
def test_codified_guard_tests_exist() -> None:
    """A codified entry's guard ref must point at a real test file."""
    repo = LEDGER.parents[1]
    for entry in _entries():
        if entry["status"] != "codified":
            continue
        test_ref = entry["refs"]["test"]
        rel_path = test_ref.split("::")[0]
        assert (repo / rel_path).is_file(), f"{entry['id']}: guard file {rel_path} missing"

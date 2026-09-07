"""One definition of the status vocabularies, enforced (ledger R-016).

Which job statuses mean "waiting for a worker", "owned by a worker" and
"finished", and which worker statuses mean "alive", are each defined
once, beside their enum in ``models/``. Every SQL placeholder list,
Python membership test and template branch builds from those constants.
The 2026-09-05 ledger found the running set retyped in seven places
with two variants, one of them the worker-delete safety gate.

Two guards keep it that way:

- the three job sets partition ``JobStatus``: a status added to the enum
  fails this file until it is placed in exactly one set;
- nothing under ``src/`` spells a whole set out inline: not as a tuple
  of enum members, not as SQL literals in a ``status IN (...)`` list,
  not as a Jinja ``in ('a', 'b')`` test. Proper subsets stay allowed:
  "outcomes" (complete, failed, skipped) and "clearable history"
  (complete, cancelled) are product choices, not copies of a vocabulary.
"""

import ast
import re
from pathlib import Path

from transcode_forge.models.job import (
    ACTIVE_JOB_STATUSES,
    TERMINAL_JOB_STATUSES,
    WAITING_JOB_STATUSES,
    JobStatus,
)
from transcode_forge.models.worker import ALIVE_WORKER_STATUSES, WorkerStatus

SRC = Path(__file__).resolve().parent.parent / "src" / "transcode_forge"
TEMPLATES = SRC / "web" / "templates"
# The two files that ARE the definition.
DEFINITIONS = {SRC / "models" / "job.py", SRC / "models" / "worker.py"}

VOCABULARY = {
    "WAITING_JOB_STATUSES": frozenset(WAITING_JOB_STATUSES),
    "ACTIVE_JOB_STATUSES": frozenset(ACTIVE_JOB_STATUSES),
    "TERMINAL_JOB_STATUSES": frozenset(TERMINAL_JOB_STATUSES),
    "ALIVE_WORKER_STATUSES": frozenset(ALIVE_WORKER_STATUSES),
}
_ENUMS: dict[str, type[JobStatus] | type[WorkerStatus]] = {
    "JobStatus": JobStatus,
    "WorkerStatus": WorkerStatus,
}

# `status IN ('a', 'b')` / `j.status NOT IN ('a','b')` inside a SQL string.
# `\bstatus` does not match `transcode_status`, the catalog's own column.
_SQL_IN = re.compile(r"\bstatus\s+(?:NOT\s+)?IN\s*\(([^)]*)\)", re.IGNORECASE)
# Jinja: `x in ('a', 'b')` or `x in ['a', 'b']`.
_JINJA_IN = re.compile(r"\bin\s*[(\[]\s*('[a-z_]+'(?:\s*,\s*'[a-z_]+')+)\s*[)\]]")
_QUOTED = re.compile(r"'([a-z_]+)'")


def test_job_status_sets_partition_the_enum() -> None:
    sets = [
        frozenset(WAITING_JOB_STATUSES),
        frozenset(ACTIVE_JOB_STATUSES),
        frozenset(TERMINAL_JOB_STATUSES),
    ]
    every_status = {s.value for s in JobStatus}
    assert frozenset().union(*sets) == every_status, "a status is in no set"
    assert sum(len(s) for s in sets) == len(every_status), "a status is in two sets"


def test_alive_worker_statuses_are_a_proper_subset() -> None:
    assert frozenset(ALIVE_WORKER_STATUSES) < {s.value for s in WorkerStatus}


def test_verifying_is_not_a_job_status() -> None:
    """Retired 2026-09-07: no code ever set it. The decode check is the
    VERIFY *phase* of a TRANSCODING job (JobPhase), not a status."""
    assert "verifying" not in {s.value for s in JobStatus}


def _whole_sets(values: set[str]) -> list[str]:
    return [name for name, members in VOCABULARY.items() if members <= values]


def _enum_value(node: ast.expr) -> str | None:
    """``JobStatus.X`` or ``JobStatus.X.value`` -> "x"; anything else -> None."""
    if isinstance(node, ast.Attribute) and node.attr == "value":
        node = node.value
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        enum = _ENUMS.get(node.value.id)
        if enum is not None and node.attr in enum.__members__:
            return str(enum[node.attr].value)
    return None


def _python_offenders(path: Path) -> list[str]:
    rel = path.relative_to(SRC)
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Tuple | ast.List | ast.Set):
            members = {v for e in node.elts if (v := _enum_value(e)) is not None}
            found.extend(f"{rel}:{node.lineno}: inline copy of {n}" for n in _whole_sets(members))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            for match in _SQL_IN.finditer(node.value):
                literals = set(_QUOTED.findall(match.group(1)))
                found.extend(
                    f"{rel}:{node.lineno}: SQL literal copy of {n}" for n in _whole_sets(literals)
                )
    return found


def _template_offenders(path: Path) -> list[str]:
    rel = path.relative_to(SRC)
    found: list[str] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        for match in _JINJA_IN.finditer(line):
            literals = set(_QUOTED.findall(match.group(1)))
            found.extend(f"{rel}:{lineno}: template copy of {n}" for n in _whole_sets(literals))
    return found


def test_no_status_set_is_spelled_out_twice() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path not in DEFINITIONS:
            offenders.extend(_python_offenders(path))
    for path in sorted(TEMPLATES.rglob("*.html")):
        offenders.extend(_template_offenders(path))
    assert not offenders, (
        "A status set is spelled out inline; build it from the constant in models/ instead:\n  "
        + "\n  ".join(offenders)
    )

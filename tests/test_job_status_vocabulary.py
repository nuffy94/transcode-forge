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
  not as a comma-joined string (``"assigned,transcoding"``), not as a
  Jinja ``in ('a', 'b')`` test or option value, not as a JS list or
  string. The browser gets the sets from the DOM the templates render
  (option values, data- attributes), never from its own copy. Proper
  subsets stay allowed: "outcomes" (complete, failed, skipped) and
  "clearable history" (complete, cancelled) are product choices, not
  copies of a vocabulary.
"""

import ast
import re
from pathlib import Path

from httpx import AsyncClient

from transcode_forge.models.job import (
    ACTIVE_JOB_STATUSES,
    TERMINAL_JOB_STATUSES,
    WAITING_JOB_STATUSES,
    Job,
    JobStatus,
)
from transcode_forge.models.worker import ALIVE_WORKER_STATUSES, WorkerStatus
from transcode_forge.repos import jobs as job_repo

SRC = Path(__file__).resolve().parent.parent / "src" / "transcode_forge"
TEMPLATES = SRC / "web" / "templates"
SCRIPTS = SRC / "web" / "static" / "js"
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
# JS: any bracketed list of quoted words, `['a', 'b'].includes(x)`.
_JS_LIST = re.compile(r"[(\[]\s*(['\"][a-z_]+['\"](?:\s*,\s*['\"][a-z_]+['\"])+)\s*[)\]]")
# A comma-joined list inside one quoted string: `"a,b,c"` (list_jobs
# filters, <option value>, JS constants).
_COMMA_STRING = re.compile(r"['\"]([a-z_]+(?:,[a-z_]+)+)['\"]")
_QUOTED = re.compile(r"['\"]([a-z_]+)['\"]")


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


def test_status_enums_are_str() -> None:
    """The set constants hold values, so `job.status in WAITING_JOB_STATUSES`
    compares a member with a str. That only works while the enums stay
    StrEnum; a plain Enum would turn every membership test False, silently."""
    assert issubclass(JobStatus, str)
    assert issubclass(WorkerStatus, str)


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
            if "," in node.value:
                tokens = {t.strip() for t in node.value.split(",")}
                found.extend(
                    f"{rel}:{node.lineno}: comma-joined copy of {n}" for n in _whole_sets(tokens)
                )
    return found


def _text_offenders(path: Path, list_pattern: re.Pattern[str], kind: str) -> list[str]:
    """Templates and scripts, line by line: quoted lists and comma-joined strings."""
    rel = path.relative_to(SRC)
    found: list[str] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        for match in list_pattern.finditer(line):
            literals = set(_QUOTED.findall(match.group(1)))
            found.extend(f"{rel}:{lineno}: {kind} copy of {n}" for n in _whole_sets(literals))
        for match in _COMMA_STRING.finditer(line):
            tokens = set(match.group(1).split(","))
            found.extend(f"{rel}:{lineno}: {kind} copy of {n}" for n in _whole_sets(tokens))
    return found


def test_no_status_set_is_spelled_out_twice() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path not in DEFINITIONS:
            offenders.extend(_python_offenders(path))
    for path in sorted(TEMPLATES.rglob("*.html")):
        offenders.extend(_text_offenders(path, _JINJA_IN, "template"))
    for path in sorted(SCRIPTS.glob("*.js")):
        offenders.extend(_text_offenders(path, _JS_LIST, "script"))
    assert not offenders, (
        "A status set is spelled out inline; build it from the constant in models/ instead:\n  "
        + "\n  ".join(offenders)
    )


async def test_browser_reads_the_sets_from_the_dom(client: AsyncClient, app) -> None:
    """queue.js owns no status list: its default filter is the option the
    server marked selected, and bulk cancel reads the data-cancellable flag
    the server sets per row. Both are rendered from the constants."""
    db = app.state.db
    for status in (JobStatus.QUEUED, JobStatus.TRANSCODING):
        job = Job(
            source_path=f"/movies/{status}.mkv",
            library="movies",
            source_codec="h264",
            quality_value=24,
            status=status,
        )
        await job_repo.create_job(db, job)

    default_filter = ",".join(WAITING_JOB_STATUSES + ACTIVE_JOB_STATUSES)
    page = (await client.get("/queue")).text
    assert f'value="{default_filter}" selected' in page

    rows = (await client.get(f"/partials/jobs?status={default_filter}")).text
    assert re.search(r'data-status="queued"\s+data-cancellable="1"', rows)
    assert re.search(r'data-status="transcoding"\s+data-cancellable="0"', rows)

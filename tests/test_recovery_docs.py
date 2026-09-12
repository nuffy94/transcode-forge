"""The recovery docs, enforced.

Five published procedures used to destroy or silently fail to restore the
data they exist to protect (Codex full review 2026-09-12, finding 5;
ledger R-034 covers the neighbouring doc errors). Each rule below is the
shape of one of them, checked against the shipped markdown so the
dangerous form cannot come back:

1. no project-wide volume wipe in a recovery doc,
2. SQLite backup and restore go through SQLite, never ``cp``,
3. every ``psql`` in these docs fails fast on the first error,
4. the restore procedure never writes to a file, so it cannot land on
   the backup it is reading,
5. rollback pins the old image before it restores the old database.

The last test is a positive control: it reproduces the WAL data loss that
rule 2 exists to prevent.
"""

import re
import shutil
import sqlite3
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"
BACKUP = DOCS / "BACKUP.md"
TROUBLESHOOTING = DOCS / "TROUBLESHOOTING.md"
UPGRADE = DOCS / "UPGRADE.md"
RECOVERY_DOCS = (BACKUP, TROUBLESHOOTING, UPGRADE)

_FENCE = re.compile(r"^\s*```")
_REDIRECT = re.compile(r"(?:^|\s)>>?\s*\S")


def _shell_lines(text: str) -> list[str]:
    """Command lines inside fenced blocks, backslash continuations joined."""
    lines: list[str] = []
    inside = False
    for raw in text.splitlines():
        if _FENCE.match(raw):
            inside = not inside
            continue
        if not inside:
            continue
        stripped = raw.strip()
        if stripped.startswith("#"):
            continue
        if lines and lines[-1].endswith("\\"):
            lines[-1] = lines[-1][:-1].rstrip() + " " + stripped
        else:
            lines.append(stripped)
    return [line for line in lines if line]


def _section(text: str, heading: str) -> str:
    """The body of one ``##`` section, its subsections included."""
    parts = text.split(f"\n## {heading}\n", 1)
    assert len(parts) == 2, f"section not found: {heading}"
    return parts[1].split("\n## ", 1)[0]


def test_no_project_wide_volume_wipe() -> None:
    offenders = [
        f"{path.name}:{lineno}: {line.strip()[:110]}"
        for path in RECOVERY_DOCS
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if "down -v" in line
    ]
    assert not offenders, (
        "A recovery doc teaches a project-wide volume wipe. It removes every "
        "named volume in the project, so a Redis repair takes the Postgres "
        "database with it. Scope the reset to the one service:\n" + "\n".join(offenders)
    )


def test_sqlite_backup_and_restore_go_through_sqlite() -> None:
    offenders = [
        line
        for line in _shell_lines(BACKUP.read_text(encoding="utf-8"))
        if re.match(r"cp\s", line) and ".db" in line
    ]
    assert not offenders, (
        "BACKUP.md copies the SQLite database file. The scheduler runs it in "
        'WAL mode, so committed rows can be lost. Use sqlite3 ".backup" / '
        '".restore" or VACUUM INTO:\n' + "\n".join(offenders)
    )


def test_every_psql_restore_fails_fast() -> None:
    offenders = [
        f"{path.name}: {line[:110]}"
        for path in RECOVERY_DOCS
        for line in _shell_lines(path.read_text(encoding="utf-8"))
        if "psql" in line and "ON_ERROR_STOP=1" not in line
    ]
    assert not offenders, (
        "psql without -v ON_ERROR_STOP=1 prints its errors and still exits 0, "
        "so a failed restore reads as a finished one:\n" + "\n".join(offenders)
    )


def test_restore_procedure_never_writes_a_file() -> None:
    section = _section(BACKUP.read_text(encoding="utf-8"), "Restore procedure")
    offenders = [line for line in _shell_lines(section) if _REDIRECT.search(line)]
    assert not offenders, (
        "The restore procedure redirects output into a file. That is how the "
        "old conflict fallback overwrote the very backup being restored. A "
        "restore only ever reads:\n" + "\n".join(offenders)
    )


def test_rollback_pins_the_old_image_before_restoring() -> None:
    section = _section(UPGRADE.read_text(encoding="utf-8"), "Rollback (if something goes wrong)")
    pin = section.find("TF_VERSION")
    restore = section.find("BACKUP.md")
    assert pin != -1, "rollback no longer pins TF_VERSION"
    assert restore != -1, "rollback no longer links the restore procedure"
    assert pin < restore, (
        "Rollback restores the database before it pins the previous image, so "
        "the new scheduler boots against the restored database and migrates it "
        "again. Pin the tag, then restore, then start once."
    )


def test_wal_committed_row_survives_backup_but_not_a_file_copy(tmp_path: Path) -> None:
    """Positive control for rule 2: why the docs cannot say ``cp``."""
    db = tmp_path / "forge.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO jobs (id) VALUES (1)")
    conn.commit()

    copied = tmp_path / "copy.db"
    shutil.copy(db, copied)

    backed_up = tmp_path / "backup.db"
    with sqlite3.connect(backed_up) as target:
        conn.backup(target)
    conn.close()

    def rows(path: Path) -> int:
        with sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True) as check:
            found = check.execute(
                "SELECT count(*) FROM sqlite_master WHERE name = 'jobs'"
            ).fetchone()[0]
            if not found:
                return -1
            return int(check.execute("SELECT count(*) FROM jobs").fetchone()[0])

    assert rows(copied) < 1, "expected the file copy to miss the committed WAL row"
    assert rows(backed_up) == 1

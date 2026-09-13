"""bootstrap.ps1 must produce a complete .env or refuse to run.

R-040 made TF_ADMIN_PASSWORD required at startup: the scheduler refuses to
create an admin, and refuses to boot, without one (repos/users.py,
admin.py). bootstrap.sh and the Linode StackScript already generate it;
bootstrap.ps1 did not, so a fresh Windows install wrote a .env with a blank
TF_ADMIN_PASSWORD and the container never came up (Codex Q19 / J01).

These tests drive the real script with PowerShell in a throwaway
directory. They self-skip if no PowerShell is on PATH (pwsh is present on
GitHub-hosted Ubuntu runners; powershell.exe on Windows).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = REPO_ROOT / "bootstrap.ps1"

PWSH = shutil.which("pwsh") or shutil.which("powershell")

pytestmark = pytest.mark.skipif(
    PWSH is None, reason="no PowerShell (pwsh/powershell) on this machine"
)


def _run_bootstrap(tmp_path: Path, password: str | None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("TF_ADMIN_PASSWORD", None)
    if password is not None:
        env["TF_ADMIN_PASSWORD"] = password
    return subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-File", str(BOOTSTRAP)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _env_values(tmp_path: Path) -> dict[str, str]:
    env_file = tmp_path / ".env"
    values: dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if len(value) >= 2 and value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        values[key] = value
    return values


def test_generated_env_carries_the_admin_password_through(tmp_path):
    result = _run_bootstrap(tmp_path, password="correct-horse-battery")

    assert result.returncode == 0, result.stderr
    assert _env_values(tmp_path).get("TF_ADMIN_PASSWORD") == "correct-horse-battery"


def test_stdout_and_stderr_never_echo_the_password(tmp_path):
    result = _run_bootstrap(tmp_path, password="correct-horse-battery")

    assert "correct-horse-battery" not in result.stdout
    assert "correct-horse-battery" not in result.stderr


@pytest.mark.parametrize(
    "bad_password",
    ["short", "x" * 73],
    ids=["below-8-chars", "above-72-bytes"],
)
def test_a_password_outside_bcrypts_window_is_refused_before_any_write(tmp_path, bad_password):
    result = _run_bootstrap(tmp_path, password=bad_password)

    assert result.returncode != 0
    assert not (tmp_path / ".env").exists()


def test_a_password_that_only_passes_on_utf16_code_units_is_still_refused(tmp_path):
    # Four emoji: 8 UTF-16 code units (PowerShell's raw .Length) but 4 Unicode
    # code points (Python's len(), what repos/users.validate_password checks).
    # A check that trusts .Length lets this through and the scheduler then
    # refuses to boot on a password it considers 4 characters long.
    password = "\U0001f600" * 4

    result = _run_bootstrap(tmp_path, password=password)

    assert result.returncode != 0
    assert not (tmp_path / ".env").exists()


def test_a_dollar_sign_in_the_password_is_written_single_quoted(tmp_path):
    # Compose's own .env parser expands $VAR/${VAR} in unquoted and
    # double-quoted values; only single-quoted values are literal. Assert
    # the raw file, not just our own reader, so a reader that also strips
    # quotes can't mask a script that forgot to add them.
    password = "correct$horsebattery"

    result = _run_bootstrap(tmp_path, password=password)

    assert result.returncode == 0, result.stderr
    raw = (tmp_path / ".env").read_text(encoding="utf-8")
    assert f"TF_ADMIN_PASSWORD='{password}'" in raw
    assert _env_values(tmp_path).get("TF_ADMIN_PASSWORD") == password


def test_a_hash_in_the_password_is_written_single_quoted(tmp_path):
    # Compose treats an unescaped # as starting a comment; single-quoting
    # keeps it part of the value instead of truncating it.
    password = "goodpass #suffix"

    result = _run_bootstrap(tmp_path, password=password)

    assert result.returncode == 0, result.stderr
    raw = (tmp_path / ".env").read_text(encoding="utf-8")
    assert f"TF_ADMIN_PASSWORD='{password}'" in raw
    assert _env_values(tmp_path).get("TF_ADMIN_PASSWORD") == password


def test_a_single_quote_in_the_password_is_refused_before_any_write(tmp_path):
    # The written value is wrapped in single quotes so Compose's own .env
    # parser can't reinterpret $ or # inside it; an embedded single quote
    # can't be represented that way, so refuse rather than write something
    # that silently isn't the password the user typed.
    password = "can't-quote-this"

    result = _run_bootstrap(tmp_path, password=password)

    assert result.returncode != 0
    assert not (tmp_path / ".env").exists()

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
    for line in env_file.read_text().splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
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

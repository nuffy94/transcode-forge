"""scripts/check_image_lock.py: the image installs the lock, nothing else."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from check_image_lock import drift, image_drift, main, pins, unpinned, unsatisfied

FREEZE = """\
Using Python 3.12.14 environment at: /usr/local
anyio==4.15.1
fastapi==0.141.1
pip==25.0.1
Prometheus_Client==0.26.0
starlette==1.6.0
transcode-forge @ file:///app
"""

EXPORT = """\
anyio==4.15.1
    # via starlette
colorama==0.4.6 ; sys_platform == 'win32'
fastapi==0.141.1
prometheus-client==0.26.0
starlette==1.6.0 \\
    --hash=sha256:abc
"""


def test_pins_reads_only_version_pins() -> None:
    assert pins(FREEZE) == {
        "anyio": "4.15.1",
        "fastapi": "0.141.1",
        "pip": "25.0.1",
        "prometheus-client": "0.26.0",
        "starlette": "1.6.0",
    }
    assert pins(EXPORT)["colorama"] == "0.4.6"
    assert pins(EXPORT)["starlette"] == "1.6.0"


def test_image_matching_lock_has_no_drift() -> None:
    assert drift(pins(FREEZE), pins(EXPORT)) == []


def test_version_mismatch_is_drift() -> None:
    installed = pins(FREEZE.replace("starlette==1.6.0", "starlette==0.52.1"))
    assert drift(installed, pins(EXPORT)) == ["starlette: image has 0.52.1, uv.lock pins 1.6.0"]


def test_package_missing_from_lock_is_drift() -> None:
    installed = pins(FREEZE + "python-multipart==0.0.20\n")
    assert drift(installed, pins(EXPORT)) == [
        "python-multipart==0.0.20 is installed but not in uv.lock"
    ]


def test_lock_only_platform_markers_are_not_drift() -> None:
    # colorama is in the lock for Windows only; a Linux image never installs it.
    assert "colorama" not in pins(FREEZE)
    assert drift(pins(FREEZE), pins(EXPORT)) == []


def test_project_itself_is_the_only_allowed_direct_install() -> None:
    assert unpinned(FREEZE) == []


def test_direct_url_install_is_drift() -> None:
    # Codex 2026-09-15: a `name @ url` entry is not a pin, and used to be
    # skipped silently even when the lock pinned a different version.
    freeze = FREEZE.replace(
        "starlette==1.6.0", "starlette @ https://example.invalid/starlette-0.52.1-py3-none-any.whl"
    )
    assert unpinned(freeze) == [
        "starlette @ https://example.invalid/starlette-0.52.1-py3-none-any.whl"
    ]
    assert image_drift(freeze, EXPORT, None) == [
        "starlette @ https://example.invalid/starlette-0.52.1-py3-none-any.whl"
        " is installed outside uv.lock"
    ]


def test_editable_install_is_drift() -> None:
    assert unpinned(FREEZE + "-e /src/something\n") == ["-e /src/something"]


def test_dry_run_with_no_changes_is_satisfied() -> None:
    assert unsatisfied("Audited 52 packages in 13ms\nWould make no changes\n") == []


def test_dry_run_that_would_install_names_the_gap() -> None:
    # Codex 2026-09-15: an image missing a locked requirement passed the
    # installed-side check alone. uv's own dry run is the reverse direction.
    plan = "Resolved 51 packages in 31ms\nWould install 1 package\n + anyio==4.15.1\n"
    assert unsatisfied(plan) == [
        "Resolved 51 packages in 31ms",
        "Would install 1 package",
        " + anyio==4.15.1",
    ]
    lines = image_drift(FREEZE, EXPORT, plan)
    assert lines[0] == "lock requirement not satisfied: Resolved 51 packages in 31ms"
    assert "lock requirement not satisfied:  + anyio==4.15.1" in lines


def test_main_exits_nonzero_on_drift(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    installed = tmp_path / "freeze.txt"
    lock = tmp_path / "export.txt"
    installed.write_text(FREEZE.replace("fastapi==0.141.1", "fastapi==0.135.2"))
    lock.write_text(EXPORT)
    assert main(["--installed", str(installed), "--lock", str(lock)]) == 1
    assert "fastapi: image has 0.135.2, uv.lock pins 0.141.1" in capsys.readouterr().out


def test_main_exits_zero_when_aligned(tmp_path: Path) -> None:
    installed = tmp_path / "freeze.txt"
    lock = tmp_path / "export.txt"
    installed.write_text(FREEZE)
    lock.write_text(EXPORT)
    assert main(["--installed", str(installed), "--lock", str(lock)]) == 0

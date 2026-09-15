"""scripts/check_image_lock.py: the image installs the lock, nothing else."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from check_image_lock import drift, main, pins

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

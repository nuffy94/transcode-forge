"""Prove the built image installed exactly what uv.lock pins, both ways.

The test suite, the LXC workers (``uv sync --frozen``) and the Docker image
must all run the versions the lock names. Before this check the Dockerfile
resolved from pyproject.toml at build time, so the image quietly carried
whatever PyPI offered that day while CI tested the lock: the 0.15.0 image
shipped Starlette 1.6.0 on a lock that said 0.52.1.

Two checks, one per direction:

1. Lock -> image. ``uv pip install --dry-run`` of the lock export (markers
   evaluated for this platform, hashes included) must report that it would
   make no changes. A missing or wrong-version requirement shows up as
   "Would install".
2. Image -> lock. Every installed distribution must be a ``name==version``
   the lock pins at that version. The project itself (installed from
   ``/app``) and base-image tooling are the only things allowed outside
   the lock; any other direct-URL or editable install is drift.

Run inside the image (CI does this after every build):

    python /scripts/check_image_lock.py

or offline against two captured listings (direction 2 only):

    python scripts/check_image_lock.py --installed freeze.txt --lock export.txt

Exit 0 when the image and the lock agree; exit 1 with one line per drift.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT = "transcode-forge"

# Tooling the base image carries that no lock will ever name.
BASE_IMAGE_TOOLING = frozenset({"pip", "setuptools", "wheel", "uv"})

_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;#\\]+)")
_DIRECT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*) @ (\S+)")
_EDITABLE = re.compile(r"^-e (\S+)")

NO_CHANGES = "Would make no changes"

EXPORT_CMD = ("uv", "export", "--frozen", "--no-dev", "--no-emit-project")
FREEZE_CMD = ("uv", "pip", "freeze", "--system")


def normalize(name: str) -> str:
    """PEP 503 name normalization so ``Prometheus_Client`` meets ``prometheus-client``."""
    return re.sub(r"[-_.]+", "-", name).lower()


def pins(text: str) -> dict[str, str]:
    """Parse ``name==version`` lines; markers, comments, hashes and noise are ignored."""
    found: dict[str, str] = {}
    for raw in text.splitlines():
        match = _PIN.match(raw.strip())
        if match:
            found[normalize(match.group(1))] = match.group(2)
    return found


def unpinned(text: str) -> list[str]:
    """Installed entries that are not version pins: direct URLs and editables.

    The project itself is installed from ``/app`` and is expected here;
    everything else is something the lock did not put in the image.
    """
    found: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        direct = _DIRECT.match(line)
        foreign_direct = direct is not None and normalize(direct.group(1)) != PROJECT
        if foreign_direct or _EDITABLE.match(line):
            found.append(line)
    return found


def drift(installed: dict[str, str], locked: dict[str, str]) -> list[str]:
    """Every installed distribution must be in the lock at the same version."""
    lines: list[str] = []
    for name in sorted(installed):
        if name in BASE_IMAGE_TOOLING:
            continue
        have = installed[name]
        want = locked.get(name)
        if want is None:
            lines.append(f"{name}=={have} is installed but not in uv.lock")
        elif want != have:
            lines.append(f"{name}: image has {have}, uv.lock pins {want}")
    return lines


def unsatisfied(dry_run_output: str) -> list[str]:
    """What ``uv pip install --dry-run`` of the lock export would still change.

    Empty when uv reports no changes; otherwise every line of its plan, so a
    requirement the image lacks (or has at the wrong version) is named.
    """
    if NO_CHANGES in dry_run_output:
        return []
    return [line for line in dry_run_output.splitlines() if line.strip()]


def image_drift(installed_text: str, lock_text: str, dry_run_output: str | None) -> list[str]:
    """All drift lines for one image, both directions."""
    lines: list[str] = []
    if dry_run_output is not None:
        lines.extend(
            f"lock requirement not satisfied: {line}" for line in unsatisfied(dry_run_output)
        )
    lines.extend(f"{line} is installed outside uv.lock" for line in unpinned(installed_text))
    lines.extend(drift(pins(installed_text), pins(lock_text)))
    return lines


def _run(cmd: tuple[str, ...]) -> str:
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return result.stdout


def _dry_run_install(lock_text: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
        handle.write(lock_text)
        path = handle.name
    try:
        result = subprocess.run(
            ("uv", "pip", "install", "--system", "--dry-run", "-r", path),
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        Path(path).unlink(missing_ok=True)
    return result.stdout + result.stderr


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--installed", type=Path, help="captured `uv pip freeze` output")
    parser.add_argument("--lock", type=Path, help="captured `uv export` output")
    args = parser.parse_args(argv)
    if (args.installed is None) != (args.lock is None):
        parser.error("--installed and --lock go together")

    dry_run: str | None
    if args.installed is not None:
        installed_text = args.installed.read_text()
        lock_text = args.lock.read_text()
        dry_run = None
    else:
        installed_text = _run(FREEZE_CMD)
        lock_text = _run(EXPORT_CMD)
        dry_run = _dry_run_install(lock_text)

    installed = pins(installed_text)
    locked = pins(lock_text)
    if not installed or not locked:
        print(f"check_image_lock: empty listing (installed={len(installed)}, locked={len(locked)})")
        return 1

    problems = image_drift(installed_text, lock_text, dry_run)
    if problems:
        print("Installed packages drift from uv.lock:")
        for line in problems:
            print(f"  {line}")
        return 1
    print(f"check_image_lock: {len(installed)} installed distributions match uv.lock")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Prove the built image installed exactly what uv.lock pins.

The test suite, the LXC workers (``uv sync --frozen``) and the Docker image
must all run the versions the lock names. Before this check the Dockerfile
resolved from pyproject.toml at build time, so the image quietly carried
whatever PyPI offered that day while CI tested the lock: the 0.15.0 image
shipped Starlette 1.6.0 on a lock that said 0.52.1.

Run inside the image (CI does this after every build):

    python /scripts/check_image_lock.py

or offline against two captured listings:

    python scripts/check_image_lock.py --installed freeze.txt --lock export.txt

Exit 0 when every installed distribution appears in the lock at the same
version; exit 1 with one line per drift otherwise.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Tooling the base image carries that no lock will ever name.
BASE_IMAGE_TOOLING = frozenset({"pip", "setuptools", "wheel", "uv"})

_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;#\\]+)")

EXPORT_CMD = (
    "uv",
    "export",
    "--frozen",
    "--no-dev",
    "--no-emit-project",
    "--no-header",
    "--no-hashes",
)
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


def _run(cmd: tuple[str, ...]) -> str:
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return result.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--installed", type=Path, help="captured `uv pip freeze` output")
    parser.add_argument("--lock", type=Path, help="captured `uv export` output")
    args = parser.parse_args(argv)
    if (args.installed is None) != (args.lock is None):
        parser.error("--installed and --lock go together")

    if args.installed is not None:
        installed_text = args.installed.read_text()
        lock_text = args.lock.read_text()
    else:
        installed_text = _run(FREEZE_CMD)
        lock_text = _run(EXPORT_CMD)

    installed = pins(installed_text)
    locked = pins(lock_text)
    if not installed or not locked:
        print(f"check_image_lock: empty listing (installed={len(installed)}, locked={len(locked)})")
        return 1

    problems = drift(installed, locked)
    if problems:
        print("Installed packages drift from uv.lock:")
        for line in problems:
            print(f"  {line}")
        return 1
    print(f"check_image_lock: {len(installed)} installed distributions match uv.lock")
    return 0


if __name__ == "__main__":
    sys.exit(main())

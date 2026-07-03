#!/usr/bin/env python
"""Build the served stylesheet from ``assets/css/forge.css`` using the pinned
Tailwind v4 standalone CLI — no Node, no npm.

Modes::

    uv run python scripts/build_css.py            # build once (minified)
    uv run python scripts/build_css.py --watch     # rebuild on change (dev loop)
    uv run python scripts/build_css.py --check      # CI freshness gate

The standalone binary is downloaded per-OS into ``.tailwind/`` (gitignored) and
verified against a pinned sha256 before it is ever executed. The build is a pure
function of (source files + pinned binary version): ``forge.css`` disables
Tailwind's content auto-detection (``source(none)``) and lists explicit
``@source`` globs, so the compiled output is byte-identical across Windows (dev)
and Linux (CI/Docker) — which is what makes ``--check`` trustworthy.
"""

from __future__ import annotations

import argparse
import hashlib
import platform
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

# Pinned Tailwind release. Bump deliberately: a new version can reorder or
# restyle the generated utilities, which would trip --check until app.css is
# rebuilt. Update TAILWIND_VERSION and the hashes together (release asset
# sha256sums.txt), then rerun the build.
TAILWIND_VERSION = "4.3.2"

# sha256 of each pinned release asset (from the v4.3.2 sha256sums.txt).
_SHA256 = {
    "linux-x64": "5036c4fb4328e0bcdbb6065c70d8ac9452e0d4c947113a788a8f94fd390425c1",
    "linux-arm64": "394ddccc2402cfa3abd97dfba56f3587781a3d6e6ce66e65ceada14beb7664b8",
    "macos-arm64": "b800b0659dc64b9f03ede5660244d9415d777d5739ae2889280877ca37be742a",
    "macos-x64": "cef8f110471e889c3c4409055cf8aff33076f58a081867b0dfc6534b290bfbb0",
    "windows-x64": "224a62a8351d3b8da9d950a4eb1d7176dc901dc4735b47f816f3dfcbc67d8654",
}

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "assets" / "css" / "forge.css"
_OUT = _ROOT / "src" / "transcode_forge" / "web" / "static" / "css" / "app.css"
_CACHE = _ROOT / ".tailwind"


def _platform_key() -> str:
    system = platform.system()
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    if system == "Windows":
        return "windows-x64"  # only an x64 Windows asset is published
    if system == "Linux":
        return f"linux-{arch}"
    if system == "Darwin":
        return f"macos-{arch}"
    raise SystemExit(f"Unsupported platform: {system}/{machine}")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ensure_binary() -> Path:
    key = _platform_key()
    if key not in _SHA256:
        raise SystemExit(f"No pinned Tailwind binary for platform '{key}'")
    ext = ".exe" if key.startswith("windows") else ""
    binary = _CACHE / f"tailwindcss-{TAILWIND_VERSION}-{key}{ext}"

    if binary.exists() and _sha256(binary.read_bytes()) == _SHA256[key]:
        return binary

    _CACHE.mkdir(exist_ok=True)
    url = (
        "https://github.com/tailwindlabs/tailwindcss/releases/download/"
        f"v{TAILWIND_VERSION}/tailwindcss-{key}{ext}"
    )
    print(f"Downloading Tailwind {TAILWIND_VERSION} for {key} ...", file=sys.stderr)
    req = urllib.request.Request(url, headers={"User-Agent": "transcode-forge-build"})
    with urllib.request.urlopen(req, timeout=180) as resp:  # noqa: S310 — pinned github URL
        data = resp.read()

    digest = _sha256(data)
    if digest != _SHA256[key]:
        raise SystemExit(
            f"sha256 mismatch for {url}\n  expected {_SHA256[key]}\n  got      {digest}"
        )
    binary.write_bytes(data)
    if not key.startswith("windows"):
        binary.chmod(0o755)
    return binary


def _build(binary: Path, out: Path, *, watch: bool) -> int:
    cmd = [str(binary), "-i", str(_SRC), "-o", str(out), "--minify"]
    if watch:
        cmd.append("--watch")
    return subprocess.call(cmd)


def _normalize(data: bytes) -> bytes:
    """Compare on content, not line endings — a CRLF/LF quirk on any host must
    never masquerade as CSS drift."""
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _check(binary: Path) -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "app.css"
        rc = _build(binary, tmp, watch=False)
        if rc != 0:
            return rc
        fresh = _normalize(tmp.read_bytes())
        committed = _normalize(_OUT.read_bytes()) if _OUT.exists() else b""
    if fresh != committed:
        print(
            "CSS drift: the committed app.css is stale relative to forge.css.\n"
            "Rebuild with: uv run python scripts/build_css.py",
            file=sys.stderr,
        )
        return 1
    print("CSS is fresh.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Forge Console CSS with Tailwind v4.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--watch", action="store_true", help="rebuild on source change")
    group.add_argument(
        "--check", action="store_true", help="fail if the committed CSS is stale (CI gate)"
    )
    args = parser.parse_args()

    binary = _ensure_binary()
    if args.check:
        return _check(binary)
    return _build(binary, _OUT, watch=args.watch)


if __name__ == "__main__":
    raise SystemExit(main())

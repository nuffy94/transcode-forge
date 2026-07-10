"""Install pixel baselines from a CI run's qa-screenshots artifact (spec D4).

Baselines MUST carry the ubuntu-CI font stack — never capture them on a
local Windows/mac box (cross-OS font rendering is exactly the flake the
CI-only comparison avoids). The flow after an intentional UI change:

    1. Push the change; let the qa-sweep job run (its pixel test writes the
       current renders to tests/qa/shots/visual/, which the existing
       qa-screenshots artifact uploads).
    2. uv run python scripts/update_qa_baselines.py --run <actions-run-id>
       (uses `gh run download`), or pass a directory you downloaded yourself.
    3. Commit tests/qa/baselines/ — the PNG diff rides the PR for review.

Stdlib only (plus the gh CLI for --run).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINES = REPO_ROOT / "tests" / "qa" / "baselines"
ARTIFACT_NAME = "qa-screenshots"


def _candidates(source: Path) -> list[Path]:
    """visual/*.png captures inside an artifact tree (diff images excluded)."""
    return sorted(
        p
        for p in source.rglob("*.png")
        if p.parent.name == "visual" and not p.name.endswith(".diff.png")
    )


def _download_artifact(run_id: str, dest: Path) -> None:
    cmd = ["gh", "run", "download", run_id, "-n", ARTIFACT_NAME, "-D", str(dest)]
    print("$", " ".join(cmd))
    result = subprocess.run(cmd, cwd=REPO_ROOT)
    if result.returncode != 0:
        raise SystemExit(f"gh run download failed (rc={result.returncode})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--run", help="GitHub Actions run id to download the artifact from")
    src.add_argument("source", nargs="?", help="already-downloaded artifact directory")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        if args.run:
            source = Path(tmp)
            _download_artifact(args.run, source)
        else:
            source = Path(args.source)
            if not source.is_dir():
                raise SystemExit(f"not a directory: {source}")

        shots = _candidates(source)
        if not shots:
            raise SystemExit(
                f"no visual/*.png captures under {source} — did the qa-sweep job run "
                "the pixel test? (tests/qa/test_visual.py writes them in CI)"
            )

        BASELINES.mkdir(parents=True, exist_ok=True)
        for shot in shots:
            target = BASELINES / shot.name
            shutil.copyfile(shot, target)
            print(f"installed {target.relative_to(REPO_ROOT)}")

    print(
        f"\n{len(shots)} baseline(s) installed. Review each PNG diff in the PR -- "
        "these images ARE the assertion now."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

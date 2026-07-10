"""Start/stop disposable demo-static instances for the AI UX sweep.

Each sweep agent gets its OWN fresh instance (own port, own temp sqlite),
so scenarios can't contaminate each other and verifiers reproduce findings
on clean state. The instance is detached — this command returns once the
app answers its health check, and `--stop` kills it by pidfile.

    uv run python qa/launch_demo.py --start --port 18811 --run-dir qa/runs/latest
    uv run python qa/launch_demo.py --stop  --port 18811 --run-dir qa/runs/latest

The app boots with no admin; the sweep helper's session() completes
first-run setup with whatever password the agent uses. State lives under
<run-dir>/instances/<port>/ (db, log, pidfile) — throwaway by design.

This file is only the CLI entry point — the instance lifecycle lives in
qa/instance.py (the one boot substrate, shared with tests/qa/). The
``READY``/``STOPPED`` stdout lines are a contract the L3 workflow's agent
prompts parse; instance.py owns their exact wording.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qa.instance import start_detached, stop_detached


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--start", action="store_true")
    mode.add_argument("--stop", action="store_true")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--run-dir", type=Path, default=Path("qa/runs/latest"))
    args = ap.parse_args()

    if args.start:
        return start_detached(args.run_dir, args.port)
    return stop_detached(args.run_dir, args.port)


if __name__ == "__main__":
    raise SystemExit(main())

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
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def _paths(run_dir: Path, port: int) -> tuple[Path, Path, Path]:
    inst = run_dir / "instances" / str(port)
    return inst / "demo.db", inst / "server.log", inst / "uvicorn.pid"


def start(run_dir: Path, port: int) -> int:
    db, log_path, pidfile = _paths(run_dir, port)
    pidfile.parent.mkdir(parents=True, exist_ok=True)

    if pidfile.exists():
        print(f"pidfile already exists for port {port} — run --stop first", file=sys.stderr)
        return 1

    env = {
        **os.environ,
        "TF_DEMO_STATIC": "true",
        "TF_DB_URL": f"sqlite:///{db.resolve().as_posix()}",
        "TF_AUTH_SECRET": "qa-sweep-fixed-secret",
        "TF_LOG_LEVEL": "warning",
    }
    # CREATE_NO_WINDOW (not DETACHED_PROCESS): the instance must never pop a
    # console window — the operator's desktop is in use while sweeps run.
    detach_flags: dict[str, object] = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    with open(log_path, "w", encoding="utf-8") as logf:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "transcode_forge.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            env=env,
            stdout=logf,
            stderr=subprocess.STDOUT,
            **detach_flags,  # type: ignore[arg-type]
        )

    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        if proc.poll() is not None:
            print(
                f"instance exited early (code {proc.returncode}) — see {log_path}", file=sys.stderr
            )
            return 1
        try:
            with urllib.request.urlopen(f"{base}/api/health/live", timeout=1) as r:
                if r.status == 200:
                    break
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
            time.sleep(0.5)
    else:
        proc.terminate()
        print(f"instance never became ready — see {log_path}", file=sys.stderr)
        return 1

    pidfile.write_text(str(proc.pid), encoding="utf-8")
    print(f"READY pid={proc.pid} base={base}")
    return 0


def stop(run_dir: Path, port: int) -> int:
    _, _, pidfile = _paths(run_dir, port)
    if not pidfile.exists():
        print(f"no pidfile for port {port} — nothing to stop")
        return 0
    pid = int(pidfile.read_text(encoding="utf-8").strip())
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError) as e:
        print(f"kill pid {pid}: {e} (already gone?)")
    pidfile.unlink(missing_ok=True)
    print(f"STOPPED pid={pid} port={port}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--start", action="store_true")
    mode.add_argument("--stop", action="store_true")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--run-dir", type=Path, default=Path("qa/runs/latest"))
    args = ap.parse_args()

    return start(args.run_dir, args.port) if args.start else stop(args.run_dir, args.port)


if __name__ == "__main__":
    raise SystemExit(main())

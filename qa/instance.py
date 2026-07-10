"""One boot substrate for QA demo instances (plans/qa-redesign-spec.md, D1).

Every QA surface that needs a real HTTP server boots it through this module,
always in demo-static mode (seeded, deterministic, no Redis/ffmpeg needed):

* ``launch()`` — attached child process for pytest (tests/qa/ conftest and
  test_setup_flow consume it via ``launch_qa_app``). Torn down on context
  exit; raises with a server-log tail if the app dies or never gets ready.
* ``start_detached()`` / ``stop_detached()`` — pidfile-managed instances that
  outlive the launching process, behind the qa/launch_demo.py CLI (the L3
  sweep's per-agent instances). Their printed ``READY``/``STOPPED`` lines are
  a contract the L3 workflow's agent prompts rely on — do not reword them.
* ``bootstrap_admin()`` — the one first-run auth bootstrap (POST
  /api/auth/setup), so no other surface carries its own copy.

A future ``pg`` mode (same app pointed at a Postgres URL) hooks in here when
S6b lands — not built yet.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

AUTH_SECRET = "qa-sweep-fixed-secret"
_READY_ATTEMPTS = 60
_READY_INTERVAL = 0.5


class InstanceExitedError(RuntimeError):
    """The spawned instance died before answering its health check."""

    def __init__(self, returncode: int | None) -> None:
        super().__init__(f"instance exited early (code {returncode})")
        self.returncode = returncode


class InstanceNotReadyError(RuntimeError):
    """The spawned instance never answered its health check in time."""


def pick_free_port() -> int:
    """OS-assigned free TCP port (bind-to-0; the tiny race window is fine
    for QA instances on loopback)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def demo_env(db: Path) -> dict[str, str]:
    """The canonical demo-static environment for a QA instance."""
    return {
        **os.environ,
        "TF_DEMO_STATIC": "true",
        "TF_DB_URL": f"sqlite:///{db.resolve().as_posix()}",
        "TF_AUTH_SECRET": AUTH_SECRET,
        "TF_LOG_LEVEL": "warning",
    }


def bootstrap_admin(base_url: str, password: str) -> None:
    """First-run setup: create the admin so authenticated pages are reachable.

    200 = created, 409 = already set up — both fine, so a seeded instance can
    be bootstrapped idempotently.
    """
    req = urllib.request.Request(
        f"{base_url}/api/auth/setup",
        data=json.dumps({"password": password}).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
    if status not in (200, 409):
        raise RuntimeError(f"admin setup failed: HTTP {status}")


def instance_paths(run_dir: Path, port: int) -> tuple[Path, Path, Path]:
    """Detached-instance layout: state under <run_dir>/instances/<port>/
    (db, log, pidfile) — throwaway by design."""
    inst = run_dir / "instances" / str(port)
    return inst / "demo.db", inst / "server.log", inst / "uvicorn.pid"


def _spawn(port: int, db: Path, logf: IO[str], *, detached: bool) -> subprocess.Popen[bytes]:
    flags: dict[str, object] = {}
    if detached:
        # CREATE_NO_WINDOW (not DETACHED_PROCESS): the instance must never pop
        # a console window — the operator's desktop is in use while sweeps run.
        flags = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
            if os.name == "nt"
            else {"start_new_session": True}
        )
    return subprocess.Popen(
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
        env=demo_env(db),
        stdout=logf,
        stderr=subprocess.STDOUT,
        **flags,  # type: ignore[arg-type]
    )


def _await_ready(proc: subprocess.Popen[bytes], base_url: str) -> None:
    for _ in range(_READY_ATTEMPTS):
        if proc.poll() is not None:
            raise InstanceExitedError(proc.returncode)
        try:
            with urllib.request.urlopen(f"{base_url}/api/health/live", timeout=1) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
            time.sleep(_READY_INTERVAL)
    raise InstanceNotReadyError()


def _log_tail(log_path: Path) -> str:
    try:
        return log_path.read_text("utf-8", "replace")[-2500:]
    except OSError:
        return "(no log)"


@contextmanager
def launch(instance_dir: Path, port: int, *, admin_password: str | None = None) -> Iterator[str]:
    """Boot an attached demo-static instance on ``port``; yield its base URL.

    ``admin_password`` completes first-run setup with that password (the
    normal sweep target); ``None`` leaves the instance fresh so /setup itself
    can be exercised. The instance is a child process — torn down on exit.
    """
    instance_dir.mkdir(parents=True, exist_ok=True)
    db = instance_dir / "demo.db"
    log_path = instance_dir / "server.log"
    base_url = f"http://127.0.0.1:{port}"

    logf = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 — closed in finally
    proc = _spawn(port, db, logf, detached=False)
    try:
        try:
            _await_ready(proc, base_url)
        except InstanceExitedError as e:
            raise RuntimeError(
                f"QA server exited early (code {e.returncode}):\n{_log_tail(log_path)}"
            ) from e
        except InstanceNotReadyError:
            raise RuntimeError(
                f"QA demo server failed to become ready:\n{_log_tail(log_path)}"
            ) from None

        if admin_password is not None:
            bootstrap_admin(base_url, admin_password)

        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        logf.close()


def start_detached(run_dir: Path, port: int) -> int:
    """Start a pidfile-managed instance that survives this process.

    Prints the CLI contract line ``READY pid=<pid> base=<url>`` on success;
    returns a process exit code. Consumed by qa/launch_demo.py.
    """
    db, log_path, pidfile = instance_paths(run_dir, port)
    pidfile.parent.mkdir(parents=True, exist_ok=True)

    if pidfile.exists():
        print(f"pidfile already exists for port {port} — run --stop first", file=sys.stderr)
        return 1

    with open(log_path, "w", encoding="utf-8") as logf:
        proc = _spawn(port, db, logf, detached=True)

    base = f"http://127.0.0.1:{port}"
    try:
        _await_ready(proc, base)
    except InstanceExitedError:
        print(f"instance exited early (code {proc.returncode}) — see {log_path}", file=sys.stderr)
        return 1
    except InstanceNotReadyError:
        proc.terminate()
        print(f"instance never became ready — see {log_path}", file=sys.stderr)
        return 1

    pidfile.write_text(str(proc.pid), encoding="utf-8")
    print(f"READY pid={proc.pid} base={base}")
    return 0


def stop_detached(run_dir: Path, port: int) -> int:
    """Stop a pidfile-managed instance; prints ``STOPPED pid=<pid> port=<port>``."""
    _, _, pidfile = instance_paths(run_dir, port)
    if not pidfile.exists():
        print(f"no pidfile for port {port} — nothing to stop")
        return 0
    pid = int(pidfile.read_text(encoding="utf-8").strip())
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        print(f"kill pid {pid}: {e} (already gone?)")
    pidfile.unlink(missing_ok=True)
    print(f"STOPPED pid={pid} port={port}")
    return 0

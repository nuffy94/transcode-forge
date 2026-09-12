"""One boot substrate for QA demo instances (plans/qa-redesign-spec.md, D1).

Every QA surface that needs a real HTTP server boots it through this module,
always in demo-static mode (seeded, deterministic, no Redis/ffmpeg needed):

* ``launch()`` — attached child process for pytest (tests/qa/ conftest
  consumes it via ``launch_qa_app``). Torn down on context exit; raises with
  a server-log tail if the app dies or never gets ready.
* ``start_detached()`` / ``stop_detached()`` — pidfile-managed instances that
  outlive the launching process, behind the qa/launch_demo.py CLI (the L3
  sweep's per-agent instances). Their printed ``READY``/``STOPPED`` lines are
  a contract the L3 workflow's agent prompts rely on — do not reword them.
Instances own themselves from boot: ``demo_env()`` passes
``TF_ADMIN_PASSWORD``, so the app mints the admin at startup (R-040) and no
surface carries its own auth bootstrap.

An instance is identified by the disposable database it was given, and that
one identity gates every step. ``demo_env()`` states every TF_* setting the
child runs on and drops the application namespace it inherited, so nothing
ambient can move the database out from under it. Readiness is not "something
answered on the port": the responder has to log in as the admin this boot
minted and name that database. Shutdown asks the same question before it
signals anything, so a pid the OS may have handed to another program is
never signalled, and the record is removed only once the instance has
stopped answering as itself.

A future ``pg`` mode (same app pointed at a Postgres URL) hooks in here when
S6b lands — not built yet.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

AUTH_SECRET = "qa-sweep-fixed-secret"

# Every QA instance owns itself from boot (R-040): the app mints the admin
# from this at startup, so nothing has to POST a bootstrap afterwards.
QA_ADMIN_PASSWORD = "qa-sweep-password-123"
_READY_ATTEMPTS = 60
_READY_INTERVAL = 0.5
_PROBE_TIMEOUT = 1.0

# How long a stop waits for the instance to let go of its port, before and
# after escalating.
_STOP_GRACE_SECONDS = 8.0
_STOP_KILL_SECONDS = 4.0
_STOP_POLL_INTERVAL = 0.25

# The application's own configuration namespace. None of it is inherited:
# an ambient TF_DB_PATH is promoted over TF_DB_URL by Settings, so a sweep
# launched from a configured service shell would migrate and mutate that
# database instead of its disposable one. AWS_* goes too, since the S3
# backend reads those directly, outside the TF_ prefix.
_APP_ENV_PREFIXES = ("TF_", "AWS_")


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


def database_url(db: Path) -> str:
    """The database URL an instance holding ``db`` reports as its own."""
    return f"sqlite:///{db.resolve().as_posix()}"


def _inherited_os_env() -> dict[str, str]:
    """The launching environment minus the application's own namespace.

    Settings match environment names case-insensitively, so the test is on
    the upper-cased key: a stray ``tf_db_path`` counts too.
    """
    return {k: v for k, v in os.environ.items() if not k.upper().startswith(_APP_ENV_PREFIXES)}


def demo_env(db: Path) -> dict[str, str]:
    """The canonical demo-static environment for a QA instance.

    OS and runtime variables are inherited; every application setting the
    instance runs on is stated here, so nothing the launching shell already
    configures can change which database the sweep opens.
    """
    return {
        **_inherited_os_env(),
        "TF_DEMO_STATIC": "true",
        "TF_DB_URL": database_url(db),
        "TF_AUTH_SECRET": AUTH_SECRET,
        "TF_ADMIN_PASSWORD": QA_ADMIN_PASSWORD,
        "TF_LOG_LEVEL": "warning",
    }


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


def _reported_database(base_url: str) -> str | None:
    """Ask whatever is on ``base_url`` which database it is serving.

    ``/api/system/info`` is behind the admin session, so a bare 200 cannot
    answer this: the responder has to be a Transcode Forge instance that
    minted the admin from the environment we handed our own child, and it
    has to name the database we allocated. Returns None when nothing usable
    answers: a stranger on the port, a half-booted app, or no listener.
    """
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    login = urllib.request.Request(
        f"{base_url}/api/auth/login",
        data=json.dumps({"password": QA_ADMIN_PASSWORD}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener.open(login, timeout=_PROBE_TIMEOUT):
            pass
        with opener.open(f"{base_url}/api/system/info", timeout=_PROBE_TIMEOUT) as r:
            payload = json.load(r)
    except (OSError, ValueError):  # URLError/HTTPError are OSError; bad JSON is ValueError
        return None
    if not isinstance(payload, dict):
        return None
    database = payload.get("database")
    return database if isinstance(database, str) else None


def _await_ready(proc: subprocess.Popen[bytes], base_url: str, database: str) -> None:
    """Wait until the child we spawned is the thing answering on the port."""
    for _ in range(_READY_ATTEMPTS):
        if proc.poll() is not None:
            raise InstanceExitedError(proc.returncode)
        if _reported_database(base_url) == database:
            return
        time.sleep(_READY_INTERVAL)
    raise InstanceNotReadyError()


def _await_gone(base_url: str, database: str, seconds: float) -> bool:
    """True once the instance has stopped answering on its port as itself."""
    deadline = time.monotonic() + seconds
    while True:
        if _reported_database(base_url) != database:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_STOP_POLL_INTERVAL)


def _log_tail(log_path: Path) -> str:
    try:
        return log_path.read_text("utf-8", "replace")[-2500:]
    except OSError:
        return "(no log)"


@contextmanager
def launch(instance_dir: Path, port: int) -> Iterator[str]:
    """Boot an attached demo-static instance on ``port``; yield its base URL.

    The instance mints its own admin at startup with ``QA_ADMIN_PASSWORD``.
    It is a child process — torn down on exit.
    """
    instance_dir.mkdir(parents=True, exist_ok=True)
    db = instance_dir / "demo.db"
    log_path = instance_dir / "server.log"
    base_url = f"http://127.0.0.1:{port}"

    logf = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 — closed in finally
    proc = _spawn(port, db, logf, detached=False)
    try:
        try:
            _await_ready(proc, base_url, database_url(db))
        except InstanceExitedError as e:
            raise RuntimeError(
                f"QA server exited early (code {e.returncode}):\n{_log_tail(log_path)}"
            ) from e
        except InstanceNotReadyError:
            raise RuntimeError(
                f"nothing on port {port} proved it is the QA instance we started:"
                f"\n{_log_tail(log_path)}"
            ) from None

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
        _await_ready(proc, base, database_url(db))
    except InstanceExitedError:
        print(f"instance exited early (code {proc.returncode}) — see {log_path}", file=sys.stderr)
        return 1
    except InstanceNotReadyError:
        proc.terminate()
        print(
            f"nothing on port {port} proved it is the instance we started. See {log_path}",
            file=sys.stderr,
        )
        return 1

    pidfile.write_text(str(proc.pid), encoding="utf-8")
    print(f"READY pid={proc.pid} base={base}")
    return 0


def _escalate(pid: int) -> None:
    """Ask harder, where the platform has a harder way to ask. Windows
    ``os.kill`` already terminates outright, so SIGTERM was the hard ask
    there. Whether it worked is decided by the caller's next wait, not by
    this call."""
    harder = getattr(signal, "SIGKILL", None)
    if harder is None:
        return
    try:
        os.kill(pid, harder)
    except OSError as e:
        print(f"escalating on pid {pid}: {e}", file=sys.stderr)


def stop_detached(run_dir: Path, port: int) -> int:
    """Stop the instance started for this run dir and port.

    Prints ``STOPPED pid=<pid> port=<port>`` once the instance is actually
    gone. The recorded pid is signalled only after the instance on that port
    proves it is ours (it serves our disposable database), so a record left
    behind by a dead instance can never take an unrelated program down with
    it. A stop that could not stop anything keeps its record and returns 1.
    """
    db, _, pidfile = instance_paths(run_dir, port)
    if not pidfile.exists():
        print(f"no pidfile for port {port} — nothing to stop")
        return 0

    pid = int(pidfile.read_text(encoding="utf-8").strip())
    base = f"http://127.0.0.1:{port}"
    database = database_url(db)

    if _reported_database(base) != database:
        pidfile.unlink(missing_ok=True)
        print(f"instance for port {port} is already gone, removed its stale record")
        return 0

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        print(f"cannot stop pid {pid} on port {port}: {e}", file=sys.stderr)
        return 1

    if not _await_gone(base, database, _STOP_GRACE_SECONDS):
        _escalate(pid)
        if not _await_gone(base, database, _STOP_KILL_SECONDS):
            print(
                f"pid {pid} is still serving port {port} after being told to stop"
                f". Record kept at {pidfile}",
                file=sys.stderr,
            )
            return 1

    pidfile.unlink(missing_ok=True)
    print(f"STOPPED pid={pid} port={port}")
    return 0

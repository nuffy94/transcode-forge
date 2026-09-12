"""Substrate self-test — the detached CLI lifecycle in qa/instance.py.

The attached `launch()` path gets live coverage from every other module in
this suite (the session fixture boots through it). What nothing else covers
is the pidfile-managed detached
mode behind qa/launch_demo.py — the L3 workflow's contract (`READY pid=…
base=…` / `STOPPED …` stdout lines, exit codes, pidfile lifecycle). Lock the
exact wording here: the L3 agent prompts parse these lines.
"""

import json
import os
import re
import subprocess
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

import pytest

from qa.instance import (
    database_url,
    demo_env,
    instance_paths,
    pick_free_port,
    start_detached,
    stop_detached,
)
from transcode_forge.config import Settings


class _Impostor(BaseHTTPRequestHandler):
    """A stranger already on the port: 200 and a plausible body for anything.

    It is the worst case for a readiness check that only looks at the status
    code, including the shape of a real system-info answer, for a database
    that is not the one this QA instance was given.
    """

    def _reply(self) -> None:
        body = json.dumps(
            {"status": "alive", "ok": True, "database": "sqlite:///not-the-qa-database.db"}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # BaseHTTPRequestHandler verb dispatch
        self._reply()

    def do_POST(self) -> None:  # BaseHTTPRequestHandler verb dispatch
        self._reply()

    def log_message(self, fmt: str, *args: object) -> None:
        pass  # keep the test output clean


@pytest.mark.qa
def test_start_refuses_when_pidfile_exists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    port = 18990
    _, _, pidfile = instance_paths(tmp_path, port)
    pidfile.parent.mkdir(parents=True)
    pidfile.write_text("12345", encoding="utf-8")

    def _no_spawn(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not spawn when a pidfile exists")

    monkeypatch.setattr(subprocess, "Popen", _no_spawn)

    assert start_detached(tmp_path, port) == 1
    captured = capsys.readouterr()
    assert f"pidfile already exists for port {port} — run --stop first" in captured.err
    assert pidfile.read_text(encoding="utf-8") == "12345"  # untouched


@pytest.mark.qa
def test_stop_without_pidfile_is_a_noop(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert stop_detached(tmp_path, 18991) == 0
    assert "no pidfile for port 18991 — nothing to stop" in capsys.readouterr().out


@pytest.mark.qa
def test_detached_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    port = pick_free_port()
    _, _, pidfile = instance_paths(tmp_path, port)

    start_rc = start_detached(tmp_path, port)
    started = capsys.readouterr().out
    try:
        assert start_rc == 0, f"start failed: {started!r}"
        ready = re.fullmatch(r"READY pid=(\d+) base=(http://127\.0\.0\.1:\d+)\n", started)
        assert ready, f"READY contract broken: {started!r}"
        record = pidfile.read_text(encoding="utf-8").splitlines()
        assert record[0] == ready.group(1)  # still a pidfile on its first line
        assert record[1] == database_url(instance_paths(tmp_path, port)[0])
        with urllib.request.urlopen(f"{ready.group(2)}/api/health/live", timeout=5) as r:
            assert r.status == 200
    finally:
        stop_rc = stop_detached(tmp_path, port)
        stopped = capsys.readouterr().out

    assert stop_rc == 0
    assert re.fullmatch(rf"STOPPED pid=\d+ port={port}\n", stopped), (
        f"STOPPED contract broken: {stopped!r}"
    )
    assert not pidfile.exists()


@pytest.mark.qa
def test_ambient_database_alias_cannot_move_the_qa_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Q01: the shell that launches a sweep may already configure a real
    instance. TF_DB_PATH is promoted over TF_DB_URL by Settings, so the
    check is on the resolved Settings the child would build, not on the
    dictionary."""
    monkeypatch.setenv("TF_DB_PATH", "do-not-open-outside-qa.db")
    monkeypatch.setenv("Tf_Db_Url", "postgresql://ambient/real")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ambient-key")

    db = tmp_path / "demo.db"
    env = demo_env(db)

    with mock.patch.dict(os.environ, env, clear=True):
        settings = Settings()

    assert settings.db_url == f"sqlite:///{db.resolve().as_posix()}"
    assert settings.db_path == ""
    assert [k for k in env if k.upper().startswith("AWS_")] == []


@pytest.mark.qa
def test_start_refuses_an_impostor_on_the_port(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Q02: a stranger answering 200 on the requested port must never be
    handed out as the target, and must leave no ownership record behind."""
    port = pick_free_port()
    impostor = HTTPServer(("127.0.0.1", port), _Impostor)
    serving = threading.Thread(target=impostor.serve_forever, daemon=True)
    serving.start()
    try:
        rc = start_detached(tmp_path, port)
        out = capsys.readouterr()
    finally:
        impostor.shutdown()
        impostor.server_close()
        serving.join(timeout=10)

    assert rc == 1, f"start accepted the impostor: {out.out!r}"
    assert "READY" not in out.out
    _, _, pidfile = instance_paths(tmp_path, port)
    assert not pidfile.exists()


@pytest.mark.qa
def test_stop_never_signals_a_pid_it_cannot_prove_is_its_own(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Q03: a record left by a dead instance names a pid the OS may since
    have handed to something else, here the test runner itself."""
    port = pick_free_port()  # nothing is listening on it
    db, _, pidfile = instance_paths(tmp_path, port)
    pidfile.parent.mkdir(parents=True)
    pidfile.write_text(f"{os.getpid()}\n{database_url(db)}\n", encoding="utf-8")

    signalled: list[int] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: signalled.append(pid))

    rc = stop_detached(tmp_path, port)
    out = capsys.readouterr().out

    assert signalled == [], "signalled a process it does not own"
    assert rc == 0
    assert "STOPPED" not in out
    assert not pidfile.exists()


@pytest.mark.qa
def test_stop_leaves_alone_a_port_it_cannot_claim(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Q03: a probe that cannot prove ownership is not proof of a dead
    instance. Something is on the port, so nothing is signalled and the
    record stays for the operator."""
    port = pick_free_port()
    db, _, pidfile = instance_paths(tmp_path, port)
    pidfile.parent.mkdir(parents=True)
    pidfile.write_text(f"{os.getpid()}\n{database_url(db)}\n", encoding="utf-8")

    signalled: list[int] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: signalled.append(pid))

    stranger = HTTPServer(("127.0.0.1", port), _Impostor)
    serving = threading.Thread(target=stranger.serve_forever, daemon=True)
    serving.start()
    try:
        rc = stop_detached(tmp_path, port)
        out = capsys.readouterr()
    finally:
        stranger.shutdown()
        stranger.server_close()
        serving.join(timeout=10)

    assert signalled == [], "signalled a process it does not own"
    assert rc == 1
    assert "STOPPED" not in out.out
    assert pidfile.exists()


@pytest.mark.qa
def test_stop_reports_a_denied_termination_as_a_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Q03: access denied is not the same as already gone. A stop that could
    not stop anything keeps its record and says so."""
    port = pick_free_port()
    assert start_detached(tmp_path, port) == 0, capsys.readouterr().err
    capsys.readouterr()
    _, _, pidfile = instance_paths(tmp_path, port)

    def _denied(pid: int, sig: int) -> None:
        raise PermissionError("access is denied")

    try:
        monkeypatch.setattr(os, "kill", _denied)
        rc = stop_detached(tmp_path, port)
        out = capsys.readouterr()
        assert rc == 1
        assert "STOPPED" not in out.out
        assert pidfile.exists(), "a failed stop threw away its own record"
    finally:
        monkeypatch.undo()
        stop_detached(tmp_path, port)
        capsys.readouterr()

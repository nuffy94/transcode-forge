"""Substrate self-test — the detached CLI lifecycle in qa/instance.py.

The attached `launch()` path gets live coverage from every other module in
this suite (the session fixture boots through it; test_setup_flow exercises
create_admin=False). What nothing else covers is the pidfile-managed detached
mode behind qa/launch_demo.py — the L3 workflow's contract (`READY pid=…
base=…` / `STOPPED …` stdout lines, exit codes, pidfile lifecycle). Lock the
exact wording here: the L3 agent prompts parse these lines.
"""

import re
import subprocess
import urllib.request
from pathlib import Path

import pytest

from qa.instance import instance_paths, pick_free_port, start_detached, stop_detached


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
        assert pidfile.read_text(encoding="utf-8") == ready.group(1)
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

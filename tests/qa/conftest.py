"""QA sweep harness — launches the real app in demo-static mode (seeded,
deterministic, no Redis/ffmpeg) as a subprocess and completes first-run setup,
so the deterministic sweep and the AI exploratory sweep share one consistent,
populated target with no live box required.

Run with:  uv run pytest tests/qa/        (excluded from the default suite)
"""

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest

QA_PORT = 18799
BASE_URL = f"http://127.0.0.1:{QA_PORT}"
ADMIN_PW = "qa-sweep-password-123"


def _post_json(url: str, payload: bytes) -> int:
    req = urllib.request.Request(
        url, data=payload, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.fixture(scope="session")
def qa_base_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    qa_dir = tmp_path_factory.mktemp("qa")
    db = qa_dir / "qa_demo.db"
    log_path = qa_dir / "server.log"
    env = {
        **os.environ,
        "TF_DEMO_STATIC": "true",
        "TF_DB_URL": f"sqlite:///{db}",
        "TF_AUTH_SECRET": "qa-sweep-fixed-secret",
        "TF_LOG_LEVEL": "warning",
    }
    logf = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 — closed in finally
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "transcode_forge.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(QA_PORT),
            "--log-level",
            "warning",
        ],
        env=env,
        stdout=logf,
        stderr=subprocess.STDOUT,
    )

    def _server_log() -> str:
        try:
            return log_path.read_text("utf-8", "replace")[-2500:]
        except OSError:
            return "(no log)"

    try:
        for _ in range(60):
            if proc.poll() is not None:
                raise RuntimeError(
                    f"QA server exited early (code {proc.returncode}):\n{_server_log()}"
                )
            try:
                with urllib.request.urlopen(f"{BASE_URL}/api/health/live", timeout=1) as r:
                    if r.status == 200:
                        break
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
                time.sleep(0.5)
        else:
            raise RuntimeError(f"QA demo server failed to become ready:\n{_server_log()}")

        # First-run setup creates the admin so authenticated pages are reachable.
        import json

        status = _post_json(
            f"{BASE_URL}/api/auth/setup", json.dumps({"password": ADMIN_PW}).encode()
        )
        assert status in (200, 409), f"setup failed: HTTP {status}"

        yield BASE_URL
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        logf.close()


@pytest.fixture(scope="session")
def admin_pw() -> str:
    return ADMIN_PW


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict) -> dict:
    return {**browser_context_args, "viewport": {"width": 1440, "height": 900}}

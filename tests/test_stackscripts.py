"""Render-mode tests for the Linode StackScripts.

Both scripts support TF_SS_RENDER_DIR: render every config file into a
directory and exit before any system mutation (no installs, mounts, or
docker). These tests exercise that path — heredoc quoting and conditional
compose assembly are where deploy scripts rot.

Requires bash (CI runners have it; Git Bash works locally). Skipped when
no working bash is on PATH.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEDULER = REPO_ROOT / "deploy" / "linode" / "stackscript-scheduler.sh"
WORKER = REPO_ROOT / "deploy" / "linode" / "stackscript-worker.sh"

SCHEDULER_FULL_ENV = {
    "DOMAIN": "forge.example.com",
    "CLOUDFLARE_DNS_TOKEN_PASSWORD": "cf-sentinel-token",
    "S3_ENDPOINT": "us-ord-1.linodeobjects.com",
    "S3_BUCKET": "forge-media",
    "S3_ACCESS_KEY": "AKIATEST",
    "S3_SECRET_PASSWORD": "s3-sentinel-secret",
}

WORKER_ENV = {
    "SERVER_URL": "https://forge.example.com",
    "WORKER_TOKEN_PASSWORD": "worker-sentinel-token",
    "S3_ENDPOINT": "https://us-ord-1.linodeobjects.com",
    "S3_ACCESS_KEY": "AKIATEST",
    "S3_SECRET_PASSWORD": "s3-sentinel-secret",
}


def _find_bash() -> str | None:
    candidates = [
        shutil.which("bash"),
        # Git Bash isn't on PATH for a stock Windows PowerShell session.
        r"C:\Program Files\Git\bin\bash.exe",
    ]
    for bash in candidates:
        if not bash or not Path(bash).exists():
            continue
        try:
            probe = subprocess.run([bash, "-c", "echo ok"], capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0 and b"ok" in probe.stdout:
            return bash
    return None


BASH = _find_bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="no working bash on PATH")


def _render(script: Path, tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    out = tmp_path / "render"
    out.mkdir(exist_ok=True)
    run_env = {
        **os.environ,
        **env,
        # Git Bash on Windows wants forward slashes.
        "TF_SS_RENDER_DIR": str(out).replace("\\", "/"),
    }
    result = subprocess.run(
        [BASH, str(script)], env=run_env, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, f"render failed:\n{result.stdout}\n{result.stderr}"
    return result


def _compose(tmp_path: Path) -> dict:
    return yaml.safe_load((tmp_path / "render" / "docker-compose.yml").read_text())


def _env_file(tmp_path: Path) -> dict[str, str]:
    lines = (tmp_path / "render" / ".env").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))


class TestBashSyntax:
    def test_scheduler_parses(self):
        assert subprocess.run([BASH, "-n", str(SCHEDULER)]).returncode == 0

    def test_worker_parses(self):
        assert subprocess.run([BASH, "-n", str(WORKER)]).returncode == 0


class TestSchedulerRender:
    def test_full_stack(self, tmp_path: Path):
        _render(SCHEDULER, tmp_path, SCHEDULER_FULL_ENV)
        compose = _compose(tmp_path)

        services = compose["services"]
        assert set(services) == {"redis", "postgres", "scheduler", "worker", "caddy"}
        # DNS-01 mode builds Caddy with the Cloudflare module.
        assert services["caddy"]["build"] == "./caddy"
        assert (tmp_path / "render" / "caddy" / "Dockerfile").exists()
        # Scheduler is loopback-only; Caddy is the public listener.
        assert services["scheduler"]["ports"] == ["127.0.0.1:8000:8000"]
        assert "80:80" in services["caddy"]["ports"]
        # Local worker joins on demand via the compose profile.
        assert services["worker"]["profiles"] == ["worker"]
        assert (tmp_path / "render" / "join-local-worker.sh").exists()

        env = _env_file(tmp_path)
        assert env["TF_S3_ENDPOINT_URL"] == "https://us-ord-1.linodeobjects.com"
        assert env["TF_S3_REGION"] == "us-ord-1"
        assert env["TF_SESSION_SECURE"] == "true"
        assert 1 <= int(env["TF_WORKER_MAX_CONCURRENT"]) <= 4

        caddyfile = (tmp_path / "render" / "Caddyfile").read_text()
        assert "dns cloudflare" in caddyfile

    def test_minimal_localhost_only(self, tmp_path: Path):
        _render(SCHEDULER, tmp_path, {})
        compose = _compose(tmp_path)

        assert set(compose["services"]) == {"redis", "postgres", "scheduler", "worker"}
        assert not (tmp_path / "render" / "Caddyfile").exists()

        env = _env_file(tmp_path)
        assert env["TF_SESSION_SECURE"] == "false"
        assert env["TF_S3_ENDPOINT_URL"] == ""
        assert env["TF_DB_URL"].startswith("postgresql://tf:")

    def test_managed_db_drops_postgres(self, tmp_path: Path):
        url = "postgresql://tf:pw@db.example.com:5432/forge?sslmode=require"
        _render(SCHEDULER, tmp_path, {"MANAGED_DB_URL_PASSWORD": url})
        compose = _compose(tmp_path)

        assert "postgres" not in compose["services"]
        assert "postgres-data" not in compose["volumes"]
        assert _env_file(tmp_path)["TF_DB_URL"] == url

    def test_http01_uses_stock_caddy(self, tmp_path: Path):
        _render(SCHEDULER, tmp_path, {"DOMAIN": "forge.example.com"})
        compose = _compose(tmp_path)

        assert compose["services"]["caddy"]["image"] == "caddy:2"
        assert "build" not in compose["services"]["caddy"]
        assert "dns cloudflare" not in (tmp_path / "render" / "Caddyfile").read_text()

    def test_secrets_never_reach_stdout(self, tmp_path: Path):
        result = _render(SCHEDULER, tmp_path, SCHEDULER_FULL_ENV)
        output = result.stdout + result.stderr
        assert "cf-sentinel-token" not in output
        assert "s3-sentinel-secret" not in output
        # Generated secrets stay in .env too.
        env = _env_file(tmp_path)
        assert env["TF_AUTH_SECRET"] not in output
        assert env["TF_PG_PASSWORD"] not in output

    @pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX-only")
    def test_env_file_is_private(self, tmp_path: Path):
        _render(SCHEDULER, tmp_path, SCHEDULER_FULL_ENV)
        mode = (tmp_path / "render" / ".env").stat().st_mode & 0o777
        assert mode == 0o600


class TestWorkerRender:
    def test_render(self, tmp_path: Path):
        _render(WORKER, tmp_path, WORKER_ENV)
        compose = _compose(tmp_path)

        worker = compose["services"]["worker"]
        assert worker["command"] == ["python", "-m", "transcode_forge.worker"]
        assert worker["environment"]["TF_PREFERRED_BACKEND"] == "cpu"
        # Outbound-only: no published ports.
        assert "ports" not in worker

        env = _env_file(tmp_path)
        assert env["TF_SERVER_URL"] == "https://forge.example.com"
        assert env["TF_S3_REGION"] == "us-ord-1"
        assert 1 <= int(env["TF_WORKER_MAX_CONCURRENT"]) <= 4

    def test_explicit_concurrency_wins(self, tmp_path: Path):
        _render(WORKER, tmp_path, {**WORKER_ENV, "WORKER_MAX_CONCURRENT": "2"})
        assert _env_file(tmp_path)["TF_WORKER_MAX_CONCURRENT"] == "2"

    def test_secrets_never_reach_stdout(self, tmp_path: Path):
        result = _render(WORKER, tmp_path, WORKER_ENV)
        output = result.stdout + result.stderr
        assert "worker-sentinel-token" not in output
        assert "s3-sentinel-secret" not in output

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


class TestPublishable:
    """The Linode API rejects a StackScript containing any non-ASCII byte
    ("Invalid special character at position N") and the publish tool prints
    that as a 400 rather than failing loudly. Two em dashes added in #104
    sat in the scheduler script for months, silently blocking every
    republish, until R-040 made republishing a hard gate. ASCII-only is the
    rule; nothing else checks it. It also matches the no-em-dash
    register, which tests/test_template_copy.py enforces for the UI."""

    @pytest.mark.parametrize("script", [SCHEDULER, WORKER], ids=["scheduler", "worker"])
    def test_is_pure_ascii(self, script: Path):
        text = script.read_text(encoding="utf-8")
        offenders = [
            (i, ch, text[:i].count("\n") + 1) for i, ch in enumerate(text) if ord(ch) > 127
        ]
        detail = "; ".join(
            f"{script.name} line {line}: U+{ord(ch):04X} {ch!r} at position {i}"
            for i, ch, line in offenders
        )
        assert not offenders, f"the Linode API will refuse to publish this: {detail}"


class TestFailsLoudly:
    """A deploy that dies must not leave a success artifact behind.

    NEXT-STEPS.txt is written ~50 lines before the first risky command, so
    when `docker compose pull` failed under `set -e` the script aborted and
    left a dead instance that looked finished: no containers, no error, and
    a cheerful next-steps file. That cost a 25 minute wait on a deploy which
    had already given up. One function owns the failure verdict now, and it
    withdraws the success artifact.
    """

    def test_failure_withdraws_the_success_artifact(self):
        body = SCHEDULER.read_text(encoding="utf-8")
        assert "deploy_failed()" in body, "no single owner for the failure verdict"
        fn = body.split("deploy_failed()", 1)[1].split("\n}", 1)[0]
        assert "rm -f" in fn and "NEXT-STEPS.txt" in fn, (
            "deploy_failed must remove NEXT-STEPS.txt, or a dead deploy still looks finished"
        )
        assert "DEPLOY-FAILED.txt" in fn, "a failed deploy should say so on disk"

    def test_every_risky_step_routes_to_it(self):
        body = SCHEDULER.read_text(encoding="utf-8")
        # The pull, the up, and the readiness gate are the three ways a
        # deploy dies with the machine otherwise healthy.
        assert body.count("deploy_failed ") >= 3, "a risky step is not routed through deploy_failed"
        assert "WARNING: scheduler not ready" not in body, (
            "not-ready is a failed deploy, not a warning"
        )

    def test_pull_is_retried_and_prefers_ipv4(self):
        body = SCHEDULER.read_text(encoding="utf-8")
        # ghcr.io is unreachable over IPv6 from us-ord; docker tries v6 first
        # on a dual-stack host and the pull resets mid-transfer.
        assert "precedence ::ffff:0:0/96" in body, "nothing makes the pull prefer IPv4"
        assert "for attempt in" in body, "the image pull is not retried"


class TestSshPolicy:
    """The image ships PermitRootLogin yes and PasswordAuthentication yes,
    and the Cloud Firewall allows 22 from anywhere, so an unhardened deploy
    is a public root-password endpoint. The deploy already requires a key,
    so password auth buys nothing. Lish does not go over SSH, so key-only
    cannot lock an operator out."""

    def test_scheduler_turns_off_password_auth(self):
        body = SCHEDULER.read_text(encoding="utf-8")
        assert "PasswordAuthentication no" in body
        assert "KbdInteractiveAuthentication no" in body
        assert "PermitRootLogin prohibit-password" in body

    def test_it_lands_in_a_drop_in_not_an_edit(self):
        # Editing sshd_config in place fights the image's own updates; a
        # drop-in is additive and survives them.
        body = SCHEDULER.read_text(encoding="utf-8")
        assert "/etc/ssh/sshd_config.d/" in body


class TestRegistryOverIpv4:
    """GitHub's blob CDN resets large transfers over IPv6 from some Linode
    regions, and a lost pull is a lost deploy. /etc/gai.conf states the
    preference, but dockerd is a Go binary and Go ignores gai.conf unless it
    resolves through glibc, so the preference alone was measurably inert:
    with it in place dockerd still opened IPv6 connections for half the
    pull. Both scripts must state the preference AND make dockerd honour it.
    """

    @pytest.mark.parametrize("script", [SCHEDULER, WORKER], ids=["scheduler", "worker"])
    def test_the_preference_reaches_dockerd(self, script: Path):
        body = script.read_text(encoding="utf-8")
        assert "precedence ::ffff:0:0/96" in body, "gai.conf preference missing"
        assert "GODEBUG=netdns=cgo" in body, (
            "gai.conf alone does not bind dockerd; without the cgo resolver "
            "the pull still goes over IPv6"
        )
        assert "/etc/systemd/system/docker.service.d/" in body

    @pytest.mark.parametrize("script", [SCHEDULER, WORKER], ids=["scheduler", "worker"])
    def test_the_pull_is_retried(self, script: Path):
        # Defence in depth behind the address-family fix, not the mechanism.
        body = script.read_text(encoding="utf-8")
        assert "Image pull attempt" in body
        assert "for attempt in 1 2 3" in body


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

    def test_every_secret_in_env_reaches_the_scheduler(self, tmp_path: Path):
        """A value written into .env that the generated compose never passes
        through is silently dropped. For TF_ADMIN_PASSWORD that is fatal
        rather than quiet: the scheduler refuses to boot without it (R-040),
        so the whole deploy would come up dead. Nothing else in CI renders
        this compose file, so the wiring is checked here."""
        _render(SCHEDULER, tmp_path, SCHEDULER_FULL_ENV)
        env = _env_file(tmp_path)
        scheduler_env = _compose(tmp_path)["services"]["scheduler"]["environment"]
        passed_through = (
            " ".join(str(v) for v in scheduler_env.values())
            if isinstance(scheduler_env, dict)
            else " ".join(scheduler_env)
        )

        for key in ("TF_ADMIN_PASSWORD", "TF_AUTH_SECRET", "TF_DB_URL"):
            assert env.get(key), f"{key} is missing from the rendered .env"
            assert f"${{{key}}}" in passed_through, (
                f"{key} is written into .env but the scheduler container never receives it"
            )

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

"""Golden-render tests for the LKE Helm chart (deploy/lke/transcode-forge).

Like tests/test_stackscripts.py for the StackScripts: render the chart with
`helm template`, parse the YAML, and pin the properties a live cluster
depends on — probe paths, ports, secret refs, the rolling-update contract,
and the Postgres/external-DB toggle. No cluster needed; CI runners ship
helm. Skipped when helm isn't on PATH.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART = REPO_ROOT / "deploy" / "lke" / "transcode-forge"

HELM = shutil.which("helm")
pytestmark = pytest.mark.skipif(HELM is None, reason="no helm on PATH")

# Sentinel secrets: never real, and asserted to stay out of the workload
# manifests (they belong only in the Secret).
BASE_SET = [
    "secrets.authSecret=sentinel-auth",
    "secrets.pgPassword=sentinel-pg",
    "secrets.s3AccessKeyId=AKIATEST",
    "secrets.s3SecretAccessKey=sentinel-s3",
    "s3.endpointUrl=https://us-ord-1.linodeobjects.com",
    "s3.region=us-ord-1",
]

FULLNAME = "tf-transcode-forge"


def _helm(*args: str) -> subprocess.CompletedProcess:
    assert HELM is not None
    return subprocess.run([HELM, *args], capture_output=True, text=True, timeout=60)


def _template(*extra_set: str, values_files: tuple[str, ...] = ()) -> dict[tuple[str, str], dict]:
    cmd = ["template", "tf", str(CHART)]
    for f in values_files:
        cmd += ["-f", str(CHART / f)]
    for s in (*BASE_SET, *extra_set):
        cmd += ["--set", s]
    result = _helm(*cmd)
    assert result.returncode == 0, f"helm template failed:\n{result.stderr}"
    docs = [d for d in yaml.safe_load_all(result.stdout) if d]
    return {(d["kind"], d["metadata"]["name"]): d for d in docs}


def _template_error(*extra_set: str) -> str:
    cmd = ["template", "tf", str(CHART)]
    for s in (*BASE_SET, *extra_set):
        cmd += ["--set", s]
    result = _helm(*cmd)
    assert result.returncode != 0, "expected render to fail"
    return result.stderr


def _container(doc: dict) -> dict:
    return doc["spec"]["template"]["spec"]["containers"][0]


def _env(container: dict) -> dict[str, dict]:
    return {e["name"]: e for e in container["env"]}


def _secret_ref(env_entry: dict) -> tuple[str, str]:
    ref = env_entry["valueFrom"]["secretKeyRef"]
    return ref["name"], ref["key"]


class TestLint:
    def test_chart_lints_clean(self):
        result = _helm(
            "lint", str(CHART), "--set", "secrets.authSecret=x", "--set", "secrets.pgPassword=x"
        )
        assert result.returncode == 0, result.stdout + result.stderr


class TestDefaultRender:
    def test_expected_objects(self):
        docs = _template()
        assert set(docs) == {
            ("Secret", FULLNAME),
            ("Service", FULLNAME),
            ("Service", f"{FULLNAME}-redis"),
            ("Service", f"{FULLNAME}-postgres"),
            ("Deployment", f"{FULLNAME}-scheduler"),
            ("Deployment", f"{FULLNAME}-redis"),
            ("Deployment", f"{FULLNAME}-worker"),
            ("StatefulSet", f"{FULLNAME}-postgres"),
        }
        # No Ingress by default, and workers never get a Service —
        # they're outbound-only.

    def test_scheduler_probes_and_rollout(self):
        sched = _template()[("Deployment", f"{FULLNAME}-scheduler")]
        strategy = sched["spec"]["strategy"]
        assert strategy["type"] == "RollingUpdate"
        assert strategy["rollingUpdate"]["maxUnavailable"] == 0

        c = _container(sched)
        assert c["livenessProbe"]["httpGet"]["path"] == "/api/health/live"
        assert c["readinessProbe"]["httpGet"]["path"] == "/api/health/ready"
        assert c["ports"][0]["containerPort"] == 8000
        # preStop drain: without it a rolling update resets whichever
        # request races the endpoint removal (observed live: 1 blip in
        # 170 polls until this landed).
        assert c["lifecycle"]["preStop"]["exec"]["command"] == ["sleep", "5"]

    def test_image_is_pinned(self):
        docs = _template()
        for kind, name in [
            ("Deployment", f"{FULLNAME}-scheduler"),
            ("Deployment", f"{FULLNAME}-worker"),
        ]:
            image = _container(docs[(kind, name)])["image"]
            assert image.startswith("ghcr.io/nuffy94/transcode-forge:")
            assert not image.endswith(":latest")

    def test_secrets_only_live_in_the_secret(self):
        docs = _template()
        secret = docs.pop(("Secret", FULLNAME))
        assert secret["stringData"]["TF_AUTH_SECRET"] == "sentinel-auth"
        # Composed DB URL carries the password and in-cluster PG host.
        assert (
            secret["stringData"]["TF_DB_URL"]
            == f"postgresql://tf:sentinel-pg@{FULLNAME}-postgres:5432/transcode_forge"
        )
        # No sentinel value leaks into any workload manifest.
        assert "sentinel" not in json.dumps(list(docs.values()))

    def test_scheduler_env_wiring(self):
        env = _env(_container(_template()[("Deployment", f"{FULLNAME}-scheduler")]))
        assert _secret_ref(env["TF_DB_URL"]) == (FULLNAME, "TF_DB_URL")
        assert _secret_ref(env["TF_AUTH_SECRET"]) == (FULLNAME, "TF_AUTH_SECRET")
        assert _secret_ref(env["TF_S3_SECRET_ACCESS_KEY"]) == (FULLNAME, "TF_S3_SECRET_ACCESS_KEY")
        assert env["TF_REDIS_URL"]["value"] == f"redis://{FULLNAME}-redis:6379/0"
        assert env["TF_S3_ENDPOINT_URL"]["value"] == "https://us-ord-1.linodeobjects.com"
        assert env["TF_S3_REGION"]["value"] == "us-ord-1"

    def test_worker_defaults(self):
        worker = _template()[("Deployment", f"{FULLNAME}-worker")]
        # Ships scaled to zero: workers need a server-issued token first.
        assert worker["spec"]["replicas"] == 0
        assert worker["spec"]["strategy"]["type"] == "Recreate"

        c = _container(worker)
        assert c["command"] == ["python", "-m", "transcode_forge.worker"]
        env = _env(c)
        assert env["TF_SERVER_URL"]["value"] == f"http://{FULLNAME}:8000"
        assert env["TF_PREFERRED_BACKEND"]["value"] == "cpu"
        assert env["TF_SCRATCH_DIR"]["value"] == "/scratch"
        assert _secret_ref(env["TF_WORKER_TOKEN"]) == (FULLNAME, "TF_WORKER_TOKEN")
        # The CPU limit is the neighbor-protection contract on shared nodes.
        assert c["resources"]["limits"]["cpu"]
        scratch = worker["spec"]["template"]["spec"]["volumes"][0]
        assert scratch["emptyDir"]["sizeLimit"]

    def test_postgres_statefulset(self):
        docs = _template()
        pg = docs[("StatefulSet", f"{FULLNAME}-postgres")]
        env = _env(_container(pg))
        assert _secret_ref(env["POSTGRES_PASSWORD"]) == (FULLNAME, "TF_PG_PASSWORD")
        claim = pg["spec"]["volumeClaimTemplates"][0]
        assert claim["spec"]["resources"]["requests"]["storage"] == "10Gi"
        # Headless service governs the StatefulSet.
        assert docs[("Service", f"{FULLNAME}-postgres")]["spec"]["clusterIP"] == "None"


class TestToggles:
    def test_external_db_drops_postgres(self):
        url = "postgresql://tf:pw@db.example.com:5432/forge?sslmode=require"
        docs = _template("postgres.enabled=false", f"externalDb.url={url}")
        kinds = {kind for kind, _ in docs}
        assert "StatefulSet" not in kinds
        assert ("Service", f"{FULLNAME}-postgres") not in docs
        assert docs[("Secret", FULLNAME)]["stringData"]["TF_DB_URL"] == url
        # No wait-for-postgres init container either.
        sched = docs[("Deployment", f"{FULLNAME}-scheduler")]
        assert "initContainers" not in sched["spec"]["template"]["spec"]

    def test_worker_replicas_require_token(self):
        stderr = _template_error("worker.replicas=1")
        assert "workerToken" in stderr

    def test_worker_scales_with_token(self):
        docs = _template("worker.replicas=2", "secrets.workerToken=sentinel-worker-token")
        assert docs[("Deployment", f"{FULLNAME}-worker")]["spec"]["replicas"] == 2

    def test_worker_extra_env(self):
        docs = _template(r"worker.extraEnv.TF_VMAF_FFMPEG=/usr/bin/ffmpeg")
        env = _env(_container(docs[("Deployment", f"{FULLNAME}-worker")]))
        assert env["TF_VMAF_FFMPEG"]["value"] == "/usr/bin/ffmpeg"

    def test_missing_auth_secret_fails(self):
        result = _helm("template", "tf", str(CHART), "--set", "secrets.pgPassword=x")
        assert result.returncode != 0
        assert "authSecret" in result.stderr

    def test_ingress_off_by_default_on_when_asked(self):
        assert not any(kind == "Ingress" for kind, _ in _template())
        docs = _template("ingress.enabled=true", "ingress.host=forge.example.com")
        ingress = docs[("Ingress", FULLNAME)]
        rule = ingress["spec"]["rules"][0]
        assert rule["host"] == "forge.example.com"
        backend = rule["http"]["paths"][0]["backend"]["service"]
        assert backend["name"] == FULLNAME


class TestTierOverlays:
    @pytest.mark.parametrize("overlay", ["values-medium.yaml", "values-large.yaml"])
    def test_overlay_renders_and_keeps_the_cpu_limit(self, overlay: str):
        docs = _template(values_files=(overlay,))
        worker = docs[("Deployment", f"{FULLNAME}-worker")]
        # Overlays resize but never remove the neighbor-protection limit,
        # and leave replicas at 0 (scaling happens at the token step).
        assert _container(worker)["resources"]["limits"]["cpu"]
        assert worker["spec"]["replicas"] == 0

"""Run the actual deploy state machine with fake Docker/Nginx transport."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
BASH = "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash")
SHA = "a" * 40
OLD = "b" * 40
IMAGE_ROOT = "ghcr.io/yyangwan/geo-visibility-analyze"
LEGACY_IMAGE = "sha256:" + "c" * 64


def exercise(tmp_path, failure="", active=True, mode="deploy"):
    checkout, state, nginx = (tmp_path / name for name in ["checkout", "state", "nginx"])
    for directory in [checkout, state, nginx]:
        directory.mkdir()
    (checkout / ".intelligence.env").write_text("# fake config\n", encoding="utf-8")
    if active:
        initial_slot = "legacy" if active == "legacy" else "blue"
        initial_image = LEGACY_IMAGE if active == "legacy" else f"{IMAGE_ROOT}:{OLD}"
        initial_port = 8000 if active == "legacy" else 18016
        (state / "active-slot").write_text(initial_slot + "\n", encoding="utf-8")
        (state / "active-image").write_text(initial_image + "\n", encoding="utf-8")
        (nginx / "upstream.inc").write_text(f"server 127.0.0.1:{initial_port};\n", encoding="utf-8")
        (nginx / "root.conf").write_text("original config\n", encoding="utf-8")
    if mode in ["rollback", "rollback-legacy"]:
        previous_slot = "legacy" if mode == "rollback-legacy" else "green"
        previous_image = LEGACY_IMAGE if mode == "rollback-legacy" else f"{IMAGE_ROOT}:{SHA}"
        (state / "previous-slot").write_text(previous_slot + "\n", encoding="utf-8")
        (state / "previous-image").write_text(previous_image + "\n", encoding="utf-8")
    script = (ROOT / "deploy/deploy-intelligence.sh").read_text(encoding="utf-8")
    for original, target in [
        ("/opt/geo-visibility-analyze", checkout),
        ("/opt/geo-visibility-deploy/intelligence", state),
        ("/etc/nginx/conf.d/genilink-intelligence-upstream.inc", nginx / "upstream.inc"),
        ("/etc/nginx/conf.d/genilink-intelligence.conf", nginx / "root.conf"),
    ]:
        script = script.replace(original, target.as_posix())
    candidate = tmp_path / "deploy.sh"
    candidate.write_text(script, encoding="utf-8", newline="\n")
    trace = tmp_path / "trace.txt"
    harness = r'''
git() { if [[ "$*" == 'rev-parse HEAD' ]]; then printf '%s\n' "$SHA"; else printf 'main\n'; fi; }
stat() { printf '600\n'; }
flock() { return 0; }
sleep() { return 0; }
mv() {
  if [[ "$FAILURE" == state && "$1" == *'.active-image.next' ]]; then return 1; fi
  command mv "$@"
}
docker() {
  printf 'docker:%s\n' "$*" >> "$TRACE"
  if [[ "$*" == 'image inspect '* ]]; then printf '%s\n' "$SHA"; return; fi
  if [[ "$*" == 'network inspect '* ]]; then return 0; fi
  if [[ "$*" == *'check_intelligence_deployment validate' ]]; then [[ "$FAILURE" != guard ]]; return; fi
  if [[ "$*" == *'run --rm --no-deps migrate' ]]; then [[ "$FAILURE" != migration ]]; return; fi
  if [[ "$*" == *'ps -q api' ]]; then printf 'candidate-api\n'; return; fi
  if [[ "$*" == *'ps -q worker' ]]; then printf 'candidate-worker\n'; return; fi
  if [[ "$*" == 'inspect '* ]]; then
    if [[ "$*" == *'Health.Status'* ]]; then printf 'healthy\n'
    elif [[ "$*" == *'{{.Image}}'* ]]; then printf '%s\n' "$LEGACY_IMAGE"
    else printf 'true\n'; fi
    return
  fi
  if [[ "$*" == *'check_intelligence_deployment health' ]]; then [[ "$FAILURE" != api ]]; return; fi
  if [[ "$*" == *'check_intelligence_deployment watermark' ]]; then printf '2026-10-17 12:00:00\n'; return; fi
  if [[ "$*" == *'check_intelligence_deployment worker '* ]]; then [[ "$FAILURE" != worker ]]; return; fi
  if [[ "$*" == 'run '* ]]; then
    local input
    read -r input
    [[ "$input" == *"$SHA"* ]]
    return
  fi
  return 0
}
curl() {
  printf 'public-health\n' >> "$TRACE"
  if [[ "$*" == *--write-out* ]]; then
    if [[ "$FAILURE" == legacy-public && "$*" == *18018* ]]; then printf '503'; else printf '200'; fi
    return
  fi
  if [[ "$FAILURE" == public ]]; then printf '{"deploymentVersion":"wrong"}\n'; else printf '{"deploymentVersion":"%s"}\n' "$SHA"; fi
}
nginx() {
  printf 'nginx:%s\n' "$*" >> "$TRACE"
  # Fail only candidate validation; restoring the original include must succeed.
  if [[ "$FAILURE" == nginx && "$1" == -t ]] && grep -q 18017 "$UPSTREAM"; then return 1; fi
  return 0
}
source "$1" "$MODE" "$SHA"
'''
    result = subprocess.run(
        [BASH, "-c", harness, "deploy-test", candidate.as_posix()],
        env={**os.environ, "TRACE": trace.as_posix(), "SHA": SHA, "LEGACY_IMAGE": LEGACY_IMAGE, "FAILURE": failure, "MODE": "rollback" if mode == "rollback-legacy" else mode},
        capture_output=True, text=True, timeout=20, check=False,
    )
    return result, trace.read_text(encoding="utf-8"), state, nginx


@pytest.mark.parametrize("failure", ["guard", "migration", "api", "worker", "nginx", "public", "state"])
def test_failure_preserves_active_release_and_restores_upstream(tmp_path, failure):
    result, trace, state, nginx = exercise(tmp_path, failure)
    assert result.returncode != 0
    assert (state / "active-slot").read_text(encoding="utf-8").strip() == "blue"
    assert (nginx / "upstream.inc").read_text(encoding="utf-8") == "server 127.0.0.1:18016;\n"
    assert (nginx / "root.conf").read_text(encoding="utf-8") == "original config\n"
    assert not any("genilink-intelligence-blue" in line and "stop worker api" in line for line in trace.splitlines())
    if failure in ["api", "worker", "nginx", "public", "state"]:
        assert "stop worker api" in trace
    if failure == "state":
        assert (state / "active-image").read_text(encoding="utf-8").strip() == f"{IMAGE_ROOT}:{OLD}"
        assert not (state / "previous-slot").exists()
    if failure == "guard":
        assert not any(line.endswith("run --rm --no-deps migrate") for line in trace.splitlines())


def test_first_install_failure_restores_absent_nginx_configuration(tmp_path):
    result, _, state, nginx = exercise(tmp_path, "public", active=False)
    assert result.returncode != 0
    assert not (nginx / "root.conf").exists()
    assert not (nginx / "upstream.inc").exists()
    assert not (state / "active-slot").exists()


def test_success_verifies_candidate_and_switched_revision_before_stopping_old_slot(tmp_path):
    result, trace, state, nginx = exercise(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (state / "active-slot").read_text(encoding="utf-8").strip() == "green"
    assert (state / "previous-slot").read_text(encoding="utf-8").strip() == "blue"
    assert (state / "previous-image").read_text(encoding="utf-8").strip() == f"{IMAGE_ROOT}:{OLD}"
    assert (nginx / "upstream.inc").read_text(encoding="utf-8") == "server 127.0.0.1:18017;\n"
    assert trace.index("check_intelligence_deployment health") < trace.index("up -d --no-deps --force-recreate worker")
    assert trace.index("check_intelligence_deployment watermark") < trace.index("up -d --no-deps --force-recreate worker")
    assert trace.index("check_intelligence_deployment worker --since 2026-10-17 12:00:00") < trace.index("nginx:-t")
    assert re.search(r"worker --since 2026-10-17 12:00:00 --worker-id [a-f0-9]{32}", trace)
    assert trace.index("public-health") < trace.index("stop worker api")
    assert "down" not in trace and "docker:pull" not in trace


def test_rollback_reuses_previous_sha_without_database_downgrade(tmp_path):
    result, trace, state, _ = exercise(tmp_path, mode="rollback")
    assert result.returncode == 0, result.stderr
    assert (state / "active-slot").read_text(encoding="utf-8").strip() == "green"
    assert "check_intelligence_deployment validate" in trace
    assert not any(line.endswith("run --rm --no-deps migrate") for line in trace.splitlines())


def test_first_release_records_running_legacy_as_rollback_target_without_stopping_it(tmp_path):
    result, trace, state, _ = exercise(tmp_path, active=False)
    assert result.returncode == 0, result.stderr
    assert (state / "active-slot").read_text(encoding="utf-8").strip() == "blue"
    assert (state / "previous-slot").read_text(encoding="utf-8").strip() == "legacy"
    assert (state / "previous-image").read_text(encoding="utf-8").strip() == LEGACY_IMAGE
    assert "stop worker api" not in trace
    assert "genilink-intelligence-legacy" not in trace


def test_first_release_can_roll_back_to_legacy_without_new_schema_or_sha_contract(tmp_path):
    result, trace, state, nginx = exercise(tmp_path, mode="rollback-legacy")
    assert result.returncode == 0, result.stderr
    assert (state / "active-slot").read_text(encoding="utf-8").strip() == "legacy"
    assert (state / "active-image").read_text(encoding="utf-8").strip() == LEGACY_IMAGE
    assert (nginx / "upstream.inc").read_text(encoding="utf-8") == "server 127.0.0.1:8000;\n"
    assert "check_intelligence_deployment" not in trace
    assert "run --rm" not in trace and "image inspect" not in trace
    assert trace.index("nginx:-s reload") < trace.index("stop worker api")
    assert "genilink-intelligence-legacy" not in trace


def test_failed_legacy_switch_keeps_current_new_api_and_worker_running(tmp_path):
    result, trace, state, nginx = exercise(tmp_path, failure="legacy-public", mode="rollback-legacy")
    assert result.returncode != 0
    assert (state / "active-slot").read_text(encoding="utf-8").strip() == "blue"
    assert (nginx / "upstream.inc").read_text(encoding="utf-8") == "server 127.0.0.1:18016;\n"
    assert "stop worker api" not in trace


def test_deployment_after_legacy_rollback_accepts_legacy_image_identity(tmp_path):
    result, trace, state, _ = exercise(tmp_path, active="legacy")
    assert result.returncode == 0, result.stderr
    assert (state / "active-slot").read_text(encoding="utf-8").strip() == "blue"
    assert (state / "previous-slot").read_text(encoding="utf-8").strip() == "legacy"
    assert "stop worker api" not in trace


def test_workflow_and_production_overlay_enforce_release_contract():
    workflow = yaml.load((ROOT / ".github/workflows/intelligence-deploy.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["jobs"]["deploy"]["if"] == "github.ref == 'refs/heads/main'"
    commands = "\n".join(step.get("run", "") for step in workflow["jobs"]["deploy"]["steps"])
    for required in ['git rev-parse origin/main', '--workflow backend-ci.yml', '--event push', '--status success', 'git pull --ff-only origin main', 'docker save "$IMAGE" | gzip -1 | ssh', 'ServerAliveInterval=30']:
        assert required in commands
    overlay = yaml.safe_load((ROOT / "compose.intelligence-production.yml").read_text(encoding="utf-8"))
    for service in ["api", "worker"]:
        assert overlay["services"][service]["restart"] == "unless-stopped"
        assert overlay["services"][service]["mem_limit"] == "640m"
        assert overlay["services"][service]["cpus"] == 0.75
    assert overlay["networks"]["default"] == {"external": True, "name": "geo-visibility-analyze_default"}

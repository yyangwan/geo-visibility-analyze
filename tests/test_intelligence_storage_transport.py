"""Exercise transport fail-closed boundaries without SSH, Docker or cloud calls."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/check-intelligence-storage.sh"
BASH = (
    "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash")
)
SHA = "a" * 40


def run_transport(tmp_path, *, overrides=None, fail_at=""):
    trace = tmp_path / "trace.txt"
    env = {
        **os.environ,
        "RELEASE_SHA": SHA,
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
        "STORAGE_HOST": "ecs.example.com",
        "STORAGE_USER": "storage_check",
        "TRACE": str(trace),
        "FAIL_AT": fail_at,
        **(overrides or {}),
    }
    # Fake only transport commands; the real script and its shell conditions run.
    harness = r'''
ssh() {
  printf 'ssh:%s\n' "$*" >> "$TRACE"
  if [[ "$*" == *'command -v docker'* && "$FAIL_AT" == preflight ]]; then return 1; fi
  if [[ "$*" == *'docker load'* ]]; then cat >/dev/null; [[ "$FAIL_AT" != transfer ]]; return; fi
  if [[ "$*" == *'bash -s --'* ]]; then cat >> "$TRACE"; fi
}
docker() {
  printf 'docker:%s\n' "$*" >> "$TRACE"
  if [[ "$1" == pull && "$FAIL_AT" == pull ]]; then return 1; fi
  if [[ "$1" == image ]]; then
    if [[ "$FAIL_AT" == revision ]]; then printf 'wrong\n'; else printf '%s\n' "$RELEASE_SHA"; fi
  fi
  if [[ "$1" == save ]]; then printf 'mock image bytes'; fi
}
source "$1"
'''
    result = subprocess.run(
        [BASH, "-c", harness, "transport-test", SCRIPT.as_posix()],
        env=env,
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result, trace.read_text(encoding="utf-8") if trace.exists() else ""


@pytest.mark.parametrize(
    "overrides",
    [
        {"RELEASE_SHA": "main"},
        {"RELEASE_SHA": SHA + ";touch /tmp/injected"},
        {"STORAGE_HOST": "-oProxyCommand=bad"},
        {"STORAGE_USER": "root;id"},
        {"GITHUB_RUN_ID": "$(id)"},
        {"GITHUB_RUN_ATTEMPT": "../1"},
    ],
)
def test_untrusted_selectors_rejected_before_any_transport(tmp_path, overrides):
    result, trace = run_transport(tmp_path, overrides=overrides)
    assert result.returncode != 0
    assert trace == ""


@pytest.mark.parametrize("stage", ["preflight", "pull", "revision", "transfer"])
def test_failed_prerequisite_never_runs_remote_probe(tmp_path, stage):
    result, trace = run_transport(tmp_path, fail_at=stage)
    assert result.returncode != 0
    assert "bash -s --" not in trace
    if stage == "preflight":
        assert "docker:pull" not in trace


def test_success_streams_from_runner_then_executes_only_explicit_probe(tmp_path):
    result, trace = run_transport(tmp_path)
    assert result.returncode == 0, result.stderr
    assert trace.index("docker:pull") < trace.index("docker:save")
    assert trace.index("docker:save") < trace.index("bash -s --")
    assert "StrictHostKeyChecking=yes" in trace
    assert "ServerAliveInterval=30" in trace
    assert "--pull never --read-only --cap-drop ALL" in trace
    assert "--write-probe --output /checks/report.json" in trace
    assert "alembic" not in trace and "compose up" not in trace


def test_manual_workflow_requires_main_and_successful_main_image_publication():
    workflow = yaml.load(
        (ROOT / ".github/workflows/intelligence-storage-check.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert set(workflow["on"]) == {"workflow_dispatch"}
    job = workflow["jobs"]["storage"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    validation = next(step["run"] for step in job["steps"] if step.get("name") == "Verify published main revision")
    assert "merge-base --is-ancestor" in validation
    assert "--event push" in validation and "--status success" in validation
    assert workflow["permissions"]["packages"] == "read"

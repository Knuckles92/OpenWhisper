"""Behavioral release-gate checks, including isolated process exit and privacy."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.check_release_health import LIMITS, REQUIRED_CHECKS, failures

ROOT = Path(__file__).resolve().parents[1]


def _report():
    return dict(schema=1, kind="synthetic_service_lifecycle", passed=True,
                metrics={key: value / 10 for key, value in LIMITS.items()},
                checks=dict.fromkeys(REQUIRED_CHECKS, True))


@pytest.mark.parametrize("name", REQUIRED_CHECKS)
def test_missing_or_failed_recovery_proof_cannot_pass_gate(name):
    report = _report()
    report["checks"].pop(name)
    assert failures(report)
    report["checks"][name] = False
    assert failures(report)


@pytest.mark.parametrize("bad", [None, -1, float("nan"), float("inf"), True, "0.1"])
def test_missing_or_invalid_metrics_fail_closed(bad):
    report = _report()
    report["metrics"]["cancel_s"] = bad
    assert failures(report)


def test_regression_and_absolute_deadline_are_enforced():
    baseline = _report()
    assert failures(baseline) == []
    report = _report()
    report["metrics"]["cancel_s"] = 1
    assert failures(report) == []
    assert any("regression" in failure for failure in failures(report, baseline))
    report["metrics"]["cancel_s"] = 3
    assert any("exceeds" in failure for failure in failures(report))
    assert failures({}, baseline)


def test_real_model_report_cannot_pass_synthetic_gate():
    report = _report()
    report["kind"] = "real_model"
    assert failures(report)


def test_fresh_process_workflow_exits_and_preserves_user_history(tmp_path, monkeypatch):
    sentinel = tmp_path / "transcription_history.json"
    sentinel.write_text('{"entries": [{"text": "private history"}]}', encoding="utf-8")
    monkeypatch.setenv("OPENWHISPER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    report = tmp_path / "health.json"
    result = subprocess.run([sys.executable, "scripts/check_release_health.py", "--output", str(report)],
                            cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert sentinel.exists() and "private history" in sentinel.read_text()
    assert not (tmp_path / "openwhisper.db").exists()
    assert "private history" not in result.stdout + result.stderr
    data = json.loads(report.read_text())
    assert failures(data) == []
    assert data["limitations"]


def test_diagnostics_entrypoint_does_not_initialize_gui_or_history(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENWHISPER_DATA_DIR", str(tmp_path))
    output = tmp_path / "support.json"
    result = subprocess.run([sys.executable, "main.py", "--diagnostics", str(output)],
                            cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(output.read_text())["app_version"]
    assert not (tmp_path / "openwhisper.db").exists()


def test_source_self_test_does_not_recursively_spawn(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENWHISPER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    result = subprocess.run([sys.executable, "main.py", "--self-test"], cwd=ROOT,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OpenWhisper package self-test passed" in result.stdout
    assert not (tmp_path / "openwhisper.db").exists()

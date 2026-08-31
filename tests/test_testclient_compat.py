from __future__ import annotations

import subprocess
import sys

from scripts import check_testclient_compat


def test_smoke_runner_uses_current_interpreter_and_timeout(monkeypatch, capsys):
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, "versions\npassed\n", "")

    monkeypatch.setattr(check_testclient_compat.subprocess, "run", fake_run)

    assert check_testclient_compat.run_smoke(3.5) == 0
    assert observed["command"][:2] == [sys.executable, "-c"]
    assert observed["kwargs"]["timeout"] == 3.5
    assert "passed" in capsys.readouterr().out


def test_smoke_runner_fails_fast_on_portal_timeout(monkeypatch, capsys):
    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(
            command,
            kwargs["timeout"],
            output="fastapi=example\n",
        )

    monkeypatch.setattr(check_testclient_compat.subprocess, "run", fake_run)

    assert check_testclient_compat.run_smoke(2.0) == 124
    captured = capsys.readouterr()
    assert "fastapi=example" in captured.err
    assert "timed out after 2s" in captured.err


def test_smoke_runner_preserves_child_failure(monkeypatch, capsys):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 7, "", "broken stack\n")

    monkeypatch.setattr(check_testclient_compat.subprocess, "run", fake_run)

    assert check_testclient_compat.run_smoke() == 7
    captured = capsys.readouterr()
    assert "broken stack" in captured.err
    assert "check the pinned" in captured.err

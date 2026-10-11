"""Tailscale's macOS app must answer CLI requests outside a terminal."""
import json
import os
from types import SimpleNamespace

import pytest

from services.remote_asr import tailscale


@pytest.mark.parametrize("args, answer", [
    (["status", "--json"], {"BackendState": "Running"}),
    (["whois", "--json", "100.64.0.2"], {"Node": {"ComputedName": "jed"}}),
])
def test_macos_tailscale_requests_work_without_a_terminal(monkeypatch, args, answer):
    monkeypatch.setattr(tailscale.sys, "platform", "darwin")
    monkeypatch.setattr(tailscale, "find_cli", lambda: "/Applications/Tailscale.app/Contents/MacOS/Tailscale")
    for name in ("SHLVL", "TERM", "TERM_PROGRAM", "PS1"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TAILSCALE_BE_CLI", "0")
    parent_env = dict(os.environ)

    def run(command, **kwargs):
        assert command[1:] == args
        env = kwargs.get("env") or os.environ
        # The app executable chooses GUI mode in a Finder launch unless the
        # caller explicitly requests its CLI, even with status/whois argv.
        output = (json.dumps(answer) if env.get("TAILSCALE_BE_CLI") == "1"
                  else "The Tailscale GUI failed to start.")
        return SimpleNamespace(returncode=0, stdout=output.encode(), stderr=b"")

    monkeypatch.setattr(tailscale.subprocess, "run", run)
    assert tailscale._run_json(args, 4.0) == answer
    assert dict(os.environ) == parent_env

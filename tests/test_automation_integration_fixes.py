"""Seams between the review-round fixes, checked once they are merged together."""

import threading
from types import SimpleNamespace

import pytest

from services.automation import app_control, filters, programs
from services.automation.control import ControlError


def _controller(*, recording=False, busy=False, running=0):
    calls = []
    return SimpleNamespace(
        recorder=SimpleNamespace(is_recording=recording),
        transcription_runtime=SimpleNamespace(has_active_job=busy),
        action_runtime=SimpleNamespace(running=lambda: running),
        cancel=lambda: calls.append("cancel"),
        calls=calls,
    )


def test_an_api_cancel_stops_actions_still_running():
    controller = _controller(running=2)
    app_control._cancel(controller)
    assert controller.calls == ["cancel"]


def test_an_api_cancel_with_nothing_going_on_is_refused():
    with pytest.raises(ControlError) as refused:
        app_control._cancel(_controller())
    assert refused.value.code == "refused"


def test_a_canceled_program_filter_stops_its_program(monkeypatch):
    seen = {}

    def run_once(argv, stdin, *, timeout_s, cwd="", env=None, cancel=None):
        seen["cancel"] = cancel
        return None, b"", programs.CANCELED

    monkeypatch.setattr(programs, "run_once", run_once)
    cancel = threading.Event()
    verdict = filters._program_verdict(["prog"], b"{}", 1.0, cancel)
    assert seen["cancel"] is cancel
    assert verdict.kind == "failed"

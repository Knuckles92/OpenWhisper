"""Seams between the review-round fixes, checked once they are merged together."""

import threading
from types import SimpleNamespace

import pytest

from services.automation import app_control, filters, programs
from services.automation.control import ControlError


def _controller(*, recording=False, busy=False):
    calls = []
    return SimpleNamespace(
        recorder=SimpleNamespace(is_recording=recording),
        transcription_runtime=SimpleNamespace(
            has_active_job=busy, cancel=lambda: calls.append("cancel") or True),
        # The user's Cancel, which also reaches their running actions.
        cancel=lambda: calls.append("user_cancel"),
        calls=calls,
    )


@pytest.mark.parametrize("state", [{"recording": True}, {"busy": True}])
def test_an_api_cancel_cancels_only_the_recording_or_job(state):
    controller = _controller(**state)
    app_control._cancel(controller)
    assert controller.calls == ["cancel"]


def test_an_api_cancel_with_nothing_recording_or_transcribing_is_refused():
    # Actions the user started may still be running; a program can't stop them.
    controller = _controller()
    with pytest.raises(ControlError) as refused:
        app_control._cancel(controller)
    assert (refused.value.code, refused.value.message) == (
        "refused", "Nothing is recording or transcribing.")
    assert controller.calls == []


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

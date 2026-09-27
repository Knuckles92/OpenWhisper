"""Controller requests made off the Qt thread.

Hotkeys call the controller on threads of their own, and a QTimer only starts
on the thread that owns it: started anywhere else it never runs. These tests
use real Qt timers and signals, which test_application_controller stubs out.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6.QtCore import QCoreApplication, QEventLoop, QObject, QTimer

from services.application_controller import ApplicationController
from transcriber.optional_backend import LocalSpeechBackend


class _ClosedEngine(LocalSpeechBackend):
    """A downloaded engine with no worker running, as after a cancel."""

    is_model_missing = False


def _controller(backend) -> ApplicationController:
    """The real controller class, with only its reload path set up."""
    controller = ApplicationController.__new__(ApplicationController)
    QObject.__init__(controller)
    controller.current_backend = backend
    controller.recorder = SimpleNamespace(is_recording=False)
    controller.meeting_active = False
    controller.executor = Mock()
    controller._reload_in_flight = False
    controller._reload_pending = False
    controller._reload_note = ""
    controller._setup_reload_timer()
    return controller


def _off_qt_thread(fn):
    result = []
    worker = threading.Thread(target=lambda: result.append(fn()))
    worker.start()
    worker.join(5)
    assert result, "the call did not finish"
    return result[0]


@pytest.mark.parametrize(
    "canceled, last_error",
    [(True, "Parakeet stopped."), (False, "")],
    ids=["closed-by-cancel", "never-loaded"],
)
def test_a_hotkey_readiness_check_really_reloads_the_engine(canceled, last_error):
    backend = _ClosedEngine("parakeet")
    backend.should_cancel = canceled
    backend.last_error = last_error
    controller = _controller(backend)
    try:
        message = _off_qt_thread(controller.transcription_readiness_message)
        assert message == "Reloading speech engine..."
        # Other threads see the reload coming before the Qt thread arms it.
        assert not controller._engine_settled()

        # The request waits for the Qt event loop, like any queued call.
        QCoreApplication.sendPostedEvents()
        assert controller._reload_timer.isActive()

        loop = QEventLoop()
        controller._reload_timer.timeout.connect(loop.quit)
        QTimer.singleShot(5000, loop.quit)
        loop.exec()
        controller.executor.submit.assert_called_once_with(controller._reload_worker)
        assert controller._reload_in_flight
    finally:
        controller._reload_timer.stop()


def test_a_reload_asked_for_on_the_qt_thread_is_armed_at_once():
    controller = _controller(_ClosedEngine("parakeet"))
    try:
        controller.reload_whisper_model()
        assert controller._reload_timer.isActive()
    finally:
        controller._reload_timer.stop()

"""The desktop app's AppControl for the live API and MCP.

Contract: docs/design/automation/design.md, "services/automation/control.py".

The listener calls in from its own threads. Reading the app's state is
thread-safe and happens there; anything that starts, stops or shows
something runs on the Qt application thread through one session-lived relay
(the Qt worker relay pattern, like ``ui_qt.widgets.agent_picker._ScanRelay``):
the caller queues ``(callable, Future)`` to it and waits up to two seconds.
"""

from __future__ import annotations

import logging
import threading
import weakref
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from functools import partial
from typing import Any, Callable, Optional, TypeVar

from PyQt6.QtCore import QCoreApplication, QObject, Qt, QThread, pyqtSignal, pyqtSlot

from services.automation.control import (
    BUSY,
    NOT_FOUND,
    REFUSED,
    UNAVAILABLE,
    ControlError,
    LiveState,
    set_app_control,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: How long a caller waits for the Qt thread to take its call.
CALL_TIMEOUT_S = 2.0
#: How much longer it waits once the call is already running there, so the
#: answer is what happened rather than a guess.
RUNNING_GRACE_S = 2.0

_NOT_READY = "OpenWhisper isn't ready for this yet."
_NO_ACTION = "No enabled action has that id."


class _Relay(QObject):
    """Runs calls from the listener's threads on the Qt application thread.

    One per session, parentless, created on first use and moved to the
    application thread; it never holds the controller between calls. Its
    slots are decorated: an undecorated bound method gets a PyQt proxy that
    stays on the thread that connected it, where no event loop runs.
    """

    call = pyqtSignal(object, object)

    def __init__(self) -> None:
        super().__init__()
        self._statuses: Optional[list[str]] = None
        self.call.connect(self._run, Qt.ConnectionType.QueuedConnection)

    @pyqtSlot(object, object)
    def _run(self, fn: Callable[[], Any], future: Future) -> None:
        if not future.set_running_or_notify_cancel():
            return  # the caller gave up before the Qt thread got here
        try:
            result = fn()
        except Exception as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)

    def with_statuses(self, signal, fn: Callable[[], T]) -> tuple[T, list[str]]:
        """Run ``fn`` and return what it reported on ``signal`` meanwhile; Qt thread."""
        statuses: list[str] = []
        self._statuses = statuses
        signal.connect(self._note_status)
        try:
            return fn(), statuses
        finally:
            signal.disconnect(self._note_status)
            self._statuses = None

    @pyqtSlot(str)
    def _note_status(self, text: str) -> None:
        if self._statuses is not None:
            self._statuses.append(text)


_relay: Optional[_Relay] = None
_relay_lock = threading.Lock()


def _app_thread() -> QThread:
    app = QCoreApplication.instance()
    if app is None:
        raise ControlError(UNAVAILABLE, _NOT_READY)
    return app.thread()


def _get_relay() -> _Relay:
    global _relay
    with _relay_lock:
        if _relay is None:
            relay = _Relay()
            relay.moveToThread(_app_thread())
            _relay = relay
        return _relay


def call_on_qt(fn: Callable[[], T]) -> T:
    """``fn()`` on the Qt application thread, from any thread.

    Raises:
        ControlError: ``unavailable`` when the Qt thread doesn't take the
            call within ``CALL_TIMEOUT_S`` (it then never runs), or doesn't
            finish one it already started; otherwise whatever ``fn`` raised.
    """
    if QThread.currentThread() is _app_thread():
        return fn()
    relay = _get_relay()
    future: Future = Future()
    relay.call.emit(fn, future)
    try:
        return future.result(timeout=CALL_TIMEOUT_S)
    except FutureTimeout:
        pass
    if future.cancel():
        raise ControlError(UNAVAILABLE, "OpenWhisper didn't respond in time. Try again.")
    try:
        return future.result(timeout=RUNNING_GRACE_S)
    except FutureTimeout:
        raise ControlError(
            UNAVAILABLE, "OpenWhisper is still working on that. Check its state in a moment."
        ) from None


# -- the work, on the Qt thread ------------------------------------------------

def _enabled_action(action_id: str):
    """The enabled action ``action_id`` for a program; raises when it may not have it.

    Every AppControl caller is a program (the live API or an MCP agent), so
    the words are a program's: an action that wouldn't take them (an agent or
    Claude Code channel action without "send words to your coding agents",
    ``program_words_allowed``) is refused now rather than after recording.
    """
    from services.actions.model import find_action
    from services.actions.runtime import program_words_allowed
    from services.automation import permissions
    from services.settings import settings_manager

    saved = settings_manager.load_all_settings()
    action = find_action(saved, action_id)
    if action is None or not action.enabled:
        raise ControlError(NOT_FOUND, _NO_ACTION)
    if not program_words_allowed(action, saved):
        raise permissions.PermissionDenied(permissions.AGENT_PROMPTS)
    return action


def _start(controller, action_id: str, source: str) -> str:
    runtime = controller.transcription_runtime
    if controller.recorder.is_recording:
        raise ControlError(BUSY, "OpenWhisper is already recording.")
    if runtime.has_active_job:
        raise ControlError(BUSY, "OpenWhisper is busy with another recording.")
    if controller.is_meeting_active():
        raise ControlError(BUSY, "Meeting Mode is active; end the meeting first.")
    if action_id:
        _enabled_action(action_id)
        start = partial(controller.start_action_recording, action_id, source=source)
    else:
        # What the record shortcut starts, with the caller as its source;
        # it refuses during backup or while the engine isn't ready.
        start = partial(runtime.start_recording, source=source)
    started, statuses = _get_relay().with_statuses(controller.status_update, start)
    if started is False:
        raise ControlError(REFUSED, statuses[-1] if statuses else "OpenWhisper couldn't start recording.")
    job = runtime.current_job
    return job.id if job is not None else ""


def _stop(controller) -> None:
    if not controller.recorder.is_recording:
        raise ControlError(REFUSED, "Nothing is recording.")
    controller.stop_recording()


def _cancel(controller) -> None:
    # Recordings and jobs still working that no Cancel has reached yet: the
    # actions the user started are theirs to stop, including those a
    # delivered job (only being saved) handed its words to.
    runtime = controller.transcription_runtime
    if not runtime.cancelable or not runtime.cancel():
        raise ControlError(REFUSED, "Nothing is recording or transcribing.")


def _notify(controller, text: str, title: str) -> None:
    text = " ".join(text.split())
    title = " ".join(title.split())
    message = f"{title}: {text}" if title else text
    ui = controller.ui_controller
    ui.set_status(message)
    ui.show_notice(message)


class QtAppControl:
    """``AppControl`` for the running desktop app; any thread.

    Holds the controller weakly, so a registration left behind never keeps
    the app's objects alive; calls after the controller is gone are
    ``unavailable``.
    """

    def __init__(self, controller) -> None:
        self._controller = weakref.ref(controller)

    def _require(self):
        controller = self._controller()
        if controller is None:
            raise ControlError(UNAVAILABLE, _NOT_READY)
        return controller

    def state(self) -> LiveState:
        from _version import __version__

        controller = self._require()
        runtime = controller.transcription_runtime
        recording = bool(controller.recorder.is_recording)
        # Until its history is saved: a start is busy until then.
        busy = bool(runtime.has_active_job)
        # As it is now: the action routing chose, once it did.
        job = runtime.current_job if (recording or busy) else None
        return LiveState(
            recording=recording,
            busy=busy,
            meeting=bool(controller.is_meeting_active()),
            mode=str(getattr(job, "mode", "") or ""),
            action_id=str(getattr(job, "action_id", "") or ""),
            job_id=str(getattr(job, "id", "") or ""),
            version=__version__,
        )

    def list_actions(self) -> list[dict]:
        from services.actions.model import load_actions
        from services.settings import settings_manager

        return [
            action.public_dict()
            for action in load_actions(settings_manager.load_all_settings())
            if action.enabled
        ]

    def run_action(self, action_id: str, text: str, *, source: str) -> str:
        controller = self._require()
        return call_on_qt(partial(
            controller.transcription_runtime.run_action_text, action_id, text, source=source
        ))

    def start_recording(self, *, action_id: str = "", source: str) -> str:
        return call_on_qt(partial(_start, self._require(), action_id, source))

    def stop_recording(self) -> None:
        call_on_qt(partial(_stop, self._require()))

    def cancel(self) -> None:
        call_on_qt(partial(_cancel, self._require()))

    def notify(self, text: str, *, title: str = "") -> None:
        call_on_qt(partial(_notify, self._require(), text, title))


def install(controller) -> None:
    """Register this app's control with ``control.set_app_control``; Qt thread."""
    set_app_control(QtAppControl(controller))

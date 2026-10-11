"""The desktop app's AppControl: thread hops onto Qt, refusals, and lifetimes."""

import gc
import threading
import time
import weakref
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QCoreApplication, QObject, QThread, pyqtSignal

from _version import __version__
from services.actions.model import Action
from services.automation import app_control
from services.automation.control import ControlError, LiveState, app_control as registered, set_app_control
from services.settings import SettingsKey, settings_manager


class FakeUi:
    def __init__(self):
        self.calls = []

    def set_status(self, text):
        self.calls.append(("status", text))

    def show_notice(self, text):
        self.calls.append(("notice", text))


class FakeRuntime:
    """TranscriptionRuntime's surface: the job slot and its two starts."""

    def __init__(self, controller):
        self._controller = weakref.ref(controller)
        self._job = None
        self._active_job = None
        self.has_active_job = False
        self.refusal = ""
        self.delay_s = 0.0
        self.calls = []

    def run_action_text(self, action_id, text, *, source):
        self.calls.append(("run", action_id, text, source, threading.current_thread()))
        if self.delay_s:
            time.sleep(self.delay_s)
        if action_id == "gone":
            raise ControlError("not_found", "No enabled action has that id.")
        return "job-run"

    def start_recording(self, *, source):
        controller = self._controller()
        self.calls.append(("start", source, threading.current_thread()))
        if self.refusal:
            controller.status_update.emit(self.refusal)
            return False
        controller.status_update.emit("Recording...")
        controller.recorder.is_recording = True
        self._job = SimpleNamespace(id="job-new", mode="dictation", action_id="")
        return True


class FakeController(QObject):
    """ApplicationController's surface used by QtAppControl."""

    status_update = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.recorder = SimpleNamespace(is_recording=False)
        self.transcription_runtime = FakeRuntime(self)
        # Actions still running after their dictation; cancel stops them too.
        self.actions_running = 0
        self.action_runtime = SimpleNamespace(running=lambda: self.actions_running)
        self.ui_controller = FakeUi()
        self.meeting = False
        self.calls = []

    def is_meeting_active(self):
        return self.meeting

    def start_action_recording(self, action_id, *, source="hotkey"):
        self.calls.append(("start_action", action_id, source))
        self.recorder.is_recording = True
        self.transcription_runtime._job = SimpleNamespace(
            id="job-action", mode="action", action_id=action_id
        )
        return True

    def stop_recording(self):
        self.calls.append(("stop", threading.current_thread()))

    def cancel(self):
        self.calls.append(("cancel", threading.current_thread()))


@pytest.fixture
def controller(_session_qt_application):
    fake = FakeController()
    app_control.install(fake)
    yield fake
    set_app_control(None)


@pytest.fixture
def control(controller):
    return registered()


@pytest.fixture
def fast_timeouts(monkeypatch):
    monkeypatch.setattr(app_control, "CALL_TIMEOUT_S", 0.1)
    monkeypatch.setattr(app_control, "RUNNING_GRACE_S", 2.0)


def in_worker(fn, *, pump=True, timeout=5.0):
    """Run ``fn`` on another thread while this (the Qt) thread handles events."""
    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout
    while thread.is_alive() and time.monotonic() < deadline:
        if pump:
            QCoreApplication.processEvents()
        time.sleep(0.002)
    thread.join(1)
    assert not thread.is_alive()
    return box


def save_actions(*actions):
    settings_manager.update_settings({SettingsKey.ACTIONS: [a.to_dict() for a in actions]})


def test_install_registers_the_qt_app_control(controller):
    assert isinstance(registered(), app_control.QtAppControl)


def test_calls_from_another_thread_run_on_the_qt_thread(control, controller):
    result = in_worker(lambda: control.run_action("a1", "hello", source="api"))
    assert result == {"value": "job-run"}
    (call,) = controller.transcription_runtime.calls
    assert call[:4] == ("run", "a1", "hello", "api")
    assert call[4] is threading.main_thread()


def test_errors_raised_on_the_qt_thread_reach_the_caller(control):
    result = in_worker(lambda: control.run_action("gone", "hello", source="api"))
    assert isinstance(result["error"], ControlError)
    assert result["error"].code == "not_found"


def test_a_call_made_on_the_qt_thread_runs_at_once(control, controller):
    control.notify("Done", title="Build")
    assert controller.ui_controller.calls == [("status", "Build: Done"), ("notice", "Build: Done")]


def test_an_unresponsive_qt_thread_is_unavailable_and_the_call_never_runs(
    control, controller, fast_timeouts
):
    controller.recorder.is_recording = True
    result = in_worker(control.stop_recording, pump=False)
    error = result["error"]
    assert isinstance(error, ControlError)
    assert (error.code, error.message) == (
        "unavailable", "OpenWhisper didn't respond in time. Try again.",
    )
    for _ in range(5):
        QCoreApplication.processEvents()
    assert controller.calls == []


def test_a_call_already_running_is_waited_for_so_the_answer_is_true(
    control, controller, fast_timeouts
):
    controller.transcription_runtime.delay_s = 0.4
    result = in_worker(lambda: control.run_action("a1", "hello", source="api"))
    assert result == {"value": "job-run"}


def test_a_call_still_running_after_the_grace_is_unavailable(control, controller, monkeypatch):
    monkeypatch.setattr(app_control, "CALL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(app_control, "RUNNING_GRACE_S", 0.1)
    controller.transcription_runtime.delay_s = 0.5
    result = in_worker(lambda: control.run_action("a1", "hello", source="api"))
    assert result["error"].code == "unavailable"
    assert result["error"].message == "OpenWhisper is still working on that. Check its state in a moment."


def test_start_returns_the_new_job(control, controller):
    result = in_worker(lambda: control.start_recording(source="api"))
    assert result == {"value": "job-new"}
    (call,) = controller.transcription_runtime.calls
    assert call[:2] == ("start", "api")
    assert call[2] is threading.main_thread()


def test_start_for_an_action_records_for_it(control, controller):
    save_actions(Action("a1", "Ask Claude", "open_link", {"template": "https://x/?q={text}"}))
    result = in_worker(lambda: control.start_recording(action_id="a1", source="mcp"))
    assert result == {"value": "job-action"}
    assert controller.calls == [("start_action", "a1", "mcp")]


@pytest.mark.parametrize(
    "setup, action_id, code, message",
    [
        (lambda c: setattr(c.recorder, "is_recording", True), "",
         "busy", "OpenWhisper is already recording."),
        (lambda c: setattr(c.transcription_runtime, "has_active_job", True), "",
         "busy", "OpenWhisper is busy with another recording."),
        (lambda c: setattr(c, "meeting", True), "",
         "busy", "Meeting Mode is active; end the meeting first."),
        (lambda c: None, "missing", "not_found", "No enabled action has that id."),
        (lambda c: None, "off", "not_found", "No enabled action has that id."),
    ],
    ids=["recording", "transcribing", "meeting", "missing-action", "disabled-action"],
)
def test_start_refusals_say_why(control, controller, setup, action_id, code, message):
    save_actions(Action("off", "Off", "clipboard", enabled=False))
    setup(controller)
    result = in_worker(lambda: control.start_recording(action_id=action_id, source="api"))
    assert (result["error"].code, result["error"].message) == (code, message)
    assert controller.calls == []
    assert controller.transcription_runtime.calls == []


def test_a_start_the_app_refuses_reports_what_it_told_the_user(control, controller):
    controller.transcription_runtime.refusal = "Speech engine is still loading"
    result = in_worker(lambda: control.start_recording(source="api"))
    assert (result["error"].code, result["error"].message) == (
        "refused", "Speech engine is still loading",
    )
    # Nothing stays connected to the controller once the start is over.
    assert controller.receivers(controller.status_update) == 0


def test_stop_and_cancel_need_something_to_act_on(control, controller):
    for call in (control.stop_recording, control.cancel):
        result = in_worker(call)
        assert result["error"].code == "refused"
    assert controller.calls == []

    controller.recorder.is_recording = True
    assert in_worker(control.stop_recording) == {"value": None}
    assert in_worker(control.cancel) == {"value": None}
    controller.recorder.is_recording = False
    controller.transcription_runtime.has_active_job = True
    assert in_worker(control.cancel) == {"value": None}
    controller.transcription_runtime.has_active_job = False
    controller.actions_running = 1
    assert in_worker(control.cancel) == {"value": None}
    assert [name for name, _thread in controller.calls] == ["stop", "cancel", "cancel", "cancel"]
    assert all(thread is threading.main_thread() for _name, thread in controller.calls)


def test_notices_are_one_line(control, controller):
    assert in_worker(lambda: control.notify("Tests\n passed", title=" CI ")) == {"value": None}
    assert in_worker(lambda: control.notify("Plain")) == {"value": None}
    assert controller.ui_controller.calls == [
        ("status", "CI: Tests passed"), ("notice", "CI: Tests passed"),
        ("status", "Plain"), ("notice", "Plain"),
    ]


def test_state_is_read_without_waiting_for_the_qt_thread(control, controller):
    idle = in_worker(control.state, pump=False)["value"]
    assert idle == LiveState(False, False, False, "", "", "", __version__)

    runtime = controller.transcription_runtime
    runtime._job = SimpleNamespace(id="job-9", mode="action", action_id="a1")
    controller.recorder.is_recording = True
    controller.meeting = True
    recording = in_worker(control.state, pump=False)["value"]
    assert recording == LiveState(True, False, True, "action", "a1", "job-9", __version__)

    # After the stop, the worker slot holds the job.
    controller.recorder.is_recording = False
    runtime._job = None
    runtime._active_job = SimpleNamespace(id="job-9", mode="action", action_id="a1")
    runtime.has_active_job = True
    busy = in_worker(control.state, pump=False)["value"]
    assert (busy.recording, busy.busy, busy.job_id) == (False, True, "job-9")


def test_listed_actions_are_the_enabled_ones_without_private_settings(control):
    save_actions(
        Action("a1", "Hook", "webhook", {"url": "https://secret.example/hook"}),
        Action("a2", "Off", "clipboard", enabled=False),
    )
    actions = in_worker(control.list_actions, pump=False)["value"]
    assert [a["id"] for a in actions] == ["a1"]
    assert "secret" not in repr(actions)


def test_the_control_never_keeps_the_controller_alive(_session_qt_application):
    controller = FakeController()
    app_control.install(controller)
    control = registered()
    try:
        assert in_worker(lambda: control.run_action("a1", "hi", source="api"))["value"] == "job-run"
        controller.transcription_runtime.refusal = "No"
        assert in_worker(lambda: control.start_recording(source="api"))["error"].code == "refused"
        gone = weakref.ref(controller)
        del controller
        gc.collect()
        assert gone() is None
        for call in (
            control.state,
            lambda: control.run_action("a1", "hi", source="api"),
            lambda: control.start_recording(source="api"),
            control.stop_recording,
            control.cancel,
            lambda: control.notify("hi"),
        ):
            result = in_worker(call)
            assert result["error"].code == "unavailable"
    finally:
        set_app_control(None)


def test_a_relay_first_needed_off_the_qt_thread_moves_onto_it(control, controller, monkeypatch):
    monkeypatch.setattr(app_control, "_relay", None)
    result = in_worker(lambda: control.run_action("a1", "hi", source="api"))
    assert result == {"value": "job-run"}
    assert app_control._relay.thread() is QThread.currentThread()
    assert controller.transcription_runtime.calls[0][4] is threading.main_thread()


def test_one_relay_serves_every_call_from_the_qt_thread(control, controller):
    for _ in range(3):
        in_worker(lambda: control.run_action("a1", "hi", source="api"))
    relay = app_control._relay
    assert relay is not None
    assert relay.thread() is QThread.currentThread()
    assert relay.parent() is None
    assert relay.children() == []
    in_worker(lambda: control.notify("hi"))
    assert app_control._relay is relay

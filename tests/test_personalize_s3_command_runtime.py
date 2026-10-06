"""Command Mode's shortcut, where the selection comes from, and transforms."""

import os
import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from config import config
from services import dictation_pipeline, dictionary, focus_context, synthetic_keys, text_rewrite
from services.dictation_pipeline import DictationJob, JobMode
from services.focus_context import AppIdentity, ContextCaptureService, FocusSnapshot, TextContext
from services.runtime import command
from services.runtime.command import CommandRuntime
from services.settings import RecordingTriggerMode, SettingsKey
from services.transcript_cleanup import CleanupInfo
from tests.fakes.settings import InMemorySettings
from ui_qt.overlay_state import OverlayState

NOTEPAD = AppIdentity("notepad.exe", "Notepad", pid=4, window="0x4")
TERMINAL = AppIdentity("WindowsTerminal.exe", "Windows Terminal", pid=5, window="0x5")
VS_CODE = AppIdentity("Code.exe", "Visual Studio Code", pid=6, window="0x6")


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return QApplication.instance() or QApplication([])


def _settle(predicate, timeout=2.0):
    """Pump Qt until ``predicate()`` holds; helper threads hop back through it."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the command runtime")
        QApplication.processEvents()
        time.sleep(0.002)


class Signal:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


class FakeService(ContextCaptureService):
    def __init__(self, snapshot=FocusSnapshot()):
        self.snapshot = snapshot
        self.requests = []

    def request(self, *, include_text, include_selection=False):
        self.requests.append((include_text, include_selection))
        future: Future = Future()
        future.set_result(self.snapshot)
        return future

    def current_identity(self):
        return None

    def reread(self, identity, callback):
        callback(None)

    def shutdown(self):
        pass


class FakeCleaner:
    def __init__(self, reply="Rewritten.", error=None, available=True):
        self.reply, self.error, self.available = reply, error, available
        self.calls = []
        self.configured = []
        self.last_error = "not run"
        self.provider, self.model = "openrouter", "test/model"

    def configure(self, provider, model, reasoning=None):
        self.configured.append((provider, model, reasoning))

    def is_available(self):
        return self.available

    def cleanup(self, text, system_prompt=None, timeout_s=None):
        self.calls.append((text, system_prompt))
        self.last_error = self.error
        return text if self.error else self.reply


class FakeRuntime:
    def __init__(self, controller, settings):
        self.controller = controller
        self.settings = settings
        self._job = None
        self._transcript_cleanup = FakeCleaner()
        self.start_ok = True
        self.begin_ok = True
        self.events = []

    def start_recording(self, profile_id="", mode=JobMode.DICTATION, *, selection=None):
        self.events.append(("start", mode))
        if not self.start_ok:
            return False
        self.controller.recorder.is_recording = True
        self._job = dictation_pipeline.begin_job(
            mode, self.settings.load_all_settings(), selection=selection)
        return True

    def begin_rewrite_job(self, job, *, source_name):
        self.events.append(("begin", job, source_name))
        return self.begin_ok

    def submit_rewrite(self, work):
        self.events.append(("submit", work))

    def abandon_rewrite_job(self, status):
        self.events.append(("abandon", status))


class FakeUI:
    def __init__(self):
        self.clipboard_selection = ""
        self.captures = []

    def capture_selection(self, callback, *, timeout_ms=None):
        self.captures.append(threading.current_thread() is threading.main_thread())
        callback(self.clipboard_selection)


@pytest.fixture
def h(monkeypatch):
    settings = InMemorySettings({
        SettingsKey.RECORDING_TRIGGER_MODE: RecordingTriggerMode.PUSH_HOLD,
    })
    monkeypatch.setattr(command, "settings_manager", settings)
    monkeypatch.setattr(text_rewrite, "provider_ready", lambda _settings: True)
    released = []
    monkeypatch.setattr(
        synthetic_keys, "wait_for_modifiers_released",
        lambda timeout_s=0.8: released.append(threading.current_thread().name) or True,
    )
    service = FakeService()
    focus_context.set_service(service)
    events = []
    recorder = SimpleNamespace(is_recording=False)

    def stop():
        events.append("stop")
        recorder.is_recording = False

    def cancel():
        events.append("cancel")
        recorder.is_recording = False

    controller = SimpleNamespace(
        recorder=recorder,
        status_update=Signal(),
        overlay_state_update=Signal(),
        stop_recording=stop,
        cancel=cancel,
        ui_controller=FakeUI(),
    )
    runtime = FakeRuntime(controller, settings)
    controller.transcription_runtime = runtime
    commands = CommandRuntime(controller)
    yield SimpleNamespace(
        settings=settings, service=service, controller=controller, runtime=runtime,
        ui=controller.ui_controller, commands=commands, events=events, released=released,
        recorder=recorder,
    )
    commands.cleanup()


def _selection_of(h):
    return h.runtime._job.selection


def _statuses(h):
    return [args[0] for args in h.controller.status_update.calls]


# --- the shortcut ------------------------------------------------------------


def test_hold_records_an_instruction_and_release_stops_it(h):
    h.service.snapshot = FocusSnapshot(NOTEPAD, TextContext(selected="old words", selection_known=True))

    h.commands.key_pressed(10.0)
    assert h.runtime.events == [("start", JobMode.COMMAND)]
    assert h.service.requests == [(False, True)]
    selection = _selection_of(h)
    assert not selection.done()

    h.commands.key_released(10.0 + config.RECORD_MIN_HOLD_MS / 1000 + 0.05)
    assert h.events == ["stop"]
    _settle(selection.done)
    assert selection.result() == "old words"
    # UI Automation knew the selection, so no keys were sent at all.
    assert h.ui.captures == [] and h.released == []


def test_a_short_tap_cancels(h):
    h.commands.key_pressed(10.0)
    selection = _selection_of(h)
    h.commands.key_released(10.1)

    assert h.events == ["cancel"]
    # A canceled recording never sends a copy.
    assert not selection.done() and h.ui.captures == [] and h.released == []
    h.commands.key_released(11.0)
    assert h.events == ["cancel"]


def test_toggle_mode_presses_start_and_stop_and_ignores_releases(h):
    h.settings.values[SettingsKey.RECORDING_TRIGGER_MODE] = RecordingTriggerMode.TOGGLE

    h.commands.key_pressed(1.0)
    h.commands.key_released(1.05)
    assert h.events == []
    h.commands.key_pressed(5.0)
    h.commands.key_released(5.1)

    assert h.runtime.events == [("start", JobMode.COMMAND)]
    assert h.events == ["stop"]


def test_without_a_provider_it_says_so_and_never_records(h, monkeypatch):
    monkeypatch.setattr(text_rewrite, "provider_ready", lambda _settings: False)

    h.commands.key_pressed(1.0)
    h.commands.key_released(2.0)

    assert _statuses(h) == ["Set up AI cleanup to use Command Mode"]
    assert h.runtime.events == [] and h.events == []


def test_a_dictation_in_progress_is_left_alone(h):
    h.recorder.is_recording = True

    h.commands.key_pressed(1.0)
    h.commands.key_released(2.0)

    assert _statuses(h) == ["Finish the current recording before using Command Mode"]
    assert h.runtime.events == [] and h.events == []


def test_a_press_during_a_stops_post_roll_is_left_to_the_runtime(h):
    h.recorder.is_recording = True
    h.runtime.has_active_job = True
    h.runtime.start_ok = False

    h.commands.key_pressed(1.0)
    h.commands.key_released(2.0)

    assert h.runtime.events == [("start", JobMode.COMMAND)]
    assert _statuses(h) == [] and h.events == []


def test_a_refused_start_leaves_nothing_to_stop(h):
    h.runtime.start_ok = False

    h.commands.key_pressed(1.0)
    h.commands.key_released(2.0)

    assert h.events == []


def test_a_recording_canceled_elsewhere_lets_the_next_press_start_again(h):
    h.settings.values[SettingsKey.RECORDING_TRIGGER_MODE] = RecordingTriggerMode.TOGGLE
    h.commands.key_pressed(1.0)
    first = _selection_of(h)
    h.recorder.is_recording = False  # the Cancel hotkey

    h.commands.key_pressed(2.0)

    assert not first.done()
    assert h.runtime.events == [("start", JobMode.COMMAND)] * 2
    assert h.events == []


def test_a_recording_stopped_elsewhere_still_reads_its_selection_once(h):
    h.service.snapshot = FocusSnapshot(NOTEPAD, None)
    h.ui.clipboard_selection = "picked words"
    h.commands.key_pressed(1.0)
    job = h.runtime._job
    h.recorder.is_recording = False  # the Stop button, not the shortcut
    result = {}

    worker = threading.Thread(
        target=lambda: result.update(out=h.commands.complete_recording("shorter", job)))
    worker.start()
    _settle(lambda: not worker.is_alive())

    assert result["out"][1] == "picked words"
    h.commands.key_released(3.0)
    assert h.events == []
    assert h.ui.captures == [True]


# --- where the selection comes from -----------------------------------------


def _stop_and_read(h):
    h.commands.key_pressed(1.0)
    selection = _selection_of(h)
    h.commands.key_released(2.0)
    _settle(selection.done)
    return selection.result()


def test_unknown_selection_is_copied_once_the_keys_are_up(h):
    h.service.snapshot = FocusSnapshot(NOTEPAD, TextContext(before="x", caret_known=True))
    h.ui.clipboard_selection = "copied words"

    assert _stop_and_read(h) == "copied words"
    assert h.released == ["command-selection"]
    # The copy itself runs on the Qt thread, after the modifier wait.
    assert h.ui.captures == [True]
    # The worker finds the selection read and never copies again.
    assert h.commands.complete_recording("shorter", h.runtime._job)[1] == "copied words"
    assert h.ui.captures == [True]


def test_an_empty_selection_from_ui_automation_is_final(h):
    h.service.snapshot = FocusSnapshot(NOTEPAD, TextContext(selected="", selection_known=True))
    h.ui.clipboard_selection = "should never be read"

    assert _stop_and_read(h) == ""
    assert h.ui.captures == []


def test_terminals_never_get_a_copy(h):
    h.service.snapshot = FocusSnapshot(TERMINAL, None)
    h.ui.clipboard_selection = "would have interrupted"

    assert _stop_and_read(h) == ""
    assert h.ui.captures == [] and h.released == []


@pytest.mark.parametrize("copied, expected", [
    ("def main():\r\n", ""),
    ("\n", ""),
    ("main", "main"),
    ("line one\nline two\n", "line one\nline two\n"),
])
def test_copy_line_editors_only_count_real_selections(h, copied, expected):
    h.service.snapshot = FocusSnapshot(VS_CODE, None)
    h.ui.clipboard_selection = copied

    assert _stop_and_read(h) == expected


def test_with_app_context_off_the_selection_is_still_read(h):
    h.settings.values[SettingsKey.APP_CONTEXT_ENABLED] = False
    h.service.snapshot = FocusSnapshot(NOTEPAD, TextContext(selected="mine", selection_known=True))

    h.commands.key_pressed(1.0)
    job = h.runtime._job
    # Asked for before the recording started, and never handed to the job.
    assert h.service.requests == [(False, True)]
    assert job.focus is None
    h.commands.key_released(2.0)
    _settle(job.selection.done)
    assert job.selection.result() == "mine"


# --- turning the transcript into text ----------------------------------------


def _job(selection="old words"):
    future: Future = Future()
    if selection is not None:
        future.set_result(selection)
    return DictationJob(mode=JobMode.COMMAND, selection=future)


def test_a_selection_is_rewritten_by_the_instruction(h):
    h.runtime._transcript_cleanup = cleaner = FakeCleaner("New words.")

    text, raw, info = h.commands.complete_recording("  make it formal ", _job())

    assert (text, raw) == ("New words.", "old words")
    assert (info.provider, info.model, info.level) == ("openrouter", "test/model", "")
    assert info.elapsed_s >= 0
    assert cleaner.configured and cleaner.calls[0][0] == "old words"
    assert "Instruction:\nmake it formal" in cleaner.calls[0][1]
    assert h.controller.overlay_state_update.calls == [(OverlayState.REWRITING,)]
    assert _statuses(h) == ["Rewriting..."]


def test_with_nothing_selected_it_writes_new_text(h):
    h.runtime._transcript_cleanup = cleaner = FakeCleaner("Dear team,")

    assert h.commands.complete_recording("write a greeting", _job("")) == (
        "Dear team,", None, CleanupInfo("openrouter", "test/model",
                                        pytest.approx(0, abs=1), level=""))
    assert cleaner.calls[0][0] == "write a greeting"
    assert _statuses(h) == ["Writing..."]


def test_writing_new_text_can_be_turned_off(h):
    h.settings.values[SettingsKey.COMMAND_MODE_INSERT_WITHOUT_SELECTION] = False

    with pytest.raises(RuntimeError, match="^Select text first$"):
        h.commands.complete_recording("write a greeting", _job(""))
    assert h.runtime._transcript_cleanup.calls == []


@pytest.mark.parametrize("raw", ["", "   "])
def test_silence_is_not_an_instruction(h, raw):
    with pytest.raises(RuntimeError, match="Didn't catch an instruction"):
        h.commands.complete_recording(raw, _job())


def test_an_unavailable_provider_is_reported(h):
    h.runtime._transcript_cleanup = FakeCleaner(available=False)

    with pytest.raises(RuntimeError, match="Set up AI cleanup to use Command Mode"):
        h.commands.complete_recording("shorter", _job())


@pytest.mark.parametrize("cleaner, message", [
    (FakeCleaner(error="timed out after 9 s"), "didn't answer in time"),
    (FakeCleaner(text_rewrite.NEEDS_SELECTION), "Select the text to change first"),
])
def test_failed_rewrites_raise_instead_of_pasting(h, cleaner, message):
    h.runtime._transcript_cleanup = cleaner
    selection = "" if "Select" in message else "old"

    with pytest.raises(RuntimeError, match=message):
        h.commands.complete_recording("make this shorter", _job(selection))


def test_a_selection_that_never_arrives_is_an_error(h, monkeypatch):
    monkeypatch.setattr(command, "SELECTION_WAIT_S", 0.01)
    h.ui.capture_selection = lambda callback, timeout_ms=None: None

    with pytest.raises(RuntimeError, match="Couldn't read the selected text"):
        h.commands.complete_recording("shorter", _job(None))
    assert h.runtime._transcript_cleanup.calls == []


def test_the_dictionary_corrects_the_spoken_instruction(h, monkeypatch):
    monkeypatch.setattr(dictionary, "apply_replacements", lambda text, terms: text.replace("acme", "Acme"))
    h.runtime._transcript_cleanup = cleaner = FakeCleaner()

    h.commands.complete_recording("mention acme", _job(""))

    assert cleaner.calls[0][0] == "mention Acme"


# --- transforms --------------------------------------------------------------


def _begun(h):
    return [event for event in h.runtime.events if event[0] == "begin"]


def test_a_transform_rewrites_the_selection(h):
    h.service.snapshot = FocusSnapshot(NOTEPAD, TextContext(selected="rough draft", selection_known=True))
    h.runtime._transcript_cleanup = cleaner = FakeCleaner("Polished draft.")

    h.commands.run_transform("polish")

    (_begin, job, source), = _begun(h)
    assert job.mode == JobMode.TRANSFORM and source == "Transform · Polish"
    assert job.snapshot().identity == NOTEPAD
    assert _statuses(h) == ["Rewriting · Polish..."]
    _settle(lambda: h.runtime.events[-1][0] == "submit")
    assert job.selection.result(timeout=0) == "rough draft"
    text, raw, info = h.runtime.events[-1][1]()
    assert (text, raw, info.level) == ("Polished draft.", "rough draft", "")
    assert "Improve the flow and clarity" in cleaner.calls[0][1]


def test_an_empty_selection_abandons_the_transform(h):
    h.service.snapshot = FocusSnapshot(NOTEPAD, None)
    h.ui.clipboard_selection = "  "

    h.commands.run_transform("polish")

    _settle(lambda: h.runtime.events[-1][0] == "abandon")
    assert h.runtime.events[-1] == ("abandon", "Select text to transform")
    assert h.released == ["transform-selection"] and h.ui.captures == [True]


def test_a_refused_claim_reads_nothing(h):
    h.runtime.begin_ok = False
    h.ui.clipboard_selection = "text"

    h.commands.run_transform("polish")
    time.sleep(0.05)
    QApplication.processEvents()

    assert [event[0] for event in h.runtime.events] == ["begin"]
    assert h.ui.captures == [] and _statuses(h) == []


def test_unknown_transforms_and_missing_providers_say_so(h, monkeypatch):
    h.commands.run_transform("missing")
    monkeypatch.setattr(text_rewrite, "provider_ready", lambda _settings: False)
    h.commands.run_transform("polish")

    assert _statuses(h) == [
        "That transform no longer exists", "Set up AI cleanup to use transforms",
    ]
    assert h.runtime.events == []


def test_transforms_keep_the_app_out_of_the_job_when_app_context_is_off(h):
    h.settings.values[SettingsKey.APP_CONTEXT_ENABLED] = False
    h.service.snapshot = FocusSnapshot(NOTEPAD, TextContext(selected="text", selection_known=True))

    h.commands.run_transform("fix-grammar")

    (_begin, job, _source), = _begun(h)
    assert job.focus is None
    _settle(lambda: h.runtime.events[-1][0] == "submit")
    assert job.selection.result(timeout=0) == "text"


def test_a_closed_runtime_ignores_shortcuts(h):
    h.commands.cleanup()
    h.commands.key_pressed(1.0)
    h.commands.run_transform("polish")
    assert h.runtime.events == []


def test_the_qt_hop_survives_a_failing_step(h, monkeypatch):
    log = Mock()
    monkeypatch.setattr(command.logger, "exception", log)

    def broken():
        raise ValueError("boom")

    h.commands._qt.requested.emit(broken)
    _settle(lambda: log.called)

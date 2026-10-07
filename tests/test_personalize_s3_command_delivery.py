"""Command Mode and transforms through the real transcription runtime: paste and history."""

import os
import time
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from services import focus_context, synthetic_keys, text_rewrite
from services.dictation_pipeline import DictationJob, JobMode
from services.focus_context import AppIdentity, ContextCaptureService, FocusSnapshot, TextContext
from services.runtime import command, transcription
from services.runtime.command import CommandRuntime
from services.runtime.transcription import TranscriptionRuntime
from services.settings import SettingsKey
from tests.fakes.settings import InMemorySettings

NOTEPAD = AppIdentity("notepad.exe", "Notepad", pid=4, window="0x4")
BROWSER = AppIdentity("chrome.exe", "Chrome", pid=9, window="0x9")


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return QApplication.instance() or QApplication([])


class Signal:
    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler

    def emit(self, *args):
        self.calls.append(args)
        if self.handler is not None:
            self.handler(*args)


class FakeUI:
    def __init__(self):
        self.statuses = []
        self.stages = []
        self.copied = []
        self.main_window = SimpleNamespace(clear_partial_transcription=lambda: None)

    def set_transcript(self, text, raw=None):
        pass

    def set_transcription_stats(self, *args, **kwargs):
        pass

    def set_status(self, text):
        self.statuses.append(text)

    def refresh_history(self):
        pass

    def discard_clipboard_prefetch(self):
        pass

    def copy_to_clipboard(self, text):
        self.copied.append(text)
        return True

    def stage_transcript_for_paste(self, text):
        self.stages.append(text)
        return SimpleNamespace(written=True, lease=None, restore_unavailable=False)

    def schedule_clipboard_restore(self, stage):
        return True

    def insert_into_scratchpad(self, text):
        return False

    def capture_selection(self, callback, *, timeout_ms=None):
        callback("")


class FakeHistory:
    def __init__(self):
        self.entries = []

    def add_entry(self, **fields):
        self.entries.append(fields)
        return SimpleNamespace(id="entry", audio_file=None)


class FakeService(ContextCaptureService):
    def __init__(self, snapshot, current=None):
        self.snapshot, self.current = snapshot, current

    def request(self, *, include_text, include_selection=False):
        future: Future = Future()
        future.set_result(self.snapshot)
        return future

    def current_identity(self):
        return self.current

    def reread(self, identity, callback):
        callback(None)

    def shutdown(self):
        pass


class FakeCleaner:
    def __init__(self, reply):
        self.reply = reply
        self.last_error = "not run"
        self.provider, self.model = "openrouter", "test/model"
        self.cancel_event = None

    def configure(self, provider, model, reasoning=None):
        pass

    def is_available(self):
        return True

    def cleanup(self, text, system_prompt=None, timeout_s=None, deadline_s=None):
        self.last_error = None
        return self.reply


@pytest.fixture
def h(monkeypatch, tmp_path):
    ui = FakeUI()
    history = FakeHistory()
    # Auto-paste off: a rewrite pastes anyway.
    settings = InMemorySettings({SettingsKey.AUTO_PASTE: False, SettingsKey.COPY_CLIPBOARD: True})
    paste = Mock()
    for module in (transcription, command):
        monkeypatch.setattr(module, "settings_manager", settings)
    monkeypatch.setattr(transcription, "history_manager", history)
    monkeypatch.setattr(transcription, "send_paste", paste)
    monkeypatch.setattr(transcription, "is_accessibility_trusted", lambda: True)
    monkeypatch.setattr(text_rewrite, "provider_ready", lambda _settings: True)
    monkeypatch.setattr(synthetic_keys, "wait_for_modifiers_released", lambda timeout_s=0.8: True)
    submitted = []
    controller = SimpleNamespace(
        ui_controller=ui,
        recorder=SimpleNamespace(is_recording=False),
        streaming_runtime=Mock(),
        is_meeting_active=lambda: False,
        overlay_state_update=Signal(),
        status_update=Signal(),
        transcription_completed=Signal(),
        transcription_failed=Signal(),
        executor=SimpleNamespace(submit=lambda fn, *args: submitted.append((fn, args))),
        persistence_executor=SimpleNamespace(submit=lambda fn, *args: fn(*args)),
        current_backend=SimpleNamespace(
            transcribe=lambda path: "make it formal", is_available=lambda: True),
        _pending_audio_path=None,
        _pending_audio_duration=None,
        _pending_file_size=None,
        _pending_source_name=None,
        _pending_streaming_text="",
        _transcription_elapsed=None,
        _transcription_start_time=None,
        _remote_timing=None,
    )
    runtime = TranscriptionRuntime(controller)
    controller.transcription_runtime = runtime
    controller.history_persisted = Signal(runtime.on_history_persisted)
    controller.transcription_completed.handler = runtime.on_transcription_complete
    controller.transcription_failed.handler = runtime.on_transcription_error
    runtime._incremental = Mock()
    runtime._incremental.transcribe.side_effect = lambda backend, path: backend.transcribe(path)
    monkeypatch.setattr(runtime, "_model_info_for_history", lambda: "parakeet")
    controller.command_runtime = CommandRuntime(controller)
    audio = tmp_path / "instruction.wav"
    audio.write_bytes(b"RIFF" + bytes(200))
    yield SimpleNamespace(
        ui=ui, history=history, settings=settings, paste=paste, submitted=submitted,
        controller=controller, runtime=runtime, audio=str(audio),
    )
    controller.command_runtime.cleanup()


def _run_submitted(h):
    while h.submitted:
        fn, args = h.submitted.pop(0)
        fn(*args)


def _command_job(selection, snapshot=None):
    future: Future = Future()
    future.set_result(selection)
    focus = None
    if snapshot is not None:
        focus = Future()
        focus.set_result(snapshot)
    return DictationJob(mode=JobMode.COMMAND, focus=focus, selection=future)


def test_a_command_pastes_its_rewrite_and_keeps_the_selection_as_raw_text(h):
    h.runtime._transcript_cleanup = FakeCleaner("Dear Sir or Madam,")
    h.controller._pending_source_name = "Command Mode"
    focus_context.set_service(FakeService(FocusSnapshot(), current=NOTEPAD))

    assert h.runtime._claim_job(_command_job("hey you", FocusSnapshot(NOTEPAD)))
    h.runtime.transcribe_audio_file(h.audio)

    assert h.ui.stages == ["Dear Sir or Madam,"]
    h.paste.assert_called_once_with()
    entry, = h.history.entries
    assert entry["text"] == "Dear Sir or Madam,"
    assert entry["raw_text"] == "hey you"
    assert entry["entry_kind"] == "command"
    assert entry["source_name"] == "Command Mode"
    assert entry["cleanup_provider"] == "openrouter"
    assert not h.runtime.has_active_job


def test_a_command_never_pastes_into_an_app_that_took_focus(h):
    h.runtime._transcript_cleanup = FakeCleaner("Rewritten.")
    focus_context.set_service(FakeService(FocusSnapshot(), current=BROWSER))

    assert h.runtime._claim_job(_command_job("old", FocusSnapshot(NOTEPAD)))
    h.runtime.transcribe_audio_file(h.audio)

    h.paste.assert_not_called()
    assert h.ui.copied == ["Rewritten."]
    assert h.ui.statuses[-1].startswith("Rewrite copied")


def test_a_failed_command_pastes_nothing(h):
    h.runtime._transcript_cleanup = FakeCleaner(text_rewrite.NEEDS_SELECTION)

    assert h.runtime._claim_job(_command_job(""))
    h.runtime.transcribe_audio_file(h.audio)

    h.paste.assert_not_called()
    assert h.history.entries == []
    assert h.ui.statuses[-1] == "Select the text to change first"
    assert not h.runtime.has_active_job


def test_a_transform_runs_through_the_job_slot_to_paste_and_history(h):
    focus_context.set_service(FakeService(
        FocusSnapshot(NOTEPAD, TextContext(selected="teh draft", selection_known=True)),
        current=NOTEPAD))
    h.runtime._transcript_cleanup = FakeCleaner("The draft.")

    h.controller.command_runtime.run_transform("fix-grammar")
    assert h.runtime.has_active_job

    deadline = time.monotonic() + 2
    while not h.submitted and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.002)
    _run_submitted(h)

    h.paste.assert_called_once_with()
    entry, = h.history.entries
    assert (entry["text"], entry["raw_text"]) == ("The draft.", "teh draft")
    assert entry["entry_kind"] == "transform"
    assert entry["source_name"] == "Transform · Fix grammar"
    assert entry["app_id"] == "notepad.exe"
    assert not h.runtime.has_active_job


def test_a_transform_with_nothing_selected_frees_the_slot(h):
    h.controller.command_runtime.run_transform("polish")

    deadline = time.monotonic() + 2
    while h.runtime.has_active_job and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.002)

    assert not h.runtime.has_active_job
    assert h.controller.status_update.calls[-1] == ("Select text to transform",)
    h.paste.assert_not_called()
    assert h.history.entries == []


def test_command_recordings_are_named_command_mode_in_history(h, monkeypatch, tmp_path):
    from config import config

    wav = tmp_path / "recorded.wav"
    wav.write_bytes(b"RIFF" + bytes(400))
    monkeypatch.setattr(config, "RECORDED_AUDIO_FILE", str(wav))
    h.controller.recorder = SimpleNamespace(
        is_recording=False, capture_canceled=False, last_capture_error=None,
        wait_for_stop_completion=lambda: True, has_recording_data=lambda: True,
        save_recording=lambda allow_incomplete=True: True,
        get_recording_duration=lambda: 1.5,
    )
    h.controller.streaming_runtime.stop_streaming_session.return_value = ""
    monkeypatch.setattr(h.runtime, "_require_backend_ready", lambda: None)
    h.runtime._transcript_cleanup = FakeCleaner("Formal.")

    assert h.runtime._claim_job(_command_job("hey"))
    h.runtime.finish_recording_job()

    entry, = h.history.entries
    assert entry["source_name"] == "Command Mode"
    assert entry["text"] == "Formal."

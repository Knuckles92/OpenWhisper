"""Command Mode and transforms reached through the controller's shortcut entry points."""

import importlib
import sys
import tempfile
import time
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

import pytest

from config import config
from services.focus_context import (
    AppIdentity,
    ContextCaptureService,
    FocusSnapshot,
    TextContext,
)
from tests import test_application_controller as harness

NOTEPAD = AppIdentity("notepad.exe", "Notepad", pid=4, window="0x4")


class SelectionService(ContextCaptureService):
    def __init__(self, selected):
        self.selected = selected

    def request(self, *, include_text, include_selection=False):
        future: Future = Future()
        future.set_result(FocusSnapshot(NOTEPAD, TextContext(
            selected=self.selected, selection_known=include_selection)))
        return future

    def current_identity(self):
        return NOTEPAD

    def reread(self, identity, callback):
        callback(None)

    def shutdown(self):
        pass


class FakeCleaner:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []
        self.last_error = "not run"
        self.provider, self.model = "openrouter", "test/model"

    def configure(self, provider, model, reasoning=None):
        pass

    def is_available(self):
        return True

    def cleanup(self, text, system_prompt=None, timeout_s=None, deadline_s=None):
        self.calls.append(text)
        self.last_error = None
        return self.reply


@pytest.fixture
def h(monkeypatch):
    settings = harness.FakeSettingsManager()
    settings.all_settings["recording_trigger_mode"] = "push_hold"
    history = harness.FakeHistoryManager()
    keyboard = harness.FakeKeyboard()
    temp_dir = tempfile.TemporaryDirectory()
    monkeypatch.setattr(config, "RECORDED_AUDIO_FILE", str(Path(temp_dir.name) / "recorded.wav"))
    importlib.import_module("transcriber.optional_backend")
    importlib.import_module("services.local_asr.process")
    importlib.import_module("services.isolated")
    speech_cache = importlib.import_module("services.local_asr.cache")
    monkeypatch.setattr(speech_cache, "model_dir",
                        lambda key: Path(temp_dir.name) / "speech-models" / key)
    stubs = harness._install_module_stubs(settings, history, keyboard, {"closed": False})
    with patch.dict(sys.modules, stubs):
        for name in (
            "services.runtime", "services.runtime.hotkeys", "services.runtime.streaming",
            "services.runtime.transcription", "services.runtime.meeting",
            "services.runtime.command", "services.application_controller",
        ):
            sys.modules.pop(name, None)
        module = importlib.import_module("services.application_controller")
        hotkeys = importlib.import_module("services.runtime.hotkeys")
        with patch.object(hotkeys.HotkeyRuntime, "setup_hook_watchdog", lambda _self: None):
            controller = module.ApplicationController(harness.DummyUIController())
            controller.executor.shutdown(wait=False)
            controller.executor = harness.FakeExecutor()
            controller._startup_executor.shutdown(wait=True)
            controller._startup_executor = harness.FakeExecutor()
            controller.persistence_executor = harness.FakeExecutor()
            command = sys.modules[type(controller.command_runtime).__module__]
            monkeypatch.setattr(command.text_rewrite, "provider_ready", lambda _settings: True)
            monkeypatch.setattr(command, "settings_manager", settings)
            from services import focus_context

            focus_context.set_service(SelectionService("hey team, meeting moved"))
            controller.transcription_runtime._transcript_cleanup = FakeCleaner(
                "Hello team, the meeting has moved.")
            yield controller, history, keyboard
            controller.command_runtime.cleanup()
    temp_dir.cleanup()


def _run_jobs(controller):
    while controller.executor.submissions:
        fn, args = controller.executor.submissions.pop(0)
        fn(*args)


def test_holding_the_command_shortcut_rewrites_the_selection_and_pastes(h):
    controller, history, keyboard = h

    controller.command_key_pressed(1.0)
    assert controller.recorder.is_recording
    controller.command_key_released(2.0)
    _run_jobs(controller)

    # Auto-paste is off in these settings; a rewrite pastes anyway.
    assert keyboard.sent == ["ctrl+v"]
    entry, = history.entries
    assert entry["text"] == "Hello team, the meeting has moved."
    assert entry["raw_text"] == "hey team, meeting moved"
    assert entry["entry_kind"] == "command"
    assert entry["source_name"] == "Command Mode"
    assert not controller.transcription_runtime.has_active_job


def test_a_transform_shortcut_rewrites_the_selection(h):
    controller, history, keyboard = h

    controller.transform_requested.emit("fix-grammar")
    deadline = time.monotonic() + 2
    while not controller.executor.submissions and time.monotonic() < deadline:
        time.sleep(0.005)
    _run_jobs(controller)

    assert keyboard.sent == ["ctrl+v"]
    entry, = history.entries
    assert entry["entry_kind"] == "transform"
    assert entry["source_name"] == "Transform · Fix grammar"
    assert entry["raw_text"] == "hey team, meeting moved"

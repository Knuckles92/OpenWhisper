"""Paste original of last dictation: the tray item and its shortcut."""

import importlib
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from config import config


def _actions():
    return importlib.import_module("ui_qt.history_actions")


def _history():
    return importlib.import_module("services.history_manager")


class FakeUI:
    def __init__(self, pastes=True):
        self.statuses = []
        self.pasted = []
        self.refreshed = 0
        self._pastes = pastes

    def set_status(self, text):
        self.statuses.append(text)

    def refresh_history(self):
        self.refreshed += 1

    def on_paste_text_now(self, text):
        self.pasted.append(text)
        return self._pastes


@pytest.fixture(autouse=True)
def quiet_sync(monkeypatch):
    sync_module = importlib.import_module("services.remote_records.sync")
    fake = type("Sync", (), {
        "record_saved": lambda *_a: None,
        "record_edited": lambda *_a: None,
        "record_deleted": lambda *_a: None,
    })()
    monkeypatch.setattr(sync_module.record_sync, "_instance", fake)


def _dictation(text, raw=None, **fields):
    fields.setdefault("entry_kind", "dictation")
    fields.setdefault("source_name", "Quick Record")
    return _history().history_manager.add_entry(text=text, raw_text=raw, model="base", **fields)


def test_nothing_dictated_yet():
    ui = FakeUI()
    _actions().paste_last_original(ui)
    assert ui.statuses == [_actions().NOTHING_YET] and ui.pasted == []


def test_the_last_dictations_original_is_pasted_and_kept():
    history = _history()
    entry = _dictation("Hello, world.", raw="um hello world")
    ui = FakeUI()

    _actions().paste_last_original(ui)

    assert ui.pasted == ["um hello world"]
    assert ui.statuses == [_actions().PASTED] and ui.refreshed == 1
    stored = history.history_manager.get_entry_by_id(entry.id)
    assert (stored.text, stored.cleaned_text) == ("um hello world", "Hello, world.")

    _actions().paste_last_original(ui)
    assert ui.pasted == ["um hello world"] * 2
    assert history.history_manager.get_entry_by_id(entry.id).text == "um hello world"


def test_an_unchanged_last_dictation_never_reaches_back_to_an_older_one():
    _dictation("Older, cleaned.", raw="older cleaned")
    _dictation("as heard")
    ui = FakeUI()
    _actions().paste_last_original(ui)
    assert ui.statuses == [_actions().NOT_EDITED] and ui.pasted == []


def test_uploads_and_rewrites_after_it_are_skipped():
    _dictation("Dictated.", raw="dictated um")
    _history().history_manager.add_entry(text="a file", model="base", entry_kind="file",
                                         source_name="talk.mp3")
    _history().history_manager.add_entry(text="Formal.", raw_text="formal pls", model="base",
                                         entry_kind="command", source_name="Quick Record")
    ui = FakeUI()
    _actions().paste_last_original(ui)
    assert ui.pasted == ["dictated um"]


def test_a_refused_paste_leaves_the_entry_and_the_status_alone():
    history = _history()
    entry = _dictation("Hello, world.", raw="um hello world")
    ui = FakeUI(pastes=False)
    _actions().paste_last_original(ui)
    assert ui.pasted == ["um hello world"] and ui.statuses == []
    assert history.history_manager.get_entry_by_id(entry.id).text == "Hello, world."


def test_without_a_paste_hook_it_says_so():
    _dictation("Hello, world.", raw="um hello world")
    ui = FakeUI()
    ui.on_paste_text_now = None
    _actions().paste_last_original(ui)
    assert ui.statuses == [_actions().UNAVAILABLE]


def test_the_controller_gives_the_ui_its_paste(monkeypatch):
    from tests import test_application_controller as harness

    # The controller wires the real outbox's hooks, not the fake above.
    sync_module = importlib.import_module("services.remote_records.sync")
    monkeypatch.setattr(sync_module.record_sync, "_instance", None)
    settings = harness.FakeSettingsManager()
    temp_dir = tempfile.TemporaryDirectory()
    monkeypatch.setattr(config, "RECORDED_AUDIO_FILE", str(Path(temp_dir.name) / "recorded.wav"))
    for name in ("transcriber.optional_backend", "services.local_asr.process", "services.isolated"):
        importlib.import_module(name)
    speech_cache = importlib.import_module("services.local_asr.cache")
    monkeypatch.setattr(speech_cache, "model_dir",
                        lambda key: Path(temp_dir.name) / "speech-models" / key)
    stubs = harness._install_module_stubs(
        settings, harness.FakeHistoryManager(), harness.FakeKeyboard(), {"closed": False}
    )
    try:
        with patch.dict(sys.modules, stubs):
            for name in (
                "services.runtime", "services.runtime.hotkeys", "services.runtime.streaming",
                "services.runtime.transcription", "services.runtime.meeting",
                "services.application_controller",
            ):
                sys.modules.pop(name, None)
            module = importlib.import_module("services.application_controller")
            hotkeys = importlib.import_module("services.runtime.hotkeys")
            with patch.object(hotkeys.HotkeyRuntime, "setup_hook_watchdog", lambda _self: None):
                controller = module.ApplicationController(harness.DummyUIController())
                try:
                    paste = controller.ui_controller.on_paste_text_now
                    assert paste == controller.transcription_runtime.paste_text_now
                finally:
                    controller.executor.shutdown(wait=False)
                    controller._startup_executor.shutdown(wait=True)
                    controller.persistence_executor.shutdown(wait=True)
    finally:
        temp_dir.cleanup()

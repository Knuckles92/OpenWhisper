"""The tray copies the last dictation's original instead of pasting it blind."""

import importlib
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from ui_qt import history_actions
from ui_qt.ui_controller import UIController


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def quiet_sync(monkeypatch):
    sync_module = importlib.import_module("services.remote_records.sync")
    fake = type("Sync", (), {
        "record_saved": lambda *_a: None,
        "record_edited": lambda *_a: None,
        "record_deleted": lambda *_a: None,
    })()
    monkeypatch.setattr(sync_module.record_sync, "_instance", fake)


def _history():
    return importlib.import_module("services.history_manager").history_manager


def _dictation(text, raw=None):
    return _history().add_entry(text=text, raw_text=raw, model="base",
                                entry_kind="dictation", source_name="Quick Record")


class FakeUI:
    def __init__(self, copies=True):
        self.statuses = []
        self.copied = []
        self.pasted = []
        self._copies = copies

    def set_status(self, text):
        self.statuses.append(text)

    def refresh_history(self):
        pass

    def copy_to_clipboard(self, text, html=""):
        self.copied.append(text)
        return self._copies

    def on_paste_text_now(self, text):
        self.pasted.append(text)
        return True


def test_copy_puts_the_original_on_the_clipboard_and_keeps_the_version():
    entry = _dictation("Hello, world.", raw="um hello world")
    ui = FakeUI()

    history_actions.copy_last_original(ui)

    assert ui.copied == ["um hello world"] and ui.pasted == []
    assert ui.statuses == [history_actions.COPIED]
    assert "Ctrl+V" in history_actions.COPIED or "Cmd+V" in history_actions.COPIED
    assert _history().get_entry_by_id(entry.id).text == "Hello, world."


def test_a_failed_copy_says_so():
    entry = _dictation("Hello, world.", raw="um hello world")
    ui = FakeUI(copies=False)

    history_actions.copy_last_original(ui)

    assert ui.statuses == [history_actions.COPY_FAILED]
    assert _history().get_entry_by_id(entry.id).text == "Hello, world."


def test_copy_with_nothing_to_copy():
    ui = FakeUI()
    history_actions.copy_last_original(ui)
    _dictation("as heard")
    history_actions.copy_last_original(ui)

    assert ui.statuses == [history_actions.NOTHING_TO_COPY, history_actions.NOT_EDITED]
    assert ui.copied == [] and ui.pasted == []


def test_the_tray_item_copies_and_never_pastes():
    _dictation("Hello, world.", raw="um hello world")
    app_ui = UIController()
    try:
        copied, pasted, statuses = [], [], []
        app_ui.copy_to_clipboard = lambda text, html="": copied.append(text) or True
        app_ui.on_paste_text_now = lambda text: pasted.append(text) or True
        app_ui.set_status = statuses.append

        app_ui.tray_manager.copy_original_action.trigger()

        assert copied == ["um hello world"] and pasted == []
        assert statuses == [history_actions.COPIED]
        assert app_ui.tray_manager.copy_original_action.text() == "Copy original of last dictation"
    finally:
        app_ui.cleanup()

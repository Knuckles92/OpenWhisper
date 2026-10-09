"""Basic and Quick Record agree with Commands: Command Mode needs an AI model, not cleanup on."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication

from services import text_rewrite
from services.hotkey_manager import format_hotkey_display
from services.settings import SettingsKey, SettingsView, settings_manager
from ui_qt.dialogs.settings_destinations import BASIC_DICTATION, COMMANDS
from tests.test_personalize_s1_settings_page import (  # noqa: F401  (fixtures)
    _qapp, _restore_metadata, _windows_text, make_dialog,
)


@pytest.fixture
def ready(monkeypatch):
    state = {"ready": True}
    monkeypatch.setattr(text_rewrite, "provider_ready", lambda _settings: state["ready"])
    return state


def _tab(command_key="", cleanup=False):
    from ui_qt.widgets.quick_record_tab import QuickRecordTab

    settings_manager.save_setting(SettingsKey.TRANSCRIPT_CLEANUP_ENABLED, cleanup)
    settings_manager.save_setting(SettingsKey.HOTKEYS, {"command_mode": command_key})
    return QuickRecordTab()


def test_quick_record_shows_the_hint_with_cleanup_off(ready):
    tab = _tab()
    assert not tab.profile_card.isVisibleTo(tab)
    assert tab.command_link.isVisibleTo(tab)
    assert tab.command_link.text() == "Set a Command Mode shortcut  →"

    settings_manager.save_setting(SettingsKey.HOTKEYS, {"command_mode": "ctrl+alt+k"})
    tab.update_hotkeys("*", "-")
    assert tab.command_hint.isVisibleTo(tab) and not tab.command_link.isVisibleTo(tab)
    # macOS spells the shortcut with symbols (⌃⌥K).
    assert tab.command_hint.text() == (
        f"Command Mode · {format_hotkey_display('ctrl+alt+k')} · "
        "Select text, then say how to change it."
    )


def test_quick_record_asks_for_ai_setup_until_a_model_is_ready(ready):
    ready["ready"] = False
    tab = _tab("ctrl+alt+k", cleanup=True)
    requested = []
    tab.settings_requested.connect(requested.append)
    assert not tab.command_hint.isVisibleTo(tab)
    assert tab.command_link.isVisibleTo(tab)
    assert tab.command_link.text() == "Set up Command Mode  →"
    assert "AI cleanup model" in tab.command_link.toolTip()
    tab.command_link.click()
    assert requested == [COMMANDS]

    # Set up in Settings, then back to the main window.
    ready["ready"] = True
    QApplication.sendEvent(tab, QEvent(QEvent.Type.WindowActivate))
    assert tab.command_hint.isVisibleTo(tab) and not tab.command_link.isVisibleTo(tab)


def _basic_detail(dialog):
    page = dialog._basic_pages[BASIC_DICTATION]
    for row, control in page._rows:
        if control is dialog.commands_basic_shortcut:
            return page, row.itemAt(0).layout().itemAt(1).widget().text()
    raise AssertionError("Command Mode row not found")


def test_basic_says_command_mode_needs_an_ai_model(make_dialog, ready):  # noqa: F811
    ready["ready"] = False
    dialog, _store = make_dialog({SettingsKey.SETTINGS_VIEW: SettingsView.BASIC})
    page, text = _basic_detail(dialog)
    assert text == "Needs an AI cleanup model, even while cleanup is off."

    ready["ready"] = True
    page.refresh()
    _page, text = _basic_detail(dialog)
    assert "AI cleanup" not in text
    assert text.startswith("Select text, then say how to change it.")

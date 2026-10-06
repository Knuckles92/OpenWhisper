"""Quick Record shows the Command Mode shortcut, or a link to set one."""

import os
from unittest.mock import Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from services import text_rewrite
from services.settings import SettingsKey, settings_manager
from ui_qt.dialogs.settings_destinations import COMMANDS
from ui_qt.widgets.quick_record_tab import QuickRecordTab


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _provider_ready(monkeypatch):
    monkeypatch.setattr(text_rewrite, "provider_ready", lambda _settings: True)


def _set_command_key(hotkey):
    settings_manager.save_setting(SettingsKey.HOTKEYS, {"command_mode": hotkey})


def test_without_a_shortcut_it_links_to_commands():
    _set_command_key("")
    tab = QuickRecordTab()
    requested = []
    tab.settings_requested.connect(requested.append)

    assert tab.command_link.isVisibleTo(tab)
    assert not tab.command_hint.isVisibleTo(tab)
    tab.command_link.click()
    assert requested == [COMMANDS]


def test_with_a_shortcut_it_shows_it_and_follows_changes():
    _set_command_key("ctrl+alt+k")
    tab = QuickRecordTab()

    assert tab.command_hint.text() == (
        "Command Mode · Ctrl+Alt+K · Select text, then say how to change it."
    )
    assert tab.command_hint.isVisibleTo(tab)
    assert not tab.command_link.isVisibleTo(tab)

    _set_command_key("")
    tab.update_hotkeys("*", "-")
    assert tab.command_link.isVisibleTo(tab)


def test_the_link_opens_settings_on_commands(monkeypatch):
    from ui_qt.ui_controller import UIController

    opened = Mock()
    monkeypatch.setattr(UIController, "open_settings_destination", opened)
    controller = UIController()
    try:
        controller.main_window.quick_record_tab.settings_requested.emit(COMMANDS)
    finally:
        controller.cleanup()
    opened.assert_called_once_with(COMMANDS)

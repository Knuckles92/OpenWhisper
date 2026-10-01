"""MCP notifications reach the GUI thread and apply current preferences."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

from PyQt6.QtCore import QObject
from PyQt6.QtWidgets import QApplication

from services.models import TranscriptionHistory
from services.settings import SettingsKey, settings_manager
from ui_qt.ui_controller import UIController
from ui_qt.widgets.history_sidebar import HistoryItemWidget


def test_settings_notifications_apply_on_gui_thread_and_use_latest_values():
    # Construct only the QObject signal bridge, without starting native services.
    ui = UIController.__new__(UIController)
    QObject.__init__(ui)
    ui._settings_dialog = None
    applied = []
    ui._apply_ui_theme = lambda value: applied.append((value, threading.get_ident()))
    ui.agent_data_changed.connect(ui._apply_agent_changes)
    settings_manager.save_setting(SettingsKey.UI_THEME, "dark")
    worker = threading.Thread(
        target=lambda: ui.agent_data_changed.emit(
            "settings", {SettingsKey.UI_THEME: "light"}
        )
    )
    worker.start()
    worker.join()
    assert not applied
    QApplication.processEvents()
    assert applied == [("dark", threading.get_ident())]


def test_desktop_hooks_refresh_preview_cleanup_trigger_and_open_settings():
    ui = UIController.__new__(UIController)
    QObject.__init__(ui)
    ui._settings_dialog = SimpleNamespace(refresh=Mock())
    ui._on_settings_streaming_changed = Mock()
    ui.overlay = SimpleNamespace(refresh_streaming_font_size=Mock())
    ui.refresh_cleanup_controls = Mock()
    ui._on_settings_recording_trigger_mode_changed = Mock()
    changes = {
        SettingsKey.STREAMING_ENABLED: True,
        SettingsKey.STREAMING_OVERLAY_FONT_SIZE: 24,
        SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: False,
        SettingsKey.RECORDING_TRIGGER_MODE: "push_hold",
    }
    settings_manager.update_settings(changes)
    ui._apply_agent_changes("settings", changes)
    ui._on_settings_streaming_changed.assert_called_once_with()
    ui.overlay.refresh_streaming_font_size.assert_called_once_with()
    ui.refresh_cleanup_controls.assert_called_once_with()
    ui._on_settings_recording_trigger_mode_changed.assert_called_once_with("push_hold")
    ui._settings_dialog.refresh.assert_called_once_with()


def test_history_uses_title_without_replacing_source_filename():
    entry = TranscriptionHistory.create(
        text="Body", model="test", source_name="original.wav"
    )
    entry.title = "Project kickoff"
    widget = HistoryItemWidget(entry)
    assert widget.title_label.text() == "Project kickoff"
    assert entry.source_name == "original.wav"

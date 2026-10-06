"""Omarchy bar parity: Scratchpad and language buttons, and the status it reads."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6.QtCore import Qt

from services import dictation_language
from services.omarchy_controls import OmarchyControls
from services.settings import SettingsKey, settings_manager


def _controls(*, recording=False, hands_free=False):
    overlay = SimpleNamespace(hands_free=hands_free)
    ui = Mock()
    ui.overlay = overlay
    ui.main_window.isActiveWindow.return_value = False
    controller = SimpleNamespace(
        ui_controller=ui,
        recorder=SimpleNamespace(is_recording=recording),
        is_transcribing=lambda: False,
        is_meeting_active=lambda: False,
        hotkey_manager=SimpleNamespace(program_enabled=True),
    )
    return OmarchyControls(controller), ui


def _status(native):
    return json.loads(native.Status())


def test_bar_buttons_open_the_scratchpad_and_switch_language():
    native, ui = _controls()
    assert native.Button("scratchpad")
    assert native.Button("cycle_language")
    ui.toggle_scratchpad.assert_called_once_with()
    ui.cycle_dictation_language.assert_called_once_with()


@pytest.mark.parametrize("recording,latched,expected", [
    (True, True, True), (True, False, False), (False, True, False),
])
def test_status_reports_hands_free_only_while_recording(recording, latched, expected):
    native, _ui = _controls(recording=recording, hands_free=latched)
    assert _status(native)["hands_free"] is expected


def test_status_reports_the_language_chip_when_there_is_a_choice():
    native, _ui = _controls()
    settings_manager.update_settings({
        SettingsKey.SELECTED_MODEL: "local_whisper",
        SettingsKey.DICTATION_LANGUAGES: ["en", "es"],
        SettingsKey.DICTATION_ACTIVE_LANGUAGE: "es",
    })
    status = _status(native)
    assert status["language"] == "ES"
    assert status["language_name"] == "Spanish"

    settings_manager.update_settings({SettingsKey.DICTATION_LANGUAGES: ["es"]})
    status = _status(native)
    assert status["language"] == ""
    assert status["language_name"] == "Spanish"


def test_status_survives_a_broken_language_list(monkeypatch):
    monkeypatch.setattr(dictation_language, "language_choices", Mock(side_effect=ValueError))
    native, _ui = _controls()
    status = _status(native)
    assert status["language"] == "" and status["language_name"] == ""
    assert status["recording"] is False


def test_bar_scratchpad_is_a_compositor_window_and_language_cycles(monkeypatch, tmp_path):
    from ui_qt.ui_controller import UIController

    monkeypatch.setenv("OPENWHISPER_UI", "omarchy")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    settings_manager.update_settings({
        SettingsKey.SELECTED_MODEL: "local_whisper",
        SettingsKey.DICTATION_LANGUAGES: ["en", "fr"],
        SettingsKey.DICTATION_ACTIVE_LANGUAGE: "en",
    })
    app_ui = UIController()
    native = OmarchyControls(SimpleNamespace(
        ui_controller=app_ui,
        recorder=SimpleNamespace(is_recording=False),
        is_transcribing=lambda: False,
        is_meeting_active=lambda: False,
        hotkey_manager=SimpleNamespace(program_enabled=True),
    ))
    try:
        assert native.Button("scratchpad")
        pad = app_ui._scratchpad
        assert pad.isVisible()
        assert not pad.windowFlags() & Qt.WindowType.FramelessWindowHint
        assert pad.header is None

        assert native.Button("cycle_language")
        assert settings_manager.get(SettingsKey.DICTATION_ACTIVE_LANGUAGE) == "fr"
        assert _status(native)["language"] == "FR"
    finally:
        pad = getattr(app_ui, "_scratchpad", None)
        if pad is not None:
            pad.hide()
            pad.deleteLater()
        app_ui.cleanup()

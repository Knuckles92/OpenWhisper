"""Who started a recording reaches its events; shortcut names; logging of URLs."""

import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from config import config
from services.hotkey_conflicts import STANDARD, STANDARD_LABELS, hotkey_conflict
from services.settings import SettingsKey

# -- source (pipeline #8) --------------------------------------------------------


def _ui():
    from ui_qt.ui_controller import UIController

    calls = []
    fake = SimpleNamespace(
        _transcription_source_tab=None,
        on_record_start=lambda **kw: calls.append(("record", kw)),
        on_profile_record_start=lambda profile_id, **kw: calls.append((profile_id, kw)),
        is_recording=False,
    )
    fake.start_recording = lambda *a, **k: UIController.start_recording(fake, *a, **k)
    return UIController, fake, calls


def test_the_window_button_records_with_source_ui():
    UIController, fake, calls = _ui()
    UIController.start_recording(fake)
    UIController.start_recording(fake, "email")
    assert calls == [("record", {"source": "ui"}), ("email", {"source": "ui"})]


def test_the_tray_records_with_source_ui_and_the_shell_with_dbus():
    UIController, fake, calls = _ui()
    fake._on_tray_toggle_recording = (
        lambda **k: UIController._on_tray_toggle_recording(fake, **k))
    UIController._on_tray_toggle_recording(fake)
    UIController._on_shell_toggle_recording(fake)
    assert calls == [("record", {"source": "ui"}), ("record", {"source": "dbus"})]


def test_an_omarchy_shell_click_records_with_source_dbus():
    from services.omarchy_controls import OmarchyControls

    ui = Mock()
    native = OmarchyControls(SimpleNamespace(
        ui_controller=ui, hotkey_manager=SimpleNamespace(program_enabled=True)))
    assert native.Button("record")
    ui._on_shell_toggle_recording.assert_called_once_with()


@pytest.mark.parametrize("source", ["ui", "dbus", "hotkey"])
def test_the_controller_passes_the_source_on(source):
    from services.application_controller import ApplicationController

    runtime = Mock()
    controller = SimpleNamespace(
        _refuse_dictation_during_meeting=lambda: False,
        transcription_readiness_message=lambda: None,
        transcription_runtime=runtime,
    )
    ApplicationController.start_recording(controller, source=source)
    ApplicationController.start_profile_recording(controller, "email", source=source)
    assert runtime.start_recording.call_args_list[0].kwargs == {"source": source}
    assert runtime.start_recording.call_args_list[1].args == ("email",)
    assert runtime.start_recording.call_args_list[1].kwargs == {"source": source}


def test_a_shortcut_still_records_with_source_hotkey():
    from services.application_controller import ApplicationController

    runtime = Mock()
    controller = SimpleNamespace(
        _refuse_dictation_during_meeting=lambda: False,
        transcription_readiness_message=lambda: None,
        transcription_runtime=runtime,
    )
    ApplicationController.start_recording(controller)
    assert runtime.start_recording.call_args.kwargs == {"source": "hotkey"}


# -- shortcut names in conflict messages (UI #14) --------------------------------

def test_conflicts_name_standard_shortcuts_as_people_read_them():
    settings = {SettingsKey.HOTKEYS: {"minimize_tray": "ctrl+alt+m"}}
    assert hotkey_conflict(config.DEFAULT_HOTKEYS["record_toggle"], settings) == "Recording"
    assert hotkey_conflict("ctrl+alt+m", settings) == "Minimize to tray"
    for action in config.DEFAULT_HOTKEYS:
        assert action in STANDARD_LABELS, f"{action} has no label"
    assert hotkey_conflict(
        config.DEFAULT_HOTKEYS["record_toggle"], settings, exclude=(STANDARD, "record_toggle")
    ) == ""


def test_the_settings_window_uses_the_same_labels():
    from ui_qt.dialogs import settings_dialog

    assert settings_dialog._HOTKEY_LABELS is STANDARD_LABELS


# -- request URLs stay out of the log (security) ---------------------------------

def test_http_client_loggers_only_warn(monkeypatch):
    from ui_qt import bootstrap

    monkeypatch.setattr(bootstrap, "RotatingFileHandler", lambda *a, **k: logging.NullHandler())
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: None)
    monkeypatch.setattr(bootstrap, "_enable_crash_logging", lambda: None)
    monkeypatch.setattr(bootstrap, "_install_qt_message_handler", lambda: None)
    import services.diagnostics as diagnostics

    monkeypatch.setattr(diagnostics, "install_failure_capture", lambda: None)
    loggers = [logging.getLogger(name) for name in ("httpx", "httpcore")]
    before = [logger.level for logger in loggers]
    try:
        for logger in loggers:
            logger.setLevel(logging.NOTSET)
        bootstrap.setup_logging()
        assert [logger.level for logger in loggers] == [logging.WARNING, logging.WARNING]
    finally:
        for logger, level in zip(loggers, before):
            logger.setLevel(level)

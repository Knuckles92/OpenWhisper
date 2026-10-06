"""The controller's personalization signals reach the UI and the command runtime."""

import importlib
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from config import config
from tests import test_application_controller as harness


@pytest.fixture
def controller(monkeypatch):
    settings = harness.FakeSettingsManager()
    settings.audio_input_device = 3
    temp_dir = tempfile.TemporaryDirectory()
    monkeypatch.setattr(config, "RECORDED_AUDIO_FILE", str(Path(temp_dir.name) / "recorded.wav"))
    importlib.import_module("transcriber.optional_backend")
    importlib.import_module("services.local_asr.process")
    importlib.import_module("services.isolated")
    speech_cache = importlib.import_module("services.local_asr.cache")
    monkeypatch.setattr(speech_cache, "model_dir",
                        lambda key: Path(temp_dir.name) / "speech-models" / key)
    stubs = harness._install_module_stubs(
        settings, harness.FakeHistoryManager(), harness.FakeKeyboard(), {"closed": False}
    )
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
            instance = module.ApplicationController(harness.DummyUIController())
            instance.executor.shutdown(wait=False)
            instance.executor = harness.FakeExecutor()
            instance._startup_executor.shutdown(wait=True)
            instance._startup_executor = harness.FakeExecutor()
            instance.persistence_executor = harness.FakeExecutor()
            yield instance
    temp_dir.cleanup()


def test_ui_signals_reach_their_ui_methods(controller):
    ui = controller.ui_controller

    controller.scratchpad_toggle_requested.emit()
    controller.cycle_language_requested.emit()
    controller.paste_last_original_requested.emit()
    controller.hands_free_changed.emit(True)
    controller.recording_device_switched.emit("USB mic", "Laptop mic")
    controller.dictionary_term_learned.emit("Kubernetes")

    assert ui.flow_calls == [
        ("toggle_scratchpad",),
        ("cycle_dictation_language",),
        ("paste_last_original",),
        ("set_hands_free", True),
        ("on_recording_device_switched", "USB mic", "Laptop mic"),
        ("on_dictionary_term_learned", "Kubernetes"),
    ]
    assert ui.overlay.hands_free is True


def test_command_keys_and_transforms_reach_the_command_runtime(controller):
    command = Mock()
    controller.command_runtime = command

    controller.command_key_pressed(1.5)
    controller.command_key_released(2.0)
    command.key_pressed.assert_called_once_with(1.5)
    command.key_released.assert_called_once_with(2.0)


def test_commands_and_transforms_without_a_provider_say_how_to_start(controller, monkeypatch):
    command_module = sys.modules[type(controller.command_runtime).__module__]
    monkeypatch.setattr(command_module.text_rewrite, "provider_ready", lambda _settings: False)

    controller.transform_requested.emit("polish")
    controller.command_key_pressed(1.0)

    assert controller.ui_controller.statuses[-2:] == [
        "Set up AI cleanup to use transforms", "Set up AI cleanup to use Command Mode",
    ]
    assert not controller.recorder.is_recording
    with pytest.raises(RuntimeError, match="Didn't catch an instruction"):
        controller.command_runtime.complete_recording("  ", None)


def test_settings_changes_refresh_shortcuts_for_transforms_and_hotkeys(controller):
    refresh = Mock()
    controller.hotkey_runtime.refresh_profile_hotkeys = refresh

    assert controller.ui_controller.on_settings_changed == controller.on_settings_changed
    for kind in ("transforms", "hotkeys", "dictionary", "snippets", "styles", "languages"):
        controller.ui_controller.on_settings_changed(kind)

    assert refresh.call_count == 2


def test_the_recorder_comes_from_settings_and_reports_switches(controller):
    assert controller.recorder.device_id == 3
    controller.recorder.device_switch_callback("USB mic", "Laptop mic")

    controller.change_audio_device(5)

    assert controller.recorder.device_switch_callback is not None
    controller.recorder.device_switch_callback("Laptop mic", "System default")
    assert controller.ui_controller.flow_calls == [
        ("on_recording_device_switched", "USB mic", "Laptop mic"),
        ("on_recording_device_switched", "Laptop mic", "System default"),
    ]


def test_cleanup_stops_playback_focus_capture_and_commands(controller, monkeypatch):
    from services import audio_player, focus_context

    stopped, shut = [], []
    monkeypatch.setattr(audio_player, "stop_playback", lambda: stopped.append(True))
    monkeypatch.setattr(focus_context, "shutdown_service", lambda: shut.append(True))
    command = Mock()
    controller.command_runtime = command

    controller.cleanup()

    assert stopped == [True] and shut == [True]
    command.cleanup.assert_called_once_with()

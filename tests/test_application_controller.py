"""Controller-level tests for the extracted application controller."""

import importlib
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from config import config


class _BoundSignal:
    def __init__(self):
        self._handlers = []

    def connect(self, handler):
        self._handlers.append(handler)

    def emit(self, *args, **kwargs):
        for handler in list(self._handlers):
            handler(*args, **kwargs)


class _SignalDescriptor:
    def __set_name__(self, owner, name):
        self.storage_name = f"__signal_{name}"

    def __get__(self, instance, owner):
        if instance is None:
            return self
        if not hasattr(instance, self.storage_name):
            setattr(instance, self.storage_name, _BoundSignal())
        return getattr(instance, self.storage_name)


def _pyqt_signal(*_args, **_kwargs):
    return _SignalDescriptor()


class _QObject:
    def __init__(self, *_args, **_kwargs):
        pass


class _QTimer:
    def __init__(self):
        self.timeout = _BoundSignal()

    def setTimerType(self, _timer_type):
        pass

    def start(self, _interval):
        pass

    def stop(self):
        pass


class _Qt:
    class TimerType:
        CoarseTimer = 1
        VeryCoarseTimer = 2


class FakeSettingsManager:
    def __init__(self):
        self.all_settings = {
            "streaming_enabled": True,
            "streaming_paste_enabled": True,
            "streaming_tiny_model_enabled": False,
            "streaming_chunk_duration": 4.0,
            "copy_clipboard": True,
            "auto_paste": False,
        }
        self.saved_model_selection = None
        self.saved_hotkeys = None
        self.audio_input_device = None

    def load_audio_input_device(self):
        return self.audio_input_device

    def load_model_selection(self):
        return "local_whisper"

    def save_model_selection(self, model_value):
        self.saved_model_selection = model_value

    def load_hotkey_settings(self):
        return {"record_toggle": "f1", "cancel": "f2", "enable_disable": "f3"}

    def save_hotkey_settings(self, hotkeys):
        self.saved_hotkeys = hotkeys

    def load_all_settings(self):
        return dict(self.all_settings)


class FakeRecorder:
    def __init__(self, device_id=None):
        self.device_id = device_id
        self.is_recording = False
        self.audio_level_callback = None
        self.streaming_callback = None
        self.cleaned_up = False

    def set_audio_level_callback(self, callback):
        self.audio_level_callback = callback

    def set_streaming_callback(self, callback):
        self.streaming_callback = callback

    def start_recording(self):
        self.is_recording = True
        return True

    def stop_recording(self):
        self.is_recording = False
        return True

    def wait_for_stop_completion(self):
        return True

    def has_recording_data(self):
        return True

    def save_recording(self):
        Path(config.RECORDED_AUDIO_FILE).write_bytes(b"x" * 256)
        return True

    def get_recording_duration(self):
        return 12.5

    def clear_recording_data(self):
        pass

    def cleanup(self):
        self.cleaned_up = True


class FakeHotkeyManager:
    def __init__(self, hotkeys):
        self.hotkeys = hotkeys
        self.callbacks = {}
        self.rehook_called = False
        self.cleaned_up = False

    def set_callbacks(self, **callbacks):
        self.callbacks = callbacks

    def update_hotkeys(self, hotkeys):
        self.hotkeys = hotkeys

    def rehook(self):
        self.rehook_called = True

    def cleanup(self):
        self.cleaned_up = True


class FakeLocalBackend:
    requires_file_splitting = False

    def __init__(self, model_name=None):
        self.model_name = model_name or "base"
        self.device_info = "cpu"
        self.is_transcribing = False
        self.cleaned_up = False

    def transcribe(self, audio_path):
        return f"local:{audio_path}"

    def transcribe_chunks(self, chunk_files):
        return " ".join(chunk_files)

    def cancel_transcription(self):
        self.is_transcribing = False

    def reload_model(self):
        self.device_info = "cpu-reloaded"

    def cleanup(self):
        self.cleaned_up = True


class FakeOpenAIBackend:
    requires_file_splitting = True

    def __init__(self, model_type):
        self.model_type = model_type
        self.is_transcribing = False
        self.cleaned_up = False

    def transcribe(self, audio_path):
        return f"api:{audio_path}"

    def transcribe_chunks(self, chunk_files):
        return "api chunks"

    def cancel_transcription(self):
        self.is_transcribing = False

    def cleanup(self):
        self.cleaned_up = True


class FakeStreamingTranscriber:
    def __init__(self, backend, chunk_duration_sec):
        self.backend = backend
        self.chunk_duration_sec = chunk_duration_sec
        self.cleaned_up = False
        self.started = False

    def feed_audio(self, _audio):
        pass

    def start_streaming(self, sample_rate, callback):
        self.started = True
        self.sample_rate = sample_rate
        self.callback = callback

    def stop_streaming(self):
        return "partial text"

    def cleanup(self):
        self.cleaned_up = True


class FakeExecutor:
    def __init__(self):
        self.submissions = []
        self.shutdown_called = False

    def submit(self, fn, *args):
        self.submissions.append((fn, args))
        return types.SimpleNamespace()

    def shutdown(self, wait=True, cancel_futures=False):
        self.shutdown_called = True


class FakeHistoryManager:
    def __init__(self):
        self.entries = []

    def add_entry(self, **kwargs):
        self.entries.append(kwargs)
        return kwargs


class FakeAudioProcessor:
    def __init__(self):
        self.check_result = (False, 1.0)

    def check_file_size(self, _audio_path):
        return self.check_result

    def split_audio_file(self, audio_path, _callback):
        return [audio_path + ".part1", audio_path + ".part2"]

    def combine_transcriptions(self, transcriptions):
        return " ".join(transcriptions)

    def cleanup_temp_files(self):
        pass


class FakeKeyboard:
    def __init__(self):
        self.sent = []

    def send(self, keys):
        self.sent.append(keys)


class FakePyperclip:
    def __init__(self):
        self.copied = []

    def copy(self, text):
        self.copied.append(text)


class DummyOverlay:
    STATE_STT_ENABLE = "stt_on"
    STATE_STT_DISABLE = "stt_off"
    STATE_LARGE_FILE_SPLITTING = "splitting"
    STATE_LARGE_FILE_PROCESSING = "processing"

    def __init__(self):
        self.large_file_info = None
        self.shown_states = []

    def set_large_file_info(self, file_size_mb):
        self.large_file_info = file_size_mb

    def show_at_cursor(self, state):
        self.shown_states.append(state)


class DummyTabbedContent:
    def set_recording_state(self, _is_recording, _tab_index):
        pass


class DummyMainWindow:
    def __init__(self):
        self.is_recording = False
        self.partial_updates = []
        self.tabbed_content = DummyTabbedContent()

    def _update_recording_state(self):
        pass

    def set_partial_transcription(self, text, is_final):
        self.partial_updates.append((text, is_final))

    def clear_partial_transcription(self):
        pass


class DummyUIController:
    def __init__(self):
        self.main_window = DummyMainWindow()
        self.overlay = DummyOverlay()
        self.is_recording = False
        self.statuses = []
        self.device_infos = []
        self.hotkeys = None
        self.refreshed_history = False
        self.transcription_text = None
        self.stats = None
        self.cleaned_up = False
        self.streaming_overlay_shown = 0
        self.streaming_overlay_hidden = 0
        self.caret_shown = 0
        self.caret_hidden = 0

    def update_hotkey_display(self, hotkeys):
        self.hotkeys = hotkeys

    def set_status(self, status):
        self.statuses.append(status)

    def set_device_info(self, device_info):
        self.device_infos.append(device_info)

    def update_audio_levels(self, _levels):
        pass

    def update_streaming_text(self, _text, _is_final):
        pass

    def show_streaming_overlay(self):
        self.streaming_overlay_shown += 1

    def hide_streaming_overlay(self):
        self.streaming_overlay_hidden += 1

    def show_caret_paste_indicator(self):
        self.caret_shown += 1

    def hide_caret_paste_indicator(self):
        self.caret_hidden += 1

    def clear_transcription_stats(self):
        self.stats = None

    def set_transcript(self, text):
        self.transcription_text = text

    def set_transcription_stats(self, transcription_time, audio_duration, file_size):
        self.stats = (transcription_time, audio_duration, file_size)

    def refresh_history(self):
        self.refreshed_history = True

    def hide_overlay(self):
        pass

    def cleanup(self):
        self.cleaned_up = True


def _install_module_stubs(settings_manager, history_manager, audio_processor, keyboard, pyperclip, db_state):
    qtcore_module = types.ModuleType("PyQt6.QtCore")
    qtcore_module.QObject = _QObject
    qtcore_module.QTimer = _QTimer
    qtcore_module.Qt = _Qt
    qtcore_module.pyqtSignal = _pyqt_signal

    pyqt_module = types.ModuleType("PyQt6")

    transcriber_module = types.ModuleType("transcriber")
    transcriber_module.TranscriptionBackend = object
    transcriber_module.LocalWhisperBackend = FakeLocalBackend
    transcriber_module.OpenAIBackend = FakeOpenAIBackend

    recorder_module = types.ModuleType("services.recorder")
    recorder_module.AudioRecorder = FakeRecorder

    hotkey_module = types.ModuleType("services.hotkey_manager")
    hotkey_module.HotkeyManager = FakeHotkeyManager

    settings_module = types.ModuleType("services.settings")
    settings_module.settings_manager = settings_manager

    history_module = types.ModuleType("services.history_manager")
    history_module.history_manager = history_manager

    audio_processor_module = types.ModuleType("services.audio_processor")
    audio_processor_module.audio_processor = audio_processor

    streaming_module = types.ModuleType("services.streaming_transcriber")
    streaming_module.StreamingTranscriber = FakeStreamingTranscriber

    database_module = types.ModuleType("services.database")
    database_module.db = types.SimpleNamespace(
        close=lambda: db_state.__setitem__("closed", True)
    )

    keyboard_module = types.ModuleType("keyboard")
    keyboard_module.send = keyboard.send

    pyperclip_module = types.ModuleType("pyperclip")
    pyperclip_module.copy = pyperclip.copy

    return {
        "PyQt6": pyqt_module,
        "PyQt6.QtCore": qtcore_module,
        "transcriber": transcriber_module,
        "services.recorder": recorder_module,
        "services.hotkey_manager": hotkey_module,
        "services.settings": settings_module,
        "services.history_manager": history_module,
        "services.audio_processor": audio_processor_module,
        "services.streaming_transcriber": streaming_module,
        "services.database": database_module,
        "keyboard": keyboard_module,
        "pyperclip": pyperclip_module,
    }


class TestApplicationController(unittest.TestCase):
    def setUp(self):
        self.settings = FakeSettingsManager()
        self.history_manager = FakeHistoryManager()
        self.audio_processor = FakeAudioProcessor()
        self.keyboard = FakeKeyboard()
        self.pyperclip = FakePyperclip()
        self.db_state = {"closed": False}

        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_recorded_audio_file = config.RECORDED_AUDIO_FILE
        config.RECORDED_AUDIO_FILE = str(Path(self.temp_dir.name) / "recorded_audio.wav")

        module_stubs = _install_module_stubs(
            self.settings,
            self.history_manager,
            self.audio_processor,
            self.keyboard,
            self.pyperclip,
            self.db_state,
        )
        self.module_patcher = patch.dict(sys.modules, module_stubs)
        self.module_patcher.start()

        for module_name in [
            "services.runtime",
            "services.runtime.hotkeys",
            "services.runtime.streaming",
            "services.runtime.transcription",
            "services.application_controller",
        ]:
            sys.modules.pop(module_name, None)

        self.app_controller_module = importlib.import_module("services.application_controller")
        self.hotkeys_runtime_module = importlib.import_module("services.runtime.hotkeys")
        self.watchdog_patcher = patch.object(
            self.hotkeys_runtime_module.HotkeyRuntime,
            "setup_hook_watchdog",
            lambda _self: None,
        )
        self.watchdog_patcher.start()

    def tearDown(self):
        self.watchdog_patcher.stop()
        self.module_patcher.stop()
        config.RECORDED_AUDIO_FILE = self.original_recorded_audio_file
        self.temp_dir.cleanup()

    def _create_controller(self):
        controller = self.app_controller_module.ApplicationController(DummyUIController())
        controller.executor.shutdown(wait=False)
        controller.executor = FakeExecutor()
        return controller

    def test_model_switch_updates_backend_and_device_info(self):
        controller = self._create_controller()

        controller.on_model_changed("API: GPT-4o Transcribe")
        self.assertEqual(controller._current_model_name, "api_gpt4o")
        self.assertEqual(self.settings.saved_model_selection, "api_gpt4o")
        self.assertEqual(controller.ui_controller.device_infos[-1], "")

        controller.on_model_changed("Local Whisper")
        self.assertEqual(controller._current_model_name, "local_whisper")
        self.assertEqual(controller.ui_controller.device_infos[-1], "cpu")

    def test_streaming_reconfigure_can_disable_runtime(self):
        controller = self._create_controller()
        self.assertIsNotNone(controller.streaming_transcriber)
        self.assertTrue(controller._streaming_enabled)

        self.settings.all_settings["streaming_enabled"] = False
        controller.reconfigure_streaming()

        self.assertIsNone(controller.streaming_transcriber)
        self.assertFalse(controller._streaming_enabled)
        self.assertIn("Streaming mode disabled", controller.ui_controller.statuses)

    def test_stop_recording_chooses_normal_or_split_transcription_path(self):
        controller = self._create_controller()

        controller.recorder.is_recording = True
        self.audio_processor.check_result = (False, 1.0)
        controller.stop_recording()
        self.assertEqual(len(controller.executor.submissions), 1)
        self.assertEqual(
            controller.executor.submissions[0][0].__name__, "transcribe_audio_file"
        )

        controller.executor = FakeExecutor()
        controller.current_backend = controller.transcription_backends["api_gpt4o"]
        controller.recorder.is_recording = True
        self.audio_processor.check_result = (True, 30.0)
        controller.stop_recording()
        self.assertEqual(len(controller.executor.submissions), 1)
        self.assertEqual(
            controller.executor.submissions[0][0].__name__,
            "transcribe_large_audio_file",
        )
        self.assertEqual(controller.ui_controller.overlay.large_file_info, 30.0)
        self.assertIn(
            controller.ui_controller.overlay.STATE_LARGE_FILE_SPLITTING,
            controller.ui_controller.overlay.shown_states,
        )

    def test_transcription_complete_saves_history_and_resets_pending_state(self):
        controller = self._create_controller()
        controller._pending_audio_path = "source.wav"
        controller._pending_audio_duration = 9.5
        controller._pending_file_size = 2048
        controller._transcription_start_time = time.time() - 1.0

        controller._on_transcription_complete("hello world")

        self.assertEqual(len(self.history_manager.entries), 1)
        entry = self.history_manager.entries[0]
        self.assertEqual(entry["text"], "hello world")
        self.assertEqual(entry["source_audio_path"], "source.wav")
        self.assertEqual(entry["audio_duration"], 9.5)
        self.assertEqual(entry["file_size"], 2048)
        self.assertTrue(controller.ui_controller.refreshed_history)
        self.assertEqual(self.pyperclip.copied[-1], "hello world")
        self.assertIsNone(controller._pending_audio_path)
        self.assertIsNone(controller._pending_audio_duration)
        self.assertIsNone(controller._pending_file_size)

    def test_cleanup_is_safe_with_partial_state(self):
        controller = self._create_controller()
        controller.hotkey_manager = None
        controller.streaming_transcriber = FakeStreamingTranscriber(
            backend=FakeLocalBackend(),
            chunk_duration_sec=2.0,
        )
        controller._streaming_backend = FakeLocalBackend(model_name="tiny.en")

        controller.cleanup()

        self.assertTrue(controller.executor.shutdown_called)
        self.assertTrue(controller.ui_controller.cleaned_up)
        self.assertTrue(self.db_state["closed"])


if __name__ == "__main__":
    unittest.main()

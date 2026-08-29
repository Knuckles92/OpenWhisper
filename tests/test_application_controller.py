"""Controller-level tests for the extracted application controller."""

import pytest
import importlib
import sys
import tempfile
import time
import types
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
        self.single_shot = False

    def setTimerType(self, _timer_type):
        pass

    def setSingleShot(self, single_shot):
        self.single_shot = single_shot

    def start(self, _interval):
        pass

    def stop(self):
        pass

    @staticmethod
    def singleShot(_interval, callback):
        callback()


class _Qt:
    class TimerType:
        CoarseTimer = 1
        VeryCoarseTimer = 2


class FakeSettingsManager:
    def __init__(self):
        self.all_settings = {
            "streaming_enabled": True,
            "streaming_chunk_duration": 4.0,
            "copy_clipboard": True,
            "auto_paste": False,
            "transcript_cleanup_enabled": False,
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

    def get(self, key, default=None):
        return self.all_settings.get(key, default)

    def save_setting(self, key, value):
        self.all_settings[key] = value

    def load_all_settings(self):
        return dict(self.all_settings)


class FakeRecorder:
    def __init__(self, device_id=None):
        self.device_id = device_id
        self.is_recording = False
        self.audio_level_callback = None
        self.streaming_callback = None
        self.cleaned_up = False
        self.last_start_error = None
        self.start_should_fail = False

    def set_audio_level_callback(self, callback):
        self.audio_level_callback = callback

    def set_streaming_callback(self, callback):
        self.streaming_callback = callback

    def start_recording(self):
        if self.start_should_fail:
            self.is_recording = False
            self.last_start_error = self.last_start_error or "No audio device available"
            return False
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
        self.record_mode = None

    def set_callbacks(self, **callbacks):
        self.callbacks = callbacks

    def set_record_mode(self, mode):
        self.record_mode = mode

    def update_hotkeys(self, hotkeys):
        self.hotkeys = hotkeys

    def rehook(self):
        self.rehook_called = True

    def cleanup(self):
        self.cleaned_up = True


class FakeLocalBackend:
    requires_file_splitting = False

    def __init__(self, model_name=None, load=True, **_kwargs):
        self.model_name = model_name or "base"
        self.device_info = "cpu"
        self.device = "cpu"
        self.is_transcribing = False
        self.cleaned_up = False
        self.is_model_missing = False
        self.last_loaded_model = self.model_name
        self.gpu_fallback_note = None
        self.gpu_fallback_cause = None
        self.load_deferred = not load
        self.model = None if not load else object()

    def is_available(self):
        if self.load_deferred:
            return False
        return not self.is_model_missing

    def transcribe(self, audio_path):
        return f"local:{audio_path}"

    def transcribe_chunks(self, chunk_files):
        return " ".join(chunk_files)

    def cancel_transcription(self):
        self.is_transcribing = False

    def reload_model(self, model_name=None):
        if model_name:
            self.model_name = model_name
        self.device_info = "cpu-reloaded"
        # Mirrors a successful cache-only load, which resets fallback state
        self.is_model_missing = False
        self.load_deferred = False
        self.model = object()
        self.last_loaded_model = self.model_name
        self.gpu_fallback_note = None
        self.gpu_fallback_cause = None

    def cleanup(self):
        self.cleaned_up = True


class FakeOpenAIBackend:
    requires_file_splitting = True

    def __init__(self, model_type):
        self.model_type = model_type
        self.is_transcribing = False
        self.cleaned_up = False

    def is_available(self):
        return True

    def transcribe(self, audio_path):
        return f"api:{audio_path}"

    def transcribe_chunks(self, chunk_files):
        return "api chunks"

    def cancel_transcription(self):
        self.is_transcribing = False

    def cleanup(self):
        self.cleaned_up = True


class FakeStreamingTranscriber:
    def __init__(self, backend, chunk_duration_sec, overlap_sec=0.75):
        self.backend = backend
        self.chunk_duration_sec = chunk_duration_sec
        self.overlap_sec = overlap_sec
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
        self.written = []

    def send(self, keys):
        self.sent.append(keys)

    def write(self, text):
        self.written.append(text)


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
        self.tray_visibility_toggles = 0
        self.recording_state_updates = []

    def _update_recording_state(self):
        self.recording_state_updates.append(self.is_recording)

    def set_partial_transcription(self, text, is_final):
        self.partial_updates.append((text, is_final))

    def clear_partial_transcription(self):
        pass

    def minimize_to_tray(self):
        self.minimized_to_tray = True

    def toggle_tray_visibility(self):
        self.tray_visibility_toggles += 1

    def refresh_past_meetings(self):
        pass


class DummyUIController:
    def __init__(self):
        self.main_window = DummyMainWindow()
        self.overlay = DummyOverlay()
        self.is_recording = False
        self.statuses = []
        self.device_infos = []
        self.device_ready_states = []
        self.engine_busy_states = []
        self.hotkeys = None
        self.refreshed_history = False
        self.transcription_text = None
        self.transcription_raw = None
        self.stats = None
        self.cleaned_up = False
        self.copied = []
        self.copy_succeeds = True
        self.streaming_overlay_shown = 0
        self.streaming_overlay_hidden = 0
        self.caret_shown = 0
        self.caret_hidden = 0
        self.consent_requests = []
        self.consent_result = "cancel"
        self.engine_controls_refreshes = 0
        self.settings_dialog_opened_with = None
        self.model_manager_refreshes = 0
        self.download_started = []
        self.download_progress = []
        self.download_finished = []
        self.batch_planned = []
        self.batch_finished = []
        self.deleted_models = []
        self.component_progress_events = []
        self.component_state_changes = 0
        self.component_install_results = []
        self.meeting_states = []
        self.meeting_statuses = []
        self.meeting_errors = []
        self.meeting_servers = []
        self.meeting_guest_links = []
        self.meeting_consent_requests = 0
        self.meeting_consent_result = False
        self.platform_ack_requests = 0
        self.platform_ack_result = True
        self.meeting_recovery_requests = []
        self.update_checks = []
        self.update_download_progress_events = []
        self.update_download_results = []
        self.on_record_start = None

    def start_recording(self):
        """Mirror UIController.start_recording, including refusal rollback."""
        self.is_recording = True
        if not self.main_window.is_recording:
            self.main_window.is_recording = True
            self.main_window._update_recording_state()
        if self.on_record_start and self.on_record_start() is False:
            self.is_recording = False
            self.main_window.is_recording = False
            self.main_window._update_recording_state()
            return False
        return True

    def show_hf_consent_dialog(self, model_name, policy, env_blocked=False):
        self.consent_requests.append((model_name, policy, env_blocked))
        return self.consent_result

    def on_meeting_state_changed(self, payload):
        self.meeting_states.append(payload)

    def set_meeting_status(self, status):
        self.meeting_statuses.append(status)

    def on_meeting_error(self, message):
        self.meeting_errors.append(message)

    def on_meeting_server_started(self, payload):
        self.meeting_servers.append(payload)

    def copy_meeting_guest_link(self, url):
        self.meeting_guest_links.append(url)

    def show_meeting_consent_dialog(self):
        self.meeting_consent_requests += 1
        return self.meeting_consent_result

    def ensure_meeting_platform_ack(self):
        self.platform_ack_requests += 1
        return self.platform_ack_result

    def show_meeting_recovery_dialog(self, meetings, on_finalize, on_discard):
        self.meeting_recovery_requests.append(meetings)

    def on_update_check_finished(self, result, error, manual):
        self.update_checks.append((result, error, manual))

    def on_update_download_progress(self, phase, done, total):
        self.update_download_progress_events.append((phase, done, total))

    def on_update_download_finished(self, path, error):
        self.update_download_results.append((path, error))

    def refresh_local_engine_controls(self):
        self.engine_controls_refreshes += 1

    def refresh_model_manager(self):
        self.model_manager_refreshes += 1

    def on_component_progress(self, component_id, phase, done, total):
        self.component_progress_events.append((component_id, phase, done, total))

    def on_component_state_changed(self):
        self.component_state_changes += 1

    def on_component_install_finished(self, component_id, success, message):
        self.component_install_results.append((component_id, success, message))

    def on_model_download_started(self, model_name):
        self.download_started.append(model_name)

    def on_model_download_progress(self, model_name, done, total):
        self.download_progress.append((model_name, done, total))

    def on_model_download_finished(self, model_name, success):
        self.download_finished.append((model_name, success))

    def on_model_batch_planned(self, model_names):
        self.batch_planned = list(model_names)

    def on_model_batch_finished(self, completed, planned):
        self.batch_finished.append((completed, planned))

    def on_model_deleted(self, model_name, success, error):
        self.deleted_models.append((model_name, success, error))

    def open_settings_dialog(self, focus_hf_policy=False):
        self.settings_dialog_opened_with = focus_hf_policy

    def update_hotkey_display(self, hotkeys):
        self.hotkeys = hotkeys

    def set_status(self, status):
        self.statuses.append(status)

    def set_device_info(self, device_info, ready=None):
        self.device_infos.append(device_info)
        self.device_ready_states.append(ready)

    def set_engine_busy(self, busy):
        self.engine_busy_states.append(busy)
        if not busy:
            self.refresh_model_manager()

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

    def set_transcript(self, text, raw=None):
        self.transcription_text = text
        self.transcription_raw = raw

    def copy_to_clipboard(self, text):
        if not self.copy_succeeds:
            return False
        self.copied.append(text)
        return True

    def set_transcription_stats(self, transcription_time, audio_duration, file_size):
        self.stats = (transcription_time, audio_duration, file_size)

    def refresh_history(self):
        self.refreshed_history = True

    def hide_overlay(self):
        pass

    def cleanup(self):
        self.cleaned_up = True


def _install_module_stubs(settings_manager, history_manager, audio_processor, keyboard, db_state):
    qtcore_module = types.ModuleType("PyQt6.QtCore")
    qtcore_module.QObject = _QObject
    qtcore_module.QTimer = _QTimer
    qtcore_module.Qt = _Qt
    qtcore_module.pyqtSignal = _pyqt_signal

    pyqt_module = types.ModuleType("PyQt6")

    # Constants holder with no behavior, safe to expose for real (imported
    # before patch.dict swaps the transcriber package for the stub).
    from transcriber import GpuFallbackCause as _RealGpuFallbackCause

    transcriber_module = types.ModuleType("transcriber")
    transcriber_module.TranscriptionBackend = object
    transcriber_module.GpuFallbackCause = _RealGpuFallbackCause
    transcriber_module.LocalWhisperBackend = FakeLocalBackend
    transcriber_module.OpenAIBackend = FakeOpenAIBackend

    recorder_module = types.ModuleType("services.recorder")
    recorder_module.AudioRecorder = FakeRecorder

    hotkey_module = types.ModuleType("services.hotkey_manager")
    hotkey_module.HotkeyManager = FakeHotkeyManager
    hotkey_module.send_paste = lambda: keyboard.send("ctrl+v")
    hotkey_module.is_accessibility_trusted = lambda: True
    # Keep the Qt focus-window hotkey fallback out of the headless test path.
    hotkey_module.USE_PYNPUT_BACKEND = False

    # SettingsKey / HuggingFaceAccessPolicy are constants holders with no
    # behavior, so the real ones are safe (and more faithful) to expose on the
    # stub than hand-rolled copies.
    from services.settings import (
        HuggingFaceAccessPolicy as _RealHFPolicy,
        RecordingTriggerMode as _RealRecordingTriggerMode,
        SettingsKey as _RealSettingsKey,
        TranscriptCleanupProvider as _RealCleanupProvider,
        TranscriptCleanupReasoning as _RealCleanupReasoning,
        default_transcript_cleanup_model as _default_cleanup_model,
        resolve_recording_trigger_mode as _resolve_recording_trigger_mode,
        resolve_transcript_cleanup_model as _resolve_cleanup_model,
        resolve_transcript_cleanup_prompt as _resolve_cleanup_prompt,
        resolve_transcript_cleanup_provider as _resolve_cleanup_provider,
        resolve_transcript_cleanup_reasoning as _resolve_cleanup_reasoning,
        resolve_update_check_enabled as _resolve_update_check_enabled,
        resolve_update_notify_enabled as _resolve_update_notify_enabled,
        resolve_update_skipped_version as _resolve_update_skipped_version,
    )

    settings_module = types.ModuleType("services.settings")
    settings_module.settings_manager = settings_manager
    settings_module.SettingsKey = _RealSettingsKey
    settings_module.HuggingFaceAccessPolicy = _RealHFPolicy
    settings_module.RecordingTriggerMode = _RealRecordingTriggerMode
    settings_module.TranscriptCleanupProvider = _RealCleanupProvider
    settings_module.TranscriptCleanupReasoning = _RealCleanupReasoning
    settings_module.default_transcript_cleanup_model = _default_cleanup_model

    def _with_fake_settings(resolver):
        return lambda settings=None: resolver(
            settings if settings is not None else settings_manager.load_all_settings()
        )

    settings_module.resolve_recording_trigger_mode = _with_fake_settings(
        _resolve_recording_trigger_mode
    )
    settings_module.resolve_transcript_cleanup_prompt = _with_fake_settings(
        _resolve_cleanup_prompt
    )
    settings_module.resolve_transcript_cleanup_provider = _with_fake_settings(
        _resolve_cleanup_provider
    )
    settings_module.resolve_transcript_cleanup_model = _with_fake_settings(
        _resolve_cleanup_model
    )
    settings_module.resolve_transcript_cleanup_reasoning = _with_fake_settings(
        _resolve_cleanup_reasoning
    )
    settings_module.resolve_update_check_enabled = _with_fake_settings(
        _resolve_update_check_enabled
    )
    settings_module.resolve_update_notify_enabled = _with_fake_settings(
        _resolve_update_notify_enabled
    )
    settings_module.resolve_update_skipped_version = _with_fake_settings(
        _resolve_update_skipped_version
    )

    from services.hf_access import (
        AccessDecision as _RealAccessDecision,
        ConsentAction as _RealConsentAction,
    )

    hf_access_module = types.ModuleType("services.hf_access")
    hf_access_module.AccessDecision = _RealAccessDecision
    hf_access_module.ConsentAction = _RealConsentAction
    hf_access_module.resolve_model_repo = lambda name: name
    hf_access_module.download_model_files = (
        lambda name, progress_callback=None: f"/cache/{name}"
    )
    hf_access_module.delete_model_from_cache = lambda name: None
    hf_access_module.is_hf_hub_offline_env_set = lambda: False
    hf_access_module.is_model_cached = lambda name: True
    # Inert coordinator: never grants downloads, never touches disk/network.
    hf_access_module.hf_access_coordinator = types.SimpleNamespace(
        begin_request=lambda model: True,
        end_request=lambda model: None,
        evaluate_access=lambda model, consume_grant=True: (
            _RealAccessDecision.NEEDS_CONSENT
        ),
        grant_once=lambda model: None,
        get_policy=lambda: _RealHFPolicy.ASK,
        set_policy=lambda policy: None,
        claim_batch=lambda models: [],
    )

    history_module = types.ModuleType("services.history_manager")
    history_module.history_manager = history_manager

    audio_processor_module = types.ModuleType("services.audio_processor")
    audio_processor_module.audio_processor = audio_processor

    streaming_module = types.ModuleType("services.streaming_transcriber")
    streaming_module.StreamingTranscriber = FakeStreamingTranscriber
    from services.streaming_transcriber import (
        append_preview_text as _append_preview_text,
    )
    streaming_module.append_preview_text = _append_preview_text

    database_module = types.ModuleType("services.database")
    database_module.db = types.SimpleNamespace(
        close=lambda: db_state.__setitem__("closed", True),
        ensure_initialized=lambda: None,
    )

    keyboard_module = types.ModuleType("keyboard")
    keyboard_module.send = keyboard.send
    keyboard_module.write = keyboard.write

    return {
        "PyQt6": pyqt_module,
        "PyQt6.QtCore": qtcore_module,
        "transcriber": transcriber_module,
        "services.recorder": recorder_module,
        "services.hotkey_manager": hotkey_module,
        "services.settings": settings_module,
        "services.hf_access": hf_access_module,
        "services.history_manager": history_module,
        "services.audio_processor": audio_processor_module,
        "services.streaming_transcriber": streaming_module,
        "services.database": database_module,
        "keyboard": keyboard_module,
    }


class TestApplicationController:
    @pytest.fixture(autouse=True)
    def _setup(self):
        self.settings = FakeSettingsManager()
        self.history_manager = FakeHistoryManager()
        self.audio_processor = FakeAudioProcessor()
        self.keyboard = FakeKeyboard()
        self.db_state = {"closed": False}

        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_recorded_audio_file = config.RECORDED_AUDIO_FILE
        config.RECORDED_AUDIO_FILE = str(Path(self.temp_dir.name) / "recorded_audio.wav")

        module_stubs = _install_module_stubs(
            self.settings,
            self.history_manager,
            self.audio_processor,
            self.keyboard,
            self.db_state,
        )
        self.module_patcher = patch.dict(sys.modules, module_stubs)
        self.module_patcher.start()

        for module_name in [
            "services.runtime",
            "services.runtime.hotkeys",
            "services.runtime.streaming",
            "services.runtime.transcription",
            # Without this, a collection-time import by another test file (e.g.
            # test_meeting_runtime) leaves MeetingRuntime bound to the REAL
            # settings_manager, so the developer's own consent grant leaks in
            # and consent-dependent tests flip depending on the local machine.
            "services.runtime.meeting",
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

    @pytest.fixture(autouse=True)
    def _teardown(self):
        yield
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
        assert controller._current_model_name == "api_gpt4o"
        assert self.settings.saved_model_selection == "api_gpt4o"
        assert controller.ui_controller.device_infos[-1] == ""

        controller.on_model_changed("Local Whisper")
        assert controller._current_model_name == "local_whisper"
        assert controller.ui_controller.device_infos[-1] == "cpu"

    def test_reload_whisper_model_runs_in_background_and_reports(self):
        controller = self._create_controller()

        # Scheduling only arms the debounce timer; nothing runs yet.
        controller.reload_whisper_model()
        assert len(controller.executor.submissions) == 0

        # Debounce fires -> work is submitted to the executor, combos go busy.
        controller._reload_timer.timeout.emit()
        assert controller._reload_in_flight
        assert controller.ui_controller.engine_busy_states[-1] == True
        assert len(controller.executor.submissions) == 1

        # Run the worker exactly as the real executor would.
        fn, args = controller.executor.submissions[0]
        fn(*args)

        assert not controller._reload_in_flight
        assert controller.ui_controller.device_infos[-1] == "cpu-reloaded"
        assert "Whisper engine ready" in controller.ui_controller.statuses
        assert controller.ui_controller.engine_busy_states[-1] == False
        # Idle after reload refreshes the manager so Delete tracks the new model.
        assert controller.ui_controller.model_manager_refreshes >= 1

    def test_declined_download_reverts_model_selection(self):
        controller = self._create_controller()
        backend = controller.transcription_backends["local_whisper"]
        backend.model_name = "small"
        backend.is_model_missing = True
        backend.last_loaded_model = "turbo"
        self.settings.all_settings["whisper_model"] = "small"
        controller.ui_controller.consent_result = "cancel"

        controller._on_hf_consent_requested("small", False, True)

        # Selection and inline combos roll back to the model that is cached
        assert self.settings.all_settings["whisper_model"] == "turbo"
        assert controller.ui_controller.engine_controls_refreshes == 1
        assert "Model 'small' is unavailable — download declined" in controller.ui_controller.statuses

        # The scheduled background reload brings the reverted model back
        controller._reload_timer.timeout.emit()
        fn, args = controller.executor.submissions[-1]
        fn(*args)
        assert backend.is_available()
        assert "Whisper engine ready" in controller.ui_controller.statuses

    def test_declined_download_without_prior_model_keeps_selection(self):
        controller = self._create_controller()
        backend = controller.transcription_backends["local_whisper"]
        backend.model_name = "small"
        backend.is_model_missing = True
        backend.last_loaded_model = None  # fresh install: nothing ever loaded
        self.settings.all_settings["whisper_model"] = "small"
        controller.ui_controller.consent_result = "cancel"

        controller._on_hf_consent_requested("small", False, True)

        assert self.settings.all_settings["whisper_model"] == "small"
        assert controller.ui_controller.engine_controls_refreshes == 0

    def test_reload_whisper_model_refused_while_recording(self):
        controller = self._create_controller()
        controller.recorder.is_recording = True

        controller.reload_whisper_model()

        assert len(controller.executor.submissions) == 0
        assert "Finish recording before changing the engine" in controller.ui_controller.statuses

    def test_hotkeys_backfill_minimize_tray_and_refresh_display_on_update(self):
        controller = self._create_controller()

        assert controller.hotkey_manager.hotkeys["minimize_tray"] == config.DEFAULT_HOTKEYS["minimize_tray"]
        assert controller.ui_controller.hotkeys["minimize_tray"] == config.DEFAULT_HOTKEYS["minimize_tray"]

        updated_hotkeys = {
            "record_toggle": "f4",
            "cancel": "f5",
            "enable_disable": "f6",
            "minimize_tray": "ctrl+alt+h",
        }
        controller.update_hotkeys(updated_hotkeys)

        assert controller.hotkey_manager.hotkeys == updated_hotkeys
        assert self.settings.saved_hotkeys == updated_hotkeys
        assert controller.ui_controller.hotkeys == updated_hotkeys
        assert "Hotkeys updated" in controller.ui_controller.statuses

    def test_minimize_hotkey_toggles_tray_visibility_on_main_thread(self):
        controller = self._create_controller()

        controller.minimize_to_tray()

        assert controller.ui_controller.main_window.tray_visibility_toggles == 1

    def test_streaming_reconfigure_can_disable_runtime(self):
        controller = self._create_controller()
        controller.notify_main_ui_ready()
        assert controller.streaming_transcriber is not None
        assert controller._streaming_enabled
        assert controller._streaming_backend is not None
        assert controller._streaming_backend.model_name == "tiny.en"

        self.settings.all_settings["streaming_enabled"] = False
        controller.reconfigure_streaming()

        assert controller.streaming_transcriber is None
        assert not controller._streaming_enabled
        assert "Streaming mode disabled" not in controller.ui_controller.statuses

    def test_streaming_defers_when_tiny_en_is_missing(self):
        sys.modules["services.hf_access"].is_model_cached = lambda name: False
        controller = self._create_controller()
        controller.notify_main_ui_ready()

        assert controller.streaming_transcriber is None
        assert not controller._streaming_enabled
        assert controller.ui_controller.consent_requests[-1][0] == "tiny.en"

    def test_stop_recording_chooses_normal_or_split_transcription_path(self):
        controller = self._create_controller()

        controller.recorder.is_recording = True
        self.audio_processor.check_result = (False, 1.0)
        controller.stop_recording()
        assert len(controller.executor.submissions) == 1
        assert controller.executor.submissions[0][0].__name__ == "transcribe_audio_file"

        controller.executor = FakeExecutor()
        controller.current_backend = controller.transcription_backends["api_gpt4o"]
        controller.recorder.is_recording = True
        self.audio_processor.check_result = (True, 30.0)
        controller.stop_recording()
        assert len(controller.executor.submissions) == 1
        assert controller.executor.submissions[0][0].__name__ == "transcribe_large_audio_file"
        assert controller.ui_controller.overlay.large_file_info == 30.0
        assert controller.ui_controller.overlay.STATE_LARGE_FILE_SPLITTING in controller.ui_controller.overlay.shown_states

    def test_transcription_complete_saves_history_and_resets_pending_state(self):
        controller = self._create_controller()
        controller._pending_audio_path = "source.wav"
        controller._pending_audio_duration = 9.5
        controller._pending_file_size = 2048
        controller._transcription_start_time = time.time() - 1.0

        controller._on_transcription_complete("hello world", None)

        assert len(self.history_manager.entries) == 1
        entry = self.history_manager.entries[0]
        assert entry["text"] == "hello world"
        assert entry.get("raw_text") is None
        assert entry["source_audio_path"] == "source.wav"
        assert entry["audio_duration"] == 9.5
        assert entry["file_size"] == 2048
        assert controller.ui_controller.refreshed_history
        assert controller.ui_controller.copied[-1] == "hello world"
        assert controller._pending_audio_path is None
        assert controller._pending_audio_duration is None
        assert controller._pending_file_size is None
        assert controller._pending_source_name is None

    def test_transcription_complete_stores_raw_and_fixed_text(self):
        controller = self._create_controller()
        controller._pending_audio_path = "source.wav"

        controller._on_transcription_complete("Fixed sentence.", "um fixed sentence")

        entry = self.history_manager.entries[0]
        assert entry["text"] == "Fixed sentence."
        assert entry["raw_text"] == "um fixed sentence"
        assert controller.ui_controller.transcription_text == "Fixed sentence."
        assert controller.ui_controller.transcription_raw == "um fixed sentence"
        assert controller.ui_controller.copied[-1] == "Fixed sentence."

    def test_start_recording_failure_stays_idle(self):
        controller = self._create_controller()
        controller.recorder.start_should_fail = True
        controller.recorder.last_start_error = "No audio device available"

        controller.start_recording()

        assert not controller.recorder.is_recording
        assert not controller.ui_controller.is_recording
        assert not controller.ui_controller.main_window.is_recording
        assert any(
            "Failed to start recording" in status
            for status in controller.ui_controller.statuses
        )
        assert any(
            "No audio device available" in status
            for status in controller.ui_controller.statuses
        )

    def test_empty_asr_skips_history_clipboard_and_paste(self):
        controller = self._create_controller()
        controller._pending_audio_path = "source.wav"
        controller._pending_audio_duration = 3.7
        controller._pending_file_size = 2048
        controller._pending_source_name = "sample.wav"
        controller._transcription_start_time = time.time() - 1.0
        self.settings.all_settings["auto_paste"] = True

        controller._on_transcription_complete("   ", None)

        assert self.history_manager.entries == []
        assert controller.ui_controller.copied == []
        assert self.keyboard.sent == []
        assert (
            controller.ui_controller.transcription_text
            == "No speech detected (empty after VAD)"
        )
        assert (
            controller.ui_controller.statuses[-1]
            == "No speech detected (empty after VAD)"
        )
        assert controller.ui_controller.stats is not None
        assert controller.ui_controller.stats[1] == 3.7
        assert controller._pending_audio_path is None
        assert controller._pending_source_name is None

    def test_copy_failure_does_not_claim_pasted(self):
        controller = self._create_controller()
        self.settings.all_settings["auto_paste"] = True
        controller.ui_controller.copy_succeeds = False

        controller._on_transcription_complete("hello world", None)

        assert controller.ui_controller.copied == []
        assert self.keyboard.sent == []
        assert (
            controller.ui_controller.statuses[-1]
            == "Transcription complete (copy failed)"
        )
        assert len(self.history_manager.entries) == 1

    def test_upload_audio_file_uses_preview_duration(self):
        controller = self._create_controller()
        clip_path = str(Path(self.temp_dir.name) / "upload.wav")
        Path(clip_path).write_bytes(b"x" * 256)

        controller.upload_audio_file(clip_path, duration_seconds=3.72)

        assert controller._pending_audio_duration == 3.72
        assert controller._pending_source_name == "upload.wav"
        assert len(controller.executor.submissions) == 1

    def test_transcribe_audio_file_applies_cleanup_when_enabled(self):
        controller = self._create_controller()
        self.settings.all_settings["transcript_cleanup_enabled"] = True
        cleanup = controller.transcription_runtime._transcript_cleanup
        cleanup.is_available = lambda: True
        cleanup.cleanup = lambda text, system_prompt=None: "Cleaned text."
        clip_path = str(Path(self.temp_dir.name) / "clip.wav")
        Path(clip_path).write_bytes(b"RIFF")

        class _Backend:
            def transcribe(self, _path):
                return "um cleaned text"

        controller.current_backend = _Backend()
        controller.transcription_runtime.transcribe_audio_file(clip_path)

        entry = self.history_manager.entries[0]
        assert entry["text"] == "Cleaned text."
        assert entry["raw_text"] == "um cleaned text"
        assert entry["cleanup_provider"] == "openrouter"
        assert entry["cleanup_model"]

    def test_transcribe_audio_file_skips_cleanup_when_disabled(self):
        controller = self._create_controller()
        self.settings.all_settings["transcript_cleanup_enabled"] = False
        cleanup = controller.transcription_runtime._transcript_cleanup
        cleanup.is_available = lambda: True
        cleanup.cleanup = lambda text, system_prompt=None: "should not run"
        clip_path = str(Path(self.temp_dir.name) / "clip.wav")
        Path(clip_path).write_bytes(b"RIFF")

        class _Backend:
            def transcribe(self, _path):
                return "raw only"

        controller.current_backend = _Backend()
        controller.transcription_runtime.transcribe_audio_file(clip_path)

        entry = self.history_manager.entries[0]
        assert entry["text"] == "raw only"
        assert entry.get("raw_text") is None
        assert entry.get("cleanup_provider") is None
        assert entry.get("cleanup_model") is None

    def test_transcribe_clip_delegates_to_current_backend(self):
        controller = self._create_controller()

        class _Backend:
            def transcribe(self, path):
                return f"clip transcript for {path}"

        controller.current_backend = _Backend()
        assert controller.transcribe_clip("dictation.wav") == "clip transcript for dictation.wav"

    def test_transcribe_clip_raises_without_backend_or_when_busy(self):
        controller = self._create_controller()

        controller.current_backend = None
        with pytest.raises(RuntimeError):
            controller.transcribe_clip("dictation.wav")

        class _BusyBackend:
            is_transcribing = True

            def transcribe(self, _path):
                return "should not run"

        controller.current_backend = _BusyBackend()
        with pytest.raises(RuntimeError):
            controller.transcribe_clip("dictation.wav")

    # ── Meeting Mode exclusivity ───────────────────────────────────

    _MEETING_REFUSAL = "Meeting Mode is active — end the meeting to use dictation"

    def test_dictation_start_is_refused_and_rolled_back_during_a_meeting(self):
        """A refused start must not leave the UI in a fake recording state."""
        controller = self._create_controller()
        ui = controller.ui_controller
        controller.meeting_active = True

        assert not ui.start_recording()

        assert not controller.recorder.is_recording
        assert not ui.is_recording
        assert not ui.main_window.is_recording
        # Flipped to recording, then reverted once the refusal came back.
        assert ui.main_window.recording_state_updates == [True, False]
        assert self._MEETING_REFUSAL in ui.statuses

    def test_recording_starts_normally_without_a_meeting(self):
        controller = self._create_controller()
        ui = controller.ui_controller

        assert ui.start_recording()

        assert controller.recorder.is_recording
        assert ui.main_window.is_recording

    def test_toggle_recording_is_refused_during_a_meeting(self):
        controller = self._create_controller()
        controller.meeting_active = True

        assert not controller.toggle_recording()

        assert not controller.recorder.is_recording
        assert self._MEETING_REFUSAL in controller.ui_controller.statuses

    def test_transcribe_clip_is_refused_during_a_meeting(self):
        """Settings-dialog rule dictation must not run beside a meeting."""
        controller = self._create_controller()
        controller.meeting_active = True

        class _Backend:
            def transcribe(self, _path):
                return "should not run"

        controller.current_backend = _Backend()
        with pytest.raises(RuntimeError):
            controller.transcribe_clip("dictation.wav")

    def test_hotkey_toggle_requires_unsupported_platform_ack(self):
        """A declined Mac/Linux warning must not start a meeting."""
        controller = self._create_controller()
        started = []
        controller.meeting_runtime.start_meeting = (
            lambda *args, **kwargs: started.append(True)
        )
        controller.ui_controller.platform_ack_result = False

        controller.toggle_meeting_mode()

        assert started == []
        assert controller.ui_controller.platform_ack_requests == 1

        controller.ui_controller.platform_ack_result = True
        controller.toggle_meeting_mode()
        assert started == [True]

    def test_second_meeting_start_is_refused_while_one_is_running(self):
        """Exclusive mode, not engine state, guards against a second engine."""
        controller = self._create_controller()
        runtime = controller.meeting_runtime
        launched = []
        runtime._launch = lambda cloud: launched.append(cloud)
        controller.meeting_active = True

        runtime.start_meeting(cloud_enabled=False)

        assert launched == []
        assert "A meeting is already in progress" in controller.ui_controller.meeting_statuses

    def test_declined_cloud_consent_resyncs_the_toggle(self):
        """Declining consent from the toggle must put the checkbox back."""
        controller = self._create_controller()
        controller.ui_controller.meeting_consent_result = False

        controller.meeting_runtime.toggle_cloud(True)

        assert controller.ui_controller.meeting_consent_requests == 1
        assert {"cloud_enabled": False} in controller.ui_controller.meeting_states
        assert "Cloud intelligence stays off" in controller.ui_controller.meeting_statuses

    def test_meeting_cleanup_gives_up_on_a_hanging_engine_shutdown(self):
        """App exit must not wait minutes for the engine to drain."""
        import threading

        controller = self._create_controller()
        runtime = controller.meeting_runtime
        runtime_module = sys.modules[type(runtime).__module__]
        blocked = threading.Event()

        class _HangingEngine:
            def shutdown(self):
                blocked.wait(30)

        runtime._engine = _HangingEngine()
        controller.meeting_active = True

        try:
            with patch.object(runtime_module, "SHUTDOWN_JOIN_TIMEOUT_S", 0.05):
                started = time.monotonic()
                runtime.cleanup()
                elapsed = time.monotonic() - started
        finally:
            blocked.set()

        assert elapsed < 5.0
        assert not controller.meeting_active
        assert runtime._engine is None

    def test_dashboard_urls_are_redacted_before_logging(self):
        controller = self._create_controller()
        redact = sys.modules[
            type(controller.meeting_runtime).__module__
        ].redact_meeting_url

        assert redact("http://127.0.0.1:8123/m/secret-host-token") == "http://127.0.0.1:8123/<redacted>"
        assert redact("") == ""
        assert redact("not-a-url") == "<redacted>"

    def test_cleanup_is_safe_with_partial_state(self):
        controller = self._create_controller()
        controller.hotkey_manager = None
        controller.streaming_transcriber = FakeStreamingTranscriber(
            backend=FakeLocalBackend(),
            chunk_duration_sec=2.0,
        )
        controller._streaming_backend = FakeLocalBackend(model_name="tiny.en")

        controller.cleanup()

        assert controller.executor.shutdown_called
        assert controller.ui_controller.cleaned_up
        assert self.db_state["closed"]

    # ── Model Manager download/delete orchestration ────────────────

    def _coordinator(self):
        return self.app_controller_module.hf_access_coordinator

    def test_manager_download_consent_cancel_keeps_selection(self):
        """A declined fetch-only download must not revert the model selection."""
        controller = self._create_controller()
        self.settings.all_settings["whisper_model"] = "base"
        controller.ui_controller.consent_result = "cancel"

        controller.request_model_download("tiny")

        assert controller.ui_controller.consent_requests[-1][0] == "tiny"
        assert self.settings.all_settings["whisper_model"] == "base"
        assert controller.ui_controller.engine_controls_refreshes == 0
        assert len(controller.executor.submissions) == 0

    def test_manager_download_already_cached_short_circuits(self):
        controller = self._create_controller()
        decision = self.app_controller_module.AccessDecision.LOAD_CACHED
        self._coordinator().evaluate_access = (
            lambda model, consume_grant=True: decision
        )
        ended = []
        self._coordinator().end_request = ended.append

        controller.request_model_download("tiny")

        assert ended == ["tiny"]
        assert controller.ui_controller.model_manager_refreshes == 1
        assert len(controller.executor.submissions) == 0

    def test_manager_fetch_only_download_leaves_engine_alone(self):
        controller = self._create_controller()
        backend = controller.transcription_backends["local_whisper"]
        decision = self.app_controller_module.AccessDecision.DOWNLOAD_ALLOWED
        self._coordinator().evaluate_access = (
            lambda model, consume_grant=True: decision
        )
        fetched = []
        with patch.object(
            self.app_controller_module,
            "download_model_files",
            side_effect=lambda name, progress_callback=None: (
                fetched.append(name) or f"/cache/{name}"
            ),
        ):
            busy_before = list(controller.ui_controller.engine_busy_states)

            controller.request_model_download("tiny")
            assert controller.ui_controller.download_started == ["tiny"]
            fn, args = controller.executor.submissions[-1]
            fn(*args)

        assert fetched == ["tiny"]
        assert backend.model_name == "base"  # engine untouched
        assert controller.ui_controller.download_finished == [("tiny", True)]
        assert controller.ui_controller.model_manager_refreshes == 1
        # Fetch-only downloads never toggle the engine-busy state.
        assert controller.ui_controller.engine_busy_states == busy_before

    def test_manager_fetch_bridges_missing_selected_model(self):
        controller = self._create_controller()
        backend = controller.transcription_backends["local_whisper"]
        backend.model_name = "tiny"
        backend.is_model_missing = True
        decision = self.app_controller_module.AccessDecision.DOWNLOAD_ALLOWED
        self._coordinator().evaluate_access = (
            lambda model, consume_grant=True: decision
        )

        controller.request_model_download("tiny")
        fn, args = controller.executor.submissions[-1]
        fn(*args)

        assert backend.is_available()
        assert controller.ui_controller.device_infos[-1] == "cpu-reloaded"
        assert "Whisper engine ready" in controller.ui_controller.statuses

    def test_manager_delete_refuses_loaded_model(self):
        controller = self._create_controller()
        backend = controller.transcription_backends["local_whisper"]
        backend.last_loaded_model = "base"

        controller.request_model_delete("base")

        assert controller.ui_controller.deleted_models == [("base", False, "Model is in use — switch models first")]
        assert len(controller.executor.submissions) == 0

    def test_manager_delete_runs_worker_and_reports(self):
        controller = self._create_controller()
        ended = []
        self._coordinator().end_request = ended.append
        deleted = []
        with patch.object(
            self.app_controller_module,
            "delete_model_from_cache",
            side_effect=deleted.append,
        ):
            controller.request_model_delete("tiny")
            fn, args = controller.executor.submissions[-1]
            fn(*args)

        assert deleted == ["tiny"]
        assert controller.ui_controller.deleted_models == [("tiny", True, "")]
        assert controller.ui_controller.model_manager_refreshes == 1
        assert ended == ["tiny"]

    def test_manager_delete_reports_locked_files(self):
        controller = self._create_controller()
        with patch.object(
            self.app_controller_module,
            "delete_model_from_cache",
            side_effect=PermissionError("locked"),
        ):
            controller.request_model_delete("tiny")
            fn, args = controller.executor.submissions[-1]
            fn(*args)

        assert controller.ui_controller.deleted_models == [("tiny", False, "Files are in use by another process")]

    # ── GPU component install / fallback ───────────────────────────

    def test_component_install_activates_and_reloads_without_restart(self):
        import threading

        controller = self._create_controller()
        self.settings.all_settings["whisper_device"] = "cpu"
        reload_requests = []
        controller.reload_whisper_model = lambda: reload_requests.append(True)

        with patch.object(
            self.app_controller_module, "install_component", lambda *a, **k: None
        ), patch.object(
            self.app_controller_module, "activate_component", lambda cid: (True, "")
        ), patch.object(
            self.app_controller_module, "gpu_runtime_available", lambda: True
        ):
            controller._component_install_worker("gpu-accel", threading.Event())

        component_id, success, message = (
            controller.ui_controller.component_install_results[-1]
        )
        assert component_id == "gpu-accel"
        assert success
        assert "Restart" not in message
        # The persisted CPU device (fallback residue) moves back to auto so the
        # follow-up reload can adopt the GPU.
        assert self.settings.all_settings["whisper_device"] == "auto"
        assert controller.ui_controller.engine_controls_refreshes == 1
        assert reload_requests == [True]

    def test_component_activation_failure_keeps_the_restart_message(self):
        import threading

        controller = self._create_controller()
        self.settings.all_settings["whisper_device"] = "cpu"
        reload_requests = []
        controller.reload_whisper_model = lambda: reload_requests.append(True)

        with patch.object(
            self.app_controller_module, "install_component", lambda *a, **k: None
        ), patch.object(
            self.app_controller_module,
            "activate_component",
            lambda cid: (False, "registration failed"),
        ), patch.object(
            self.app_controller_module, "gpu_runtime_available", lambda: False
        ):
            controller._component_install_worker("gpu-accel", threading.Event())

        _cid, success, message = (
            controller.ui_controller.component_install_results[-1]
        )
        assert success
        assert "Restart OpenWhisper" in message
        assert self.settings.all_settings["whisper_device"] == "cpu"
        assert reload_requests == []

    def test_meeting_agent_install_does_not_ask_for_restart(self):
        import threading

        controller = self._create_controller()
        with patch.object(
            self.app_controller_module, "install_component", lambda *a, **k: None
        ), patch.object(
            self.app_controller_module, "activate_component", lambda cid: (True, "")
        ):
            controller._component_install_worker("meeting-agent", threading.Event())

        component_id, success, message = (
            controller.ui_controller.component_install_results[-1]
        )
        assert component_id == "meeting-agent"
        assert success
        assert "Restart" not in message

    def test_auto_update_check_failure_sets_status(self):
        controller = self._create_controller()
        controller._on_update_check_finished(
            None, "GitHub rate-limited this update check.", False
        )
        assert any("rate limit" in status.lower() for status in controller.ui_controller.statuses)
        assert controller.ui_controller.update_checks[-1] == (
            None, "GitHub rate-limited this update check.", False
        )

    def test_gpu_fallback_reverts_device_setting_and_names_the_fix(self):
        controller = self._create_controller()
        backend = controller.transcription_backends["local_whisper"]
        backend.gpu_fallback_note = "GPU unavailable, using CPU"
        backend.gpu_fallback_cause = (
            self.app_controller_module.GpuFallbackCause.MISSING_LIBRARIES
        )
        self.settings.all_settings["whisper_device"] = "cuda"

        with patch.object(
            self.app_controller_module,
            "available_component_ids",
            lambda: ("gpu-accel",),
        ):
            controller.gpu_fallback_detected.emit()

        assert self.settings.all_settings["whisper_device"] == "cpu"
        assert controller.ui_controller.engine_controls_refreshes == 1
        assert "Manage models" in controller.ui_controller.statuses[-1]

    def test_gpu_fallback_out_of_memory_does_not_advertise_the_component(self):
        controller = self._create_controller()
        backend = controller.transcription_backends["local_whisper"]
        backend.gpu_fallback_note = "GPU out of memory, using CPU"
        backend.gpu_fallback_cause = (
            self.app_controller_module.GpuFallbackCause.OUT_OF_MEMORY
        )
        self.settings.all_settings["whisper_device"] = "auto"

        controller.gpu_fallback_detected.emit()

        assert self.settings.all_settings["whisper_device"] == "cpu"
        status = controller.ui_controller.statuses[-1]
        assert "out of memory" in status
        assert "Manage models" not in status

    def test_reload_worker_leaves_the_fallback_warning_visible(self):
        controller = self._create_controller()
        backend = controller.transcription_backends["local_whisper"]

        def reload_with_fallback(model_name=None):
            backend.device_info = "turbo | cpu (int8) — GPU unavailable, using CPU"
            backend.gpu_fallback_note = "GPU unavailable, using CPU"
            backend.gpu_fallback_cause = (
                self.app_controller_module.GpuFallbackCause.MISSING_LIBRARIES
            )

        backend.reload_model = reload_with_fallback
        self.settings.all_settings["whisper_device"] = "cuda"

        with patch.object(
            self.app_controller_module,
            "available_component_ids",
            lambda: ("gpu-accel",),
        ):
            controller.reload_whisper_model()
            controller._reload_timer.timeout.emit()
            fn, args = controller.executor.submissions[0]
            fn(*args)

        statuses = controller.ui_controller.statuses
        assert "Whisper engine ready" in statuses
        # Emitted after the ready status, so the actionable warning is what
        # remains on screen.
        assert "Manage models" in statuses[-1]
        assert self.settings.all_settings["whisper_device"] == "cpu"

    def test_startup_fallback_is_reported_once_the_ui_is_ready(self):
        controller = self._create_controller()
        backend = controller.transcription_backends["local_whisper"]
        backend.gpu_fallback_note = "GPU unavailable, using CPU"
        backend.gpu_fallback_cause = (
            self.app_controller_module.GpuFallbackCause.MISSING_LIBRARIES
        )
        self.settings.all_settings["whisper_device"] = "auto"

        with patch.object(
            self.app_controller_module,
            "available_component_ids",
            lambda: ("gpu-accel",),
        ):
            controller.notify_main_ui_ready()

        assert self.settings.all_settings["whisper_device"] == "cpu"
        assert "Manage models" in controller.ui_controller.statuses[-1]

    def test_notify_main_ui_ready_loads_deferred_local_backend(self):
        controller = self._create_controller()
        backend = controller.transcription_backends["local_whisper"]
        backend.load_deferred = True
        backend.model = None

        controller.notify_main_ui_ready()

        assert controller._reload_in_flight
        assert controller.ui_controller.engine_busy_states[-1] is True
        assert "Loading whisper engine..." in controller.ui_controller.statuses
        assert len(controller.executor.submissions) == 1

        fn, args = controller.executor.submissions[0]
        fn(*args)

        assert backend.load_deferred is False
        assert backend.is_available()
        assert "Whisper engine ready" in controller.ui_controller.statuses
        assert controller.streaming_transcriber is not None

    def test_notify_main_ui_ready_skips_local_load_for_api_backend(self):
        controller = self._create_controller()
        backend = controller.transcription_backends["local_whisper"]
        backend.load_deferred = True
        controller.current_backend = controller.transcription_backends["api_gpt4o"]

        controller.notify_main_ui_ready()

        assert controller.executor.submissions == []
        assert not controller._reload_in_flight
        assert backend.load_deferred is True

    def test_start_recording_refused_while_local_engine_loading(self):
        controller = self._create_controller()
        backend = controller.transcription_backends["local_whisper"]
        backend.load_deferred = True

        assert controller.start_recording() is False
        assert "still loading" in controller.ui_controller.statuses[-1].lower()
        assert controller.recorder.is_recording is False



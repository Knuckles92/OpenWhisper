"""Main Qt-facing application controller."""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from config import config
from services.database import db
from services.recorder import AudioRecorder
from services.runtime import (
    HotkeyRuntime,
    StreamingRuntime,
    TranscriptionRuntime,
)
from services.settings import settings_manager
from transcriber import LocalWhisperBackend, OpenAIBackend, TranscriptionBackend


class ApplicationController(QObject):
    """Main application controller integrating UI and logic."""

    transcription_completed = pyqtSignal(str)
    transcription_failed = pyqtSignal(str)
    status_update = pyqtSignal(str)
    stt_state_changed = pyqtSignal(bool)
    recording_state_changed = pyqtSignal(bool)
    partial_transcription = pyqtSignal(str, bool)
    streaming_text_update = pyqtSignal(str, bool)
    streaming_overlay_show = pyqtSignal()
    streaming_overlay_hide = pyqtSignal()
    caret_indicator_show = pyqtSignal()
    caret_indicator_hide = pyqtSignal()

    def __init__(self, ui_controller):
        super().__init__()
        self.ui_controller = ui_controller

        saved_device_id = settings_manager.load_audio_input_device()
        self.recorder = AudioRecorder(device_id=saved_device_id)
        self.executor = ThreadPoolExecutor(max_workers=2)

        self.hotkey_manager = None
        self.streaming_transcriber = None
        self._streaming_backend = None

        self.transcription_backends: Dict[str, TranscriptionBackend] = {}
        self.current_backend: Optional[TranscriptionBackend] = None
        self._current_model_name = "local_whisper"

        self._streaming_enabled = False
        self._streaming_paste_enabled = False

        self._pending_audio_path: Optional[str] = None
        self._pending_audio_duration: Optional[float] = None
        self._pending_file_size: Optional[int] = None
        self._transcription_start_time: Optional[float] = None

        self.hotkey_runtime = HotkeyRuntime(self)
        self.streaming_runtime = StreamingRuntime(self)
        self.transcription_runtime = TranscriptionRuntime(self)

        self._setup_transcription_backends()
        self._setup_ui_callbacks()
        self.hotkey_runtime.setup_hotkeys()
        self.streaming_runtime.setup_audio_level_callback()
        self.streaming_runtime.setup_streaming()
        self._connect_signals()
        self.hotkey_runtime.setup_hook_watchdog()

    def _setup_transcription_backends(self) -> None:
        """Initialize transcription backends."""
        logging.info("Setting up transcription backends...")

        self.transcription_backends["local_whisper"] = LocalWhisperBackend()
        self.transcription_backends["api_whisper"] = OpenAIBackend("api_whisper")
        self.transcription_backends["api_gpt4o"] = OpenAIBackend("api_gpt4o")
        self.transcription_backends["api_gpt4o_mini"] = OpenAIBackend("api_gpt4o_mini")

        saved_model = settings_manager.load_model_selection()
        self.current_backend = self.transcription_backends.get(
            saved_model, self.transcription_backends["local_whisper"]
        )
        logging.info(f"Using transcription backend: {saved_model}")

    def _setup_ui_callbacks(self) -> None:
        """Setup UI event callbacks."""
        self.ui_controller.on_record_start = self.start_recording
        self.ui_controller.on_record_stop = self.stop_recording
        self.ui_controller.on_record_cancel = self.cancel
        self.ui_controller.on_model_changed = self.on_model_changed
        self.ui_controller.on_hotkeys_changed = self.update_hotkeys
        self.ui_controller.on_retranscribe = self.retranscribe_audio
        self.ui_controller.on_upload_audio = self.upload_audio_file
        self.ui_controller.on_whisper_settings_changed = self.reload_whisper_model
        self.ui_controller.on_audio_device_changed = self.change_audio_device
        self.ui_controller.on_streaming_settings_changed = self.reconfigure_streaming

    def reload_whisper_model(self) -> None:
        """Reload the local whisper model with current settings."""
        logging.info("Reloading whisper model...")
        self.ui_controller.set_status("Reloading whisper engine...")

        local_backend = self.transcription_backends.get("local_whisper")
        if local_backend:
            local_backend.reload_model()

            if hasattr(local_backend, "device_info"):
                self.ui_controller.set_device_info(local_backend.device_info)
                logging.info(f"Whisper reloaded: {local_backend.device_info}")

            self.ui_controller.set_status("Whisper engine reloaded")
        else:
            logging.warning("Local whisper backend not found")
            self.ui_controller.set_status("Ready")

    def change_audio_device(self, device_id: Optional[int]) -> None:
        """Change the audio input device."""
        logging.info(f"Changing audio device to: {device_id}")

        if self.recorder.is_recording:
            logging.warning("Cannot change audio device while recording")
            self.ui_controller.set_status("Stop recording before changing device")
            return

        self.recorder.cleanup()
        self.recorder = AudioRecorder(device_id=device_id)
        self.streaming_runtime.setup_audio_level_callback()

        device_name = "System Default" if device_id is None else f"Device {device_id}"
        logging.info(f"Audio device changed to: {device_name}")
        self.ui_controller.set_status("Audio device changed")

    def update_hotkeys(self, hotkeys: Dict[str, str]) -> None:
        self.hotkey_runtime.update_hotkeys(hotkeys)

    def reconfigure_streaming(self) -> None:
        self.streaming_runtime.reconfigure_streaming()

    def start_recording(self) -> None:
        """Start audio recording (UI callback target)."""
        self.transcription_runtime.start_recording()

    def stop_recording(self) -> None:
        """Stop recording and submit transcription (UI callback target)."""
        self.transcription_runtime.stop_recording()

    def toggle_recording(self) -> None:
        """Toggle recording on/off (hotkey callback target)."""
        self.transcription_runtime.toggle_recording()

    def cancel(self) -> None:
        """Cancel an active recording or transcription (UI/hotkey callback target)."""
        self.transcription_runtime.cancel()

    def retranscribe_audio(self, audio_path: str) -> None:
        """Re-transcribe an existing audio file (UI callback target)."""
        self.transcription_runtime.retranscribe_audio(audio_path)

    def upload_audio_file(self, audio_path: str) -> None:
        """Transcribe an uploaded audio file (UI callback target)."""
        self.transcription_runtime.upload_audio_file(audio_path)

    def on_model_changed(self, model_name: str) -> None:
        """Switch the active transcription backend (UI callback target)."""
        self.transcription_runtime.on_model_changed(model_name)

    def update_status_with_auto_hide(self, status: str) -> None:
        """Emit a thread-safe status update (HotkeyManager callback target)."""
        self.hotkey_runtime.update_status_with_auto_hide(status)

    def _connect_signals(self) -> None:
        """Connect Qt signals to UI controller methods."""
        self.transcription_completed.connect(self._on_transcription_complete)
        self.transcription_failed.connect(self._on_transcription_error)
        self.status_update.connect(self.ui_controller.set_status)
        self.stt_state_changed.connect(self.hotkey_runtime.on_stt_state_changed)
        self.recording_state_changed.connect(self._on_recording_state_changed)
        self.partial_transcription.connect(
            self.ui_controller.main_window.set_partial_transcription
        )
        self.streaming_text_update.connect(self.ui_controller.update_streaming_text)
        self.streaming_overlay_show.connect(self.ui_controller.show_streaming_overlay)
        self.streaming_overlay_hide.connect(self.ui_controller.hide_streaming_overlay)
        self.caret_indicator_show.connect(
            self.ui_controller.show_caret_paste_indicator
        )
        self.caret_indicator_hide.connect(
            self.ui_controller.hide_caret_paste_indicator
        )

    def _on_recording_state_changed(self, is_recording: bool) -> None:
        """Handle recording state change on main thread."""
        self.ui_controller.is_recording = is_recording
        if self.ui_controller.main_window.is_recording != is_recording:
            self.ui_controller.main_window.is_recording = is_recording
            self.ui_controller.main_window._update_recording_state()

    def _on_transcription_complete(self, transcript: str) -> None:
        self.transcription_runtime.on_transcription_complete(transcript)

    def _on_transcription_error(self, error_message: str) -> None:
        self.transcription_runtime.on_transcription_error(error_message)

    def cleanup(self) -> None:
        """Cleanup resources."""
        logging.info("Starting application cleanup...")

        try:
            if self.current_backend and self.current_backend.is_transcribing:
                logging.info("Cancelling ongoing transcription...")
                self.current_backend.cancel_transcription()
        except Exception as exc:
            logging.debug(f"Error cancelling transcription: {exc}")

        try:
            if hasattr(self, "_watchdog_timer") and self._watchdog_timer:
                self._watchdog_timer.stop()
            if hasattr(self, "_periodic_refresh_timer") and self._periodic_refresh_timer:
                self._periodic_refresh_timer.stop()
        except Exception as exc:
            logging.debug(f"Error stopping watchdog timers: {exc}")

        try:
            if self.hotkey_manager:
                self.hotkey_manager.cleanup()
        except Exception as exc:
            logging.debug(f"Error during hotkey cleanup: {exc}")

        try:
            if self.recorder:
                self.recorder.cleanup()
        except Exception as exc:
            logging.debug(f"Error during recorder cleanup: {exc}")

        try:
            self.streaming_runtime.cleanup()
        except Exception as exc:
            logging.debug(f"Error during streaming cleanup: {exc}")

        try:
            self.executor.shutdown(wait=True, cancel_futures=True)
        except TypeError:
            self.executor.shutdown(wait=False)
        except Exception as exc:
            logging.debug(f"Error during executor shutdown: {exc}")

        try:
            for backend_name, backend in self.transcription_backends.items():
                try:
                    logging.info(f"Cleaning up transcription backend: {backend_name}")
                    backend.cleanup()
                except Exception as exc:
                    logging.debug(f"Error cleaning up {backend_name} backend: {exc}")
            self.transcription_backends.clear()
            self.current_backend = None
        except Exception as exc:
            logging.debug(f"Error during transcription backends cleanup: {exc}")

        try:
            self.ui_controller.cleanup()
        except Exception as exc:
            logging.debug(f"Error during UI controller cleanup: {exc}")

        try:
            db.close()
        except Exception as exc:
            logging.debug(f"Error closing database: {exc}")

        logging.info("Application controller cleaned up")

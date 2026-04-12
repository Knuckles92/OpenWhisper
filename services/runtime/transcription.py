"""Recording and transcription helpers for the application controller."""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING

import keyboard
import pyperclip

from config import config
from services.audio_processor import audio_processor
from services.history_manager import history_manager
from services.settings import settings_manager

if TYPE_CHECKING:
    from services.application_controller import ApplicationController


class TranscriptionRuntime:
    """Owns recording flow and transcription job orchestration."""

    def __init__(self, controller: "ApplicationController"):
        self.controller = controller

    def start_recording(self) -> None:
        """Start audio recording."""
        if self.controller.recorder.start_recording():
            logging.info("Recording started")
            self.controller.ui_controller.clear_transcription_stats()
            self.controller.ui_controller.main_window.clear_partial_transcription()
            self.controller.streaming_runtime.start_streaming_session()
            self.controller.recording_state_changed.emit(True)
            self.controller.status_update.emit("Recording...")
        else:
            self.controller.status_update.emit("Failed to start recording")

    def stop_recording(self) -> None:
        """Stop audio recording and start transcription."""
        if self.controller._streaming_paste_enabled:
            self.controller.streaming_overlay_hide.emit()
            settings = settings_manager.load_all_settings()
            if settings.get("auto_paste", True):
                self.controller.caret_indicator_show.emit()

        self.controller.streaming_runtime.stop_streaming_session()

        if not self.controller.recorder.stop_recording():
            self.controller.status_update.emit("Failed to stop recording")
            return

        self.controller.recording_state_changed.emit(False)
        self.controller.status_update.emit("Processing...")

        if not self.controller.recorder.wait_for_stop_completion():
            logging.warning(
                "Proceeding without confirmed post-roll completion; "
                "tail of recording may be short"
            )

        if not self.controller.recorder.has_recording_data():
            logging.error("No recording data available")
            self.on_transcription_error("No audio data recorded")
            return

        if not self.controller.recorder.save_recording():
            logging.error("Failed to save recording")
            self.on_transcription_error("Failed to save audio file")
            return

        if not os.path.exists(config.RECORDED_AUDIO_FILE):
            logging.error(f"Audio file not found: {config.RECORDED_AUDIO_FILE}")
            self.on_transcription_error("Audio file not created")
            return

        file_size = os.path.getsize(config.RECORDED_AUDIO_FILE)
        logging.info(f"Audio file size: {file_size} bytes")
        if file_size < 100:
            logging.error(f"Audio file too small: {file_size} bytes")
            self.on_transcription_error("Audio file is empty or corrupted")
            return

        self.controller._pending_audio_file = config.RECORDED_AUDIO_FILE
        self.controller._pending_audio_duration = (
            self.controller.recorder.get_recording_duration()
        )
        self.controller._pending_file_size = file_size

        try:
            self._submit_transcription_job(config.RECORDED_AUDIO_FILE)
            logging.info(
                "Transcription started. Duration: "
                f"{self.controller.recorder.get_recording_duration():.2f}s"
            )
        except Exception as exc:
            logging.error(f"Failed to start transcription: {exc}")
            self.on_transcription_error(f"Failed to process audio: {exc}")

    def toggle_recording(self) -> None:
        """Toggle between starting and stopping recording."""
        logging.info(
            f"Toggle recording. Current state: {self.controller.recorder.is_recording}"
        )
        if not self.controller.recorder.is_recording:
            self.start_recording()
        else:
            self.stop_recording()

    def cancel_recording(self) -> None:
        """Cancel recording or transcription."""
        logging.info(f"Cancel called. Recording: {self.controller.recorder.is_recording}")

        if self.controller.recorder.is_recording:
            self.controller.streaming_runtime.cancel_streaming_session()
            self.controller.recording_state_changed.emit(False)
            self.controller.recorder.stop_recording()
            self.controller.recorder.clear_recording_data()
            self.controller.status_update.emit("Recording cancelled")
            logging.info("Recording cancelled")
        elif self.controller.current_backend and self.controller.current_backend.is_transcribing:
            self.controller.current_backend.cancel_transcription()
            self.controller.status_update.emit("Transcription cancelled")
            logging.info("Transcription cancelled")
        else:
            self.controller.status_update.emit("Cancelled")

    def retranscribe_audio(self, audio_file_path: str) -> None:
        """Re-transcribe an existing audio file."""
        if not os.path.exists(audio_file_path):
            logging.error(
                f"Audio file not found for re-transcription: {audio_file_path}"
            )
            self.controller.status_update.emit("Error: Audio file not found")
            return

        logging.info(f"Re-transcribing audio file: {audio_file_path}")
        self.controller._pending_audio_file = None
        self.controller.status_update.emit("Processing...")

        try:
            self.controller._pending_file_size = os.path.getsize(audio_file_path)
            self.controller._pending_audio_duration = None
            self._submit_transcription_job(audio_file_path)
        except Exception as exc:
            logging.error(f"Failed to start re-transcription: {exc}")
            self.on_transcription_error(f"Failed to process audio: {exc}")

    def upload_audio_file(self, audio_file_path: str) -> None:
        """Transcribe an uploaded audio file."""
        if not os.path.exists(audio_file_path):
            logging.error(f"Uploaded audio file not found: {audio_file_path}")
            self.controller.status_update.emit("Error: Audio file not found")
            return

        logging.info(f"Processing uploaded audio file: {audio_file_path}")
        self.controller._pending_audio_file = None
        self.controller.status_update.emit("Processing uploaded file...")

        try:
            self.controller._pending_file_size = os.path.getsize(audio_file_path)
            self.controller._pending_audio_duration = None
            self._submit_transcription_job(audio_file_path)
        except Exception as exc:
            logging.error(f"Failed to process uploaded audio: {exc}")
            self.on_transcription_error(f"Failed to process audio: {exc}")

    def transcribe_audio_file(self, audio_file_path: str) -> None:
        """Transcribe a single audio file in a background thread."""
        try:
            if self.controller._pending_file_size is None:
                self.controller._pending_file_size = os.path.getsize(audio_file_path)
            self.controller.status_update.emit("Transcribing...")
            self.controller._transcription_start_time = time.time()
            transcribed_text = self.controller.current_backend.transcribe(audio_file_path)
            self.controller.transcription_completed.emit(transcribed_text)
        except Exception as exc:
            logging.error(f"Transcription failed: {exc}")
            self.controller.transcription_failed.emit(str(exc))

    def transcribe_large_audio_file(self, audio_file_path: str) -> None:
        """Transcribe a large audio file by splitting it into chunks."""
        chunk_files = []
        if self.controller._pending_file_size is None:
            self.controller._pending_file_size = os.path.getsize(audio_file_path)
        self.controller._transcription_start_time = time.time()
        try:
            def progress_callback(message: str) -> None:
                self.controller.status_update.emit(message)

            chunk_files = audio_processor.split_audio_file(
                audio_file_path, progress_callback
            )
            if not chunk_files:
                raise Exception("Failed to split audio file")

            if hasattr(self.controller.current_backend, "transcribe_chunks"):
                self.controller.status_update.emit(
                    f"Transcribing {len(chunk_files)} chunks..."
                )
                transcribed_text = self.controller.current_backend.transcribe_chunks(
                    chunk_files
                )
            else:
                transcriptions = []
                for index, chunk_file in enumerate(chunk_files):
                    self.controller.status_update.emit(
                        f"Transcribing chunk {index + 1}/{len(chunk_files)}..."
                    )
                    transcriptions.append(
                        self.controller.current_backend.transcribe(chunk_file)
                    )
                transcribed_text = audio_processor.combine_transcriptions(transcriptions)

            self.controller.transcription_completed.emit(transcribed_text)
        except Exception as exc:
            logging.error(f"Large audio transcription failed: {exc}")
            self.controller.transcription_failed.emit(str(exc))
        finally:
            try:
                audio_processor.cleanup_temp_files()
            except Exception as cleanup_error:
                logging.warning(
                    f"Failed to cleanup temp files: {cleanup_error}"
                )

    def on_transcription_complete(self, transcribed_text: str) -> None:
        """Handle transcription completion."""
        self.controller.ui_controller.set_transcription(transcribed_text)
        self.controller.ui_controller.set_status("Transcription complete!")
        self.controller.ui_controller.hide_overlay()

        transcription_time = None
        if self.controller._transcription_start_time is not None:
            transcription_time = time.time() - self.controller._transcription_start_time
            self.controller._transcription_start_time = None

        if transcription_time is not None:
            self.controller.ui_controller.set_transcription_stats(
                transcription_time,
                self.controller._pending_audio_duration or 0.0,
                self.controller._pending_file_size or 0,
            )

        try:
            model_info = self.controller._current_model_name
            if self.controller._current_model_name == "local_whisper":
                local_backend = self.controller.transcription_backends.get("local_whisper")
                if local_backend and hasattr(local_backend, "device_info"):
                    model_info = f"local_whisper ({local_backend.device_info})"

            history_manager.add_entry(
                text=transcribed_text,
                model=model_info,
                source_audio_file=self.controller._pending_audio_file,
                transcription_time=transcription_time,
                audio_duration=self.controller._pending_audio_duration,
                file_size=self.controller._pending_file_size,
            )
            self.controller.ui_controller.refresh_history()
            logging.info("Transcription saved to history")
        except Exception as exc:
            logging.error(f"Failed to save transcription to history: {exc}")
        finally:
            self.controller._pending_audio_file = None
            self.controller._pending_audio_duration = None
            self.controller._pending_file_size = None

        settings = settings_manager.load_all_settings()
        copy_clipboard = settings.get("copy_clipboard", True)
        auto_paste = settings.get("auto_paste", True)

        if copy_clipboard:
            try:
                pyperclip.copy(transcribed_text)
                logging.info("Transcription copied to clipboard")
            except Exception as exc:
                logging.error(f"Failed to copy to clipboard: {exc}")

        if auto_paste:
            try:
                keyboard.send("ctrl+v")
                logging.info("Transcription auto-pasted")
                self.controller.ui_controller.set_status("Ready (Pasted)")
            except Exception as exc:
                logging.error(f"Failed to auto-paste: {exc}")
                self.controller.ui_controller.set_status(
                    "Transcription complete (paste failed)"
                )
        else:
            self.controller.ui_controller.set_status("Ready")

        if self.controller._streaming_paste_enabled:
            self.controller.caret_indicator_hide.emit()

    def on_transcription_error(self, error_message: str) -> None:
        """Handle transcription error."""
        self.controller.ui_controller.set_status(f"Error: {error_message}")
        self.controller.ui_controller.set_transcription(f"Error: {error_message}")
        self.controller.ui_controller.hide_overlay()
        if self.controller._streaming_paste_enabled:
            self.controller.caret_indicator_hide.emit()

    def on_model_changed(self, model_name: str) -> None:
        """Handle model selection change."""
        model_value = config.MODEL_VALUE_MAP.get(model_name)
        if model_value and model_value in self.controller.transcription_backends:
            self.controller.current_backend = self.controller.transcription_backends[
                model_value
            ]
            self.controller._current_model_name = model_value
            settings_manager.save_model_selection(model_value)
            logging.info(f"Switched to model: {model_value}")

            if model_value == "local_whisper":
                local_backend = self.controller.transcription_backends.get("local_whisper")
                if local_backend and hasattr(local_backend, "device_info"):
                    self.controller.ui_controller.set_device_info(
                        local_backend.device_info
                    )
            else:
                self.controller.ui_controller.set_device_info("")

    def show_large_file_overlay(self, file_size_mb: float, is_splitting: bool) -> None:
        """Show the large-file overlay state."""
        overlay = self.controller.ui_controller.overlay
        overlay.set_large_file_info(file_size_mb)

        if is_splitting:
            overlay.show_at_cursor(overlay.STATE_LARGE_FILE_SPLITTING)
        else:
            overlay.show_at_cursor(overlay.STATE_LARGE_FILE_PROCESSING)

    def _submit_transcription_job(self, audio_file_path: str) -> None:
        needs_splitting, file_size_mb = audio_processor.check_file_size(audio_file_path)
        should_split = (
            needs_splitting and self.controller.current_backend.requires_file_splitting
        )

        if should_split:
            logging.info(
                f"Large file ({file_size_mb:.2f} MB), backend requires splitting"
            )
            self.show_large_file_overlay(file_size_mb, is_splitting=True)
            self.controller.status_update.emit(
                f"Splitting large file ({file_size_mb:.1f} MB)..."
            )
            self.controller.executor.submit(
                self.transcribe_large_audio_file, audio_file_path
            )
        elif needs_splitting:
            logging.info(
                f"Large file ({file_size_mb:.2f} MB), processing without splitting"
            )
            self.show_large_file_overlay(file_size_mb, is_splitting=False)
            self.controller.status_update.emit(
                f"Processing large file ({file_size_mb:.1f} MB)..."
            )
            self.controller.executor.submit(self.transcribe_audio_file, audio_file_path)
        else:
            self.controller.executor.submit(self.transcribe_audio_file, audio_file_path)

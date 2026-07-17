"""Recording and transcription helpers for the application controller."""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING, Optional

import pyperclip

from config import config
from services.hotkey_manager import is_accessibility_trusted, send_paste
from services.audio_processor import audio_processor
from services.history_manager import history_manager
from services.transcript_cleanup import TranscriptCleanup
try:
    from services.settings import (
        SettingsKey,
        resolve_transcript_cleanup_model,
        resolve_transcript_cleanup_prompt,
        resolve_transcript_cleanup_provider,
        resolve_transcript_cleanup_reasoning,
        settings_manager,
    )
except ImportError:  # pragma: no cover - supports lightweight test stubs
    from services.settings import settings_manager

    class SettingsKey:
        AUTO_PASTE = "auto_paste"
        COPY_CLIPBOARD = "copy_clipboard"
        TRANSCRIPT_CLEANUP_ENABLED = "transcript_cleanup_enabled"

    def resolve_transcript_cleanup_prompt(settings=None):
        return config.TRANSCRIPT_CLEANUP_PROMPT

    def resolve_transcript_cleanup_provider(settings=None):
        return config.TRANSCRIPT_CLEANUP_PROVIDER

    def resolve_transcript_cleanup_model(settings=None):
        return config.TRANSCRIPT_CLEANUP_MODEL

    def resolve_transcript_cleanup_reasoning(settings=None):
        return config.TRANSCRIPT_CLEANUP_REASONING

from ui_qt.overlay_state import OverlayState

if TYPE_CHECKING:
    from services.application_controller import ApplicationController

logger = logging.getLogger(__name__)


class TranscriptionRuntime:
    """Owns recording flow and transcription job orchestration."""

    def __init__(self, controller: "ApplicationController"):
        self.controller = controller
        self._transcript_cleanup = TranscriptCleanup()

    def start_recording(self) -> None:
        """Start audio recording."""
        if self.controller.recorder.start_recording():
            logger.info("Recording started")
            self.controller.ui_controller.clear_transcription_stats()
            self.controller.ui_controller.main_window.clear_partial_transcription()
            self.controller.streaming_runtime.start_streaming_session()
            self.controller.recording_state_changed.emit(True)
            self.controller.overlay_state_update.emit(OverlayState.RECORDING)
            self.controller.status_update.emit("Recording...")
        else:
            self.controller.overlay_state_update.emit(OverlayState.NONE)
            self.controller.status_update.emit("Failed to start recording")

    def stop_recording(self) -> None:
        """Stop audio recording and start transcription."""
        if self.controller._streaming_enabled:
            # Dismiss preview overlay immediately so the classic waveform
            # processing/transcribing states are the only post-stop UI.
            self.controller.streaming_overlay_hide.emit()

        self.controller.streaming_runtime.stop_streaming_session()

        if not self.controller.recorder.stop_recording():
            self.controller.overlay_state_update.emit(OverlayState.NONE)
            self.controller.status_update.emit("Failed to stop recording")
            return

        self.controller.recording_state_changed.emit(False)
        self.controller.overlay_state_update.emit(OverlayState.PROCESSING)
        self.controller.status_update.emit("Processing...")

        if not self.controller.recorder.wait_for_stop_completion():
            logger.warning(
                "Proceeding without confirmed post-roll completion; "
                "tail of recording may be short"
            )

        if not self.controller.recorder.has_recording_data():
            logger.error("No recording data available")
            self.on_transcription_error("No audio data recorded")
            return

        if not self.controller.recorder.save_recording():
            logger.error("Failed to save recording")
            self.on_transcription_error("Failed to save audio file")
            return

        if not os.path.exists(config.RECORDED_AUDIO_FILE):
            logger.error(f"Audio file not found: {config.RECORDED_AUDIO_FILE}")
            self.on_transcription_error("Audio file not created")
            return

        file_size = os.path.getsize(config.RECORDED_AUDIO_FILE)
        logger.info(f"Audio file size: {file_size} bytes")
        if file_size < 100:
            logger.error(f"Audio file too small: {file_size} bytes")
            self.on_transcription_error("Audio file is empty or corrupted")
            return

        self.controller._pending_audio_path = config.RECORDED_AUDIO_FILE
        self.controller._pending_audio_duration = (
            self.controller.recorder.get_recording_duration()
        )
        self.controller._pending_file_size = file_size

        try:
            self._submit_transcription_job(config.RECORDED_AUDIO_FILE)
            logger.info(
                "Transcription started. Duration: "
                f"{self.controller.recorder.get_recording_duration():.2f}s"
            )
        except Exception as exc:
            logger.error(f"Failed to start transcription: {exc}")
            self.on_transcription_error(f"Failed to process audio: {exc}")

    def toggle_recording(self) -> None:
        """Toggle between starting and stopping recording."""
        logger.info(
            f"Toggle recording. Current state: {self.controller.recorder.is_recording}"
        )
        if not self.controller.recorder.is_recording:
            self.start_recording()
        else:
            self.stop_recording()

    def cancel(self) -> None:
        """Cancel an active recording or transcription, depending on state."""
        logger.info(f"Cancel called. Recording: {self.controller.recorder.is_recording}")

        if self.controller.recorder.is_recording:
            self._cancel_recording()
        elif self.controller.current_backend and self.controller.current_backend.is_transcribing:
            self._cancel_transcription()
        else:
            self.controller.overlay_state_update.emit(OverlayState.CANCELING)
            self.controller.status_update.emit("Canceled")

    def _cancel_recording(self) -> None:
        """Discard the active recording without transcribing."""
        self.controller.streaming_runtime.cancel_streaming_session()
        self.controller.recording_state_changed.emit(False)
        self.controller.recorder.stop_recording()
        self.controller.recorder.clear_recording_data()
        self.controller.overlay_state_update.emit(OverlayState.CANCELING)
        self.controller.status_update.emit("Recording canceled")
        logger.info("Recording canceled")

    def _cancel_transcription(self) -> None:
        """Cancel an in-progress transcription job."""
        self.controller.current_backend.cancel_transcription()
        self.controller.overlay_state_update.emit(OverlayState.CANCELING)
        self.controller.status_update.emit("Transcription canceled")
        logger.info("Transcription canceled")

    def retranscribe_audio(self, audio_path: str) -> None:
        """Re-transcribe an existing audio file."""
        if not os.path.exists(audio_path):
            logger.error(
                f"Audio file not found for re-transcription: {audio_path}"
            )
            self.controller.overlay_state_update.emit(OverlayState.NONE)
            self.controller.status_update.emit("Error: Audio file not found")
            return

        logger.info(f"Re-transcribing audio file: {audio_path}")
        self.controller._pending_audio_path = None
        self.controller.overlay_state_update.emit(OverlayState.PROCESSING)
        self.controller.status_update.emit("Processing...")

        try:
            self.controller._pending_file_size = os.path.getsize(audio_path)
            self.controller._pending_audio_duration = None
            self._submit_transcription_job(audio_path)
        except Exception as exc:
            logger.error(f"Failed to start re-transcription: {exc}")
            self.on_transcription_error(f"Failed to process audio: {exc}")

    def upload_audio_file(self, audio_path: str) -> None:
        """Transcribe an uploaded audio file."""
        if not os.path.exists(audio_path):
            logger.error(f"Uploaded audio file not found: {audio_path}")
            self.controller.overlay_state_update.emit(OverlayState.NONE)
            self.controller.status_update.emit("Error: Audio file not found")
            return

        logger.info(f"Processing uploaded audio file: {audio_path}")
        self.controller._pending_audio_path = None
        self.controller.overlay_state_update.emit(OverlayState.PROCESSING)
        self.controller.status_update.emit("Processing uploaded file...")

        try:
            self.controller._pending_file_size = os.path.getsize(audio_path)
            self.controller._pending_audio_duration = None
            self._submit_transcription_job(audio_path)
        except Exception as exc:
            logger.error(f"Failed to process uploaded audio: {exc}")
            self.on_transcription_error(f"Failed to process audio: {exc}")

    def _maybe_cleanup_transcript(self, raw: str) -> tuple[str, Optional[str]]:
        """Optionally clean up ASR text; return (fixed, raw_or_none)."""
        settings = settings_manager.load_all_settings()
        enabled = settings.get(
            SettingsKey.TRANSCRIPT_CLEANUP_ENABLED,
            config.TRANSCRIPT_CLEANUP_ENABLED,
        )
        if not enabled or not raw or not raw.strip():
            return raw, None

        # Re-apply provider/model each run so Settings changes take effect
        # without restarting (a provider switch rebuilds the client).
        self._transcript_cleanup.configure(
            resolve_transcript_cleanup_provider(settings),
            resolve_transcript_cleanup_model(settings),
            resolve_transcript_cleanup_reasoning(settings),
        )
        if not self._transcript_cleanup.is_available():
            logger.warning(
                "Transcript cleanup enabled but unavailable; using raw text"
            )
            return raw, None

        self.controller.overlay_state_update.emit(OverlayState.CLEANING)
        self.controller.status_update.emit("Cleaning up...")
        prompt = resolve_transcript_cleanup_prompt(settings)
        fixed = self._transcript_cleanup.cleanup(raw, system_prompt=prompt)
        if fixed != raw:
            return fixed, raw
        return fixed, None

    def transcribe_audio_file(self, audio_path: str) -> None:
        """Transcribe a single audio file in a background thread."""
        try:
            if self.controller._pending_file_size is None:
                self.controller._pending_file_size = os.path.getsize(audio_path)
            self.controller.overlay_state_update.emit(OverlayState.TRANSCRIBING)
            self.controller.status_update.emit("Transcribing...")
            self.controller._transcription_start_time = time.time()
            raw = self.controller.current_backend.transcribe(audio_path)
            fixed, raw_text = self._maybe_cleanup_transcript(raw)
            self.controller.transcription_completed.emit(fixed, raw_text)
        except Exception as exc:
            logger.error(f"Transcription failed: {exc}")
            self.controller.transcription_failed.emit(str(exc))

    def transcribe_large_audio_file(self, audio_path: str) -> None:
        """Transcribe a large audio file by splitting it into chunks."""
        chunk_files = []
        if self.controller._pending_file_size is None:
            self.controller._pending_file_size = os.path.getsize(audio_path)
        self.controller._transcription_start_time = time.time()
        try:
            def progress_callback(message: str) -> None:
                self.controller.status_update.emit(message)

            chunk_files = audio_processor.split_audio_file(
                audio_path, progress_callback
            )
            if not chunk_files:
                raise Exception("Failed to split audio file")

            if hasattr(self.controller.current_backend, "transcribe_chunks"):
                self.controller.overlay_state_update.emit(OverlayState.TRANSCRIBING)
                self.controller.status_update.emit(
                    f"Transcribing {len(chunk_files)} chunks..."
                )
                raw = self.controller.current_backend.transcribe_chunks(
                    chunk_files
                )
            else:
                transcripts = []
                for index, chunk_file in enumerate(chunk_files):
                    self.controller.overlay_state_update.emit(OverlayState.TRANSCRIBING)
                    self.controller.status_update.emit(
                        f"Transcribing chunk {index + 1}/{len(chunk_files)}..."
                    )
                    transcripts.append(
                        self.controller.current_backend.transcribe(chunk_file)
                    )
                raw = audio_processor.combine_transcriptions(transcripts)

            fixed, raw_text = self._maybe_cleanup_transcript(raw)
            self.controller.transcription_completed.emit(fixed, raw_text)
        except Exception as exc:
            logger.error(f"Large audio transcription failed: {exc}")
            self.controller.transcription_failed.emit(str(exc))
        finally:
            try:
                audio_processor.cleanup_temp_files()
            except Exception as cleanup_error:
                logger.warning(
                    f"Failed to cleanup temp files: {cleanup_error}"
                )

    def on_transcription_complete(
        self, transcript: str, raw_text: Optional[str] = None
    ) -> None:
        """Handle transcription completion."""
        self.controller.ui_controller.set_transcript(transcript, raw=raw_text)
        self.controller.ui_controller.set_status("Transcription complete!")
        self.controller.overlay_state_update.emit(OverlayState.NONE)

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
                text=transcript,
                model=model_info,
                source_audio_path=self.controller._pending_audio_path,
                transcription_time=transcription_time,
                audio_duration=self.controller._pending_audio_duration,
                file_size=self.controller._pending_file_size,
                raw_text=raw_text,
            )
            self.controller.ui_controller.refresh_history()
            logger.info("Transcription saved to history")
        except Exception as exc:
            logger.error(f"Failed to save transcription to history: {exc}")
        finally:
            self.controller._pending_audio_path = None
            self.controller._pending_audio_duration = None
            self.controller._pending_file_size = None

        settings = settings_manager.load_all_settings()
        copy_clipboard = settings.get(SettingsKey.COPY_CLIPBOARD, True)
        auto_paste = settings.get(SettingsKey.AUTO_PASTE, True)

        # Synthetic paste posts a key event, which needs macOS Accessibility
        # permission. Without it, degrade to clipboard so the text isn't lost and
        # the user can paste manually with Cmd+V.
        paste_blocked = auto_paste and not is_accessibility_trusted()

        if copy_clipboard or paste_blocked:
            try:
                pyperclip.copy(transcript)
                logger.info("Transcription copied to clipboard")
            except Exception as exc:
                logger.error(f"Failed to copy to clipboard: {exc}")

        if auto_paste and not paste_blocked:
            try:
                send_paste()
                logger.info("Transcription auto-pasted")
                self.controller.ui_controller.set_status("Ready (Pasted)")
            except Exception as exc:
                logger.error(f"Failed to auto-paste: {exc}")
                self.controller.ui_controller.set_status(
                    "Transcription complete (paste failed)"
                )
        elif paste_blocked:
            logger.warning(
                "Auto-paste skipped: macOS Accessibility permission not granted."
            )
            self.controller.ui_controller.set_status(
                "Copied to clipboard — press Cmd+V (enable Accessibility to auto-paste)"
            )
        else:
            self.controller.ui_controller.set_status("Ready")

    def on_transcription_error(self, error_message: str) -> None:
        """Handle transcription error."""
        self.controller.ui_controller.set_status(f"Error: {error_message}")
        self.controller.ui_controller.set_transcript(f"Error: {error_message}")
        self.controller.overlay_state_update.emit(OverlayState.NONE)

    def on_model_changed(self, model_name: str) -> None:
        """Handle model selection change."""
        model_value = config.MODEL_VALUE_MAP.get(model_name)
        if model_value and model_value in self.controller.transcription_backends:
            self.controller.current_backend = self.controller.transcription_backends[
                model_value
            ]
            self.controller._current_model_name = model_value
            settings_manager.save_model_selection(model_value)
            logger.info(f"Switched to model: {model_value}")

            if model_value == "local_whisper":
                local_backend = self.controller.transcription_backends.get("local_whisper")
                if local_backend and hasattr(local_backend, "device_info"):
                    self.controller.ui_controller.set_device_info(
                        local_backend.device_info
                    )
                # A missing local model needs the download-consent flow the
                # moment the user selects this backend.
                self.controller.ensure_local_model_available()
            else:
                self.controller.ui_controller.set_device_info("")

            # Streaming preview requires Local Whisper; rebuild when backend changes.
            self.controller.streaming_runtime.reconfigure_streaming()

    def show_large_file_overlay(self, file_size_mb: float, is_splitting: bool) -> None:
        """Show the large-file overlay state."""
        overlay = self.controller.ui_controller.overlay
        overlay.set_large_file_info(file_size_mb)

        if is_splitting:
            overlay.show_at_cursor(overlay.STATE_LARGE_FILE_SPLITTING)
        else:
            overlay.show_at_cursor(overlay.STATE_LARGE_FILE_PROCESSING)

    def _submit_transcription_job(self, audio_path: str) -> None:
        backend = self.controller.current_backend
        if not backend.is_available() and getattr(backend, "is_model_missing", False):
            # Trigger the consent/download flow, but never transcribe with a
            # model the user has not approved downloading.
            self.controller.ensure_local_model_available()
            raise Exception(
                "Whisper model is not downloaded yet — approve the download "
                "and try again"
            )

        needs_splitting, file_size_mb = audio_processor.check_file_size(audio_path)
        should_split = (
            needs_splitting and self.controller.current_backend.requires_file_splitting
        )

        if should_split:
            logger.info(
                f"Large file ({file_size_mb:.2f} MB), backend requires splitting"
            )
            self.show_large_file_overlay(file_size_mb, is_splitting=True)
            self.controller.status_update.emit(
                f"Splitting large file ({file_size_mb:.1f} MB)..."
            )
            self.controller.executor.submit(
                self.transcribe_large_audio_file, audio_path
            )
        elif needs_splitting:
            logger.info(
                f"Large file ({file_size_mb:.2f} MB), processing without splitting"
            )
            self.show_large_file_overlay(file_size_mb, is_splitting=False)
            self.controller.status_update.emit(
                f"Processing large file ({file_size_mb:.1f} MB)..."
            )
            self.controller.executor.submit(self.transcribe_audio_file, audio_path)
        else:
            self.controller.executor.submit(self.transcribe_audio_file, audio_path)

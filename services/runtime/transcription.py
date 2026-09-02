"""Recording and transcription helpers for the application controller."""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import TYPE_CHECKING, Optional

from config import config
from services.hotkey_manager import is_accessibility_trusted, send_paste
from services.audio_processor import audio_processor
from services.history_manager import history_manager
from services.transcript_cleanup import CleanupInfo, TranscriptCleanup
try:
    from services.settings import (
        SettingsKey,
        compose_transcript_cleanup_prompt,
        resolve_transcript_cleanup_model,
        resolve_transcript_cleanup_prompt,
        resolve_transcript_cleanup_provider,
        resolve_transcript_cleanup_reasoning,
        resolve_transcript_cleanup_rules,
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

    def resolve_transcript_cleanup_rules(settings=None):
        return []

    def compose_transcript_cleanup_prompt(base_prompt, rules):
        return base_prompt

from ui_qt.overlay_state import OverlayState

if TYPE_CHECKING:
    from services.application_controller import ApplicationController

logger = logging.getLogger(__name__)

EMPTY_ASR_MESSAGE = "No speech detected (empty after VAD)"


class TranscriptionRuntime:
    """Owns recording flow and transcription job orchestration."""

    def __init__(self, controller: "ApplicationController"):
        self.controller = controller
        self._transcript_cleanup = TranscriptCleanup()
        self._job_lock = threading.Lock()
        self._job_active = False

    @property
    def has_active_job(self) -> bool:
        """Whether one recording result is queued, transcribing, or cleaning."""
        with self._job_lock:
            return self._job_active

    def _claim_job(self) -> bool:
        """Atomically reserve the single transcription workflow slot."""
        with self._job_lock:
            if self._job_active:
                return False
            self._job_active = True
            return True

    def _finish_job(self) -> None:
        with self._job_lock:
            self._job_active = False

    def _report_busy(self, action: str) -> None:
        message = f"A transcription is already in progress — wait before {action}"
        self.controller.status_update.emit(message)
        logger.info(message)

    def start_recording(self) -> None:
        if self.controller.is_meeting_active():
            self.controller.status_update.emit(
                "Meeting Mode is active — end the meeting to use dictation"
            )
            return
        readiness = getattr(self.controller, "transcription_readiness_message", None)
        message = readiness() if callable(readiness) else None
        if message:
            self.controller.status_update.emit(message)
            return
        if self.has_active_job:
            self._report_busy("starting another recording")
            return
        if self.controller.recorder.start_recording():
            logger.info("Recording started")
            self.controller.ui_controller.clear_transcription_stats()
            self.controller.ui_controller.main_window.clear_partial_transcription()
            self.controller.streaming_runtime.start_streaming_session()
            self.controller.recording_state_changed.emit(True)
            self.controller.overlay_state_update.emit(OverlayState.RECORDING)
            self.controller.status_update.emit("Recording...")
        else:
            reason = getattr(
                self.controller.recorder, "last_start_error", None
            ) or "Could not open the audio stream"
            logger.error("Failed to start recording: %s", reason)
            self.controller.recording_state_changed.emit(False)
            self.controller.overlay_state_update.emit(OverlayState.NONE)
            self.controller.status_update.emit(f"Failed to start recording: {reason}")

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

        # Reserve the workflow before post-roll/save work.  Once the recorder
        # flips to inactive, an upload can arrive from another UI thread; a
        # late claim would let it take the slot and could make this path clear
        # or overwrite that upload's metadata on an error.
        if not self._claim_job():
            self._report_busy("processing this recording")
            self.controller.overlay_state_update.emit(OverlayState.NONE)
            return

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
        self.controller._pending_source_name = "Quick Record"

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
        self.controller.streaming_runtime.cancel_streaming_session()
        self.controller.recording_state_changed.emit(False)
        self.controller.recorder.stop_recording()
        self.controller.recorder.clear_recording_data()
        self.controller.overlay_state_update.emit(OverlayState.CANCELING)
        self.controller.status_update.emit("Recording canceled")
        logger.info("Recording canceled")

    def _cancel_transcription(self) -> None:
        self.controller.current_backend.cancel_transcription()
        self.controller.overlay_state_update.emit(OverlayState.CANCELING)
        self.controller.status_update.emit("Transcription canceled")
        logger.info("Transcription canceled")

    def retranscribe_audio(self, audio_path: str) -> None:
        """Re-transcribe a saved recording."""
        if self.controller.is_meeting_active():
            self.controller.status_update.emit(
                "Meeting Mode is active — end it before retranscribing"
            )
            return
        if not os.path.exists(audio_path):
            logger.error(
                f"Audio file not found for re-transcription: {audio_path}"
            )
            self.controller.overlay_state_update.emit(OverlayState.NONE)
            self.controller.status_update.emit("Error: Audio file not found")
            return
        if self.controller.recorder.is_recording:
            self._report_busy("re-transcribing audio")
            return
        if not self._claim_job():
            self._report_busy("re-transcribing audio")
            return

        logger.info("Re-transcribing audio file: %s", audio_path)
        self.controller._pending_audio_path = None
        self.controller._pending_source_name = os.path.basename(audio_path)
        self.controller.overlay_state_update.emit(OverlayState.PROCESSING)
        self.controller.status_update.emit("Processing...")

        try:
            self.controller._pending_file_size = os.path.getsize(audio_path)
            self.controller._pending_audio_duration = None
            self._submit_transcription_job(audio_path)
        except Exception as exc:
            logger.error(f"Failed to start re-transcription: {exc}")
            self.on_transcription_error(f"Failed to process audio: {exc}")

    def upload_audio_file(
        self, audio_path: str, duration_seconds: Optional[float] = None
    ) -> None:
        """Transcribe an uploaded audio file."""
        if self.controller.is_meeting_active():
            self.controller.status_update.emit(
                "Meeting Mode is active — end it before uploading audio"
            )
            return
        if not os.path.exists(audio_path):
            logger.error(f"Uploaded audio file not found: {audio_path}")
            self.controller.overlay_state_update.emit(OverlayState.NONE)
            self.controller.status_update.emit("Error: Audio file not found")
            return
        if self.controller.recorder.is_recording:
            self._report_busy("uploading audio")
            return
        if not self._claim_job():
            self._report_busy("uploading audio")
            return

        logger.info(f"Processing uploaded audio file: {audio_path}")
        self.controller._pending_audio_path = None
        self.controller._pending_source_name = os.path.basename(audio_path)
        self.controller.overlay_state_update.emit(OverlayState.PROCESSING)
        self.controller.status_update.emit("Processing uploaded file...")

        try:
            self.controller._pending_file_size = os.path.getsize(audio_path)
            self.controller._pending_audio_duration = duration_seconds
            self._submit_transcription_job(audio_path)
        except Exception as exc:
            logger.error(f"Failed to process uploaded audio: {exc}")
            self.on_transcription_error(f"Failed to process audio: {exc}")

    def _maybe_cleanup_transcript(
        self, raw: str
    ) -> tuple[str, Optional[str], Optional[CleanupInfo]]:
        """Return fixed text, distinct raw text, and successful cleanup metadata."""
        settings = settings_manager.load_all_settings()
        enabled = settings.get(
            SettingsKey.TRANSCRIPT_CLEANUP_ENABLED,
            config.TRANSCRIPT_CLEANUP_ENABLED,
        )
        if not enabled or not raw or not raw.strip():
            return raw, None, None

        # Re-apply provider/model each run so Model Manager changes take effect
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
            return raw, None, None

        self.controller.overlay_state_update.emit(OverlayState.CLEANING)
        self.controller.status_update.emit("Cleaning up...")
        prompt = compose_transcript_cleanup_prompt(
            resolve_transcript_cleanup_prompt(settings),
            resolve_transcript_cleanup_rules(settings),
        )
        fixed = self._transcript_cleanup.cleanup(raw, system_prompt=prompt)
        # A changed transcript also proves cleanup ran, covering stubs that
        # bypass the real cleanup() and never touch last_error.
        cleaned = self._transcript_cleanup.last_error is None or fixed != raw
        info = (
            CleanupInfo(
                provider=self._transcript_cleanup.provider,
                model=self._transcript_cleanup.model,
            )
            if cleaned
            else None
        )
        if fixed != raw:
            return fixed, raw, info
        return fixed, None, info

    def transcribe_audio_file(self, audio_path: str) -> None:
        try:
            if self.controller._pending_file_size is None:
                self.controller._pending_file_size = os.path.getsize(audio_path)
            self.controller.overlay_state_update.emit(OverlayState.TRANSCRIBING)
            self.controller.status_update.emit("Transcribing...")
            self.controller._transcription_start_time = time.time()
            raw = self.controller.current_backend.transcribe(audio_path)
            fixed, raw_text, cleanup_info = self._maybe_cleanup_transcript(raw)
            self.controller.transcription_completed.emit(fixed, raw_text, cleanup_info)
        except Exception as exc:
            logger.error(f"Transcription failed: {exc}")
            self.controller.transcription_failed.emit(str(exc))

    def transcribe_large_audio_file(self, audio_path: str) -> None:
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

            fixed, raw_text, cleanup_info = self._maybe_cleanup_transcript(raw)
            self.controller.transcription_completed.emit(fixed, raw_text, cleanup_info)
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
        self,
        transcript: str,
        raw_text: Optional[str] = None,
        cleanup_info: Optional[CleanupInfo] = None,
    ) -> None:
        is_empty = not (transcript or "").strip()
        display_text = transcript if not is_empty else EMPTY_ASR_MESSAGE
        self.controller.ui_controller.set_transcript(
            display_text, raw=raw_text
        )
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

        source_name = getattr(self.controller, "_pending_source_name", None)

        if is_empty:
            logger.info(
                "Empty ASR result; skipping history, clipboard, and paste"
            )
            self.controller.ui_controller.set_status(EMPTY_ASR_MESSAGE)
            self._clear_pending_audio_metadata()
            self._finish_job()
            return

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
                cleanup_provider=cleanup_info.provider if cleanup_info else None,
                cleanup_model=cleanup_info.model if cleanup_info else None,
                source_name=source_name,
            )
            self.controller.ui_controller.refresh_history()
            logger.info("Transcription saved to history")
        except Exception as exc:
            logger.error(f"Failed to save transcription to history: {exc}")
        finally:
            self._clear_pending_audio_metadata()

        try:
            self._apply_clipboard_and_paste(transcript)
        finally:
            self._finish_job()

    def _clear_pending_audio_metadata(self) -> None:
        """Drop one-shot metadata attached to the current transcription job."""
        self.controller._pending_audio_path = None
        self.controller._pending_audio_duration = None
        self.controller._pending_file_size = None
        self.controller._pending_source_name = None

    def _apply_clipboard_and_paste(self, transcript: str) -> None:
        """Copy and optionally paste only after a successful clipboard write."""
        settings = settings_manager.load_all_settings()
        copy_clipboard = settings.get(SettingsKey.COPY_CLIPBOARD, True)
        auto_paste = settings.get(SettingsKey.AUTO_PASTE, True)

        # Synthetic paste posts a key event, which needs macOS Accessibility
        # permission. Without it, degrade to clipboard so the text isn't lost and
        # the user can paste manually with Cmd+V.
        paste_blocked = auto_paste and not is_accessibility_trusted()

        if auto_paste and not paste_blocked:
            stage = self.controller.ui_controller.stage_transcript_for_paste(
                transcript
            )
            if not stage.written:
                logger.error("Failed to copy transcription for auto-paste")
                self.controller.ui_controller.set_status(
                    "Transcription complete (copy failed)"
                )
                return

            logger.info("Transcription copied to clipboard for auto-paste")
            try:
                send_paste()
                logger.info("Transcription auto-pasted")
            except Exception as exc:
                logger.error(f"Failed to auto-paste: {exc}")
                if not self.controller.ui_controller.commit_transcript_clipboard(
                    stage, transcript
                ):
                    logger.warning(
                        "Could not leave transcription in clipboard after paste failure"
                    )
                self.controller.ui_controller.set_status(
                    "Transcription complete (paste failed)"
                )
                return

            if stage.restore_unavailable:
                logger.warning(
                    "Transcription pasted, but the previous clipboard could not be captured"
                )
                self.controller.ui_controller.set_status(
                    "Ready (Pasted; clipboard restore unavailable)"
                )
                return

            self.controller.ui_controller.set_status("Ready (Pasted)")
            if stage.lease is not None:
                self.controller.ui_controller.schedule_clipboard_restore(stage)
            return

        should_copy = copy_clipboard or paste_blocked
        copy_ok = False
        if should_copy:
            copy_ok = bool(
                self.controller.ui_controller.copy_to_clipboard(transcript)
            )
            if copy_ok:
                logger.info("Transcription copied to clipboard")
            else:
                logger.error("Failed to copy to clipboard")

        if paste_blocked:
            if copy_ok:
                logger.warning(
                    "Auto-paste skipped: macOS Accessibility permission not granted."
                )
                self.controller.ui_controller.set_status(
                    "Copied to clipboard — press Cmd+V "
                    "(enable Accessibility to auto-paste)"
                )
            else:
                self.controller.ui_controller.set_status(
                    "Transcription complete (copy failed)"
                )
            return

        if copy_clipboard and not copy_ok:
            self.controller.ui_controller.set_status(
                "Transcription complete (copy failed)"
            )
            return

        self.controller.ui_controller.set_status("Ready")

    def on_transcription_error(self, error_message: str) -> None:
        recovery_name = None
        pending_audio = self.controller._pending_audio_path
        if pending_audio and os.path.isfile(pending_audio):
            try:
                recovery_name = history_manager.preserve_recording(pending_audio)
            except Exception as exc:
                logger.error("Failed to preserve audio after transcription error: %s", exc)
        status = f"Error: {error_message}"
        if recovery_name:
            status += f" — audio saved in Recordings as {recovery_name}"
        self.controller.ui_controller.set_status(status)
        self.controller.ui_controller.set_transcript(f"Error: {error_message}")
        self.controller.overlay_state_update.emit(OverlayState.NONE)
        self.controller._transcription_start_time = None
        self._clear_pending_audio_metadata()
        self._finish_job()

    def on_model_changed(self, model_name: str) -> None:
        if self.controller.is_meeting_active():
            self.controller.status_update.emit(
                "End the meeting before changing transcription models"
            )
            return
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
                        local_backend.device_info,
                        local_backend.is_available(),
                    )
                # A missing local model needs the download-consent flow the
                # moment the user selects this backend.
                self.controller.ensure_local_model_available()
            else:
                self.controller.ui_controller.set_device_info("", None)

            # Streaming preview requires Local Whisper; rebuild when backend changes.
            self.controller.streaming_runtime.reconfigure_streaming()

    def show_large_file_state(self, file_size_mb: float, is_splitting: bool) -> None:
        self.controller.ui_controller.show_large_file_state(
            file_size_mb, is_splitting
        )

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
            self.show_large_file_state(file_size_mb, is_splitting=True)
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
            self.show_large_file_state(file_size_mb, is_splitting=False)
            self.controller.status_update.emit(
                f"Processing large file ({file_size_mb:.1f} MB)..."
            )
            self.controller.executor.submit(
                self.transcribe_audio_file, audio_path
            )
        else:
            self.controller.executor.submit(
                self.transcribe_audio_file, audio_path
            )

"""Recording and transcription helpers for the application controller."""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import TYPE_CHECKING, Optional

from config import config
from services import audio_player, dictation_pipeline, recognition_context
from services.dictation_pipeline import JobMode
from services.hotkey_manager import is_accessibility_trusted, send_paste
from services.history_manager import history_manager
from services.transcript_cleanup import (
    CANCELED_REASON,
    CleanupInfo,
    TranscriptCleanup,
)
from services.cleanup_profiles import find_cleanup_profile
from services.incremental_dictation import IncrementalDictation
from services.batch_upload import (
    BatchItemResult,
    BatchResult,
    BatchUploadRequest,
    batch_source_name,
    format_batch_transcript,
    join_raw_parts,
)
from services.settings import (
    SETTING_DEFAULTS,
    SettingsKey,
    resolve_transcript_cleanup_model,
    resolve_transcript_cleanup_provider,
    resolve_transcript_cleanup_reasoning,
    resolve_transcript_cleanup_rules,
    setting_value,
    settings_manager,
)

from ui_qt.overlay_state import OverlayState

if TYPE_CHECKING:
    from services.application_controller import ApplicationController

logger = logging.getLogger(__name__)

EMPTY_ASR_MESSAGE = "No speech detected (empty after VAD)"
#: Shown when the full pass found nothing but the streaming preview did. Names
#: the source, because the text on screen is the preview's and is not saved.
EMPTY_PREVIEW_FALLBACK_MESSAGE = (
    "No speech detected — showing the live preview (not saved)"
)


class TranscriptionRuntime:
    """Owns recording flow and transcription job orchestration."""

    def __init__(self, controller: "ApplicationController"):
        self.controller = controller
        self._job_lock = threading.Lock()
        self._capture_lock = threading.RLock()
        self._job_active = False
        # Backends reset their own cancel flag on every transcribe() call, so
        # a cancel that lands between files of a batch would be lost; this
        # event outlives the individual calls.
        self._cancel_requested = threading.Event()
        # The same event ends a cleanup request's wait, so a Cancel during
        # "Cleaning up..." frees the job instead of waiting on the provider.
        self._transcript_cleanup = TranscriptCleanup(
            cancel_event=self._cancel_requested, defer_client=True
        )
        # Why the most recent cleanup pass fell back to raw text, or None.
        self._last_cleanup_failure: Optional[str] = None
        # Dictation lands in whatever application the hotkey was pressed in, so
        # its result is copied and pasted. An upload is started from inside the
        # window and its result stays there; the Upload File tab has its own
        # Copy buttons. Set per job after the slot is claimed.
        self._deliver_to_clipboard = True
        self._recording_profile = None
        self._profile_settings = None
        # The recording's DictationJob from its start until its stop claims
        # the slot; uploads never inherit one.
        self._job: Optional[dictation_pipeline.DictationJob] = None
        # The job the claimed slot is working on. The worker, delivery and
        # history read only this, copied to a local first.
        self._active_job: Optional[dictation_pipeline.DictationJob] = None
        # Rich-text alternative for the transcript being delivered, set by
        # the worker before it emits transcription_completed.
        self._delivery_html = ""
        # A rewrite claimed the slot and has not been submitted yet.
        self._rewrite_pending = False
        # Decodes a long dictation's completed windows while it is recorded.
        self._incremental = IncrementalDictation()
        self._stop_started_at = 0.0
        self._cancel_started_at = 0.0

    @property
    def has_active_job(self) -> bool:
        """Whether one recording result is queued, transcribing, or cleaning."""
        with self._job_lock:
            return self._job_active

    def begin_shutdown(self) -> None:
        self._cancel_requested.set()
        self._incremental.discard()

    def _claim_job(self, job: Optional[dictation_pipeline.DictationJob] = None) -> bool:
        """Atomically reserve the single transcription workflow slot for ``job``."""
        with self._job_lock:
            if (self._job_active or getattr(self.controller, '_shutting_down', False) is True
                    or getattr(self.controller, '_backup_in_progress', False) is True):
                return False
            self._job_active = True
            self._active_job = job
            self._cancel_requested.clear()
        self._rearm_remote_engine()
        return True

    def _forget_recording(self) -> None:
        """Drop the recording's job and profile so no later job inherits them."""
        with self._job_lock:
            self._job = None
            self._recording_profile = None
            self._profile_settings = None

    def _rearm_remote_engine(self) -> None:
        """Clear a remote engine's cancel once no job it was meant for is left.

        A local engine is reloaded after a cancel, which clears the flag. A
        remote one keeps its connection instead
        (RemoteSpeechBackend.cancel_transcription), so the next recording or
        job clears it; by then the canceled job has given up the slot.
        """
        backend = getattr(self.controller, "current_backend", None)
        if getattr(backend, "is_remote", False) and backend.should_cancel:
            backend.reset_cancel_flag()

    def _finish_job(self) -> None:
        self._stop_started_at = 0.0
        if self._cancel_started_at:
            from services.diagnostics import record_metrics
            record_metrics(cancel_s=time.monotonic() - self._cancel_started_at)
            self._cancel_started_at = 0.0
        with self._job_lock:
            self._recording_profile = None
            self._profile_settings = None
            self._job = None
            self._active_job = None
            self._delivery_html = ""
            self._rewrite_pending = False
            self._job_active = False
            self._deliver_to_clipboard = True

    def _report_busy(self, action: str) -> None:
        message = f"A transcription is already in progress — wait before {action}"
        self.controller.status_update.emit(message)
        logger.info(message)

    def recover_recordings(self) -> None:
        """Startup worker: make interrupted dictation audio available in Recordings."""
        from services.isolated import cleanup_orphaned_preview_audio
        from services.recording_journal import recover_recordings
        cleanup_orphaned_preview_audio()
        recovered = recover_recordings(config.RECORDED_AUDIO_FILE, config.RECORDINGS_FOLDER)
        if recovered:
            self.controller.status_update.emit(
                f'Recovered {len(recovered)} interrupted recording(s) in Recordings'
            )

    def on_capture_error(self, message: str) -> None:
        """Qt slot: stop previews and preserve partial audio, never auto-paste it."""
        recorder = self.controller.recorder
        if (getattr(recorder, 'last_capture_error', None) != message
                or getattr(recorder, 'capture_canceled', False) is True):
            return  # queued notification from a canceled/replaced capture
        self.controller.recording_state_changed.emit(False)
        self.controller.status_update.emit(message + ' — preserving captured audio')
        self.controller.streaming_runtime.cancel_streaming_session()
        self._incremental.discard()
        if self.has_active_job:
            return  # finish_recording_job observes last_capture_error after the save
        if self._claim_job(self._job):
            self.controller.executor.submit(self.finish_recording_job)

    def start_recording(
        self,
        profile_id: str = "",
        mode: str = JobMode.DICTATION,
        *,
        selection=None,
    ) -> bool:
        """Start a recording; False when it was refused or could not start.

        Args:
            profile_id: Cleanup profile to format this dictation with.
            mode: A JobMode; Command Mode records an instruction.
            selection: For Command Mode, the selected text to rewrite, as a
                str or a Future[str].
        """
        with self._capture_lock:
            return self._start_recording(profile_id, mode, selection)

    def _begin_job(self, mode: str, settings, selection) -> dictation_pipeline.DictationJob:
        """The job for a recording that just started; never raises."""
        try:
            if settings is None:
                settings = settings_manager.load_all_settings()
            return dictation_pipeline.begin_job(mode, settings, selection=selection)
        except Exception:
            logger.debug("Dictation job context unavailable", exc_info=True)
            return dictation_pipeline.DictationJob(mode=mode)

    def _start_recording(self, profile_id: str, mode: str = JobMode.DICTATION,
                         selection=None) -> bool:
        if getattr(self.controller, "_backup_in_progress", False) is True:
            self.controller.status_update.emit("Wait for backup or restore preparation to finish")
            return False
        if self.controller.is_meeting_active():
            self.controller.status_update.emit(
                "Meeting Mode is active — end the meeting to use dictation"
            )
            return False
        readiness = getattr(self.controller, "transcription_readiness_message", None)
        message = readiness() if callable(readiness) else None
        if message:
            self.controller.status_update.emit(message)
            return False
        if self.has_active_job:
            self._report_busy("starting another recording")
            return False
        if self.controller.recorder.is_recording:
            return False
        # Before the preview and early decoding look at the engine.
        self._rearm_remote_engine()
        settings = settings_manager.load_all_settings() if profile_id else None
        profile = find_cleanup_profile(settings, profile_id) if profile_id else None
        if profile_id and profile is None:
            self.controller.status_update.emit("Cleanup profile no longer exists")
            return False
        try:
            # The output device and a recording would fight over audio.
            audio_player.stop_playback()
        except Exception:
            logger.debug("Could not stop playback before recording", exc_info=True)
        if self.controller.recorder.start_recording():
            # Snapshot before publishing Recording: edits and other shortcuts
            # cannot replace the format of a recording already in progress.
            self._recording_profile = profile
            self._profile_settings = settings
            # Before the preview and early decoding, which read its
            # recognition context, and so the focus capture runs while the
            # user speaks.
            self._job = self._begin_job(mode, settings, selection)
            logger.info("Recording started")
            self.controller.ui_controller.clear_transcription_stats()
            self.controller.ui_controller.main_window.clear_partial_transcription()
            self.controller.streaming_runtime.start_streaming_session()
            self._incremental.start(self.controller, recognition=self._job.recognition)
            self.controller.recording_state_changed.emit(True)
            if mode == JobMode.COMMAND:
                self.controller.overlay_state_update.emit(OverlayState.COMMAND_LISTENING)
                self.controller.status_update.emit("Command Mode · Listening...")
            else:
                self.controller.overlay_state_update.emit(OverlayState.RECORDING)
                self.controller.status_update.emit(
                    f"Recording · {profile.name}..." if profile else "Recording..."
                )
            # Auto-paste copies the user's clipboard so it can put it back.
            # Take that copy while the user speaks instead of in front of the
            # paste; it is queued to the Qt thread and never delays this start.
            # Command Mode reads the selection through the clipboard first,
            # so its snapshot would no longer be the user's.
            if mode == JobMode.DICTATION and settings_manager.get(
                SettingsKey.AUTO_PASTE, SETTING_DEFAULTS[SettingsKey.AUTO_PASTE]
            ):
                self.controller.ui_controller.prefetch_clipboard_snapshot()
            return True
        else:
            self._job = None
            reason = getattr(
                self.controller.recorder, "last_start_error", None
            ) or "Could not open the audio stream"
            logger.error("Failed to start recording: %s", reason)
            self.controller.ui_controller.discard_clipboard_prefetch()
            self.controller.recording_state_changed.emit(False)
            self.controller.overlay_state_update.emit(OverlayState.NONE)
            self.controller.status_update.emit(f"Failed to start recording: {reason}")
            return False

    def stop_recording(self) -> None:
        """Stop audio recording and start transcription."""
        with self._capture_lock:
            self._stop_recording()

    def _stop_recording(self) -> None:
        if self.controller.recorder.capture_canceled is True:
            # A cancel already discarded this capture and the stream is only
            # closing; a stop now (the record hotkey, or a push-and-hold
            # release) must not claim a job and transcribe what is left.
            logger.info("Stop ignored: this recording was canceled")
            return
        if self.controller._streaming_enabled:
            # Dismiss preview overlay immediately so the classic waveform
            # processing/transcribing states are the only post-stop UI.
            self.controller.streaming_overlay_hide.emit()

        self.controller._pending_streaming_text = ""
        self.controller.streaming_runtime.begin_stop_streaming_session()

        if not self.controller.recorder.stop_recording():
            self._forget_recording()
            self.controller.overlay_state_update.emit(OverlayState.NONE)
            self.controller.status_update.emit("Failed to stop recording")
            return

        self.controller.recording_state_changed.emit(False)
        self.controller.overlay_state_update.emit(OverlayState.PROCESSING)
        self.controller.status_update.emit("Processing...")
        self._stop_started_at = time.monotonic()

        # Reserve the workflow before post-roll/save work.  Once the recorder
        # flips to inactive, an upload can arrive from another UI thread; a
        # late claim would let it take the slot and could make this path clear
        # or overwrite that upload's metadata on an error.
        if not self._claim_job(self._job):
            self._forget_recording()
            self._report_busy("processing this recording")
            self.controller.overlay_state_update.emit(OverlayState.NONE)
            return

        # Everything left is blocking: capture keeps running for
        # POST_ROLL_MS after the stop request, so waiting it out here would
        # freeze the Qt thread for over a second — exactly while the
        # "Processing" overlay is meant to be animating — and the WAV write
        # follows it. Both go to a worker; the job slot claimed above is what
        # keeps a second recording or upload out in the meantime.
        self.controller.executor.submit(self.finish_recording_job)

    def finish_recording_job(self) -> None:
        """Post-roll wait, WAV write, and transcription, off the Qt thread.

        Runs on an executor worker, so every UI report here goes through a
        signal. Owns the job slot claimed by ``stop_recording`` and must
        release it on every exit — ``on_transcription_error`` does that for
        the failure paths.
        """
        try:
            stop_completed = False
            try:
                stop_completed = bool(
                    self.controller.recorder.wait_for_stop_completion()
                )
            finally:
                # The window preview stops without decoding its unfinished
                # window, so the final decode starts at once; that window is
                # decoded only if the transcript comes back empty (see
                # _complete_preview_fallback). A native stream still flushes
                # here, which can block, so this stays off Qt.
                self.controller._pending_streaming_text = (
                    self.controller.streaming_runtime.stop_streaming_session()
                )

            if self._cancel_requested.is_set():
                # Cancel landed during post-roll: _cancel_recording discarded
                # the capture, and transcribing what is left would paste audio
                # the user had just thrown away.
                self._abandon_canceled_job("during post-roll")
                return

            if not stop_completed:
                message = (
                    "Recording did not finish stopping; captured audio was "
                    "kept for recovery."
                )
                fail_capture = getattr(self.controller.recorder, '_fail_capture', None)
                if callable(fail_capture):
                    fail_capture(message)
                self.controller.transcription_failed.emit(message)
                return

            if not self.controller.recorder.has_recording_data():
                logger.error("No recording data available")
                self.controller.transcription_failed.emit(
                    getattr(self.controller.recorder, 'last_capture_error', None)
                    or "No audio data recorded"
                )
                return

            if not self.controller.recorder.save_recording(allow_incomplete=True):
                logger.error("Failed to save recording")
                self.controller.transcription_failed.emit(
                    getattr(self.controller.recorder, 'last_capture_error', None)
                    or "Failed to save audio file; recovery copy kept"
                )
                return

            if not os.path.exists(config.RECORDED_AUDIO_FILE):
                logger.error(f"Audio file not found: {config.RECORDED_AUDIO_FILE}")
                self.controller.transcription_failed.emit("Audio file not created")
                return

            file_size = os.path.getsize(config.RECORDED_AUDIO_FILE)
            logger.info(f"Audio file size: {file_size} bytes")
            if file_size < 100:
                logger.error(f"Audio file too small: {file_size} bytes")
                self.controller.transcription_failed.emit("Audio file is empty or corrupted")
                return

            self.controller._pending_audio_path = config.RECORDED_AUDIO_FILE
            self.controller._pending_audio_duration = (
                self.controller.recorder.get_recording_duration()
            )
            self.controller._pending_file_size = file_size
            job = self._active_job
            self.controller._pending_source_name = (
                "Command Mode" if job is not None and job.mode == JobMode.COMMAND
                else f"Quick Record · {self._recording_profile.name}"
                if self._recording_profile else "Quick Record"
            )

            capture_error = getattr(self.controller.recorder, 'last_capture_error', None)
            if isinstance(capture_error, str) and capture_error:
                self.controller.transcription_failed.emit(capture_error)
                return

            if self._cancel_requested.is_set():
                # Cancel landed while the preview stopped or the WAV saved,
                # when no engine was running for _cancel to interrupt.
                self._abandon_canceled_job("before transcription")
                return

            logger.info(
                "Transcription started. Duration: "
                f"{self.controller.recorder.get_recording_duration():.2f}s"
            )
            self._run_transcription_job(config.RECORDED_AUDIO_FILE)
        except Exception as exc:
            logger.error(f"Failed to start transcription: {exc}")
            self.controller.transcription_failed.emit(f"Failed to process audio: {exc}")

    def _run_transcription_job(self, audio_path: str) -> None:
        """Transcribe on the worker thread this job already runs on.

        The mirror of ``_submit_transcription_job``, which runs on the Qt
        thread and hands the same work to the executor.
        """
        self._raise_if_canceled()
        self._require_backend_ready()
        self.transcribe_audio_file(audio_path)

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
        with self._capture_lock:
            self._cancel()

    def _cancel(self) -> None:
        logger.info(f"Cancel called. Recording: {self.controller.recorder.is_recording}")
        self._cancel_started_at = time.monotonic() if (
            self.controller.recorder.is_recording or self.has_active_job
        ) else 0.0

        if self.controller.recorder.is_recording:
            self._cancel_recording()
        elif self.controller.current_backend and self.controller.current_backend.is_transcribing:
            self._cancel_requested.set()
            self._cancel_transcription()
        else:
            if self.has_active_job:
                self._cancel_requested.set()
            self.controller.overlay_state_update.emit(OverlayState.CANCELING)
            self.controller.status_update.emit("Canceled")

    def _cancel_recording(self) -> None:
        if self.has_active_job:
            # Only a stop claims the job while the recorder still runs, so this
            # is post-roll; finish_recording_job checks the flag before saving.
            self._cancel_requested.set()
        self.controller.streaming_runtime.cancel_streaming_session()
        self._incremental.discard()
        self.controller.recording_state_changed.emit(False)
        self.controller.recorder.cancel_recording()
        self.controller.ui_controller.discard_clipboard_prefetch()
        self._forget_recording()
        self.controller.overlay_state_update.emit(OverlayState.CANCELING)
        self.controller.status_update.emit("Recording canceled")
        logger.info("Recording canceled")
        if not self.has_active_job and self._cancel_started_at:
            from services.diagnostics import record_metrics
            record_metrics(cancel_s=time.monotonic() - self._cancel_started_at)
            self._cancel_started_at = 0.0

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
        self._forget_recording()

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
        self._forget_recording()

        logger.info(f"Processing uploaded audio file: {audio_path}")
        self._deliver_to_clipboard = False
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

    def upload_audio_files(self, request: BatchUploadRequest) -> None:
        """Transcribe several uploaded files as one job.

        The job slot is held for the whole batch and the files run one after
        another, because the audio processor's temp files are global and the
        executor is shared with model loads. A single file takes the ordinary
        upload path so its behavior is unchanged.
        """
        if not request.items:
            self.controller.status_update.emit("No audio files to transcribe")
            return
        if len(request.items) == 1:
            item = request.items[0]
            self.upload_audio_file(item.audio_path, item.duration_seconds)
            return
        if self.controller.is_meeting_active():
            self.controller.status_update.emit(
                "Meeting Mode is active — end it before uploading audio"
            )
            return
        for item in request.items:
            if not os.path.exists(item.audio_path):
                logger.error(f"Uploaded audio file not found: {item.audio_path}")
                self.controller.overlay_state_update.emit(OverlayState.NONE)
                self.controller.status_update.emit(
                    f"Error: Audio file not found: {item.source_name}"
                )
                return
        if self.controller.recorder.is_recording:
            self._report_busy("uploading audio")
            return
        if not self._claim_job():
            self._report_busy("uploading audio")
            return
        self._forget_recording()

        logger.info("Processing %d uploaded audio files", len(request.items))
        self._deliver_to_clipboard = False
        # Batches carry their metadata in the request and result objects; the
        # one-shot _pending_* slots stay empty so nothing stale leaks into a
        # later single-file job.
        self._clear_pending_audio_metadata()
        self.controller.overlay_state_update.emit(OverlayState.PROCESSING)
        self.controller.status_update.emit(
            f"Processing {len(request.items)} uploaded files..."
        )
        try:
            self._require_backend_ready()
            self.controller.executor.submit(self.transcribe_batch, request)
        except Exception as exc:
            logger.error(f"Failed to process uploaded audio files: {exc}")
            self.on_transcription_error(f"Failed to process audio: {exc}")

    def transcribe_batch(self, request: BatchUploadRequest) -> None:
        """Worker for a multi-file upload; strictly serial."""
        started = time.time()
        total = len(request.items)
        results: list[BatchItemResult] = []
        raw_parts: list[str] = []
        canceled = False
        try:
            for position, item in enumerate(request.items, start=1):
                if self._cancel_requested.is_set():
                    canceled = True
                    break
                self.controller.batch_progress.emit(
                    position, total, item.source_name
                )
                item_started = time.time()
                file_size = self._file_size_or_none(item.audio_path)
                try:
                    raw = self._transcribe_path(item.audio_path)
                except Exception as exc:
                    if self._cancel_requested.is_set():
                        canceled = True
                        break
                    if request.combine:
                        # One missing part invalidates a stitched transcript.
                        raise
                    logger.error(
                        "Batch file %s failed: %s", item.source_name, exc
                    )
                    results.append(
                        BatchItemResult(
                            item,
                            error=str(exc),
                            elapsed_s=time.time() - item_started,
                            file_size=file_size,
                        )
                    )
                    self.controller.batch_item_finished.emit(position, False, "")
                    continue

                item_asr_elapsed = time.time() - item_started
                # A combined job has one transcript, so its rows get no text
                # of their own: the per-file ASR is an unfinished part of it.
                own_transcript = ""
                if request.combine:
                    raw_parts.append(raw)
                    results.append(
                        BatchItemResult(
                            item,
                            text=raw,
                            elapsed_s=item_asr_elapsed,
                            file_size=file_size,
                        )
                    )
                else:
                    fixed, raw_text, info = self._maybe_cleanup_transcript(
                        raw, batch_context=request.batch_context(item)
                    )
                    item_cleanup_elapsed = (
                        info.elapsed_s if info is not None else 0.0
                    )
                    results.append(
                        BatchItemResult(
                            item,
                            text=fixed,
                            raw_text=raw_text,
                            cleanup_provider=info.provider if info else None,
                            cleanup_model=info.model if info else None,
                            elapsed_s=item_asr_elapsed,
                            cleanup_elapsed_s=item_cleanup_elapsed,
                            file_size=file_size,
                        )
                    )
                    own_transcript = fixed.strip()
                self.controller.batch_item_finished.emit(
                    position, True, own_transcript
                )

            total_elapsed = time.time() - started
            total_transcription_time = sum(r.elapsed_s for r in results)
            if request.combine:
                if canceled:
                    self.controller.transcription_failed.emit(
                        "Transcription canceled"
                    )
                    return
                joined = join_raw_parts(raw_parts)
                fixed, raw_text, info = self._maybe_cleanup_transcript(
                    joined,
                    batch_context=request.batch_context(),
                    timeout_s=config.TRANSCRIPT_BATCH_CLEANUP_TIMEOUT_S,
                )
                combined_cleanup_elapsed = (
                    info.elapsed_s if info is not None else 0.0
                )
                result = BatchResult(
                    request=request,
                    items=tuple(results),
                    combined_text=fixed,
                    combined_raw_text=raw_text,
                    combined_cleanup_provider=info.provider if info else None,
                    combined_cleanup_model=info.model if info else None,
                    cleanup_error=self._last_cleanup_failure,
                    total_elapsed_s=total_elapsed,
                    transcription_time_s=total_transcription_time,
                    cleanup_time_s=combined_cleanup_elapsed,
                )
            else:
                total_cleanup_time = sum(r.cleanup_elapsed_s for r in results)
                result = BatchResult(
                    request=request,
                    items=tuple(results),
                    canceled=canceled,
                    total_elapsed_s=total_elapsed,
                    transcription_time_s=total_transcription_time,
                    cleanup_time_s=total_cleanup_time,
                )
            self.controller.batch_completed.emit(result)
        except Exception as exc:
            logger.error(f"Batch transcription failed: {exc}")
            self.controller.transcription_failed.emit(str(exc))

    @staticmethod
    def _file_size_or_none(audio_path: str) -> Optional[int]:
        try:
            return os.path.getsize(audio_path)
        except OSError:
            return None

    def _transcribe_path(self, audio_path: str) -> str:
        """ASR for one file of a batch, on the worker thread."""
        backend = self.controller.current_backend
        self.controller.overlay_state_update.emit(OverlayState.PROCESSING)
        self._announce_transcription(backend, audio_path)
        return backend.transcribe(audio_path)

    def _announce_transcription(self, backend, audio_path: str) -> None:
        """Show the stage a file starts in, from the worker thread.

        A backend that splits this file (the OpenAI API, over its upload
        limit) starts with the large-file notice, and reports the steps after
        it through ``report_backend_progress``; every other file goes straight
        to transcribing, whatever its size.
        """
        probe = getattr(backend, "large_file_size_mb", None)
        file_size_mb = probe(audio_path) if probe is not None else None
        if file_size_mb is None:
            self.controller.overlay_state_update.emit(OverlayState.TRANSCRIBING)
            self.controller.status_update.emit("Transcribing...")
            return
        logger.info(f"Large file ({file_size_mb:.2f} MB), backend splits it")
        self.controller.large_file_detected.emit(file_size_mb)
        self.controller.status_update.emit(
            f"Splitting large file ({file_size_mb:.1f} MB)..."
        )

    def report_backend_progress(
        self, message: str, transcribing: bool = False
    ) -> None:
        """A backend's step inside ``transcribe`` (its ``on_progress``)."""
        if transcribing:
            self.controller.overlay_state_update.emit(OverlayState.TRANSCRIBING)
        self.controller.status_update.emit(message)

    def on_batch_complete(self, result: BatchResult) -> None:
        request = result.request
        ui = self.controller.ui_controller
        succeeded = [r for r in result.items if r.succeeded]
        saveable = [r for r in succeeded if r.text.strip()]

        if request.combine:
            display = result.combined_text or ""
            raw_display = result.combined_raw_text
            is_empty = not display.strip()
            if is_empty:
                display = EMPTY_ASR_MESSAGE
                raw_display = None
        else:
            if not succeeded:
                if result.canceled:
                    self.on_transcription_error("Transcription canceled")
                else:
                    first_error = result.items[0].error if result.items else "no files"
                    self.on_transcription_error(
                        f"All {len(result.items)} files failed: {first_error}"
                    )
                return
            display = format_batch_transcript(
                [
                    (
                        r.item.source_name,
                        f"Error: {r.error}" if r.error
                        else (r.text.strip() or EMPTY_ASR_MESSAGE),
                    )
                    for r in result.items
                ]
            )
            raw_display = None
            if any(r.raw_text for r in result.items):
                raw_display = format_batch_transcript(
                    [
                        (
                            r.item.source_name,
                            f"Error: {r.error}" if r.error
                            else (r.raw_text or r.text.strip() or EMPTY_ASR_MESSAGE),
                        )
                        for r in result.items
                    ]
                )
            is_empty = not saveable

        # The tab treats NONE while it is still running as a failure, so the
        # transcript must land first.
        ui.set_transcript(display, raw=raw_display)
        self.controller.overlay_state_update.emit(OverlayState.NONE)
        ui.set_transcription_stats(
            result.effective_transcription_time_s,
            result.total_duration_seconds,
            result.total_file_size,
            cleanup_time=(
                result.effective_cleanup_time_s
                if result.effective_cleanup_time_s > 0
                else None
            ),
        )

        if is_empty:
            logger.info("Empty batch result; skipping history")
            ui.set_status(EMPTY_ASR_MESSAGE)
            self._finish_job()
            return

        entries = []
        model_info = self._model_info_for_history()
        if request.combine:
            entries.append(dict(
                text=result.combined_text,
                model=model_info,
                source_audio_path=None,
                transcription_time=result.effective_transcription_time_s,
                audio_duration=result.total_duration_seconds,
                file_size=result.total_file_size,
                raw_text=result.combined_raw_text,
                cleanup_provider=result.combined_cleanup_provider,
                cleanup_model=result.combined_cleanup_model,
                source_name=batch_source_name(
                    [r.item.source_name for r in result.items]
                ),
            ))
        else:
            for r in saveable:
                entries.append(dict(
                    text=r.text,
                    model=model_info,
                    source_audio_path=None,
                    transcription_time=r.elapsed_s,
                    audio_duration=r.item.duration_seconds,
                    file_size=r.file_size,
                    raw_text=r.raw_text,
                    cleanup_provider=r.cleanup_provider,
                    cleanup_model=r.cleanup_model,
                    source_name=r.item.source_name,
                ))
        context = self._history_context(None, None, live=False)
        for entry in entries:
            entry.update(context)

        if result.canceled:
            ui.set_status(
                f"Canceled — kept {len(saveable)} of {len(request.items)} files"
            )
            self._queue_history(entries)
            return

        status = "Ready"
        if result.cleanup_error == CANCELED_REASON:
            status += " — AI cleanup canceled; showing raw text"
        elif result.cleanup_error:
            status += (
                f" — AI cleanup failed ({result.cleanup_error}); showing raw text"
            )
        ui.set_status(status)
        self._queue_history(entries)

    def _maybe_cleanup_transcript(
        self,
        raw: str,
        batch_context: Optional[str] = None,
        timeout_s: Optional[float] = None,
    ) -> tuple[str, Optional[str], Optional[CleanupInfo]]:
        """Return fixed text, distinct raw text, and successful cleanup metadata.

        Args:
            raw: ASR text to clean.
            batch_context: The user's description of how a multi-file upload
                fits together, appended to the prompt after the learned rules.
            timeout_s: Per-request timeout override for text far longer than
                a dictation.
        """
        self._last_cleanup_failure = None
        self._delivery_html = ""
        job = self._active_job
        profile = self._recording_profile
        settings = self._profile_settings if profile else settings_manager.load_all_settings()
        if not raw or not raw.strip():
            return raw, None, None
        # Before the switch below: the dictionary applies with cleanup off,
        # and to uploads and batches too.
        prepared = dictation_pipeline.prepare_text(raw, job, settings)
        enabled = profile is not None or setting_value(SettingsKey.TRANSCRIPT_CLEANUP_ENABLED, settings)
        if not enabled or prepared.skip_cleanup:
            return self._finish_without_cleanup(prepared)

        # Re-apply provider/model each run so Settings model changes take effect
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
            self._last_cleanup_failure = "cleanup unavailable"
            return self._finish_without_cleanup(prepared)

        self.controller.overlay_state_update.emit(OverlayState.CLEANING)
        self.controller.status_update.emit(
            f"Formatting · {profile.name}..." if profile else "Cleaning up..."
        )
        rules = resolve_transcript_cleanup_rules(settings)
        prompt = dictation_pipeline.compose_cleanup_prompt(
            job=job,
            settings=settings,
            profile=profile,
            rules=rules,
            prepared=prepared,
            batch_context=batch_context,
        )
        cleanup_input = prepared.cleanup_input
        # The dictation path passes no timeout so the call stays identical to
        # the one existing stubs of cleanup() accept.
        extra = {} if timeout_s is None else {"timeout_s": timeout_s}
        cleanup_start = time.time()
        fixed = self._transcript_cleanup.cleanup(
            cleanup_input, system_prompt=prompt, **extra
        )
        cleanup_elapsed = time.time() - cleanup_start
        # A changed transcript also proves cleanup ran, covering stubs that
        # bypass the real cleanup() and never touch last_error.
        cleaned = self._transcript_cleanup.last_error is None or fixed != cleanup_input
        if not cleaned:
            self._last_cleanup_failure = (
                self._transcript_cleanup.last_error or "cleanup failed"
            )
        finished = dictation_pipeline.finish_text(fixed, prepared)
        self._delivery_html = finished.html
        if not finished.ok:
            # The cleanup dropped a snippet, so its output is not used.
            self._last_cleanup_failure = "snippet placeholder lost"
            return finished.text, None, None
        info = (
            CleanupInfo(
                provider=self._transcript_cleanup.provider,
                model=self._transcript_cleanup.model,
                elapsed_s=cleanup_elapsed,
                level=dictation_pipeline.cleanup_level(settings, profile),
            )
            if cleaned
            else None
        )
        if finished.text != finished.raw_text:
            return finished.text, finished.raw_text, info
        return finished.text, None, info

    def _finish_without_cleanup(
        self, prepared: dictation_pipeline.PreparedText,
    ) -> tuple[str, None, None]:
        """The prepared text with its snippets expanded, as no AI ran."""
        finished = dictation_pipeline.finish_text(prepared.cleanup_input, prepared)
        self._delivery_html = finished.html
        return finished.text, None, None

    def transcribe_audio_file(self, audio_path: str) -> None:
        job = self._active_job
        try:
            self._raise_if_canceled()
            if self.controller._pending_file_size is None:
                self.controller._pending_file_size = os.path.getsize(audio_path)
            backend = self.controller.current_backend
            self._announce_transcription(backend, audio_path)
            recognition = self._final_pass_recognition(job)
            self.controller._transcription_start_time = time.time()
            # Windows decoded while recording aren't this pass's time, so
            # only the requests from here on count toward the stats line.
            mark = backend.timing_mark() if getattr(backend, "is_remote", False) else None
            raw = recognition_context.transcribe(
                self._incremental, backend, audio_path, recognition
            )
            self.controller._transcription_elapsed = (
                time.time() - self.controller._transcription_start_time
            )
            self.controller._transcription_start_time = None
            if mark is not None:
                self.controller._remote_timing = backend.timing_since(mark)
            self._complete_preview_fallback(audio_path, raw)
            self._raise_if_canceled()
            if job is not None and job.mode != JobMode.DICTATION:
                fixed, raw_text, cleanup_info = self._complete_command(raw, job)
            else:
                fixed, raw_text, cleanup_info = self._maybe_cleanup_transcript(raw)
            self._raise_if_canceled()
            self.controller.transcription_completed.emit(fixed, raw_text, cleanup_info)
        except Exception as exc:
            logger.error(f"Transcription failed: {exc}")
            self.controller.transcription_failed.emit(str(exc))

    def _final_pass_recognition(self, job):
        """The language and vocabulary for ``job``'s final pass, read now.

        None for uploads and other jobs without one, which keep the engine's
        own settings.
        """
        if job is None:
            return None
        try:
            return dictation_pipeline.recognition_for(
                job, settings_manager.load_all_settings()
            )
        except Exception:
            logger.debug("Recognition context unavailable", exc_info=True)
            return None

    def _complete_command(self, raw: str, job):
        """Hand a Command Mode recording's transcript to the command runtime."""
        command_runtime = getattr(self.controller, "command_runtime", None)
        complete = getattr(command_runtime, "complete_recording", None)
        if complete is None:
            raise RuntimeError("Command Mode isn't available")
        return complete(raw, job)

    def _abandon_canceled_job(self, stage: str) -> None:
        """Release a dictation job canceled before any transcript existed.

        ``_cancel`` already showed the canceled state, so this only drops what
        the job still holds: its early-decode session, the clipboard snapshot
        taken for the paste, and the pending metadata.
        """
        logger.info("Dictation canceled %s; nothing transcribed", stage)
        self._incremental.discard()
        self.controller.ui_controller.discard_clipboard_prefetch()
        self._clear_pending_audio_metadata()
        self._finish_job()

    def _raise_if_canceled(self) -> None:
        """Stop a job whose cancel arrived while no engine was decoding.

        When the engine is idle (queued behind a preview window, already
        finished, or the cleanup HTTP call is running), ``_cancel`` can only
        set the flag and show "Canceled". Checked before cleanup, so canceled
        text never reaches a cleanup provider, and after it: the flag ends
        cleanup's wait early with the raw text, which must not be pasted into
        whatever has focus. The message matches the one an engine raises
        when canceled mid-decode, so both end the same way.
        """
        if self._cancel_requested.is_set():
            raise RuntimeError("Transcription canceled")

    def _complete_preview_fallback(self, audio_path: str, raw: str) -> None:
        """Finish the live preview's text when this dictation came back empty.

        on_transcription_complete reads the preview only for an empty final
        transcript, so the recording job stopped the preview without decoding
        its last partial window and that window is decoded here, on this
        worker, for just that case. Uploads and retranscriptions never stopped
        a preview of their own; their _pending_audio_path is None.
        """
        if (raw or "").strip() or self._cancel_requested.is_set():
            return
        if self.controller._pending_audio_path != audio_path:
            return
        text = self.controller.streaming_runtime.finalize_streaming_text()
        if text:
            self.controller._pending_streaming_text = text

    def on_transcription_complete(
        self,
        transcript: str,
        raw_text: Optional[str] = None,
        cleanup_info: Optional[CleanupInfo] = None,
    ) -> None:
        job = self._active_job
        if self._stop_started_at:
            from services.diagnostics import record_metrics
            record_metrics(stop_to_result_s=time.monotonic() - self._stop_started_at)
            self._stop_started_at = 0.0
        if self._deliver_to_clipboard and self._cancel_requested.is_set():
            # The cancel arrived between the worker's emit and this slot.
            self.on_transcription_error("Transcription canceled")
            return
        is_empty = not (transcript or "").strip()
        preview = ""
        if is_empty:
            preview = (
                getattr(self.controller, "_pending_streaming_text", "") or ""
            ).strip()

        if is_empty and preview:
            # The full pass dropped everything (Whisper's VAD, or an optional
            # engine's quiet-window gate), but the preview heard speech in its
            # short windows. Show that rather than leave the user with nothing
            # — but it is a beam-1 preview with a repeated word at each chunk
            # seam, so it is never written to history and never reaches the
            # clipboard. Recovering the words is worth a rough transcript;
            # pasting one into whatever has focus is not.
            display_text = preview
        else:
            display_text = transcript if not is_empty else EMPTY_ASR_MESSAGE
        self.controller.ui_controller.set_transcript(
            display_text, raw=raw_text
        )
        self.controller.overlay_state_update.emit(OverlayState.NONE)

        transcription_time = getattr(self.controller, "_transcription_elapsed", None)
        self.controller._transcription_elapsed = None
        if transcription_time is None and self.controller._transcription_start_time is not None:
            transcription_time = time.time() - self.controller._transcription_start_time
            self.controller._transcription_start_time = None
        remote_timing = getattr(self.controller, "_remote_timing", None)
        self.controller._remote_timing = None

        cleanup_time = (
            cleanup_info.elapsed_s
            if cleanup_info and cleanup_info.elapsed_s > 0
            else None
        )

        if transcription_time is not None:
            self.controller.ui_controller.set_transcription_stats(
                transcription_time,
                self.controller._pending_audio_duration or 0.0,
                self.controller._pending_file_size or 0,
                cleanup_time=cleanup_time,
                remote=remote_timing,
            )

        source_name = getattr(self.controller, "_pending_source_name", None)

        if is_empty:
            logger.info(
                "Empty ASR result; skipping history, clipboard, and paste"
            )
            self.controller.ui_controller.set_status(
                EMPTY_PREVIEW_FALLBACK_MESSAGE if preview else EMPTY_ASR_MESSAGE
            )
            self.controller.ui_controller.discard_clipboard_prefetch()
            self._clear_pending_audio_metadata()
            self._finish_job()
            return

        entry = dict(
                    text=transcript,
                    model=self._model_info_for_history(),
                    source_audio_path=self.controller._pending_audio_path,
                    transcription_time=transcription_time,
                    audio_duration=self.controller._pending_audio_duration,
                    file_size=self.controller._pending_file_size,
                    raw_text=raw_text,
                    cleanup_provider=cleanup_info.provider if cleanup_info else None,
                    cleanup_model=cleanup_info.model if cleanup_info else None,
                    source_name=source_name,
        )
        # Only a recording's own WAV is dictated live; a re-transcription is
        # delivered like one but read from a file.
        live = bool(self._deliver_to_clipboard and self.controller._pending_audio_path)

        cleanup_notice = (
            f" — {self._recording_profile.name} formatting failed "
            f"({self._last_cleanup_failure}); using raw transcript"
            if self._recording_profile and self._last_cleanup_failure else ""
        )
        if not self._deliver_to_clipboard:
            self.controller.ui_controller.set_status("Ready" + cleanup_notice)
            entry.update(self._history_context(job, cleanup_info, live=live))
            self._queue_history([entry])
            return

        # Deliver immediately; copying retained audio, fsync, and SQLite writes
        # run on a worker while Qt and the target application remain responsive.
        try:
            self._deliver(transcript, job, status_suffix=cleanup_notice)
        finally:
            entry.update(self._history_context(job, cleanup_info, live=live))
            self._queue_history([entry])

    def _deliver(self, transcript: str, job, status_suffix: str = "") -> None:
        """Put a finished transcript where the user is working."""
        ui = self.controller.ui_controller
        insert = getattr(ui, "insert_into_scratchpad", None)
        if callable(insert):
            try:
                inserted = insert(transcript) is True
            except Exception:
                logger.exception("Could not add the transcript to the Scratchpad")
                inserted = False
            if inserted:
                ui.discard_clipboard_prefetch()
                ui.set_status("Ready (Added to Scratchpad)" + status_suffix)
                return

        # A command or transform exists to replace text, so it pastes even
        # with auto-paste off, but never into an app that took focus since.
        forced = job is not None and job.mode != JobMode.DICTATION
        if forced and not dictation_pipeline.paste_target_ok(job):
            ui.discard_clipboard_prefetch()
            if ui.copy_to_clipboard(transcript):
                ui.set_status(
                    "Rewrite copied — the app changed; paste it where you want"
                )
            else:
                ui.set_status("Transcription complete (copy failed)")
            return

        text = dictation_pipeline.text_for_paste(transcript, job)
        if self._apply_clipboard_and_paste(
            text,
            status_suffix=status_suffix,
            html=self._delivery_html,
            force_paste=forced,
        ):
            dictation_pipeline.after_paste(job, text)

    def _history_context(self, job, cleanup_info, *, live: bool) -> dict:
        """The context columns for this job's history entry; never raises."""
        try:
            settings = self._profile_settings or settings_manager.load_all_settings()
            return dictation_pipeline.history_fields(
                job, cleanup_info, live=live, settings=settings
            )
        except Exception:
            logger.debug("History context unavailable", exc_info=True)
            return {}

    def _queue_history(self, entries: list[dict]) -> None:
        """Snapshot metadata before queuing; keep the slot until persistence ends.

        Keeping the slot also prevents Quick Record from replacing its WAV while
        the writer copies it. Clipboard delivery and Qt repaint do not wait on IO.
        """
        try:
            self.controller.persistence_executor.submit(self._persist_history, entries)
        except Exception as exc:
            self.on_history_persisted(dict(error=f'History could not be saved: {exc}'))

    def _persist_history(self, entries: list[dict]) -> None:
        error = ''
        try:
            for fields in entries:
                entry = history_manager.add_entry(**fields)
                try:
                    # Stats are a side table: their failure never touches the
                    # saved entry or the status.
                    dictation_pipeline.record_stats(fields, entry)
                except Exception:
                    logger.debug("Could not record dictation stats", exc_info=True)
                source = fields.get('source_audio_path')
                if source and os.path.isfile(source):
                    if not getattr(entry, 'audio_file', None):
                        error = 'Transcript saved, but audio could not be retained; recovery copy kept'
                    elif os.path.abspath(source) == os.path.abspath(config.RECORDED_AUDIO_FILE):
                        acknowledge = getattr(self.controller.recorder, 'acknowledge_recording', None)
                        if acknowledge:
                            acknowledge()
        except Exception as exc:
            logger.exception('Failed to save transcription to history')
            error = f'History could not be saved: {exc}. Copy the transcript before closing.'
        self.controller.history_persisted.emit(dict(error=error, refresh=True))

    def on_history_persisted(self, outcome: dict) -> None:
        """Qt slot for persistence completion; failures must not look like success."""
        try:
            if outcome.get('refresh'):
                self.controller.ui_controller.refresh_history()
            if outcome.get('error'):
                self.controller.ui_controller.set_status(outcome['error'])
        except Exception:
            logger.exception('Could not refresh saved transcription history')
        finally:
            self._clear_pending_audio_metadata()
            self._finish_job()

    def _clear_pending_audio_metadata(self) -> None:
        """Drop one-shot metadata attached to the current transcription job."""
        self.controller._pending_audio_path = None
        self.controller._pending_audio_duration = None
        self.controller._pending_file_size = None
        self.controller._pending_source_name = None
        self.controller._pending_streaming_text = ""

    def _model_info_for_history(self) -> str:
        model_info = self.controller._current_model_name
        if self.controller._current_model_name == "local_whisper":
            local_backend = self.controller.transcription_backends.get("local_whisper")
            if local_backend and hasattr(local_backend, "device_info"):
                model_info = f"local_whisper ({local_backend.device_info})"
        from transcriber.optional_backend import LocalSpeechBackend
        if isinstance(self.controller.current_backend, LocalSpeechBackend):
            model_info = f"{model_info} ({self.controller.current_backend.device_info})"
        return model_info

    def _apply_clipboard_and_paste(
        self,
        transcript: str,
        status_suffix: str = "",
        *,
        html: str = "",
        force_paste: bool = False,
    ) -> bool:
        """Copy and optionally paste only after a successful clipboard write.

        Args:
            transcript: Text to place in the clipboard.
            status_suffix: Appended to whichever outcome status is shown, so a
                batch can report a cleanup fallback without hiding whether the
                paste itself succeeded.
            html: Rich-text alternative pasted with ``transcript``, or "".
            force_paste: Paste even with auto-paste turned off.

        Returns:
            True when the paste keystroke was sent.
        """
        settings = settings_manager.load_all_settings()
        copy_clipboard = setting_value(SettingsKey.COPY_CLIPBOARD, settings)
        auto_paste = force_paste or setting_value(SettingsKey.AUTO_PASTE, settings)

        def _status(text: str) -> None:
            self.controller.ui_controller.set_status(text + status_suffix)

        # Synthetic paste posts a key event, which needs macOS Accessibility
        # permission. Without it, degrade to clipboard so the text isn't lost and
        # the user can paste manually with Cmd+V.
        paste_blocked = auto_paste and not is_accessibility_trusted()

        if auto_paste and not paste_blocked:
            ui = self.controller.ui_controller
            # Without rich text the call stays the one every UI stand-in takes.
            stage = (
                ui.stage_transcript_for_paste(transcript, html=html)
                if html else ui.stage_transcript_for_paste(transcript)
            )
            if not stage.written:
                logger.error("Failed to copy transcription for auto-paste")
                _status("Transcription complete (copy failed)")
                return False

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
                _status("Transcription complete (paste failed)")
                return False

            if stage.restore_unavailable:
                logger.warning(
                    "Transcription pasted, but the previous clipboard could not be captured"
                )
                _status("Ready (Pasted; clipboard restore unavailable)")
                return True

            _status("Ready (Pasted)")
            if stage.lease is not None:
                self.controller.ui_controller.schedule_clipboard_restore(stage)
            return True

        # Only a paste consumes the clipboard snapshot prefetched when the
        # recording started (auto-paste may have been turned off since).
        self.controller.ui_controller.discard_clipboard_prefetch()
        should_copy = copy_clipboard or paste_blocked
        copy_ok = False
        if should_copy:
            # A formatted snippet keeps its formatting when only copied.
            copy_ok = bool(
                self.controller.ui_controller.copy_to_clipboard(transcript, html=html)
                if html else self.controller.ui_controller.copy_to_clipboard(transcript)
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
                _status(
                    "Copied to clipboard — press Cmd+V "
                    "(enable Accessibility to auto-paste)"
                )
            else:
                _status("Transcription complete (copy failed)")
            return False

        if copy_clipboard and not copy_ok:
            _status("Transcription complete (copy failed)")
            return False

        _status("Ready")
        return False

    def begin_rewrite_job(self, job: dictation_pipeline.DictationJob, *, source_name: str) -> bool:
        """Claim the slot for a rewrite of selected text; Qt thread.

        Refused while recording or while another job runs. The caller then
        either submits the rewrite or abandons it, which frees the slot.

        Args:
            job: A command or transform DictationJob carrying the selection.
            source_name: How the history entry names where the text came from.
        """
        if self.controller.recorder.is_recording:
            self._report_busy("rewriting text")
            return False
        if not self._claim_job(job):
            self._report_busy("rewriting text")
            return False
        self._forget_recording()
        with self._job_lock:
            self._rewrite_pending = True
            self._deliver_to_clipboard = True
        self._clear_pending_audio_metadata()
        self.controller._pending_source_name = source_name
        self.controller.overlay_state_update.emit(OverlayState.REWRITING)
        self.controller.status_update.emit("Rewriting...")
        return True

    def submit_rewrite(self, work) -> None:
        """Run ``work() -> (text, raw_text, info)`` on the executor; Qt thread.

        The result takes the path a dictation's does: transcription_completed
        pastes and saves it, transcription_failed reports a RuntimeError's
        message and frees the slot.
        """
        with self._job_lock:
            self._rewrite_pending = False
        try:
            self.controller.executor.submit(self._run_rewrite, work)
        except Exception as exc:
            logger.error("Could not start the rewrite: %s", exc)
            self.on_transcription_error(f"Could not start the rewrite: {exc}")

    def _run_rewrite(self, work) -> None:
        try:
            self._raise_if_canceled()
            text, raw_text, info = work()
            self._raise_if_canceled()
            self.controller.transcription_completed.emit(text, raw_text, info)
        except Exception as exc:
            logger.error("Rewrite failed: %s", exc)
            self.controller.transcription_failed.emit(str(exc))

    def abandon_rewrite_job(self, status: str) -> None:
        """Free a slot claimed by begin_rewrite_job that was never submitted."""
        with self._job_lock:
            if not self._rewrite_pending:
                return
            self._rewrite_pending = False
        self.controller.overlay_state_update.emit(OverlayState.NONE)
        if status:
            self.controller.status_update.emit(status)
        self._clear_pending_audio_metadata()
        self._finish_job()

    def paste_text_now(self, text: str) -> bool:
        """Paste ``text`` at the caret without saving it anywhere; Qt thread.

        Pastes even with auto-paste off, as the user asked for exactly this.
        Refused while recording or while a job is delivering its own text.
        """
        if self.controller.recorder.is_recording or self.has_active_job:
            self._report_busy("pasting")
            return False
        if not text:
            return False
        return self._apply_clipboard_and_paste(text, force_paste=True)

    def on_transcription_error(self, error_message: str) -> None:
        self._stop_started_at = 0.0
        pending_audio = self.controller._pending_audio_path
        status = f"Error: {error_message}"
        self.controller.ui_controller.set_status(status)
        self.controller.ui_controller.set_transcript(f"Error: {error_message}")
        self.controller.overlay_state_update.emit(OverlayState.NONE)
        self.controller._transcription_start_time = None
        self.controller._transcription_elapsed = None
        self.controller._remote_timing = None
        self.controller.ui_controller.discard_clipboard_prefetch()
        if pending_audio:
            try:
                self.controller.persistence_executor.submit(self._preserve_failed_audio, pending_audio, status)
                return
            except Exception:
                logger.exception('Could not queue failed recording preservation')
        self._clear_pending_audio_metadata()
        self._finish_job()

    def _preserve_failed_audio(self, path: str, status: str) -> None:
        try:
            name = history_manager.preserve_recording(path)
            if name:
                status += f' — audio saved in Recordings as {name}'
                if os.path.abspath(path) == os.path.abspath(config.RECORDED_AUDIO_FILE):
                    acknowledge = getattr(self.controller.recorder, 'acknowledge_recording', None)
                    if acknowledge:
                        acknowledge()
            elif os.path.isfile(path):
                status += ' — audio retention failed; original and recovery copy kept'
        except Exception:
            logger.exception('Failed to preserve audio after transcription error')
            status += ' — could not retain audio; recovery copy kept'
        self.controller.history_persisted.emit(dict(error=status))

    def on_model_changed(self, model_name: str) -> None:
        if self.controller.is_meeting_active():
            self.controller.status_update.emit(
                "End the meeting before changing transcription models"
            )
            return
        model_value = config.MODEL_VALUE_MAP.get(model_name)
        if model_value and model_value in self.controller.transcription_backends:
            previous = self.controller.current_backend
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

            # Reconfigure preview before handing memory to the selected engine.
            self.controller.streaming_runtime.reconfigure_streaming()
            from transcriber.optional_backend import LocalSpeechBackend
            # Whisper may have been released before an intervening API selection.
            needs_whisper_load = (
                model_value == "local_whisper"
                and not self.controller.current_backend.is_available()
            )
            if (
                needs_whisper_load
                or isinstance(self.controller.current_backend, LocalSpeechBackend)
                or isinstance(previous, LocalSpeechBackend)
            ):
                self.controller.reload_whisper_model()

    def _require_backend_ready(self) -> None:
        backend = self.controller.current_backend
        from transcriber.optional_backend import LocalSpeechBackend
        if isinstance(backend, LocalSpeechBackend) and not backend.is_available():
            if backend.is_model_missing:
                self.controller.ensure_local_model_available()
            else:
                self.controller.reload_whisper_model()
            raise RuntimeError(backend.device_info)
        if not backend.is_available() and getattr(backend, "is_model_missing", False):
            # Trigger the consent/download flow, but never transcribe with a
            # model the user has not approved downloading.
            self.controller.ensure_local_model_available()
            raise Exception(
                "Whisper model is not downloaded yet — approve the download "
                "and try again"
            )

    def _submit_transcription_job(self, audio_path: str) -> None:
        self._raise_if_canceled()
        self._require_backend_ready()
        self.controller.executor.submit(self.transcribe_audio_file, audio_path)

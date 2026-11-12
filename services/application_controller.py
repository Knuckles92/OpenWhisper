"""Main Qt-facing application controller."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Callable, Dict, Optional

from PyQt6.QtCore import QObject, pyqtSignal, QTimer

from config import config
from services.components import component_coordinator
from services.database import db
from services import dictionary
from services.recorder import AudioRecorder
from services.runtime import (
    HotkeyRuntime,
    MeetingRuntime,
    StreamingRuntime,
    TranscriptionRuntime,
)
from services.runtime.command import CommandRuntime
from services.runtime.components import ComponentRuntime
from services.runtime.models import ModelRuntime
from services.runtime.remote import RemoteRuntime
from services.runtime.updater import UpdaterRuntime
from services.runtime.walkthrough import WalkthroughRuntime
from services.actions.runtime import ActionRuntime
from services.dictation_pipeline import JobMode
from services.settings import (
    seed_new_install_settings,
    settings_manager,
)
from transcriber import (
    LocalWhisperBackend,
    OpenAIBackend,
    TranscriptionBackend,
)

logger = logging.getLogger(__name__)

#: What the user's Cancel says when it found nothing to cancel.
NOTHING_TO_CANCEL = "Nothing to cancel"

def _preload_recording_reader(backend) -> None:
    """Load PyAV once an optional or remote engine has loaded.

    Those engines read every recording through it (services/local_asr/audio.py),
    and a session's first dictation otherwise waited for the import at its
    stop: 51 ms from a warm disk (November 2026), longer from a cold one.
    Loading it with the engine also keeps the import out of a recording,
    where it would hold the interpreter while audio blocks arrive.
    """
    if not backend.is_available():
        return
    try:
        import av  # noqa: F401 - loaded ahead of the first transcription
    except Exception:
        logger.debug("PyAV preload failed; the first transcription imports it", exc_info=True)


def _import_openai_sdk() -> None:
    """Warm-up target: a later client build then only constructs the client."""
    try:
        import openai  # noqa: F401
    except Exception:
        # Only a head start: the first request imports it again and reports
        # any real failure there.
        logger.debug("openai SDK warm-up failed; skipping the early import", exc_info=True)


class PreviewEvents:
    """Live preview text as ``transcription.preview``, at most four a second; Qt thread.

    Text that arrives too soon is held and sent when the quarter second is
    up (only the newest), so listeners always end on the latest preview
    rather than a stale one. A final preview goes out at once. ``publish``
    (TranscriptionRuntime.publish_preview) drops text held, or still on its
    way, when its recording stops or is canceled.
    """

    INTERVAL_S = 0.25

    def __init__(self, publish: Callable[[object, str, bool], None]) -> None:
        self._publish = publish
        # Created on the Qt thread, like the controller's other timers.
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._flush)
        self._sent_at: Optional[float] = None
        self._held: Optional[tuple] = None

    def offer(self, job, text: str, final: bool) -> None:
        wait = 0.0 if self._sent_at is None else (
            self._sent_at + self.INTERVAL_S - time.monotonic()
        )
        if final or wait <= 0:
            self._timer.stop()
            self._held = None
            self._send(job, text, final)
            return
        self._held = (job, text, final)
        if not self._timer.isActive():
            self._timer.start(max(1, int(wait * 1000) + 1))

    def _flush(self) -> None:
        held, self._held = self._held, None
        if held is not None:
            self._send(*held)

    def _send(self, job, text: str, final: bool) -> None:
        self._sent_at = time.monotonic()
        self._publish(job, text, final)


class ApplicationController(QObject):
    """Main application controller integrating UI and logic."""

    # fixed text, optional raw text, optional CleanupInfo
    transcription_completed = pyqtSignal(str, object, object)
    transcription_failed = pyqtSignal(str)
    # a finished walkthrough's WalkthroughResult, written up or not
    walkthrough_written_up = pyqtSignal(object)
    recording_capture_failed = pyqtSignal(str)
    history_persisted = pyqtSignal(object)
    startup_database_ready = pyqtSignal(str)
    # Multi-file upload, emitted from the batch worker thread.
    # (1-based position, total, source name) as each file starts
    batch_progress = pyqtSignal(int, int, str)
    # (1-based position, succeeded, the file's own transcript) as each file
    # ends. The transcript is empty on failure and for a combined job, whose
    # files have no finished text of their own.
    batch_item_finished = pyqtSignal(int, bool, str)
    # BatchResult; also emitted on a per-file cancel with the finished items
    batch_completed = pyqtSignal(object)
    # (file size in MB, will be split) for a large file inside a batch. The
    # single-file path announces this on the caller thread before submitting.
    large_file_detected = pyqtSignal(float)
    status_update = pyqtSignal(str)
    recording_state_changed = pyqtSignal(bool)
    recording_destination_changed = pyqtSignal(str)
    # The user's Cancel reached the actions still running; emitted from the
    # hotkey dispatcher thread too, and ActionRuntime works on the Qt thread.
    actions_cancel_requested = pyqtSignal()
    overlay_state_update = pyqtSignal(object)
    transcription_retry_progress = pyqtSignal(str)
    minimize_to_tray_requested = pyqtSignal()
    # Emitted from the background reload worker (thread-safe UI updates).
    # (readout, engine is loaded and usable)
    device_info_update = pyqtSignal(str, bool)
    engine_busy_changed = pyqtSignal(bool)
    # Arm the engine reload's debounce timer (emitted from any thread; hotkeys
    # and transcription workers ask for reloads off the Qt thread).
    reload_debounce_requested = pyqtSignal()
    # A shortcut's outcome near the pointer; any thread, shown on the Qt thread.
    notice_requested = pyqtSignal(str, bool)
    # (old, new) microphone names after a recording moved to the next one.
    recording_device_switched = pyqtSignal(str, str)
    # A correction the user made after a paste became a dictionary term.
    dictionary_term_learned = pyqtSignal(str)

    def __init__(self, ui_controller, local_backend: Optional[LocalWhisperBackend] = None):
        super().__init__()
        self.ui_controller = ui_controller
        # First, so nothing this launch writes the settings file before a
        # brand-new install is told apart.
        seed_new_install_settings(settings_manager)

        self.recorder = AudioRecorder.from_settings()
        self.recorder.error_callback = self.recording_capture_failed.emit
        self.recorder.device_switch_callback = self.recording_device_switched.emit
        self._shutdown_cancel = threading.Event()
        self._shutting_down = False
        self._backup_in_progress = False
        self.backup_coordinator = None
        self.executor = ThreadPoolExecutor(max_workers=2)
        self._startup_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="database-startup"
        )
        self._startup_database_requested = False
        self._startup_database_ready = False
        self._startup_recovery_future = None
        self.persistence_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="history-save"
        )
        # Component installs get their own single worker. They can run for
        # tens of minutes, and letting one occupy the shared 2-worker pool
        # would starve transcription and model loading.
        self.component_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="component"
        )
        # Walkthrough library edits get one too: redoing a write-up runs an
        # agent for minutes, and dictation finishes on the shared pool.
        self.walkthrough_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="walkthrough-library"
        )
        self._walkthrough_library_busy = False

        self.hotkey_manager = None
        self.streaming_transcriber = None
        self._streaming_backend = None
        self.transcription_backends: Dict[str, TranscriptionBackend] = {}
        self.current_backend: Optional[TranscriptionBackend] = None
        self._current_model_name = config.DEFAULT_BACKEND

        self._streaming_enabled = False

        self._pending_audio_path: Optional[str] = None
        self._pending_audio_duration: Optional[float] = None
        self._pending_file_size: Optional[int] = None
        self._pending_source_name: Optional[str] = None
        # The streaming preview's text for the current job, kept only so an
        # empty full-pass result can still show the user what was said.
        self._pending_streaming_text: str = ""
        self._transcription_start_time: Optional[float] = None
        self._transcription_elapsed: Optional[float] = None
        # Where a remote engine's pass spent its time (a RemoteTiming).
        self._remote_timing = None

        # Debounced, background whisper reload. The ~1s model swap (cleanup +
        # load) must not run on the UI thread, and rapid combo changes are
        # coalesced into a single reload via this single-shot timer.
        self._engine_lock = threading.RLock()
        self._reload_in_flight = False
        self._pending_streaming_setup = False
        # True while the local Whisper model has been released so a
        # Meeting Mode consumer can load its own. Only ``release_local_engine``
        # sets it, so an API-only user (whose local model never loaded) is
        # never handed one by ``restore_local_engine``.
        self._engine_released_for_lease = False
        # A restore that arrived while a reload was in flight. That reload
        # normally loads the model, but its warmup runs outside the engine
        # lock, so a lease can close the worker it just loaded; the reload's
        # finish then restarts it. Guarded with _reload_in_flight's handoff.
        self._restore_after_reload = False
        self._reload_handoff_lock = threading.Lock()
        self._setup_reload_timer()
        # True from a reload request until its timer either starts the reload
        # or drops it, so another thread can tell a settled engine from one
        # about to reload (the timer itself is only safe to read on this one).
        self._reload_pending = False
        # Status for the next reload instead of "Reloading speech engine...",
        # such as which model a remote engine switch is loading.
        self._reload_note = ""

        self.update_readiness_error = ""

        # Exclusive mode: True while a meeting session runs; dictation start
        # paths and engine reloads refuse while set (owned by MeetingRuntime).
        self.meeting_active = False

        self.hotkey_runtime = HotkeyRuntime(self)
        self.streaming_runtime = StreamingRuntime(self)
        self.transcription_runtime = TranscriptionRuntime(self)
        self.command_runtime = CommandRuntime(self)
        self.action_runtime = ActionRuntime(self)
        self.walkthrough_runtime = WalkthroughRuntime(self)
        self._preview_events = PreviewEvents(self.transcription_runtime.publish_preview)
        self.meeting_runtime = MeetingRuntime(self)
        self.remote = RemoteRuntime(self)
        self.models = ModelRuntime(self)
        self.components = ComponentRuntime(self)
        self.updater = UpdaterRuntime(self)

        self._setup_transcription_backends(local_backend=local_backend)
        self._setup_ui_callbacks()
        self.hotkey_runtime.setup_hotkeys()
        self.streaming_runtime.setup_audio_level_callback()
        self._connect_signals()
        # Streaming (and its optional preview-model load) waits until the main
        # window is shown — see notify_main_ui_ready.
        self.hotkey_runtime.setup_hook_watchdog()

    def _setup_transcription_backends(
        self, local_backend: Optional[LocalWhisperBackend] = None
    ) -> None:
        """Accept an optional local backend (may still be unloaded)."""
        logger.info("Setting up transcription backends...")

        self.transcription_backends["local_whisper"] = (
            local_backend if local_backend is not None else LocalWhisperBackend()
        )
        api = OpenAIBackend("api")
        # Splitting a file over the upload limit reports its steps.
        api.on_progress = self.transcription_runtime.report_backend_progress
        self.transcription_backends["api"] = api

        from services.local_asr.catalog import BACKENDS
        from transcriber.optional_backend import LocalSpeechBackend
        for key in BACKENDS:
            self.transcription_backends[key] = LocalSpeechBackend(key)
        from transcriber.remote_backend import REMOTE_BACKEND, RemoteSpeechBackend
        remote = RemoteSpeechBackend()
        remote.on_link_changed = self.remote.remote_link_poke.emit
        self.transcription_backends[REMOTE_BACKEND] = remote
        from services.remote_asr.service import RemoteEngineService
        # Serves whichever engine is selected here, when sharing is on, and
        # lets paired computers switch it to another model ready here.
        self.remote_engine = RemoteEngineService(
            lambda: self.current_backend,
            on_client_changed=self.remote.remote_pairing_changed.emit,
            on_host_catalog=self.remote.remote_catalog_received.emit,
            switch_engine=self.remote.switch_engine_for_client,
            configure_engine=self.remote.configure_engine_for_client,
            engine_settled=self._engine_settled,
        )
        self.remote_engine.add_listener(self.remote.on_host_event)
        self.ui_controller.remote_engine = self.remote_engine
        from services.remote_records.sync import record_sync
        # Where this computer's records go while paired; waits on the meeting
        # runtime so a meeting is sent only once nothing more is written to it.
        self.record_sync = record_sync
        meetings = record_sync.kinds.get("meeting")
        if meetings is not None:
            meetings.busy = self.meeting_runtime.meeting_busy
            meetings.in_use_check = self.meeting_runtime.meeting_in_use
        record_sync.add_listener(self.remote.on_record_sync_event)
        self.ui_controller.record_sync = record_sync
        saved_model = settings_manager.load_model_selection()
        self._current_model_name = saved_model
        self.current_backend = self.transcription_backends.get(
            saved_model, self.transcription_backends["local_whisper"]
        )
        logger.info(f"Using transcription backend: {saved_model}")

    def _setup_ui_callbacks(self) -> None:
        self.ui_controller.on_record_start = self.start_recording
        self.ui_controller.on_profile_record_start = self.start_profile_recording
        self.ui_controller.on_cleanup_profiles_changed = self.hotkey_runtime.refresh_profile_hotkeys
        self.ui_controller.on_profile_hotkey_capture = self.hotkey_runtime.set_capture_suspended
        self.ui_controller.on_record_stop = self.stop_recording
        self.ui_controller.on_record_cancel = self.cancel
        self.ui_controller.on_model_changed = self.on_model_changed
        self.ui_controller.on_hotkeys_changed = self.update_hotkeys
        self.ui_controller.on_recording_trigger_mode_changed = (
            self.update_recording_trigger_mode
        )
        self.ui_controller.on_retranscribe = self.retranscribe_audio
        self.ui_controller.on_recover_walkthrough = self.transcription_runtime.recover_walkthrough
        self.ui_controller.walkthrough_writing_up = lambda: self.transcription_runtime.writing_up
        self.ui_controller.acquire_walkthrough_library = self._acquire_walkthrough_library
        self.ui_controller.release_walkthrough_library = self._release_walkthrough_library
        self.ui_controller.submit_walkthrough_library = self.walkthrough_executor.submit
        self.ui_controller.on_upload_audio = self.upload_audio_file
        self.ui_controller.on_upload_audio_files = self.upload_audio_files
        self.ui_controller.on_upload_cancel = self.cancel
        self.ui_controller.on_whisper_settings_changed = self._on_local_engine_settings_changed
        self.ui_controller.on_audio_device_changed = self.change_audio_device
        self.ui_controller.on_streaming_settings_changed = self.reconfigure_streaming
        self.ui_controller.on_hf_policy_changed = self.models.on_hf_policy_changed
        self.ui_controller.on_model_download_requested = self.models.request_model_download
        self.ui_controller.on_model_delete_requested = self.models.request_model_delete
        self.ui_controller.on_model_batch_download = (
            self.models.request_model_batch_download
        )
        self.ui_controller.on_model_batch_stop = self.models.cancel_model_batch_download
        self.ui_controller.get_loaded_local_model = self.models.get_loaded_local_model
        self.ui_controller.get_remote_models = self.remote.remote_models
        self.ui_controller.on_remote_model_selected = self.remote.select_remote_model
        self.ui_controller.on_host_model_selected = self.remote.select_host_model
        self.ui_controller.on_remote_runtime_selected = self.remote.select_remote_runtime
        self.ui_controller.get_missing_local_runtime = self.models.get_missing_local_runtime
        self.ui_controller.on_dictation_transcribe = self.transcribe_clip
        self.ui_controller.get_meeting_active = self.is_meeting_active
        self.ui_controller.on_component_install = self.components.request_install
        self.ui_controller.on_component_cancel = self.components.cancel_install
        self.ui_controller.on_component_remove = self.components.request_uninstall
        self.ui_controller.on_check_for_updates = (
            lambda: self.updater.request_check(manual=True)
        )
        self.ui_controller.on_update_download = self.updater.request_download
        self.ui_controller.on_update_cancel = self.updater.cancel_download
        self.ui_controller.on_update_abandon = self.updater.discard_handoff
        self.ui_controller.get_transcribing = self.is_transcribing
        self.ui_controller.get_current_backend = lambda: self.current_backend
        self.ui_controller.on_paste_text_now = self.transcription_runtime.paste_text_now
        self.ui_controller.get_component_installing = (
            lambda: component_coordinator.is_any_installing()
        )
        self.ui_controller.on_meeting_start = self.meeting_runtime.start_meeting
        self.ui_controller.on_meeting_start_demo = (
            self.meeting_runtime.start_demo_meeting
        )
        self.ui_controller.on_meeting_end = self.meeting_runtime.end_meeting
        self.ui_controller.on_meeting_pause = self.meeting_runtime.pause_meeting
        self.ui_controller.on_meeting_resume = self.meeting_runtime.resume_meeting
        self.ui_controller.on_meeting_open_dashboard = (
            self.meeting_runtime.open_dashboard
        )
        self.ui_controller.on_meeting_open_past = (
            self.meeting_runtime.show_past_meeting
        )
        self.ui_controller.on_meeting_copy_transcript = (
            self.meeting_runtime.copy_past_meeting_transcript
        )
        self.ui_controller.on_meeting_delete_past = (
            self.meeting_runtime.delete_past_meeting
        )
        self.ui_controller.on_meeting_clear_past = (
            self.meeting_runtime.clear_past_meetings
        )
        self.ui_controller.on_meeting_open_report = (
            self.meeting_runtime.open_report
        )
        self.ui_controller.on_meeting_copy_guest_link = (
            self.meeting_runtime.copy_guest_link
        )
        self.ui_controller.on_meeting_toggle_cloud = self.meeting_runtime.toggle_cloud
        self.ui_controller.on_meeting_retry_insights = (
            self.meeting_runtime.retry_insights
        )
        self.ui_controller.on_meeting_retry_speakers = (
            self.meeting_runtime.retry_speakers
        )
        self.ui_controller.on_meeting_retry_step = (
            self.meeting_runtime.retry_finalization
        )
        self.ui_controller.on_meeting_background = (
            self.meeting_runtime.continue_in_background
        )
        self.ui_controller.on_meeting_defer_insights = (
            self.meeting_runtime.defer_finalization_card
        )
        self.ui_controller.on_settings_changed = self.on_settings_changed
        self.ui_controller.on_try_action = self.action_runtime.test
        self.ui_controller.on_cancel_try_runs = (
            lambda: self.action_runtime.cancel(tests_only=True))

    def command_key_pressed(self, at: float) -> None:
        """The Command Mode shortcut went down; hotkey dispatcher thread."""
        self.command_runtime.key_pressed(at)

    def command_key_released(self, at: float) -> None:
        """The Command Mode shortcut came up; hotkey dispatcher thread."""
        self.command_runtime.key_released(at)

    def on_settings_changed(self, kind: str) -> None:
        """A Settings page saved something of ``kind`` (see notify_changed)."""
        if kind in ("transforms", "hotkeys", "actions"):
            # Transforms, actions and the newer standard shortcuts register
            # as global shortcuts.
            self.hotkey_runtime.refresh_profile_hotkeys()
        if kind in ("webhooks", "programs", "filters", "mcp_events"):
            from services.automation import service as automation_service

            automation_service.reload(kind)
        if kind == "updates":
            # Turning automatic checks back on checks once it is due.
            self.updater.schedule_check()

    def reload_whisper_model(self) -> None:
        """Schedule a debounced, background reload of the local whisper model.

        Called by both the Settings dialog and the inline main-GUI engine
        controls. Rapid changes (e.g. flipping device then quant) are coalesced
        into a single reload, and the request is refused while a recording or
        transcription is in progress. Safe from any thread.
        """
        if self.is_meeting_active():
            logger.info("Ignoring whisper reload: a meeting is in progress")
            self.status_update.emit("End the meeting before changing the engine")
            self.engine_busy_changed.emit(False)
            return

        if self.recorder.is_recording or self.is_transcribing():
            logger.info("Ignoring whisper reload: recording/transcribing in progress")
            self.status_update.emit("Finish recording before changing the engine")
            self.engine_busy_changed.emit(False)
            return

        self._reload_pending = True
        self.reload_debounce_requested.emit()

    def _setup_reload_timer(self) -> None:
        self._reload_timer = QTimer()
        self._reload_timer.setSingleShot(True)
        self._reload_timer.timeout.connect(self._do_reload_whisper_model)
        # A QTimer started off its own thread never runs, so requests arm it
        # through a signal: at once on the Qt thread, queued from any other.
        self.reload_debounce_requested.connect(self._arm_reload_timer)

    def _arm_reload_timer(self) -> None:
        self._reload_timer.start(config.WHISPER_RELOAD_DEBOUNCE_MS)

    def _do_reload_whisper_model(self) -> None:
        if self.recorder.is_recording or self.is_transcribing() or self.host_update_active():
            self._reload_timer.start(config.WHISPER_RELOAD_DEBOUNCE_MS)
            return
        if self.is_meeting_active():
            logger.info("Canceling queued whisper reload: meeting owns the engine")
            self._reload_pending = False
            self._reload_note = ""
            self.status_update.emit("End the meeting before changing the engine")
            self.engine_busy_changed.emit(False)
            return
        if self._reload_in_flight:
            # A reload is already running; retry shortly so the newest settings win.
            self._reload_timer.start(config.WHISPER_RELOAD_DEBOUNCE_MS)
            return

        self._reload_in_flight = True
        self._reload_pending = False
        note, self._reload_note = self._reload_note, ""
        self.engine_busy_changed.emit(True)
        self.status_update.emit(note or "Reloading speech engine...")
        self.executor.submit(self._reload_worker)

    def _engine_settled(self) -> bool:
        """No reload running or queued. Safe to call from any thread."""
        return not self._reload_in_flight and not self._reload_pending

    def host_update_active(self) -> bool:
        active = getattr(getattr(self, "remote_engine", None), "update_active", None)
        return callable(active) and active() is True

    def _reload_worker(self) -> None:
        if self._shutting_down:
            return
        warm = self._reload_selected_engine()
        if warm is None:
            return
        # A fresh worker's first decode pays a one-time cost (see
        # config.SPEECH_WARMUP_BACKENDS), so take it now instead of in the
        # user's first dictation. The model is already published: a recording
        # may start meanwhile, but its decode cannot begin before the hold
        # and post-roll end, long after this ~0.3 s. It stays in the busy
        # state so engine changes and runtime installs wait for a settled
        # worker, and it finishes before the preview is set up, because the
        # native-stream warmup takes the same decode lock on the Qt thread.
        # It runs outside the engine lock so a Meeting Mode lease never
        # waits for it: releasing the engine closes the worker and ends it.
        try:
            warm.warmup()
        finally:
            self._finish_speech_reload(warm)

    def _reload_selected_engine(self) -> Optional[TranscriptionBackend]:
        """Reload the selected engine under the engine lock.

        Returns the optional speech engine that ``_reload_worker`` still has
        to warm and finish, or None when the reload has finished here.
        """
        with self._engine_lock:
            if (self._shutting_down or self.is_meeting_active()
                    or self.recorder.is_recording or self.is_transcribing()):
                self._reload_in_flight = False
                self.engine_busy_changed.emit(False)
                return None
            from transcriber.optional_backend import LocalSpeechBackend
            selected = self.current_backend
            _release_unselected_engines(self, selected)
            if isinstance(selected, LocalSpeechBackend):
                return _reload_speech_engine(self, selected)
            if self._current_model_name == "local_whisper":
                self._reload_whisper_worker()
            else:
                self._reload_in_flight = False
                self.engine_busy_changed.emit(False)
            return None

    def _finish_speech_reload(self, selected) -> None:
        if self._shutting_down:
            return
        with self._reload_handoff_lock:
            self._reload_in_flight = False
            restore, self._restore_after_reload = self._restore_after_reload, False
        unloaded = selected is self.current_backend and not selected.is_available()
        if restore and unloaded and not self.is_meeting_active():
            # A lease closed this worker during its warmup and was returned
            # before the reload finished, so nothing else would load it again.
            self._submit_restore_reload()
            return
        self.engine_busy_changed.emit(False)
        # An engine a lease released is not missing its runtime.
        leased = restore or self._engine_released_for_lease
        # A remote engine that can't connect has no local runtime to install.
        remote = getattr(selected, "is_remote", False)
        if unloaded and not selected.is_model_missing and not leased and not remote:
            self.models.runtime_consent_requested.emit(selected.model_name)
        # The preview shares this worker, so it can only be set up
        # once the load has settled. A remote engine is whatever the host
        # runs now, so its preview is checked again after every reload.
        if remote:
            self._pending_streaming_setup = True
        self._flush_pending_streaming_setup()
        if remote and selected is self.current_backend:
            # Starts the link watch, and retries a host that didn't answer.
            self.remote.remote_settled.emit("reload")

    def _reload_whisper_worker(self) -> None:
        """Reload the local backend off the UI thread; report results via signals.

        Runs on a ThreadPoolExecutor worker, so it must NOT touch the UI
        directly — all updates go through Qt signals, which are delivered on the
        main thread.
        """
        try:
            if self._shutting_down:
                return
            local_backend = self.transcription_backends.get("local_whisper")
            if local_backend:
                local_backend.reload_model()
                info = getattr(local_backend, "device_info", "")
                self.device_info_update.emit(info, local_backend.is_available())
                if (
                    not local_backend.is_available()
                    and getattr(local_backend, "is_model_missing", False)
                ):
                    # Cache-first load found no local copy; route through the
                    # consent flow instead of downloading silently.
                    self.status_update.emit(
                        f"Model '{local_backend.model_name}' is not downloaded"
                    )
                    self.ensure_local_model_available()
                else:
                    self.status_update.emit("Whisper engine ready")
                    logger.info(f"Whisper reloaded: {info}")
                # After the ready status, so the warning is what remains visible.
                if getattr(local_backend, "gpu_fallback_note", None):
                    self.models.gpu_fallback_detected.emit()
            else:
                logger.warning("Local whisper backend not found")
                self.status_update.emit("Ready")
        except Exception as exc:
            logger.error(f"Whisper reload failed: {exc}", exc_info=True)
            self.status_update.emit("Engine reload failed")
        finally:
            self._reload_in_flight = False
            self.engine_busy_changed.emit(False)
            self._flush_pending_streaming_setup()

    def _flush_pending_streaming_setup(self) -> None:
        if self._shutting_down:
            return
        if self._pending_streaming_setup:
            self._pending_streaming_setup = False
            self.streaming_runtime.streaming_setup_requested.emit()

    def _start_initial_whisper_load(self) -> None:
        """Load the deferred local model after the main window is shown."""
        if self._reload_in_flight:
            return
        self._reload_in_flight = True
        self.engine_busy_changed.emit(True)
        self.status_update.emit("Loading speech engine...")
        self.executor.submit(self._reload_worker)

    def release_local_engine(self) -> bool:
        """Release resident speech weights before a meeting or final pass loads."""
        with self._engine_lock:
            released = False
            for backend in self.transcription_backends.values():
                # A remote engine's weights are on the host, so closing its
                # connection would free nothing here.
                if getattr(backend, "model", None) is not None and not getattr(backend, "is_remote", False):
                    backend.cleanup()
                    released = True
            preview = getattr(self, "_streaming_backend", None)
            if preview is not None:
                self.streaming_runtime.cleanup()
                self._pending_streaming_setup = True
            self._engine_released_for_lease = self._engine_released_for_lease or released
            if released:
                self.device_info_update.emit("Released while Meeting Mode runs", False)
            return released

    def restore_local_engine(self) -> None:
        """Reload the local model released by ``release_local_engine``.

        A no-op unless a release actually happened. Reuses ``_reload_worker``
        so the restore takes the same path as any other reload: it re-reads the
        model setting, reports device info, and routes a missing model through
        the consent flow.
        """
        if not self._engine_released_for_lease:
            return
        self._engine_released_for_lease = False
        with self._reload_handoff_lock:
            if self._reload_in_flight:
                # A reload already queued by another path will load the model;
                # if the lease closed the worker it had loaded, its finish
                # (_finish_speech_reload) restarts it.
                self._restore_after_reload = True
                return
        self._submit_restore_reload()

    def _submit_restore_reload(self) -> None:
        self._reload_in_flight = True
        self.engine_busy_changed.emit(True)
        self.status_update.emit("Reloading speech engine...")
        self.executor.submit(self._reload_worker)

    def local_whisper_loading_message(self) -> Optional[str]:
        """Status text when Local Whisper is selected but not yet loaded."""
        if not isinstance(self.current_backend, LocalWhisperBackend):
            return None
        if self.current_backend.is_available():
            return None
        if getattr(self.current_backend, "is_model_missing", False):
            return None
        if getattr(self.current_backend, "load_deferred", False) or self._reload_in_flight:
            return "Whisper engine is still loading..."
        return None

    def transcription_readiness_message(self) -> Optional[str]:
        """Return an actionable reason capture must not start yet."""
        backend = getattr(self, "current_backend", None)
        if backend is None:
            return "No transcription engine is selected"
        if backend.is_available():
            return None

        from transcriber.optional_backend import LocalSpeechBackend
        if isinstance(backend, LocalSpeechBackend):
            return _speech_engine_readiness_message(self, backend)
        loading = self.local_whisper_loading_message()
        if loading:
            return loading
        if isinstance(backend, LocalWhisperBackend) and getattr(
            backend, "is_model_missing", False
        ):
            self.ensure_local_model_available()
            return (
                f"Whisper model '{backend.model_name}' is not downloaded — "
                "finish the model download before recording"
            )
        if self._current_model_name == "api":
            return "The selected API transcription engine needs a valid API key"
        return "The selected transcription engine is unavailable"

    def is_transcribing(self) -> bool:
        runtime = getattr(self, "transcription_runtime", None)
        if runtime is not None and runtime.has_active_job:
            return True
        backend = getattr(self, "current_backend", None)
        return bool(backend and getattr(backend, "is_transcribing", False))

    def setup_backup(self) -> None:
        from services.backup_manager import BackupCoordinator

        self.backup_coordinator = BackupCoordinator(
            self, acquire=self._acquire_backup, release=self._release_backup,
            worker_context=self._backup_record_context,
        )
        self.ui_controller.backup_coordinator = self.backup_coordinator
        self.backup_coordinator.restore_staged.connect(self._on_restore_staged)

    @contextmanager
    def _backup_record_context(self):
        with self.record_sync.paused_for_backup():
            with self.remote_engine.paused_records_for_backup():
                yield

    def _acquire_backup(self):
        if self._shutting_down or not self._startup_database_ready:
            return "Wait for OpenWhisper to finish starting before backing up or restoring."
        recovery = self._startup_recovery_future
        if recovery is not None and not recovery.done():
            return "Wait for recording recovery to finish before backing up or restoring."
        runtime, meeting = self.transcription_runtime, self.meeting_runtime
        with runtime._capture_lock, meeting._lock, runtime._job_lock:
            if self._walkthrough_library_busy or runtime.writing_up:
                return "Wait for the walkthrough to finish saving before backing up or restoring."
            if (self._backup_in_progress or self.recorder.is_recording
                    or runtime._job_active
                    or bool(getattr(self.current_backend, "is_transcribing", False))):
                return "Finish recording or transcription before backing up or restoring."
            if (meeting.is_claimed or meeting.is_finalizing or meeting._background_engines
                    or meeting._archive_starting or meeting._retry_meeting_id):
                return "Finish the meeting and its processing before backing up or restoring."
            dialog = getattr(self.ui_controller, "_settings_dialog", None)
            rules = getattr(dialog, "cleanup_rules", None) if dialog is not None else None
            if getattr(rules, "dictation_state", "idle") != "idle":
                return "Finish dictating the cleanup rule before backing up or restoring."
            self._backup_in_progress = True
        central = self.ui_controller.main_window.centralWidget()
        self._backup_central_enabled = central.isEnabled() if central else False
        if central:
            central.setEnabled(False)
        return None

    def _release_backup(self):
        self._backup_in_progress = False
        if not self._shutting_down:
            central = self.ui_controller.main_window.centralWidget()
            if central:
                central.setEnabled(self._backup_central_enabled)

    def _acquire_walkthrough_library(self):
        """Reserve the walkthrough library for one edit; Qt thread.

        An edit shuts out a backup or restore, another edit, and a
        walkthrough being transcribed or written up into the library.
        Dictation keeps working: it never touches the library.
        """
        if self._shutting_down or not self._startup_database_ready:
            return "Wait for OpenWhisper to finish starting."
        recovery = self._startup_recovery_future
        if recovery is not None and not recovery.done():
            # Startup recovery attaches audio to drafts and prunes old entries.
            return "Wait for recording recovery to finish."
        runtime = self.transcription_runtime
        with runtime._job_lock:
            if self._backup_in_progress:
                return "Wait for the backup or restore to finish."
            if self._walkthrough_library_busy:
                return "Wait for the other walkthrough change to finish."
            if runtime.writing_up:
                return "Wait for the AI write-up to finish."
            job = runtime._active_job
            if runtime._job_active and job is not None and job.mode == JobMode.WALKTHROUGH:
                return "Wait for the walkthrough being transcribed to finish."
            self._walkthrough_library_busy = True
        return None

    def _release_walkthrough_library(self):
        self._walkthrough_library_busy = False

    def _on_restore_staged(self, _path, error):
        if not error and not self._shutting_down:
            from PyQt6.QtWidgets import QApplication
            from services.backup_startup import RESTART_EXIT_CODE

            # main.py relaunches only after bootstrap closes every data writer.
            QApplication.exit(RESTART_EXIT_CODE)

    def notify_main_ui_ready(self) -> None:
        """Called by bootstrap once the main window is shown.

        Local Whisper loads here on a worker when the saved backend is local
        and construction deferred the model. API-only users skip that load.
        Streaming setup waits until after first paint (and after the local
        load when one is in flight) so the preview model cannot block the
        splash; an optional engine's preview shares its worker, so setup
        always follows that load.

        For a new installation whose selected backend is Local Whisper with an
        uncached model, this is also the moment the consent dialog may first
        appear — after the main UI is available, never during startup, and
        never for API-only users. The same deferral applies to a GPU fallback
        from the model load: it is reported here, once there is a UI to
        report it to.
        """
        from transcriber.optional_backend import LocalSpeechBackend
        if isinstance(self.current_backend, LocalSpeechBackend):
            self._pending_streaming_setup = True
            self._start_initial_whisper_load()
        elif isinstance(self.current_backend, LocalWhisperBackend):
            backend = self.transcription_backends.get("local_whisper")
            if backend is not None and getattr(backend, "load_deferred", False):
                self._pending_streaming_setup = True
                self._start_initial_whisper_load()
            else:
                QTimer.singleShot(0, self.ensure_local_model_available)
                if backend is not None and getattr(backend, "gpu_fallback_note", None):
                    self.models.gpu_fallback_detected.emit()
                QTimer.singleShot(0, self.streaming_runtime.setup_streaming)
        else:
            QTimer.singleShot(0, self.streaming_runtime.setup_streaming)
        # Migrations and legacy history import may be slow on first launch.
        # The completion signal starts dependent services after the shared
        # database is ready, without holding up the first event-loop paint.
        if not self._startup_database_requested:
            self._startup_database_requested = True
            self._startup_executor.submit(self._initialize_startup_database)
        self.component_executor.submit(self.updater.prune_leftovers)
        # Measures every installed runtime (a walk of ~30k files for Qwen)
        # and probes the system GPU runtime, both remembered, so Settings'
        # first open doesn't do it on the Qt thread.
        self.component_executor.submit(component_coordinator.list_components)
        # The GitHub metadata check waits out startup; after it, the timer
        # re-arms itself for as long as the app runs.
        self.updater.schedule_check(launch=True)
        # Share this computer's engine if Settings says so. Its own thread:
        # a first run creates the TLS certificate, and the two executors can
        # be busy for minutes with model loads and installs.
        threading.Thread(
            target=self.remote_engine.apply_host_settings,
            name="remote-host-start",
            daemon=True,
        ).start()
        self._warm_openai_sdk()
        try:
            from services.automation import service as automation_service

            automation_service.start(self)
        except Exception:
            logger.exception("Could not start automation")

    def _initialize_startup_database(self) -> None:
        error = ""
        try:
            db.ensure_initialized()
        except Exception as exc:
            error = str(exc) or "Could not initialize the database"
            logger.exception("Could not initialize the database")
        if not self._shutting_down:
            self.startup_database_ready.emit(error)

    def _on_startup_database_ready(self, error: str) -> None:
        if self._shutting_down:
            return
        if error:
            self.update_readiness_error = error
            self.ui_controller.set_status(error)
            return
        self._startup_database_ready = True
        self._startup_recovery_future = self.executor.submit(self.transcription_runtime.recover_recordings)
        self.meeting_runtime.setup()
        self.record_sync.start()
        self.remote_engine.start_client_history()
        if self.backup_coordinator is not None:
            self.backup_coordinator.start()

    def _warm_openai_sdk(self) -> None:
        """Build what this session's OpenAI clients need, on worker threads.

        Neither the API engine nor transcript cleanup builds its client at
        startup. The API engine's is built here; its uploads load the SDK in
        a worker process of their own (IsolatedOpenAIClient.warm). Cleanup
        runs in this process whichever engine transcribes, so a session that
        may clean up imports the SDK here, or its first cleanup would wait
        out that import, most of a second. Sessions that use neither never
        load it.
        """
        try:
            from services.cleanup_profiles import cleanup_may_run

            targets = []
            prepare = getattr(self.current_backend, "prepare_client", None)
            if callable(prepare):
                targets.append(prepare)
            if cleanup_may_run(settings_manager.load_all_settings()):
                targets.append(_import_openai_sdk)
            for target in targets:
                threading.Thread(
                    target=target, name="openai-sdk-warmup", daemon=True
                ).start()
        except Exception:
            logger.warning(
                "Could not start the openai SDK warm-up; the first request will import it",
                exc_info=True,
            )

    # ---- the selected engine ----

    def _on_local_engine_settings_changed(self) -> None:
        """A local engine's model, device or quantization changed in Settings."""
        if self._selected_remote() is not None:
            # They apply to this computer's engines, which Remote isn't using.
            return
        self.reload_whisper_model()

    def _selected_remote(self):
        """The Remote engine while it is the selected one, else None."""
        backend = self.current_backend
        return backend if getattr(backend, "is_remote", False) else None

    def ensure_local_model_available(self) -> None:
        """Make sure the selected local model is loaded; any thread (see ModelRuntime)."""
        self.models.ensure_local_model_available()

    def change_audio_device(self, _choice=None) -> None:
        """Rebuild the recorder after Settings saved a new microphone order."""
        if self.recorder.is_recording or self.transcription_runtime.has_active_job:
            logger.warning("Cannot change audio device while recording")
            self.ui_controller.set_status("Wait for the recording to finish before changing device")
            return

        self.recorder.cleanup()
        # Settings saved the choice before calling here, and the saved
        # microphone list is what the recorder opens from.
        self.recorder = AudioRecorder.from_settings()
        self.recorder.error_callback = self.recording_capture_failed.emit
        self.recorder.device_switch_callback = self.recording_device_switched.emit
        self.streaming_runtime.setup_audio_level_callback()

        logger.info("Microphone order changed; recorder rebuilt")
        self.ui_controller.set_status("Audio device changed")

    def update_hotkeys(self, hotkeys: Dict[str, str]) -> None:
        self.hotkey_runtime.update_hotkeys(hotkeys)

    def update_recording_trigger_mode(self, mode: str) -> None:
        self.hotkey_runtime.set_recording_trigger_mode(mode)

    def reconfigure_streaming(self) -> None:
        self.streaming_runtime.reconfigure_streaming()

    def start_recording(self, *, source: str = "hotkey") -> bool:
        """Start audio recording (UI callback and push-and-hold target).

        Args:
            source: Who asked, for the recording's events: "hotkey", or
                "ui" / "dbus" from the window, tray and desktop shell.

        Returns:
            False when the start was refused (Meeting Mode is active, or the
            local engine is still loading) so the caller can roll its
            recording UI back; True otherwise.
        """
        if self._refuse_dictation_during_meeting():
            return False
        unavailable = self.transcription_readiness_message()
        if unavailable:
            self.status_update.emit(unavailable)
            return False
        return self.transcription_runtime.start_recording(source=source) is not False

    def start_profile_recording(self, profile_id: str, *, source: str = "hotkey") -> bool:
        if self._refuse_dictation_during_meeting():
            return False
        return self.transcription_runtime.start_recording(profile_id, source=source) is not False

    def toggle_profile_recording(self, profile_id: str) -> None:
        # A shortcut pressed during capture only stops that capture. Its
        # profile snapshot remains the one chosen when recording started.
        if self.recorder.is_recording:
            self.stop_recording()
        else:
            self.start_profile_recording(profile_id)

    def start_action_recording(self, action_id: str, *, source: str = "hotkey") -> bool:
        """Record words for a saved action; False when refused."""
        if self._refuse_dictation_during_meeting():
            return False
        return self.transcription_runtime.start_recording(
            mode=JobMode.ACTION, action_id=action_id, source=source,
        ) is not False

    def toggle_action_recording(self, action_id: str) -> None:
        """An action's shortcut: stop the recording, or record for that action."""
        if self.recorder.is_recording:
            self.stop_recording()
        else:
            self.start_action_recording(action_id)

    def toggle_route_recording(self) -> None:
        """Route by voice: stop the recording, or record words for whichever action they name.

        Hotkey thread, like toggle_recording.
        """
        if self.recorder.is_recording:
            self.stop_recording()
            return
        if self._refuse_dictation_during_meeting():
            return
        from services.actions import routing

        status = routing.status(settings_manager.load_all_settings())
        if not status.ready:
            self.status_update.emit(status.message)
            return
        self.transcription_runtime.start_recording(route="hotkey", source="hotkey")

    def stop_recording(self) -> None:
        self.transcription_runtime.stop_recording()

    def toggle_recording(self) -> bool:
        """Toggle recording on/off (hotkey callback target).

        Returns:
            False when a start was refused because Meeting Mode is active;
            True otherwise.
        """
        if not self.recorder.is_recording and self._refuse_dictation_during_meeting():
            return False
        if not self.recorder.is_recording:
            unavailable = self.transcription_readiness_message()
            if unavailable:
                self.status_update.emit(unavailable)
                return False
        self.transcription_runtime.toggle_recording()
        return True

    def is_meeting_active(self) -> bool:
        """Whether Meeting Mode currently owns the microphone.

        Exposed to the UI layer (Settings dialog rule dictation) so features
        that open their own capture stream can refuse before starting one.

        Returns:
            True while a meeting session is running.
        """
        runtime = getattr(self, "meeting_runtime", None)
        return bool(
            runtime.is_claimed if runtime is not None else self.meeting_active
        )

    def _refuse_dictation_during_meeting(self) -> bool:
        if self._backup_in_progress:
            self.status_update.emit("Wait for backup or restore preparation to finish")
            return True
        if getattr(getattr(self, "ui_controller", None), "_meeting_setup_open", False) is True:
            self.status_update.emit("Close Meeting Mode setup before using Quick Record.")
            return True
        if not self.is_meeting_active():
            return False
        logger.info("Dictation start refused: Meeting Mode is active")
        self.status_update.emit(
            "Meeting Mode is active — end the meeting to use dictation"
        )
        return True

    def _refuse_engine_load_during_meeting(self) -> bool:
        """Refuse loading a model into the dictation engine mid-meeting.

        ``reload_whisper_model`` already gates the UI edge, but the Hugging
        Face workers reach ``reload_model`` / ``download_and_load`` directly
        on an executor thread without passing through it. A load landing
        mid-meeting would put a second copy of the weights beside the
        meeting's own model, which is what the engine lease exists to avoid.

        Returns:
            True when the caller must not load into the engine.
        """
        if not self.is_meeting_active():
            return False
        logger.info("Engine load refused: a meeting owns the Whisper engine")
        self.status_update.emit("End the meeting before changing the engine")
        return True

    def cancel(self) -> None:
        """The user's Cancel (shortcut, buttons, tray, D-Bus).

        Cancels the recording or transcription in progress, and only that:
        a Quick answer still thinking from before keeps going. With neither,
        it cancels the actions still running, which is how a long Quick
        answer is stopped. Either way the status says what was canceled,
        or that there was nothing to cancel (a dictation already pasted and
        being saved included). An AI write-up still running is stopped only
        when nothing is being recorded or transcribed.
        """
        runtime = self.transcription_runtime
        if not runtime.cancel() and not runtime.cancel_write_up():
            self.actions_cancel_requested.emit()

    def _cancel_actions(self) -> None:
        """The user's Cancel for the actions still running; Qt thread."""
        if not self.action_runtime.cancel():
            self.ui_controller.set_status(NOTHING_TO_CANCEL)
            self.ui_controller.show_notice(NOTHING_TO_CANCEL)

    def minimize_to_tray(self) -> None:
        """Toggle the main window between tray-hidden and foreground states.

        Hotkey callbacks run on a background thread, so this only emits a signal;
        the actual window change happens on the Qt main thread via the connection
        in ``_connect_signals``.
        """
        self.minimize_to_tray_requested.emit()

    def toggle_meeting_mode(self) -> None:
        """Start or end Meeting Mode from its optional global hotkey."""
        self.meeting_runtime.toggle_from_hotkey()

    def transcribe_clip(self, audio_path: str) -> str:
        """Transcribe a short audio clip outside the main recording flow.

        Used by the settings dialog's rule-dictation feature; called from a
        worker thread, returns the transcript synchronously.

        Args:
            audio_path: Path to the audio clip to transcribe.

        Returns:
            The transcript text.

        Raises:
            RuntimeError: When Meeting Mode is active, no backend is ready, or
                the engine is busy.
        """
        if self._backup_in_progress:
            raise RuntimeError("Wait for backup or restore preparation to finish")
        if self.host_update_active():
            raise RuntimeError("OpenWhisper is preparing an update. Try again shortly.")
        if self.is_meeting_active():
            # Exclusive mode: the meeting owns the microphone and a dedicated
            # Whisper instance; a second capture stream and model would fight
            # it for the device and the GPU.
            raise RuntimeError(
                "Meeting Mode is active — end the meeting to use dictation"
            )
        backend = self.current_backend
        if backend is None:
            raise RuntimeError("No transcription engine is available")
        if self.is_transcribing():
            raise RuntimeError("Transcription engine is busy")
        return backend.transcribe(audio_path)

    def retranscribe_audio(self, audio_path: str) -> None:
        if not self._refuse_dictation_during_meeting():
            self.transcription_runtime.retranscribe_audio(audio_path)

    def upload_audio_file(
        self, audio_path: str, duration_seconds: Optional[float] = None
    ) -> None:
        if not self._refuse_dictation_during_meeting():
            self.transcription_runtime.upload_audio_file(
                audio_path, duration_seconds=duration_seconds
            )

    def upload_audio_files(self, request) -> None:
        if not self._refuse_dictation_during_meeting():
            self.transcription_runtime.upload_audio_files(request)

    def on_model_changed(self, model_name: str) -> None:
        if self.recorder.is_recording or self.is_transcribing():
            self.status_update.emit("Finish recording or transcription before changing engines")
            return
        if not self._refuse_dictation_during_meeting():
            self.models.forget_runtime_prompts()
            self.transcription_runtime.on_model_changed(model_name)

    def update_status_with_auto_hide(self, status: str) -> None:
        self.hotkey_runtime.update_status_with_auto_hide(status)

    def _connect_signals(self) -> None:
        self.startup_database_ready.connect(self._on_startup_database_ready)
        self.actions_cancel_requested.connect(self._cancel_actions)
        self.transcription_completed.connect(self._on_transcription_complete)
        self.transcription_failed.connect(self._on_transcription_error)
        self.walkthrough_written_up.connect(self.transcription_runtime.on_walkthrough_written_up)
        self.batch_completed.connect(self._on_batch_complete)
        self.batch_progress.connect(self.ui_controller.set_batch_progress)
        self.batch_item_finished.connect(self.ui_controller.set_batch_item_finished)
        self.large_file_detected.connect(self.ui_controller.show_large_file_state)
        self.models.connect_signals()
        self.remote.connect_signals()
        self.hotkey_runtime.connect_signals()
        self.notice_requested.connect(
            lambda text, done: self.ui_controller.show_notice(text, done=done)
        )
        self.recording_state_changed.connect(self.walkthrough_runtime.on_recording_state_changed)
        self.ui_controller.on_walkthrough = self.walkthrough_runtime.toggle
        self.ui_controller.on_screenshot = self.walkthrough_runtime.snap
        self.recording_device_switched.connect(
            lambda old, new: self.ui_controller.on_recording_device_switched(old, new)
        )
        self.dictionary_term_learned.connect(
            lambda term: self.ui_controller.on_dictionary_term_learned(term)
        )
        # Learning saves from a worker thread; the queued signal brings the
        # notice to the Qt thread.
        dictionary.set_learned_callback(self.dictionary_term_learned.emit)
        self.status_update.connect(self.ui_controller.set_status)
        self.recording_capture_failed.connect(self.transcription_runtime.on_capture_error)
        self.history_persisted.connect(self.transcription_runtime.on_history_persisted)
        self.device_info_update.connect(self.ui_controller.set_device_info)
        self.engine_busy_changed.connect(self.ui_controller.set_engine_busy)
        self.engine_busy_changed.connect(self.remote.on_engine_busy)
        self.streaming_runtime.connect_signals()
        self.components.connect_signals()
        self.meeting_runtime.connect_signals()
        self.updater.connect_signals()
        if hasattr(self.ui_controller, "set_overlay_state"):
            self.overlay_state_update.connect(self.ui_controller.set_overlay_state)
        if hasattr(self.ui_controller, "show_transcription_retry"):
            self.transcription_retry_progress.connect(self.ui_controller.show_transcription_retry)
        self.recording_state_changed.connect(self._on_recording_state_changed)
        self.recording_destination_changed.connect(
            self.ui_controller.overlay.set_recording_destination
        )
        self.minimize_to_tray_requested.connect(
            self.ui_controller.main_window.toggle_tray_visibility
        )

    def _publish_preview(self, text: str, is_final: bool) -> None:
        """Live preview text as transcription.preview; Qt thread."""
        self._preview_events.offer(self.transcription_runtime._job, text, is_final)

    def _on_recording_state_changed(self, is_recording: bool) -> None:
        if is_recording:
            self.ui_controller.on_dictation_started(self.transcription_runtime._recording_profile)
        self.ui_controller.is_recording = is_recording
        self.ui_controller.main_window.is_recording = is_recording
        self.ui_controller.main_window._update_recording_state()

    def _on_transcription_complete(
        self, transcript: str, raw_text=None, cleanup_info=None
    ) -> None:
        self.transcription_runtime.on_transcription_complete(
            transcript, raw_text, cleanup_info
        )

    def _on_transcription_error(self, error_message: str) -> None:
        self.transcription_runtime.on_transcription_error(error_message)

    def _on_batch_complete(self, result) -> None:
        self.transcription_runtime.on_batch_complete(result)

    def cleanup(self) -> None:
        """Release application resources in dependency order."""
        logger.info("Starting application cleanup...")
        shutdown_started = time.perf_counter()
        self._shutting_down = True
        if self.backup_coordinator is not None:
            self.backup_coordinator.shutdown()
        self._startup_executor.shutdown(wait=True, cancel_futures=True)
        if hasattr(self, "_shutdown_cancel"):
            self._shutdown_cancel.set()
        self.models.cancel_model_batch_download()
        _attempt(lambda: self.walkthrough_runtime.cleanup(), "Error stopping Auto Mode")
        self.transcription_runtime.begin_shutdown()
        cancel_library = getattr(self.ui_controller, "cancel_walkthrough_library", None)
        if callable(cancel_library):
            cancel_library()

        _attempt(lambda: self.command_runtime.cleanup(), "Error during command runtime cleanup")
        _attempt(self._stop_automation, "Error stopping automation")
        _attempt(_stop_playback_and_focus_capture, "Error stopping playback or focus capture")
        _attempt(self._cancel_active_transcription, "Error canceling transcription")
        _attempt(self._stop_timers, "Error stopping watchdog timers")

        # Close every backend before joining the executor: it may be waiting
        # inside model construction, preview warmup, or a network request, even
        # when is_transcribing is False. These process/connection boundaries
        # interrupt running work; cancel_futures alone cannot do that.
        self._interrupt_backends()

        # Shut the meeting engine down early: it owns capture streams, a
        # web server, and possibly a sidecar process.
        _attempt(lambda: self.meeting_runtime.cleanup(), "Error during meeting runtime cleanup")
        _attempt(lambda: self.hotkey_runtime.cleanup(), "Error during hotkey runtime cleanup")
        _attempt(lambda: _cleanup_if_present(self.hotkey_manager), "Error during hotkey cleanup")
        _attempt(lambda: _cleanup_if_present(self.recorder), "Error during recorder cleanup")
        _attempt(lambda: self.streaming_runtime.cleanup(), "Error during streaming cleanup")
        # Must happen before the shutdown below: cancel_futures only
        # cancels futures that have not started, so an in-flight
        # multi-gigabyte download would hold shutdown open for minutes.
        # The download loop checks its cancel event once per MiB.
        _attempt(lambda: component_coordinator.cancel_all(), "Error cancelling component installs")
        # An upload in progress resumes from where it stopped next time.
        _attempt(lambda: self.record_sync.stop(timeout=2.0), "Error stopping the record sync")
        _attempt(self._stop_remote_engine, "Error stopping the remote engine host")

        for executor in (self.executor, self.component_executor, self.walkthrough_executor):
            _attempt(lambda: executor.shutdown(wait=True, cancel_futures=True),
                     "Error during executor shutdown")

        # Accepted transcripts must survive Quit even while another history
        # save is running. Drain this queue before SQLite or UI resources close.
        self.persistence_executor.shutdown(wait=True, cancel_futures=False)

        _attempt(self._cleanup_backends, "Error during transcription backends cleanup")
        _attempt(lambda: self.ui_controller.cleanup(), "Error during UI controller cleanup")
        _attempt(lambda: db.close(), "Error closing database")

        logger.info("Application controller cleaned up")
        from services.diagnostics import record_metrics
        record_metrics(shutdown_s=time.perf_counter() - shutdown_started)

    def _stop_automation(self) -> None:
        self.action_runtime.close()
        from services.automation import service as automation_service

        automation_service.stop()

    def _cancel_active_transcription(self) -> None:
        if self.current_backend and self.current_backend.is_transcribing:
            logger.info("Canceling ongoing transcription...")
            self.current_backend.cancel_transcription()

    def _stop_timers(self) -> None:
        for name in (
            "_watchdog_timer", "_reload_timer", "_periodic_refresh_timer",
        ):
            if hasattr(self, name) and getattr(self, name):
                getattr(self, name).stop()
        remote = getattr(self, "remote", None)
        if remote is not None:
            remote.stop_timers()
        updater = getattr(self, "updater", None)
        if updater is not None:
            updater.stop()

    def _interrupt_backends(self) -> None:
        for name, backend in self.transcription_backends.items():
            try:
                backend.cancel_transcription()
                backend.cleanup()
            except Exception as exc:
                logger.warning("Error interrupting %s: %s", name, exc, exc_info=True)
        from services.local_asr.process import close_all_workers
        close_all_workers()

    def _stop_remote_engine(self) -> None:
        self.remote_engine.shutdown()
        # A model switch waits minutes for the host; the executor below
        # would wait with it, so end the connection first.
        remote = self.transcription_backends.get("remote")
        if remote is not None:
            remote.cleanup()

    def _cleanup_backends(self) -> None:
        for backend_name, backend in self.transcription_backends.items():
            try:
                logger.info(f"Cleaning up transcription backend: {backend_name}")
                backend.cleanup()
            except Exception as exc:
                logger.warning(f"Error cleaning up {backend_name} backend: {exc}", exc_info=True)
        self.transcription_backends.clear()
        self.current_backend = None


def _attempt(step: Callable[[], object], failure: str) -> None:
    """Run one shutdown step; a failure is logged as ``failure: error`` and skipped."""
    try:
        step()
    except Exception as exc:
        logger.warning(f"{failure}: {exc}", exc_info=True)


def _cleanup_if_present(resource) -> None:
    if resource:
        resource.cleanup()


def _stop_playback_and_focus_capture() -> None:
    from services import audio_player, focus_context

    audio_player.stop_playback()
    # Its capture threads hold accessibility (COM) objects that must
    # be released before the interpreter shuts down.
    focus_context.shutdown_service()

# ---- ApplicationController helpers taking the controller explicitly ----
# Tests bind the methods that call these onto minimal stand-ins, which carry
# only the methods they bind, so these helpers live outside the class.


def _release_unselected_engines(controller, selected) -> None:
    from services.local_asr.catalog import BACKENDS
    for key in ("local_whisper", *BACKENDS, "remote"):
        other = controller.transcription_backends.get(key)
        if other is not None and other is not selected:
            other.cleanup()


def _reload_speech_engine(controller, selected) -> Optional[TranscriptionBackend]:
    """Reload an optional speech engine; return it when it still has to be warmed."""
    warm = None
    try:
        selected.reload_model()
        if selected is not controller.current_backend:
            selected.cleanup()
            return None
        _announce_speech_engine(controller, selected)
        _preload_recording_reader(selected)
        if selected.is_model_missing:
            controller.ensure_local_model_available()
        elif selected.is_available() and selected.backend_id in config.SPEECH_WARMUP_BACKENDS:
            warm = selected
    except Exception as exc:
        logger.error("Engine load failed: %s", exc, exc_info=True)
        controller.status_update.emit(f"Engine load failed: {exc}")
        controller.device_info_update.emit(str(exc), False)
    finally:
        if warm is None:
            controller._finish_speech_reload(selected)
    return warm


def _announce_speech_engine(controller, selected) -> None:
    controller.device_info_update.emit(selected.device_info, selected.is_available())
    controller.status_update.emit(selected.device_info)
    # A paired computer that kept its model says why.
    if getattr(selected, "switch_error", ""):
        controller.status_update.emit(selected.switch_error)
    # Auto fell back to the CPU; say why after the ready line.
    if getattr(selected, "cpu_fallback_note", ""):
        controller.status_update.emit(selected.cpu_fallback_note)


def _speech_engine_readiness_message(controller, backend) -> Optional[str]:
    if controller._reload_in_flight:
        return "Speech engine is still loading..."
    if getattr(backend, "is_remote", False):
        return _remote_engine_readiness_message(controller, backend)
    if backend.is_model_missing:
        controller.ensure_local_model_available()
    elif backend.should_cancel or not backend.last_error:
        controller.reload_whisper_model()
        return "Reloading speech engine..."
    return backend.device_info


def _remote_engine_readiness_message(controller, backend) -> str:
    # The host may be back (woke up, came into range): try now
    # rather than at the next scheduled retry.
    controller.remote.retry_now()
    host = backend.host_name or "the remote engine"
    if controller.remote.restoring or not backend.last_error:
        return f"Connecting to {host}..."
    return f"{backend.last_error} Trying again..."

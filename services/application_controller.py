"""Main Qt-facing application controller."""

from __future__ import annotations

import logging
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from PyQt6.QtCore import QObject, pyqtSignal, QTimer

from config import config
from services.component_runtime import activate_component
from services.components import (
    ComponentCanceled,
    ComponentError,
    ComponentId,
    available_component_ids,
    component_coordinator,
    gpu_runtime_available,
    install_component,
    uninstall_component,
)
from services.credentials import resolve_credential
from services.database import db
from services.hf_access import (
    AccessDecision,
    ConsentAction,
    delete_model_from_cache,
    download_model_files,
    hf_access_coordinator,
    is_hf_hub_offline_env_set,
    is_model_cached,
    resolve_model_repo,
)
from services.recorder import AudioRecorder
from services.runtime import (
    HotkeyRuntime,
    MeetingRuntime,
    StreamingRuntime,
    TranscriptionRuntime,
)
from services.settings import HuggingFaceAccessPolicy, SettingsKey, settings_manager
from transcriber import (
    GpuFallbackCause,
    LocalWhisperBackend,
    OpenAIBackend,
    TranscriptionBackend,
)

logger = logging.getLogger(__name__)

#: How often the Remote engine's watch reads its connection. Reading sends
#: nothing; websockets keeps the socket's state current on its own thread.
REMOTE_WATCH_INTERVAL_MS = 1000
#: Seconds before each automatic reconnect to an unreachable host; the last
#: repeats until it answers.
REMOTE_RETRY_DELAYS_S = (2, 5, 15, 30, 60)


def _import_openai_sdk() -> None:
    """Warm-up target: a later client build then only constructs the client."""
    try:
        import openai  # noqa: F401
    except Exception:
        logger.debug("openai SDK warm-up failed", exc_info=True)


@dataclass
class _ClientModelSwitch:
    """A paired computer's model choice, answered on the Qt thread."""

    family: str
    model: str
    device_name: str
    runtime: Optional[dict] = None
    target_device: Optional[str] = None
    #: Why the switch can't happen now; empty once it is queued.
    error: Optional[str] = None
    done: threading.Event = field(default_factory=threading.Event)


class ApplicationController(QObject):
    """Main application controller integrating UI and logic."""

    # fixed text, optional raw text, optional CleanupInfo
    transcription_completed = pyqtSignal(str, object, object)
    transcription_failed = pyqtSignal(str)
    remote_catalog_received = pyqtSignal(object, object, object)
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
    large_file_detected = pyqtSignal(float, bool)
    status_update = pyqtSignal(str)
    stt_state_changed = pyqtSignal(bool)
    recording_state_changed = pyqtSignal(bool)
    profile_record_requested = pyqtSignal(str)
    partial_transcription = pyqtSignal(str, bool)
    streaming_text_update = pyqtSignal(str, bool)
    streaming_overlay_show = pyqtSignal()
    streaming_overlay_hide = pyqtSignal()
    overlay_state_update = pyqtSignal(object)
    minimize_to_tray_requested = pyqtSignal()
    # Emitted from the background reload worker (thread-safe UI updates).
    # (readout, engine is loaded and usable)
    device_info_update = pyqtSignal(str, bool)
    engine_busy_changed = pyqtSignal(bool)
    # The Remote engine's link may read differently (emitted from any thread;
    # the slot takes a fresh RemoteLink on the Qt thread).
    remote_link_poke = pyqtSignal()
    # The Remote engine settled: a background reconnect's restore_link()
    # outcome, or "reload" when a reload of it finished.
    remote_settled = pyqtSignal(str)
    # Reconnect now (emitted from any thread; hotkeys start recordings off it).
    remote_retry_requested = pyqtSignal()
    # Arm the engine reload's debounce timer (emitted from any thread; hotkeys
    # and transcription workers ask for reloads off the Qt thread).
    reload_debounce_requested = pyqtSignal()
    # Host: the paired computers connected now, as connected_clients() lists them.
    remote_clients_changed = pyqtSignal(list)
    # Hop streaming setup onto the Qt main thread after the first local load.
    streaming_setup_requested = pyqtSignal()
    # Consent for Hugging Face model downloads: emitted (possibly from worker
    # threads) with (model_name, env_blocked, load_into_engine); the connected
    # slot shows the consent dialog on the Qt main thread. load_into_engine is
    # True for the selected-model flow (download + load) and False for
    # Settings → Downloads fetch-only downloads.
    hf_consent_requested = pyqtSignal(str, bool, bool)
    runtime_consent_requested = pyqtSignal(str)
    model_download_started = pyqtSignal(str)
    model_download_progress = pyqtSignal(str, int, int)
    model_download_finished = pyqtSignal(str, bool)
    model_deleted = pyqtSignal(str, bool, str)
    model_cache_changed = pyqtSignal()
    # Batched fetch-only downloads (Settings → Downloads): the approved plan up
    # front, then started/finished per model, then (completed, planned).
    batch_download_planned = pyqtSignal(list)
    batch_download_finished = pyqtSignal(int, int)
    # Downloadable components (emitted possibly from worker threads).
    # (component_id, phase, done_units, total_units); throttled in the worker
    # so a multi-gigabyte download cannot flood the Qt event queue.
    component_progress = pyqtSignal(str, str, int, int)
    component_install_started = pyqtSignal(str)
    # (component_id, success, message). Unlike model downloads there are many
    # distinct user-facing failure causes, so the message is carried along.
    component_install_finished = pyqtSignal(str, bool, str)
    component_state_changed = pyqtSignal()
    # A local model load ended in a GPU→CPU fallback (emitted possibly from
    # worker threads); the connected slot reverts the persisted device setting
    # and raises a cause-specific warning on the Qt main thread.
    gpu_fallback_detected = pyqtSignal()
    # Meeting Mode signals may originate from engine worker threads.
    meeting_engine_event = pyqtSignal(object, str, object)
    meeting_state_changed = pyqtSignal(object)
    meeting_status_update = pyqtSignal(str)
    meeting_error = pyqtSignal(str)
    meeting_server_started = pyqtSignal(object)
    meeting_recovery_found = pyqtSignal(object)
    # One-time cloud-intelligence consent; the connected slot shows the
    # consent dialog on the Qt main thread and routes the result back into
    # MeetingRuntime.on_consent_result (mirrors hf_consent_requested).
    meeting_consent_requested = pyqtSignal()
    # Unsupported-platform acknowledgement before a hotkey start; hops to
    # the Qt main thread the same way meeting_consent_requested does.
    meeting_platform_ack_requested = pyqtSignal()
    # Guest URL ready for clipboard copy (clipboard access is main-thread only).
    meeting_guest_link_ready = pyqtSignal(str)
    # Past Meetings sidebar rebuild; always hop to the Qt GUI thread.
    past_meetings_refresh_requested = pyqtSignal()
    # App updater: (result_or_none, error, manual).
    update_check_finished = pyqtSignal(object, str, bool)
    update_download_progress = pyqtSignal(object, str, int, int)
    update_download_finished = pyqtSignal(object, object, str)
    # The Remote engine's pairing changed (emitted from the pairing worker).
    remote_pairing_changed = pyqtSignal()
    # Records moved between this computer and its paired host, either way.
    records_changed = pyqtSignal()
    # A paired computer chose one of this computer's models (emitted from its
    # connection thread with a _ClientModelSwitch the slot answers).
    client_model_switch_requested = pyqtSignal(object)

    def __init__(self, ui_controller, local_backend: Optional[LocalWhisperBackend] = None):
        super().__init__()
        self.ui_controller = ui_controller

        saved_device_id = settings_manager.load_audio_input_device()
        self.recorder = AudioRecorder(device_id=saved_device_id)
        self.executor = ThreadPoolExecutor(max_workers=2)
        # Component installs get their own single worker. They can run for
        # tens of minutes, and letting one occupy the shared 2-worker pool
        # would starve transcription and model loading.
        self.component_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="component"
        )

        self.hotkey_manager = None
        self.streaming_transcriber = None
        self._streaming_backend = None
        # Set from the Qt thread, read in the batch download worker; a plain
        # bool flag is enough because stopping is only checked between models.
        self._batch_stop_requested = False
        self._runtime_prompted = set()
        # The "Use this GPU" offer: None until shown, then "shown" while it is
        # open and "answered" after. A missing model's download consent waits
        # for it, since the answer decides which model and device to fetch for.
        self._gpu_offer_state: Optional[str] = None

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

        # The Remote engine's watch: a tick that reads the socket's state
        # (nothing is sent) while Remote is selected, and a backed-off quiet
        # reconnect when the host drops or can't be reached.
        self._remote_watch = QTimer()
        self._remote_watch.setInterval(REMOTE_WATCH_INTERVAL_MS)
        self._remote_watch.timeout.connect(self._on_remote_watch)
        self._remote_retry_timer = QTimer()
        self._remote_retry_timer.setSingleShot(True)
        self._remote_retry_timer.timeout.connect(self._retry_remote)
        self._remote_retry_step = 0
        self._remote_retry_at: Optional[float] = None
        self._remote_restoring = False
        self._remote_link = None
        # device_info the watch last reported, so repeat failures stay quiet.
        self._remote_reported = ""

        self._update_check_timer = QTimer()
        self._update_check_timer.setSingleShot(True)
        self._update_check_timer.timeout.connect(self._maybe_start_update_check)
        self._update_cancel = threading.Event()
        self._update_attempt = None
        self._update_download_lock = threading.Lock()
        self.update_readiness_error = ""

        # Exclusive mode: True while a meeting session runs; dictation start
        # paths and engine reloads refuse while set (owned by MeetingRuntime).
        self.meeting_active = False

        self.hotkey_runtime = HotkeyRuntime(self)
        self.streaming_runtime = StreamingRuntime(self)
        self.transcription_runtime = TranscriptionRuntime(self)
        self.meeting_runtime = MeetingRuntime(self)

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
        self.transcription_backends["api"] = OpenAIBackend("api")

        from services.local_asr.catalog import BACKENDS
        from transcriber.optional_backend import LocalSpeechBackend
        for key in BACKENDS:
            self.transcription_backends[key] = LocalSpeechBackend(key)
        from transcriber.remote_backend import REMOTE_BACKEND, RemoteSpeechBackend
        remote = RemoteSpeechBackend()
        remote.on_link_changed = self.remote_link_poke.emit
        self.transcription_backends[REMOTE_BACKEND] = remote
        from services.remote_asr.service import RemoteEngineService
        # Serves whichever engine is selected here, when sharing is on, and
        # lets paired computers switch it to another model ready here.
        self.remote_engine = RemoteEngineService(
            lambda: self.current_backend,
            on_client_changed=self.remote_pairing_changed.emit,
            on_host_catalog=self.remote_catalog_received.emit,
            switch_engine=self._switch_engine_for_client,
            configure_engine=self._configure_engine_for_client,
            engine_settled=self._engine_settled,
        )
        self.remote_engine.add_listener(self._on_remote_host_event)
        self.ui_controller.remote_engine = self.remote_engine
        from services.remote_records.sync import record_sync
        # Where this computer's records go while paired; waits on the meeting
        # runtime so a meeting is sent only once nothing more is written to it.
        self.record_sync = record_sync
        meetings = record_sync.kinds.get("meeting")
        if meetings is not None:
            meetings.busy = self.meeting_runtime.meeting_busy
            meetings.in_use_check = self.meeting_runtime.meeting_in_use
        record_sync.add_listener(self._on_record_sync_event)
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
        self.ui_controller.on_upload_audio = self.upload_audio_file
        self.ui_controller.on_upload_audio_files = self.upload_audio_files
        self.ui_controller.on_upload_cancel = self.cancel
        self.ui_controller.on_whisper_settings_changed = self._on_local_engine_settings_changed
        self.ui_controller.on_audio_device_changed = self.change_audio_device
        self.ui_controller.on_streaming_settings_changed = self.reconfigure_streaming
        self.ui_controller.on_hf_policy_changed = self.on_hf_policy_changed
        self.ui_controller.on_api_keys_changed = self.on_api_keys_changed
        self.ui_controller.on_model_download_requested = self.request_model_download
        self.ui_controller.on_model_delete_requested = self.request_model_delete
        self.ui_controller.on_model_batch_download = (
            self.request_model_batch_download
        )
        self.ui_controller.on_model_batch_stop = self.cancel_model_batch_download
        self.ui_controller.get_loaded_local_model = self.get_loaded_local_model
        self.ui_controller.get_remote_models = self.remote_models
        self.ui_controller.on_remote_model_selected = self.select_remote_model
        self.ui_controller.on_remote_runtime_selected = self.select_remote_runtime
        self.ui_controller.get_missing_local_runtime = self.get_missing_local_runtime
        self.ui_controller.on_dictation_transcribe = self.transcribe_clip
        self.ui_controller.get_meeting_active = self.is_meeting_active
        self.ui_controller.on_component_install = self.request_component_install
        self.ui_controller.on_component_cancel = self.cancel_component_install
        self.ui_controller.on_component_remove = self.request_component_uninstall
        self.ui_controller.on_check_for_updates = (
            lambda: self.request_update_check(manual=True)
        )
        self.ui_controller.on_update_download = self.request_update_download
        self.ui_controller.on_update_cancel = self.cancel_update_download
        self.ui_controller.on_update_abandon = self.discard_update_handoff
        self.ui_controller.get_transcribing = self.is_transcribing
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
        self.ui_controller.on_meeting_start_new = (
            self.meeting_runtime.start_new_meeting
        )

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
        if self.recorder.is_recording or self.is_transcribing():
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

    def _reload_worker(self) -> None:
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
            if self.is_meeting_active() or self.recorder.is_recording or self.is_transcribing():
                self._reload_in_flight = False
                self.engine_busy_changed.emit(False)
                return None
            from transcriber.optional_backend import LocalSpeechBackend
            from services.local_asr.catalog import BACKENDS
            selected = self.current_backend
            for key in ("local_whisper", *BACKENDS, "remote"):
                other = self.transcription_backends.get(key)
                if other is not None and other is not selected:
                    other.cleanup()
            if isinstance(selected, LocalSpeechBackend):
                warm = None
                try:
                    selected.reload_model()
                    if selected is not self.current_backend:
                        selected.cleanup()
                        return None
                    self.device_info_update.emit(selected.device_info, selected.is_available())
                    self.status_update.emit(selected.device_info)
                    # A paired computer that kept its model says why.
                    if getattr(selected, "switch_error", ""):
                        self.status_update.emit(selected.switch_error)
                    if selected.is_model_missing:
                        self.ensure_local_model_available()
                    elif selected.is_available() and selected.backend_id in config.SPEECH_WARMUP_BACKENDS:
                        warm = selected
                except Exception as exc:
                    self.status_update.emit(f"Engine load failed: {exc}")
                    self.device_info_update.emit(str(exc), False)
                finally:
                    if warm is None:
                        self._finish_speech_reload(selected)
                return warm
            if self._current_model_name == "local_whisper":
                self._reload_whisper_worker()
            else:
                self._reload_in_flight = False
                self.engine_busy_changed.emit(False)
            return None

    def _finish_speech_reload(self, selected) -> None:
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
            self.runtime_consent_requested.emit(selected.model_name)
        # The preview shares this worker, so it can only be set up
        # once the load has settled. A remote engine is whatever the host
        # runs now, so its preview is checked again after every reload.
        if remote:
            self._pending_streaming_setup = True
        self._flush_pending_streaming_setup()
        if remote and selected is self.current_backend:
            # Starts the link watch, and retries a host that didn't answer.
            self.remote_settled.emit("reload")

    def _reload_whisper_worker(self) -> None:
        """Reload the local backend off the UI thread; report results via signals.

        Runs on a ThreadPoolExecutor worker, so it must NOT touch the UI
        directly — all updates go through Qt signals, which are delivered on the
        main thread.
        """
        try:
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
                    self.gpu_fallback_detected.emit()
            else:
                logger.warning("Local whisper backend not found")
                self.status_update.emit("Ready")
        except Exception as exc:
            logger.error(f"Whisper reload failed: {exc}")
            self.status_update.emit("Engine reload failed")
        finally:
            self._reload_in_flight = False
            self.engine_busy_changed.emit(False)
            self._flush_pending_streaming_setup()

    def _flush_pending_streaming_setup(self) -> None:
        if self._pending_streaming_setup:
            self._pending_streaming_setup = False
            self.streaming_setup_requested.emit()

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
            if self._reload_in_flight:
                return "Speech engine is still loading..."
            if getattr(backend, "is_remote", False):
                # The host may be back (woke up, came into range): try now
                # rather than at the next scheduled retry.
                self.retry_remote_now()
                host = backend.host_name or "the remote engine"
                if self._remote_restoring or not backend.last_error:
                    return f"Connecting to {host}..."
                return f"{backend.last_error} Trying again..."
            if backend.is_model_missing:
                self.ensure_local_model_available()
            elif backend.should_cancel or not backend.last_error:
                self.reload_whisper_model()
                return "Reloading speech engine..."
            return backend.device_info
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

    def request_update_check(self, manual: bool = True) -> None:
        """Run a GitHub latest-release check on a worker thread."""
        self.executor.submit(self._update_check_worker, manual)

    def _maybe_start_update_check(self) -> None:
        """Automatic check: skip while busy, or when prefs/throttle say no."""
        from services.app_update import should_auto_check

        if getattr(self.recorder, "is_recording", False) or self.is_meeting_active():
            self._update_check_timer.start(config.UPDATE_CHECK_DELAY_MS)
            return
        try:
            settings = settings_manager.load_all_settings()
        except Exception as exc:
            logger.warning("Could not load update-check settings: %s", exc)
            return
        if not should_auto_check(settings):
            return
        self.request_update_check(manual=False)

    def _update_check_worker(self, manual: bool) -> None:
        from services.app_update import AppUpdateError, check_for_update

        try:
            result = check_for_update()
        except Exception as exc:
            logger.warning("Update check failed: %s", exc)
            message = str(exc) if isinstance(exc, AppUpdateError) else (
                "Could not check for updates."
            )
            self.update_check_finished.emit(None, message, manual)
            return
        self.update_check_finished.emit(result, "", manual)

    def _on_update_check_finished(self, result, error: str, manual: bool) -> None:
        if error and not manual:
            from services.app_update import update_check_failure_status

            self.status_update.emit(update_check_failure_status(error))
        handler = getattr(self.ui_controller, "on_update_check_finished", None)
        if handler:
            handler(result, error, manual)

    def request_update_download(self, result, force_setup: bool = False) -> None:
        """Download and prepare one verified update attempt on the worker."""
        # Never clear and reuse a prior token: a quick retry must not revive a
        # canceled worker that has not unwound yet.
        self._update_cancel.set()
        cancel = threading.Event()
        attempt = object()
        self._update_cancel = cancel
        self._update_attempt = attempt
        self.component_executor.submit(
            self._update_download_worker,
            result,
            force_setup,
            cancel,
            attempt,
        )

    def cancel_update_download(self) -> None:
        self._update_cancel.set()

    def is_transcribing(self) -> bool:
        runtime = getattr(self, "transcription_runtime", None)
        if runtime is not None and runtime.has_active_job:
            return True
        backend = getattr(self, "current_backend", None)
        return bool(backend and getattr(backend, "is_transcribing", False))

    def _update_download_worker(
        self,
        result,
        force_setup: bool = False,
        cancel: Optional[threading.Event] = None,
        attempt=None,
    ) -> None:
        token = cancel if cancel is not None else self._update_cancel

        def is_current() -> bool:
            return attempt is None or self._update_attempt is attempt

        release = getattr(result, "release", None)
        if release is None:
            if is_current():
                self.update_download_finished.emit(attempt, None, "No installer is available.")
            return

        def progress(phase: str, done: int, total: int) -> None:
            if is_current() and not token.is_set():
                self.update_download_progress.emit(attempt, phase, done, total)

        from services.app_update import apply_update

        try:
            with self._update_download_lock:
                handoff = apply_update(
                    result,
                    progress=progress,
                    cancel=token,
                    force_setup=force_setup,
                )
        except Exception as exc:
            logger.warning("Update download failed: %s", exc)
            message = str(exc) if str(exc) else "The download failed."
            if is_current():
                self.update_download_finished.emit(attempt, None, message)
            return
        if token.is_set() or not is_current():
            from services.app_update import discard_prepared_result

            discard_prepared_result(handoff)
            if is_current():
                self.update_download_finished.emit(attempt, None, "The download was canceled.")
            return
        self.update_download_finished.emit(attempt, handoff, "")

    def discard_update_handoff(self, handoff: str) -> None:
        from services.app_update import discard_prepared_result

        self.component_executor.submit(discard_prepared_result, handoff)

    def _on_update_download_progress(self, attempt, phase, done, total) -> None:
        if attempt is self._update_attempt and not self._update_cancel.is_set():
            self.ui_controller.on_update_download_progress(phase, done, total)

    def _on_update_download_finished(self, attempt, handoff, error) -> None:
        if attempt is not self._update_attempt or self._update_cancel.is_set():
            if handoff:
                self.discard_update_handoff(handoff)
            if attempt is self._update_attempt:
                self.ui_controller.on_update_download_finished(
                    None, "The update was canceled."
                )
            return
        self.ui_controller.on_update_download_finished(handoff, error)

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
                    self.gpu_fallback_detected.emit()
                QTimer.singleShot(0, self.streaming_runtime.setup_streaming)
        else:
            QTimer.singleShot(0, self.streaming_runtime.setup_streaming)
        # Meeting crash recovery: scan now that there is a UI to show the
        # recovery dialog over. Initialize SQLite on this thread first so
        # the two setup workers do not race create_all on a missing file.
        try:
            db.ensure_initialized()
        except Exception as exc:
            self.update_readiness_error = str(exc) or "Could not initialize the database"
            logger.exception("Could not initialize the database")
        QTimer.singleShot(0, self.meeting_runtime.setup)
        self.component_executor.submit(self._prune_update_leftovers)
        # Defer the GitHub metadata check so HF consent / recovery win the
        # first modal slot, and so a recording or meeting can start first.
        self._update_check_timer.start(config.UPDATE_CHECK_DELAY_MS)
        # Share this computer's engine if Settings says so. Its own thread:
        # a first run creates the TLS certificate, and the two executors can
        # be busy for minutes with model loads and installs.
        threading.Thread(
            target=self.remote_engine.apply_host_settings,
            name="remote-host-start",
            daemon=True,
        ).start()
        # Records kept on (or copied to) the paired host: after the database
        # is initialized above, on the sync's own thread.
        self.record_sync.start()
        self._warm_openai_sdk()

    def _warm_openai_sdk(self) -> None:
        """Import the openai SDK on a worker when this session is set to use it.

        Neither the API engine nor transcript cleanup builds its client at
        startup, so without this the first API dictation or cleanup would
        wait out the SDK's import, most of a second. Sessions that use
        neither never load it.
        """
        try:
            target = getattr(self.current_backend, "prepare_client", None)
            if not callable(target):
                from services.cleanup_profiles import cleanup_may_run

                if not cleanup_may_run(settings_manager.load_all_settings()):
                    return
                target = _import_openai_sdk
            threading.Thread(
                target=target, name="openai-sdk-warmup", daemon=True
            ).start()
        except Exception:
            logger.debug("Could not start the openai SDK warm-up", exc_info=True)

    # ---- remote engine: choosing the paired computer's model ----

    def remote_models(self):
        """What the Remote engine's Model field shows (a ``RemoteModels``)."""
        remote = self.transcription_backends.get("remote")
        return remote.model_choices() if remote is not None else None

    def select_remote_model(self, family: str, model: str) -> None:
        """Client: switch the paired computer to one of its models.

        The choice rides on the next reload, which asks the host to load it
        and reconnects to what the host then runs, so the Model field's
        switch shows the same busy state as any other engine change.
        """
        remote = self.transcription_backends.get("remote")
        if remote is None or self.current_backend is not remote:
            return
        current = remote.current_model
        if current is not None and current.key == (family, model):
            return
        refusal = None
        if self.is_meeting_active():
            refusal = "End the meeting before changing the engine"
        elif self.recorder.is_recording or self.is_transcribing():
            refusal = "Finish recording before changing the engine"
        if refusal:
            self.status_update.emit(refusal)
            self.ui_controller.refresh_remote_models()
            return
        choice = remote.request_model(family, model)
        host = remote.host_name or "the remote computer"
        self._reload_note = f"Switching {host} to {choice.label}..."
        self.reload_whisper_model()

    def _switch_engine_for_client(self, family: str, model: str, device_name: str, device: Optional[str] = None) -> None:
        """Host: switch to a model a paired computer chose, and wait for the answer.

        Called on that computer's connection thread. The switch itself runs
        on the Qt thread, like any engine change; this raises RuntimeError
        with the reason when it can't happen now.
        """
        request = _ClientModelSwitch(family, model, device_name, target_device=device)
        self.client_model_switch_requested.emit(request)
        if not request.done.wait(10.0):
            raise RuntimeError(f"{socket.gethostname()} didn't respond. Try again in a moment.")
        if request.error:
            raise RuntimeError(request.error)

    def select_remote_runtime(self, family: str, model: str, changes: dict) -> None:
        remote = self._selected_remote()
        if remote is None:
            return
        if (self.is_meeting_active() or self.recorder.is_recording or self.is_transcribing()
                or self._reload_in_flight or self._reload_pending):
            self.status_update.emit("Finish the current activity before changing the remote runtime")
            self.ui_controller.refresh_remote_models()
            return
        try:
            remote.request_runtime(family, model, changes)
        except ValueError as exc:
            self.status_update.emit(str(exc))
            self.ui_controller.refresh_remote_models()
            return
        self._reload_note = f"Updating the runtime on {remote.host_name or 'the remote computer'}..."
        self.reload_whisper_model()

    def _configure_engine_for_client(self, family: str, model: str, changes: dict, device_name: str) -> None:
        request = _ClientModelSwitch(family, model, device_name, runtime=changes)
        self.client_model_switch_requested.emit(request)
        if not request.done.wait(10.0):
            raise RuntimeError(f"{socket.gethostname()} didn't respond. Try again in a moment.")
        if request.error:
            raise RuntimeError(request.error)

    def _on_client_model_switch(self, request: "_ClientModelSwitch") -> None:
        try:
            if request.target_device is not None:
                request.error = self._switch_engine_to(request.family, request.model, request.device_name,
                                                      target_device=request.target_device)
            elif request.runtime is None:
                request.error = self._switch_engine_to(request.family, request.model, request.device_name)
            else:
                request.error = self._switch_engine_to(request.family, request.model, request.device_name, runtime=request.runtime)
        except Exception as exc:
            logger.exception("Switching models for a paired computer failed")
            request.error = f"{socket.gethostname()} couldn't switch models: {exc}"
        finally:
            request.done.set()

    def _switch_engine_to(self, family: str, model: str, device_name: str, *, runtime: Optional[dict] = None,
                          target_device: Optional[str] = None) -> Optional[str]:
        """Select a model here as the user would; returns why not, or None once queued.

        Persists the model the way the engine fields do, selects its backend
        through the main window so every view follows, and queues the reload
        whose end ``_engine_settled`` reports.
        """
        from services.local_asr.catalog import BACKENDS, MODELS, WHISPER_BACKEND

        name = socket.gethostname()
        if self.is_meeting_active():
            return f"{name} is running a meeting. Change its model after the meeting ends."
        if self.recorder.is_recording or self.is_transcribing():
            return f"{name} is transcribing right now. Try again in a moment."
        if target_device is not None:
            from services.remote_asr.dependencies import validate_model_device

            if self._reload_in_flight or self._reload_pending:
                return f"{name} is loading its engine. Try again in a moment."
            validate_model_device(family, model, target_device)
            if family == WHISPER_BACKEND:
                settings_manager.update_settings({SettingsKey.WHISPER_DEVICE: target_device,
                                                  SettingsKey.WHISPER_COMPUTE_TYPE: "auto"})
            else:
                devices = dict(settings_manager.get(SettingsKey.LOCAL_ASR_DEVICES, {}) or {})
                devices[family] = target_device
                settings_manager.save_setting(SettingsKey.LOCAL_ASR_DEVICES, devices)
        if runtime is not None:
            from services.remote_asr.engines import host_engine_for
            from services.remote_asr.runtime import validate_runtime

            if self._reload_in_flight or self._reload_pending:
                return f"{name} is loading its engine. Try again in a moment."
            updates = validate_runtime(host_engine_for(self.current_backend).describe(), family, model, runtime)
            settings_manager.update_settings(updates)
        if family in BACKENDS and model in MODELS and MODELS[model].backend == family:
            models = dict(settings_manager.load_all_settings().get(SettingsKey.LOCAL_ASR_MODELS) or {})
            models[family] = model
            settings_manager.save_setting(SettingsKey.LOCAL_ASR_MODELS, models)
            label = MODELS[model].label
        elif family == WHISPER_BACKEND and model in config.WHISPER_MODEL_CHOICES and model != "auto":
            settings_manager.save_setting(SettingsKey.WHISPER_MODEL, model)
            label = f"Whisper {model}"
        else:
            return f"{name} can't run {model}."
        logger.info("Paired computer %s switched this computer to %s", device_name, label)
        if self._current_model_name != family:
            display = next(key for key, value in config.MODEL_VALUE_MAP.items() if value == family)
            self.ui_controller.select_transcription_backend(display)
            if self._current_model_name != family:
                return f"{name} couldn't switch to {label} right now."
        self.ui_controller.refresh_local_engine_controls()
        whisper = self.transcription_backends.get(WHISPER_BACKEND)
        if (runtime is None and target_device is None and family == WHISPER_BACKEND and whisper is not None and whisper.is_available()
                and whisper.last_loaded_model == model and not self._reload_pending):
            return None  # Selecting the backend was all it took.
        self._reload_note = f"Switching to {label} for {device_name}..."
        self.reload_whisper_model()
        return None

    def _on_remote_pairing_changed(self) -> None:
        """Reconnect the Remote engine after pairing or forgetting a host."""
        self.record_sync.wake(now=True)
        self.records_changed.emit()
        if self._current_model_name == "remote":
            self._cancel_remote_retry()
            self.reload_whisper_model()
            return
        remote = self.transcription_backends.get("remote")
        if remote is not None:
            remote.cleanup()

    def _on_local_engine_settings_changed(self) -> None:
        """A local engine's model, device or quantization changed in Settings."""
        if self._selected_remote() is not None:
            # They apply to this computer's engines, which Remote isn't using.
            return
        self.reload_whisper_model()

    # ---- remote engine: watching the connection ----

    def _selected_remote(self):
        """The Remote engine while it is the selected one, else None."""
        backend = self.current_backend
        return backend if getattr(backend, "is_remote", False) else None

    def _watch_remote(self) -> None:
        """Start the link watch while Remote is selected; its tick stops it after."""
        if self._selected_remote() is not None and not self._remote_watch.isActive():
            self._remote_watch.start()
        self._publish_remote_link()

    def _on_remote_watch(self) -> None:
        remote = self._selected_remote()
        if remote is None:
            self._remote_watch.stop()
            self._cancel_remote_retry()
            self._remote_link = None
            return
        if remote.check_link():
            # Closed between requests: say so now, not at the next recording.
            self.device_info_update.emit(remote.device_info, False)
            self._schedule_remote_retry()
        self._publish_remote_link()

    def _publish_remote_link(self) -> None:
        """Hand the engine card a fresh RemoteLink when it reads differently."""
        remote = self._selected_remote()
        if remote is None:
            return
        from dataclasses import replace

        link = replace(remote.link(), retry_at=self._remote_retry_at)
        if link != self._remote_link:
            self._remote_link = link
            self.ui_controller.set_remote_link(link)

    def _schedule_remote_retry(self) -> None:
        """Try the host again after the next backoff delay, unless already due."""
        remote = self._selected_remote()
        if (remote is None or remote.is_available() or self._remote_restoring
                or self._remote_retry_timer.isActive()):
            return
        if remote.link().state == "unpaired":
            return
        delays = REMOTE_RETRY_DELAYS_S
        delay = delays[min(self._remote_retry_step, len(delays) - 1)]
        self._remote_retry_step += 1
        self._remote_retry_at = time.monotonic() + delay
        self._remote_retry_timer.start(int(delay * 1000))
        self._watch_remote()

    def _cancel_remote_retry(self) -> None:
        self._remote_retry_timer.stop()
        self._remote_retry_at = None
        self._remote_retry_step = 0

    def retry_remote_now(self) -> None:
        """Reconnect to the paired computer now. Safe from any thread."""
        self.remote_retry_requested.emit()

    def _retry_remote(self) -> None:
        remote = self._selected_remote()
        if remote is None or self._remote_restoring or self._reload_in_flight or self._reload_pending:
            return
        self._remote_retry_timer.stop()
        self._remote_retry_at = None
        if remote.is_available():
            self._cancel_remote_retry()
            self._publish_remote_link()
            return
        self._remote_restoring = True
        self._publish_remote_link()
        threading.Thread(
            target=self._remote_restore_worker, args=(remote,),
            name="remote-reconnect", daemon=True,
        ).start()

    def _remote_restore_worker(self, remote) -> None:
        try:
            outcome = remote.restore_link()
        except Exception:
            logger.exception("Remote engine reconnect failed")
            outcome = "offline"
        self.remote_settled.emit(outcome)

    def _on_remote_settled(self, outcome: str) -> None:
        """A reconnect attempt or a reload of the Remote engine ended (Qt thread)."""
        if outcome != "reload":
            self._remote_restoring = False
        remote = self._selected_remote()
        if remote is None:
            return
        available = remote.is_available()
        if outcome != "reload" and (available or outcome == "changed"
                                    or remote.device_info != self._remote_reported):
            # A reload reports its own result; a quiet retry only when it
            # changed something, so a host that stays away doesn't clear the
            # status line every minute.
            self.device_info_update.emit(remote.device_info, available)
        self._remote_reported = remote.device_info
        if available:
            self._cancel_remote_retry()
            if outcome != "reload":
                # The preview may be waiting for a first connection, or be
                # built for another engine than the host runs now.
                self._pending_streaming_setup = True
                self._flush_pending_streaming_setup()
        elif outcome != "unpaired":
            self._schedule_remote_retry()
        self._watch_remote()

    def _on_engine_busy_for_host(self, busy: bool) -> None:
        """A load settled: paired computers learn if the shared engine changed."""
        remote_engine = getattr(self, "remote_engine", None)
        if not busy and remote_engine is not None:
            remote_engine.engine_changed()

    def _on_remote_host_event(self, kind: str) -> None:
        """Host: which paired computers are connected, and busy (any thread)."""
        if kind in ("clients", "activity", "state"):
            self.remote_clients_changed.emit(self.remote_engine.connected_clients())
        if kind == "models":
            self.model_cache_changed.emit()
        if kind == "components":
            self.component_state_changed.emit()
            self.model_cache_changed.emit()
        if kind == "records":
            # A paired computer stored or removed a record here.
            self.records_changed.emit()

    def _on_record_sync_event(self, kind: str) -> None:
        """Client: a record moved to, or came back from, the paired host (any thread)."""
        if kind == "records":
            self.records_changed.emit()

    def _on_records_changed(self) -> None:
        self.ui_controller.main_window.refresh_history()
        self.ui_controller.main_window.refresh_past_meetings()

    def _on_remote_catalog(self, pairing, ready: dict, catalog: dict) -> None:
        remote = self.transcription_backends.get("remote")
        if remote is not None and remote.refresh_host_catalog(pairing, ready, catalog):
            self.ui_controller.refresh_remote_models()

    def _prune_update_leftovers(self) -> None:
        """Collect the downloads and transactions an earlier update left behind."""
        from services.app_update import prune_stale_downloads
        from services.app_update_apply import prune_abandoned_transactions

        try:
            removed = prune_stale_downloads()
        except Exception:
            logger.exception("Could not prune finished update downloads")
        else:
            if removed:
                logger.info(
                    "Removed finished update downloads: %s", ", ".join(removed)
                )
        try:
            transactions = prune_abandoned_transactions()
            from services.setup_update import cleanup_setup_backup
            from services.app_update_apply import running_app_dir
            cleanup_setup_backup(running_app_dir())
        except Exception:
            logger.exception("Could not prune abandoned update transactions")
            return
        if transactions:
            logger.info(
                "Removed abandoned update transactions: %s", ", ".join(transactions)
            )

    def on_api_keys_changed(self) -> None:
        """Rebuild the OpenAI clients after a key is saved or removed in Settings.

        Transcript cleanup and the meeting agent resolve their keys on each
        run, so only the long-lived transcription backends need a push.
        """
        api_key = resolve_credential("OPENAI_API_KEY")
        for backend in self.transcription_backends.values():
            if isinstance(backend, OpenAIBackend):
                backend.update_api_key(api_key)

    def on_hf_policy_changed(self, policy: str) -> None:
        """React to a Hugging Face access-policy change from Settings.

        Switching to ``always`` authorizes downloads without prompting, so a
        missing selected model can be fetched right away. Other policies take
        effect on the next model request without further action.
        """
        from transcriber.optional_backend import LocalSpeechBackend
        if policy == HuggingFaceAccessPolicy.ALWAYS and isinstance(
            self.current_backend, (LocalWhisperBackend, LocalSpeechBackend)
        ):
            self.ensure_local_model_available()

    def ensure_local_model_available(self) -> None:
        """Make sure the local Whisper model is loaded, requesting consent if needed.

        Safe to call from any thread: consent dialogs are raised on the Qt
        main thread via ``hf_consent_requested``, and downloads run on the
        executor. Concurrent calls for the same model are deduplicated by the
        access coordinator so at most one dialog and one download exist.
        """
        backend = self.current_backend if self._current_model_name != "api" else self.transcription_backends.get("local_whisper")
        if backend is None or backend.is_available():
            return
        if not getattr(backend, "is_model_missing", False):
            # Load failed for another reason (hardware, corrupt install);
            # downloading would not help.
            return
        if self._gpu_offer_waiting(backend):
            # Asked after "Use this GPU" is answered: it may pick another
            # model, and accepting grants this download itself. The offer
            # (idempotent) is what shows; declining it calls back here.
            self.gpu_fallback_detected.emit()
            return

        model_name = backend.model_name
        if not hf_access_coordinator.begin_request(model_name):
            return  # consent dialog or download already in flight

        try:
            # Advisory check only — the download worker performs the
            # authoritative (grant-consuming) evaluation before any network.
            decision = hf_access_coordinator.evaluate_access(
                model_name, consume_grant=False
            )
        except Exception:
            hf_access_coordinator.end_request(model_name)
            raise

        if decision in (AccessDecision.LOAD_CACHED, AccessDecision.DOWNLOAD_ALLOWED):
            self._start_hf_model_task(model_name)
        elif decision == AccessDecision.BLOCKED_BY_ENV:
            self.hf_consent_requested.emit(model_name, True, True)
        else:  # NEEDS_CONSENT
            self.hf_consent_requested.emit(model_name, False, True)

    def get_missing_local_runtime(self) -> Optional[str]:
        """Use the runtime resolved by the backend, including auto's CPU fallback."""
        from services.components import is_installed
        from transcriber.optional_backend import LocalSpeechBackend

        backend = self.current_backend
        if isinstance(backend, LocalSpeechBackend):
            component_id = backend.runtime_component
            if component_id and not is_installed(component_id):
                return component_id
        return None

    def get_loaded_local_model(self) -> Optional[str]:
        """Return the loaded model so its memory-mapped files cannot be deleted."""
        backend = self.current_backend if self._current_model_name != "api" else self.transcription_backends.get("local_whisper")
        if backend is not None and backend.is_available():
            return getattr(backend, "last_loaded_model", None)
        return None

    def request_model_download(self, model_name: str) -> None:
        """Fetch a model into the local cache via the consent flow (Settings → Downloads).

        Fetch-only: the download never changes the active model selection or
        touches the loaded engine (unless the model happens to be the missing
        selected one, in which case the worker also loads it). Routes through
        the same coordinator policy/grant/dedup machinery as
        ``ensure_local_model_available``.

        """
        from services.local_asr.catalog import missing_runtime

        self._runtime_prompted.discard(missing_runtime(model_name, settings_manager.load_all_settings()))
        if not hf_access_coordinator.begin_request(model_name):
            return

        try:
            decision = hf_access_coordinator.evaluate_access(
                model_name, consume_grant=False
            )
        except Exception:
            hf_access_coordinator.end_request(model_name)
            raise

        if decision == AccessDecision.LOAD_CACHED:
            # The manager's row was stale — files are already present.
            hf_access_coordinator.end_request(model_name)
            self.model_cache_changed.emit()
            self.runtime_consent_requested.emit(model_name)
        elif decision == AccessDecision.DOWNLOAD_ALLOWED:
            self._start_hf_model_task(model_name, load_into_engine=False)
        elif decision == AccessDecision.BLOCKED_BY_ENV:
            self.hf_consent_requested.emit(model_name, True, False)
        else:  # NEEDS_CONSENT
            self.hf_consent_requested.emit(model_name, False, False)

    def request_model_batch_download(self, model_names: List[str]) -> None:
        """Fetch several models back-to-back via Settings → Downloads.

        The window's confirmation dialog acts as consent for every listed
        model, so no per-model consent dialogs appear; the worker grants
        each model immediately before fetching it. Runs strictly
        sequentially on the shared executor — parallel multi-gigabyte
        extractions thrash disk and network without finishing sooner.
        """
        if is_hf_hub_offline_env_set():
            self.status_update.emit("Downloads are disabled by HF_HUB_OFFLINE")
            return
        if hf_access_coordinator.requests_in_flight:
            # Keep the app's one-download-at-a-time invariant even when the
            # request came from somewhere else (e.g. a single-model download).
            self.status_update.emit(
                "Another model request is in progress — try again when it finishes"
            )
            return
        pending = hf_access_coordinator.claim_batch(model_names)
        if not pending:
            # Everything requested is already cached or in flight; rescan so
            # stale rows correct themselves.
            self.model_cache_changed.emit()
            return
        self._batch_stop_requested = False
        self.batch_download_planned.emit(pending)
        self.executor.submit(self._hf_batch_worker, pending)

    def cancel_model_batch_download(self) -> None:
        """Stop the queue once the model currently downloading finishes."""
        self._batch_stop_requested = True

    def request_model_delete(self, model_name: str) -> None:
        """Delete a model's files from the local HF cache (Settings → Downloads).

        Refuses to delete the currently loaded model: ctranslate2 memory-maps
        the files, so removal would fail (Windows) or yank data out from under
        the engine. The coordinator's request slot also guards against a
        concurrent download of the same model.

        """
        from services.local_asr.catalog import MODELS
        if model_name in MODELS and self.is_meeting_active():
            self.model_deleted.emit(model_name, False, "End the meeting before removing speech models")
            return
        backend = (self.transcription_backends.get(MODELS[model_name].backend)
                   if model_name in MODELS else self.transcription_backends.get("local_whisper"))
        if backend is not None and backend.is_available():
            loaded = getattr(backend, "last_loaded_model", None)
            if loaded and resolve_model_repo(loaded) == resolve_model_repo(model_name):
                self.model_deleted.emit(
                    model_name, False, "Model is in use — switch models first"
                )
                return

        if not hf_access_coordinator.begin_request(model_name):
            self.model_deleted.emit(
                model_name, False, "A download for this model is in progress"
            )
            return

        self.executor.submit(self._model_delete_worker, model_name)

    def _model_delete_worker(self, model_name: str) -> None:
        try:
            with self._engine_lock:
                from services.local_asr.catalog import MODELS
                if model_name in MODELS:
                    backend = self.transcription_backends.get(MODELS[model_name].backend)
                    if self.is_meeting_active() or (backend and backend.is_available() and backend.last_loaded_model == model_name):
                        raise RuntimeError("Model is in use — switch models first")
                delete_model_from_cache(model_name)
        except (PermissionError, OSError) as exc:
            logger.error(f"Model delete failed for '{model_name}': {exc}")
            self.model_deleted.emit(
                model_name, False, "Files are in use by another process"
            )
        except Exception as exc:
            logger.error(f"Model delete failed for '{model_name}': {exc}")
            self.model_deleted.emit(model_name, False, str(exc))
        else:
            self.status_update.emit(f"Model '{model_name}' deleted")
            self.model_deleted.emit(model_name, True, "")
            self.model_cache_changed.emit()
        finally:
            hf_access_coordinator.end_request(model_name)

    def _on_hf_consent_requested(
        self, model_name: str, env_blocked: bool, load_into_engine: bool
    ) -> None:
        """Show the consent dialog and act on the result (Qt main thread).

        The request slot claimed by the requester is either handed to the
        download worker or released here.
        """
        policy = hf_access_coordinator.get_policy()
        try:
            action = self.ui_controller.show_hf_consent_dialog(
                model_name, policy, env_blocked
            )
        except Exception:
            hf_access_coordinator.end_request(model_name)
            raise

        if env_blocked:
            hf_access_coordinator.end_request(model_name)
            self.status_update.emit(
                f"Model '{model_name}' unavailable — downloads disabled by HF_HUB_OFFLINE"
            )
            return

        if action == ConsentAction.DOWNLOAD_ONCE:
            hf_access_coordinator.grant_once(model_name)
            self._start_hf_model_task(model_name, load_into_engine)
        elif action == ConsentAction.ALWAYS_ALLOW:
            hf_access_coordinator.set_policy(HuggingFaceAccessPolicy.ALWAYS)
            self._start_hf_model_task(model_name, load_into_engine)
        elif action == ConsentAction.OPEN_SETTINGS:
            hf_access_coordinator.end_request(model_name)
            self.ui_controller.open_settings_dialog(focus_hf_policy=True)
        else:  # canceled: no network activity
            hf_access_coordinator.end_request(model_name)
            self.status_update.emit(
                f"Model '{model_name}' is unavailable — download declined"
            )
            if load_into_engine:
                # A declined Downloads-page download must not touch the
                # selected model.
                self._revert_declined_model_selection(model_name)

    def _revert_declined_model_selection(self, declined_model: str) -> None:
        """Roll the whisper-model selection back to the last loaded model.

        Declining a download would otherwise leave the settings (and the
        engine combos) pointing at a model that was never downloaded, with no
        usable engine. Reverting to the previously loaded model — still in the
        local cache — keeps the selection aligned with what can actually run.
        Runs on the Qt main thread (called from the consent slot).
        """
        from services.local_asr.catalog import MODELS
        if declined_model in MODELS:
            return
        backend = self.transcription_backends.get("local_whisper")
        if backend is None or backend.is_available():
            return

        last_loaded = getattr(backend, "last_loaded_model", None)
        if not last_loaded or last_loaded == declined_model:
            # Nothing ever loaded (e.g. fresh install) — leave the selection
            # alone; the status message already reports it as unavailable.
            return

        logger.info(
            f"Reverting whisper model selection from '{declined_model}' "
            f"to '{last_loaded}' after declined download"
        )
        settings_manager.save_setting(SettingsKey.WHISPER_MODEL, last_loaded)
        self.ui_controller.refresh_local_engine_controls()
        # Background reload picks the reverted model up from settings; it is
        # cached, so this never re-enters the consent flow.
        self.reload_whisper_model()

    def request_component_install(self, component_id: str) -> None:
        """Start an install unless the same component is already in flight."""
        from services.local_asr.catalog import RUNTIME_IDS
        from transcriber.optional_backend import LocalSpeechBackend
        if component_id in RUNTIME_IDS:
            if (self.is_meeting_active() or self.recorder.is_recording
                    or self.is_transcribing() or self._reload_in_flight):
                self.status_update.emit("Finish the current speech job before installing its runtime")
                return
            for backend in self.transcription_backends.values():
                if isinstance(backend, LocalSpeechBackend) and backend.runtime_component == component_id:
                    backend.cleanup()
        cancel_event = component_coordinator.begin_install(component_id)
        if cancel_event is None:
            return

        self.component_install_started.emit(component_id)
        self.component_executor.submit(
            self._component_install_worker, component_id, cancel_event
        )

    def cancel_component_install(self, component_id: str) -> None:
        component_coordinator.cancel_install(component_id)

    def request_component_uninstall(self, component_id: str) -> None:
        from services.local_asr.catalog import RUNTIME_IDS
        if component_id in RUNTIME_IDS:
            if (self.is_meeting_active() or self.recorder.is_recording
                    or self.is_transcribing() or self._reload_in_flight):
                self.status_update.emit("Finish the current speech job before removing its runtime")
                return
            self.component_executor.submit(self._component_uninstall_worker, component_id)
        else:
            self._component_uninstall_worker(component_id)

    def _component_uninstall_worker(self, component_id: str) -> None:
        try:
            from services.local_asr.catalog import RUNTIME_IDS
            from transcriber.optional_backend import LocalSpeechBackend
            with self._engine_lock:
                if component_id in RUNTIME_IDS:
                    if (self.is_meeting_active() or self.recorder.is_recording
                            or self.is_transcribing() or self._reload_in_flight):
                        raise RuntimeError("Finish the current speech job before removing its runtime")
                    for backend in self.transcription_backends.values():
                        if isinstance(backend, LocalSpeechBackend) and backend.runtime_component == component_id:
                            backend.cleanup()
                            self.device_info_update.emit(backend.device_info, False)
                uninstall_component(component_id)
            self.status_update.emit("Component removed")
        except Exception as exc:
            logger.error("Failed to remove component '%s': %s", component_id, exc)
            self.status_update.emit(f"Could not remove the component: {exc}")
        finally:
            self.component_state_changed.emit()

    def _component_install_worker(
        self, component_id: str, cancel_event
    ) -> None:
        """Download and install a component off the Qt thread.

        Progress is emitted at most ~10 times per second: a 2 GB download in
        1 MiB chunks would otherwise queue thousands of cross-thread events
        and visibly stall the UI.

        """
        import time

        last_emit = [0.0]
        last_percent = [-1]

        def progress(phase: str, done: int, total: int) -> None:
            now = time.monotonic()
            percent = int(done * 100 / total) if total > 0 else -1
            is_final = total > 0 and done >= total
            if (now - last_emit[0] < 0.1
                    and percent == last_percent[0]
                    and not is_final):
                return
            last_emit[0] = now
            last_percent[0] = percent
            self.component_progress.emit(component_id, phase, done, total)

        success, message = False, ""
        try:
            entry = component_coordinator.catalog_entry(component_id)
            if entry is None:
                raise ComponentError(
                    "Could not reach the download server. Check your internet "
                    "connection and try again."
                )

            install_component(component_id, entry, progress, cancel_event)
            success = True
            # Activate in the running process so the component works without a
            # restart: Windows resolves DLL names fresh on every load attempt,
            # so registering the new directory now is enough. The engine reload
            # that actually picks the GPU up happens on the main thread, in
            # _on_component_install_finished.
            activated, activation_reason = activate_component(component_id)
            if activated:
                message = "Installed and ready to use — no restart needed."
            else:
                logger.warning(
                    f"Component '{component_id}' installed but could not be "
                    f"activated in this session: {activation_reason}"
                )
                message = "Restart OpenWhisper to enable this component."
        except ComponentCanceled:
            message = "Installation canceled."
            logger.info(f"Install of '{component_id}' was canceled")
        except ComponentError as exc:
            message = str(exc)
            logger.error(f"Install of '{component_id}' failed: {exc}")
        except Exception as exc:
            message = f"Installation failed: {exc}"
            logger.error(f"Install of '{component_id}' failed", exc_info=True)
        finally:
            component_coordinator.end_install(component_id)
            self.component_install_finished.emit(component_id, success, message)
            self.component_state_changed.emit()

    def _on_component_install_finished(
        self, component_id: str, success: bool, _message: str
    ) -> None:
        """Put a freshly installed GPU component to use (Qt main thread).

        A persisted device of "cpu" is moved back to "auto" first: it is
        either the residue of an earlier GPU fallback (see
        ``_on_gpu_fallback``) or a pre-existing choice the install supersedes —
        nobody downloads a ~1 GB CUDA runtime to keep transcribing on the CPU.
        "auto" rather than "cuda" so machines without a usable GPU degrade
        silently instead of falling back again.
        """
        from transcriber.optional_backend import LocalSpeechBackend
        from services.local_asr.catalog import RUNTIME_IDS
        # A remote engine runs on the host, so nothing installed here changes it.
        remote = self._selected_remote() is not None
        if success and component_id in RUNTIME_IDS and isinstance(self.current_backend, LocalSpeechBackend):
            if not remote:
                self.reload_whisper_model()
            return
        if not success or component_id != ComponentId.GPU_ACCEL:
            return
        if not gpu_runtime_available():
            return  # activation failed; the restart message already covers it

        device = settings_manager.get(
            SettingsKey.WHISPER_DEVICE, config.FASTER_WHISPER_DEVICE
        )
        if device == "cpu":
            logger.info(
                "GPU component installed — moving whisper device setting "
                "from 'cpu' back to 'auto'"
            )
            settings_manager.save_setting(SettingsKey.WHISPER_DEVICE, "auto")
            self.ui_controller.refresh_local_engine_controls()

        backend = self.transcription_backends.get("local_whisper")
        if (
            backend is not None
            and backend.is_available()
            and getattr(backend, "device", None) == "cuda"
        ):
            return  # already on the GPU (e.g. a component update)
        if remote:
            return  # Local Whisper picks the GPU up when it next loads

        self.reload_whisper_model()

    def _on_gpu_fallback(self) -> None:
        """Reflect a GPU→CPU fallback in settings and the UI (Qt main thread).

        Persists the device the engine actually uses, so the Device combo does
        not claim CUDA while transcription runs on the CPU, and raises a
        status message that names the fix instead of just the symptom.
        """
        backend = self.transcription_backends.get("local_whisper")
        if backend is None or not getattr(backend, "gpu_fallback_note", None):
            return

        device = settings_manager.get(
            SettingsKey.WHISPER_DEVICE, config.FASTER_WHISPER_DEVICE
        )
        if device != "cpu":
            logger.info(
                f"GPU fallback: reverting whisper device setting "
                f"from '{device}' to 'cpu'"
            )
            settings_manager.save_setting(SettingsKey.WHISPER_DEVICE, "cpu")
            self.ui_controller.refresh_local_engine_controls()

        self.status_update.emit(self._describe_gpu_fallback(backend))
        self._offer_gpu_setup(backend)

    # ---- "Use this GPU" ----

    def _gpu_offer_waiting(self, backend) -> bool:
        """The offer is due or open for this backend. Cheap; any thread."""
        return (
            self._gpu_offer_state != "answered"
            and backend is self.transcription_backends.get("local_whisper")
            and getattr(backend, "gpu_fallback_cause", None) == GpuFallbackCause.MISSING_LIBRARIES
            and ComponentId.GPU_ACCEL in available_component_ids()
            and not settings_manager.get(SettingsKey.WHISPER_GPU_OFFER_DECLINED, False)
        )

    def _offer_gpu_setup(self, backend) -> None:
        """Offer one action for the GPU the libraries are missing for (Qt thread).

        Shown once: "Keep using the CPU" is remembered, and GPU
        Acceleration stays in Downloads either way. A model download that
        waited on the answer carries on after "Keep using the CPU".
        """
        if self._gpu_offer_state is not None or not self._gpu_offer_waiting(backend):
            return
        if self.is_meeting_active() or self.recorder.is_recording or self.is_transcribing():
            return  # asked at the next load instead
        from services.gpu_setup import plan_gpu_setup

        supported = getattr(backend, "_get_supported_compute_types", None)
        try:
            plan = plan_gpu_setup(
                settings_manager.load_all_settings(), supported("cuda")
            ) if supported is not None else None
        except Exception:
            logger.warning("Could not plan GPU setup", exc_info=True)
            plan = None
        if plan is None or (plan.model_download_bytes and is_hf_hub_offline_env_set()):
            self._gpu_offer_state = "answered"
            self.ensure_local_model_available()
            return
        self._gpu_offer_state = "shown"
        try:
            accepted = self.ui_controller.show_use_gpu_dialog(plan)
        finally:
            self._gpu_offer_state = "answered"
        if accepted:
            self._start_gpu_setup(plan)
        else:
            settings_manager.save_setting(SettingsKey.WHISPER_GPU_OFFER_DECLINED, True)
            self.ensure_local_model_available()

    def _start_gpu_setup(self, plan) -> None:
        """Carry out an accepted "Use this GPU" (Qt thread).

        The dialog is the consent for the model download too. With the CUDA
        libraries missing, installing them comes first, and
        ``_on_component_install_finished`` reloads the engine when it lands;
        the reload then finds the model missing and downloads it on this
        grant, and loads it on the GPU.
        """
        logger.info(
            "Setting up Local Whisper on the GPU: %s at %s (install CUDA: %s, download: %s)",
            plan.model, plan.compute_type, plan.install_component, bool(plan.model_download_bytes),
        )
        settings_manager.save_setting(SettingsKey.WHISPER_DEVICE, "auto")
        if plan.switches_model:
            settings_manager.save_setting(SettingsKey.WHISPER_MODEL, plan.model_setting)
        if plan.model_download_bytes:
            hf_access_coordinator.grant_once(plan.model)
        self.ui_controller.refresh_local_engine_controls()
        if plan.install_component:
            self.status_update.emit("Installing GPU Acceleration...")
            self.request_component_install(ComponentId.GPU_ACCEL)
        else:
            self.reload_whisper_model()

    @staticmethod
    def _describe_gpu_fallback(backend) -> str:
        cause = getattr(backend, "gpu_fallback_cause", None)
        if cause == GpuFallbackCause.OUT_OF_MEMORY:
            return (
                "GPU out of memory — using CPU. Pick a smaller model or int8 "
                "quantization, then set the device back to auto."
            )
        if (
            cause == GpuFallbackCause.MISSING_LIBRARIES
            and ComponentId.GPU_ACCEL in available_component_ids()
        ):
            return (
                "GPU found, but CUDA is not installed — using CPU. Install "
                "GPU Acceleration from Downloads to enable it."
            )
        if cause == GpuFallbackCause.MISSING_LIBRARIES:
            return (
                "GPU found, but its CUDA libraries are missing — using CPU. "
                "Install requirements-gpu.txt to enable GPU acceleration."
            )
        return "GPU failed to load — using CPU. See openwhisper.log for details."

    def _hf_download_progress(self, model_name: str):
        """Return a throttled byte-progress callback for one model download."""
        import time

        last_emit = [0.0]
        last_percent = [-1]

        def progress(done: int, total: int) -> None:
            now = time.monotonic()
            percent = int(done * 100 / total) if total > 0 else -1
            is_final = total > 0 and done >= total
            if (
                now - last_emit[0] < 0.1
                and percent == last_percent[0]
                and not is_final
            ):
                return
            last_emit[0] = now
            last_percent[0] = percent
            self.model_download_progress.emit(model_name, done, total)

        return progress

    def _on_model_download_finished(self, model_name: str, success: bool) -> None:
        """Load a downloaded optional model, or activate the tiny.en preview."""
        from transcriber.optional_backend import LocalSpeechBackend
        if success:
            self._prompt_for_model_runtime(model_name)
        if success and isinstance(self.current_backend, LocalSpeechBackend):
            # A remote engine's model_name is the host's, and downloading the
            # same model here doesn't change what the host runs.
            if self.current_backend.model_name == model_name and self._selected_remote() is None:
                self.reload_whisper_model()
            return
        if not success or model_name != "tiny.en":
            return
        if not is_model_cached("tiny.en"):
            return
        settings = settings_manager.load_all_settings()
        if settings.get(SettingsKey.STREAMING_ENABLED, config.STREAMING_ENABLED):
            self.streaming_runtime.reconfigure_streaming()

    def _start_hf_model_task(self, model_name: str, load_into_engine: bool = True) -> None:
        """Run an approved load or fetch-only download on a worker."""
        from services.local_asr.catalog import MODELS
        if model_name in MODELS:
            load_into_engine = False
            self.runtime_consent_requested.emit(model_name)
        if load_into_engine:
            self.engine_busy_changed.emit(True)
        self.model_download_started.emit(model_name)
        self.executor.submit(self._hf_model_worker, model_name, load_into_engine)

    def _prompt_for_model_runtime(self, model_name: str) -> None:
        """Ask once per runtime per session, on the Qt thread."""
        from services.local_asr.catalog import missing_runtime

        if (is_hf_hub_offline_env_set() or self._reload_in_flight
                or self.is_meeting_active() or self.recorder.is_recording or self.is_transcribing()):
            return
        component = missing_runtime(model_name, settings_manager.load_all_settings())
        if not component or component in self._runtime_prompted or component_coordinator.is_installing(component):
            return
        self._runtime_prompted.add(component)
        if self.ui_controller.show_required_runtime_dialog(model_name, component):
            self.ui_controller.open_downloads(component_id=component)
            self.request_component_install(component)

    def _hf_model_worker(self, model_name: str, load_into_engine: bool = True) -> None:
        from services.local_asr.catalog import MODELS
        requested_load = load_into_engine
        load_into_engine = load_into_engine and model_name not in MODELS
        backend = self.transcription_backends.get("local_whisper")
        success = False
        try:
            if not load_into_engine:
                success = self._download_model_to_cache(model_name)
                return
            if self._refuse_engine_load_during_meeting():
                return
            decision = hf_access_coordinator.evaluate_access(model_name)
            if decision == AccessDecision.DOWNLOAD_ALLOWED:
                self.status_update.emit(f"Downloading model '{model_name}'...")
                download_model_files(model_name, progress_callback=self._hf_download_progress(model_name))
            elif decision != AccessDecision.LOAD_CACHED:
                self.status_update.emit(f"Model '{model_name}' is unavailable")
                return
            success = True
            # A download can finish after the user switched engines or started
            # a meeting. Only the selected engine may acquire resident weights.
            with self._engine_lock:
                if self._refuse_engine_load_during_meeting() or self.current_backend is not backend:
                    return
                backend.reload_model(model_name)
                success = backend.is_available()
                self.device_info_update.emit(backend.device_info, success)
                self.status_update.emit("Whisper engine ready" if success else "Model failed to load")
                if getattr(backend, "gpu_fallback_note", None):
                    self.gpu_fallback_detected.emit()
        except Exception as exc:
            logger.error("Model download/load failed for '%s': %s", model_name, exc)
            self.status_update.emit(f"Model download failed: {exc}")
        finally:
            hf_access_coordinator.end_request(model_name)
            self.model_download_finished.emit(model_name, success)
            if success:
                self.model_cache_changed.emit()
            if requested_load:
                self.engine_busy_changed.emit(False)

    def _download_model_to_cache(self, model_name: str) -> bool:
        """Fetch-only worker body shared by single and batched downloads.

        The caller owns the coordinator request slot and the
        started/finished signals; this only evaluates access (consuming any
        grant), downloads, and bridges the engine revival when the fetched
        model happens to be the missing selected one. Returns True when the
        files are present afterwards.
        """
        backend = self.transcription_backends.get("local_whisper")
        decision = hf_access_coordinator.evaluate_access(model_name)
        if decision not in (
            AccessDecision.LOAD_CACHED,
            AccessDecision.DOWNLOAD_ALLOWED,
        ):
            logger.warning(
                f"Fetch of '{model_name}' aborted: access decision {decision}"
            )
            self.status_update.emit(f"Model '{model_name}' is unavailable")
            return False
        if decision == AccessDecision.DOWNLOAD_ALLOWED:
            self.status_update.emit(
                f"Downloading model '{model_name}' from Hugging Face..."
            )
            download_model_files(
                model_name,
                progress_callback=self._hf_download_progress(model_name),
            )
            self.status_update.emit(f"Model '{model_name}' downloaded")
        # Bridge: fetching the currently-missing selected model also
        # revives the engine (now a pure cache hit, no consent re-entry).
        if (
            backend is not None
            and self.current_backend is backend
            and getattr(backend, "is_model_missing", False)
            and backend.model_name == model_name
        ):
            # The download itself succeeded, so this still reports True; only
            # reviving the engine waits for the meeting to end.
            if self._refuse_engine_load_during_meeting():
                return True
            with self._engine_lock:
                if self.is_meeting_active() or self.current_backend is not backend:
                    return True
                backend.reload_model(model_name)
            if backend.is_available():
                self.device_info_update.emit(backend.device_info, True)
                self.status_update.emit("Whisper engine ready")
            if getattr(backend, "gpu_fallback_note", None):
                self.gpu_fallback_detected.emit()
        return True

    def _hf_batch_worker(self, model_names: List[str]) -> None:
        """Download a confirmed queue of models strictly sequentially.

        Each model is granted and announced right before its own download;
        stopping between models releases the remaining slots so no consent
        or request claim outlives the queue.
        """
        processed = 0
        succeeded = 0
        try:
            for model_name in model_names:
                if self._batch_stop_requested:
                    break
                hf_access_coordinator.grant_once(model_name)
                self.model_download_started.emit(model_name)
                success = False
                try:
                    success = self._download_model_to_cache(model_name)
                except Exception as exc:
                    logger.error(f"Model download failed for '{model_name}': {exc}")
                    self.status_update.emit(f"Model download failed: {exc}")
                finally:
                    # ``claim_batch`` transfers ownership of one coordinator
                    # slot per model to this worker.  Release processed models
                    # here; the outer ``finally`` only owns models the queue
                    # never reached (for example after Stop was requested).
                    hf_access_coordinator.end_request(model_name)
                    self.model_download_finished.emit(model_name, success)
                    if success:
                        self.model_cache_changed.emit()
                processed += 1
                if success:
                    succeeded += 1
        finally:
            stopped = self._batch_stop_requested
            self._batch_stop_requested = False
            for model_name in model_names[processed:]:
                hf_access_coordinator.end_request(model_name)
            if stopped:
                logger.info(
                    f"Batch download stopped after {succeeded} of "
                    f"{len(model_names)} models"
                )
            self.batch_download_finished.emit(succeeded, len(model_names))

    def change_audio_device(self, device_id: Optional[int]) -> None:
        logger.info(f"Changing audio device to: {device_id}")

        if self.recorder.is_recording:
            logger.warning("Cannot change audio device while recording")
            self.ui_controller.set_status("Stop recording before changing device")
            return

        self.recorder.cleanup()
        self.recorder = AudioRecorder(device_id=device_id)
        self.streaming_runtime.setup_audio_level_callback()

        device_name = "System Default" if device_id is None else f"Device {device_id}"
        logger.info(f"Audio device changed to: {device_name}")
        self.ui_controller.set_status("Audio device changed")

    def update_hotkeys(self, hotkeys: Dict[str, str]) -> None:
        self.hotkey_runtime.update_hotkeys(hotkeys)

    def update_recording_trigger_mode(self, mode: str) -> None:
        self.hotkey_runtime.set_recording_trigger_mode(mode)

    def reconfigure_streaming(self) -> None:
        self.streaming_runtime.reconfigure_streaming()

    def start_recording(self) -> bool:
        """Start audio recording (UI callback target).

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
        return self.transcription_runtime.start_recording() is not False

    def start_profile_recording(self, profile_id: str) -> bool:
        if self._refuse_dictation_during_meeting():
            return False
        return self.transcription_runtime.start_recording(profile_id) is not False

    def toggle_profile_recording(self, profile_id: str) -> None:
        # A shortcut pressed during capture only stops that capture. Its
        # profile snapshot remains the one chosen when recording started.
        if self.recorder.is_recording:
            self.stop_recording()
        else:
            self.start_profile_recording(profile_id)

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
        self.transcription_runtime.cancel()

    def minimize_to_tray(self) -> None:
        """Toggle the main window between tray-hidden and foreground states.

        Hotkey callbacks run on a background thread, so this only emits a signal;
        the actual window change happens on the Qt main thread via the connection
        in ``_connect_signals``.
        """
        self.minimize_to_tray_requested.emit()

    def toggle_meeting_mode(self) -> None:
        """Start or end Meeting Mode from its optional global hotkey."""
        if self.meeting_runtime.is_active:
            self.meeting_runtime.end_meeting()
        elif not self.meeting_runtime.is_claimed:
            self.meeting_platform_ack_requested.emit()

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
            self._runtime_prompted.clear()
            self.transcription_runtime.on_model_changed(model_name)

    def update_status_with_auto_hide(self, status: str) -> None:
        self.hotkey_runtime.update_status_with_auto_hide(status)

    def _on_meeting_platform_ack_requested(self) -> None:
        """Run Meeting Mode readiness gates, then start if allowed."""
        policy = None
        try:
            policy = self.ui_controller.ensure_meeting_start_readiness()
        except Exception as exc:
            logger.error(f"Meeting start readiness failed: {exc}")
        if policy is not None:
            self.meeting_runtime.start_meeting(system_audio_policy=policy)

    def _on_meeting_consent_requested(self) -> None:
        """Show the meeting cloud-consent dialog and act on it (Qt main thread).

        Mirrors the HF consent round-trip: the runtime emitted the request
        (possibly holding a pending start or toggle), this slot shows the
        dialog, persists a granted consent, and hands the outcome back to the
        runtime's continuation.
        """
        granted = False
        try:
            granted = self.ui_controller.show_meeting_consent_dialog()
        except Exception as exc:
            logger.error(f"Meeting consent dialog failed: {exc}")

        if granted:
            try:
                settings_manager.save_setting(
                    SettingsKey.MEETING_CLOUD_CONSENT_GIVEN, True
                )
            except Exception as exc:
                logger.warning(f"Could not persist meeting cloud consent: {exc}")

        self.meeting_runtime.on_consent_result(granted)

    def _on_meeting_recovery_found(self, meetings) -> None:
        try:
            self.ui_controller.show_meeting_recovery_dialog(
                list(meetings),
                on_finalize=self.meeting_runtime.finalize_recovered,
                on_discard=self.meeting_runtime.discard_recovered,
            )
        except Exception as exc:
            logger.error(f"Meeting recovery dialog failed: {exc}")

    def _connect_signals(self) -> None:
        self.profile_record_requested.connect(self.toggle_profile_recording)
        self.transcription_completed.connect(self._on_transcription_complete)
        self.transcription_failed.connect(self._on_transcription_error)
        self.batch_completed.connect(self._on_batch_complete)
        self.batch_progress.connect(self.ui_controller.set_batch_progress)
        self.batch_item_finished.connect(self.ui_controller.set_batch_item_finished)
        self.large_file_detected.connect(self.ui_controller.show_large_file_state)
        self.hf_consent_requested.connect(self._on_hf_consent_requested)
        self.runtime_consent_requested.connect(self._prompt_for_model_runtime)
        self.remote_pairing_changed.connect(self._on_remote_pairing_changed)
        self.records_changed.connect(self._on_records_changed)
        self.client_model_switch_requested.connect(self._on_client_model_switch)
        self.remote_link_poke.connect(self._publish_remote_link)
        self.remote_catalog_received.connect(self._on_remote_catalog)
        self.remote_settled.connect(self._on_remote_settled)
        self.remote_retry_requested.connect(self._retry_remote)
        self.remote_clients_changed.connect(self.ui_controller.set_remote_clients)
        self.ui_controller.on_remote_retry = self.retry_remote_now
        self.status_update.connect(self.ui_controller.set_status)
        self.device_info_update.connect(self.ui_controller.set_device_info)
        self.engine_busy_changed.connect(self.ui_controller.set_engine_busy)
        self.engine_busy_changed.connect(self._on_engine_busy_for_host)
        self.streaming_setup_requested.connect(self.streaming_runtime.setup_streaming)
        self.model_download_started.connect(
            self.ui_controller.on_model_download_started
        )
        self.model_download_progress.connect(
            self.ui_controller.on_model_download_progress
        )
        self.model_download_finished.connect(
            self.ui_controller.on_model_download_finished
        )
        self.batch_download_planned.connect(
            self.ui_controller.on_model_batch_planned
        )
        self.batch_download_finished.connect(
            self.ui_controller.on_model_batch_finished
        )
        self.model_download_finished.connect(self._on_model_download_finished)
        self.model_deleted.connect(self.ui_controller.on_model_deleted)
        self.model_cache_changed.connect(self.ui_controller.refresh_model_views)
        self.model_cache_changed.connect(
            self.ui_controller.refresh_local_engine_controls
        )
        self.component_progress.connect(self.ui_controller.on_component_progress)
        self.component_install_started.connect(
            lambda _component_id: self.ui_controller.on_component_state_changed()
        )
        self.component_install_finished.connect(
            self.ui_controller.on_component_install_finished
        )
        self.component_install_finished.connect(self._on_component_install_finished)
        self.gpu_fallback_detected.connect(self._on_gpu_fallback)
        self.component_state_changed.connect(
            self.ui_controller.on_component_state_changed
        )
        self.meeting_engine_event.connect(self.meeting_runtime._route_engine_event)
        self.meeting_state_changed.connect(
            self.ui_controller.on_meeting_state_changed
        )
        self.meeting_status_update.connect(self.ui_controller.set_meeting_status)
        self.meeting_error.connect(self.ui_controller.on_meeting_error)
        self.meeting_server_started.connect(
            self.ui_controller.on_meeting_server_started
        )
        self.meeting_recovery_found.connect(self._on_meeting_recovery_found)
        self.meeting_consent_requested.connect(self._on_meeting_consent_requested)
        self.meeting_platform_ack_requested.connect(
            self._on_meeting_platform_ack_requested
        )
        self.meeting_guest_link_ready.connect(
            self.ui_controller.copy_meeting_guest_link
        )
        self.past_meetings_refresh_requested.connect(
            self.ui_controller.main_window.refresh_past_meetings
        )
        self.update_check_finished.connect(self._on_update_check_finished)
        self.update_download_progress.connect(
            self._on_update_download_progress
        )
        self.update_download_finished.connect(
            self._on_update_download_finished
        )
        if hasattr(self.ui_controller, "set_overlay_state"):
            self.overlay_state_update.connect(self.ui_controller.set_overlay_state)
        self.stt_state_changed.connect(self.hotkey_runtime.on_stt_state_changed)
        self.recording_state_changed.connect(self._on_recording_state_changed)
        self.minimize_to_tray_requested.connect(
            self.ui_controller.main_window.toggle_tray_visibility
        )
        self.partial_transcription.connect(
            self.ui_controller.main_window.set_partial_transcription
        )
        self.streaming_text_update.connect(self.ui_controller.update_streaming_text)
        self.streaming_overlay_show.connect(self.ui_controller.show_streaming_overlay)
        self.streaming_overlay_hide.connect(self.ui_controller.hide_streaming_overlay)

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

        try:
            if self.current_backend and self.current_backend.is_transcribing:
                logger.info("Canceling ongoing transcription...")
                self.current_backend.cancel_transcription()
        except Exception as exc:
            logger.debug(f"Error canceling transcription: {exc}")

        try:
            if hasattr(self, "_watchdog_timer") and self._watchdog_timer:
                self._watchdog_timer.stop()
            if hasattr(self, "_periodic_refresh_timer") and self._periodic_refresh_timer:
                self._periodic_refresh_timer.stop()
            if hasattr(self, "_update_check_timer") and self._update_check_timer:
                self._update_check_timer.stop()
            if hasattr(self, "_update_cancel"):
                self._update_cancel.set()
        except Exception as exc:
            logger.debug(f"Error stopping watchdog timers: {exc}")

        try:
            # Shut the meeting engine down early: it owns capture streams, a
            # web server, and possibly a sidecar process.
            self.meeting_runtime.cleanup()
        except Exception as exc:
            logger.debug(f"Error during meeting runtime cleanup: {exc}")

        try:
            self.hotkey_runtime.cleanup()
        except Exception as exc:
            logger.debug(f"Error during hotkey runtime cleanup: {exc}")

        try:
            if self.hotkey_manager:
                self.hotkey_manager.cleanup()
        except Exception as exc:
            logger.debug(f"Error during hotkey cleanup: {exc}")

        try:
            if self.recorder:
                self.recorder.cleanup()
        except Exception as exc:
            logger.debug(f"Error during recorder cleanup: {exc}")

        try:
            self.streaming_runtime.cleanup()
        except Exception as exc:
            logger.debug(f"Error during streaming cleanup: {exc}")

        try:
            # Must happen before the shutdown below: cancel_futures only
            # cancels futures that have not started, so an in-flight
            # multi-gigabyte download would hold shutdown open for minutes.
            # The download loop checks its cancel event once per MiB.
            component_coordinator.cancel_all()
        except Exception as exc:
            logger.debug(f"Error cancelling component installs: {exc}")

        try:
            # An upload in progress resumes from where it stopped next time.
            self.record_sync.stop(timeout=2.0)
        except Exception as exc:
            logger.debug(f"Error stopping the record sync: {exc}")

        try:
            self.remote_engine.shutdown()
            # A model switch waits minutes for the host; the executor below
            # would wait with it, so end the connection first.
            remote = self.transcription_backends.get("remote")
            if remote is not None:
                remote.cleanup()
        except Exception as exc:
            logger.debug(f"Error stopping the remote engine host: {exc}")

        for executor in (self.executor, self.component_executor):
            try:
                executor.shutdown(wait=True, cancel_futures=True)
            except Exception as exc:
                logger.debug(f"Error during executor shutdown: {exc}")

        try:
            for backend_name, backend in self.transcription_backends.items():
                try:
                    logger.info(f"Cleaning up transcription backend: {backend_name}")
                    backend.cleanup()
                except Exception as exc:
                    logger.debug(f"Error cleaning up {backend_name} backend: {exc}")
            self.transcription_backends.clear()
            self.current_backend = None
        except Exception as exc:
            logger.debug(f"Error during transcription backends cleanup: {exc}")

        try:
            self.ui_controller.cleanup()
        except Exception as exc:
            logger.debug(f"Error during UI controller cleanup: {exc}")

        try:
            db.close()
        except Exception as exc:
            logger.debug(f"Error closing database: {exc}")

        logger.info("Application controller cleaned up")

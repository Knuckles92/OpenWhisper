"""Main Qt-facing application controller."""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional

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
from services.database import db
from services.hf_access import (
    AccessDecision,
    ConsentAction,
    delete_model_from_cache,
    download_model_files,
    hf_access_coordinator,
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


class ApplicationController(QObject):
    """Main application controller integrating UI and logic."""

    # fixed text, optional raw_text, optional CleanupInfo
    transcription_completed = pyqtSignal(str, object, object)
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
    overlay_state_update = pyqtSignal(object)
    minimize_to_tray_requested = pyqtSignal()
    # Emitted from the background reload worker (thread-safe UI updates).
    device_info_update = pyqtSignal(str)
    engine_busy_changed = pyqtSignal(bool)
    # Consent for Hugging Face model downloads: emitted (possibly from worker
    # threads) with (model_name, env_blocked, load_into_engine); the connected
    # slot shows the consent dialog on the Qt main thread. load_into_engine is
    # True for the selected-model flow (download + load) and False for
    # Model Manager fetch-only downloads.
    hf_consent_requested = pyqtSignal(str, bool, bool)
    # Model Manager lifecycle (emitted possibly from worker threads).
    model_download_started = pyqtSignal(str)
    model_download_finished = pyqtSignal(str, bool)
    model_deleted = pyqtSignal(str, bool, str)
    model_cache_changed = pyqtSignal()
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
    # Meeting Mode (emitted possibly from engine worker threads).
    # Partial state payload dict for the meeting panel: any of active/paused/
    # status/cloud_enabled/elapsed_s.
    meeting_state_changed = pyqtSignal(object)
    meeting_status_update = pyqtSignal(str)
    meeting_error = pyqtSignal(str)
    # Start result dict: meeting_id, url, host_url, guest_url.
    meeting_server_started = pyqtSignal(object)
    # List of interrupted meeting dicts found by the startup recovery scan.
    meeting_recovery_found = pyqtSignal(object)
    # One-time cloud-intelligence consent; the connected slot shows the
    # consent dialog on the Qt main thread and routes the result back into
    # MeetingRuntime.on_consent_result (mirrors hf_consent_requested).
    meeting_consent_requested = pyqtSignal()
    # Guest URL ready for clipboard copy (clipboard access is main-thread only).
    meeting_guest_link_ready = pyqtSignal(str)

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

        self.transcription_backends: Dict[str, TranscriptionBackend] = {}
        self.current_backend: Optional[TranscriptionBackend] = None
        self._current_model_name = "local_whisper"

        self._streaming_enabled = False

        self._pending_audio_path: Optional[str] = None
        self._pending_audio_duration: Optional[float] = None
        self._pending_file_size: Optional[int] = None
        self._transcription_start_time: Optional[float] = None

        # Debounced, background whisper reload. The ~1s model swap (cleanup +
        # load) must not run on the UI thread, and rapid combo changes are
        # coalesced into a single reload via this single-shot timer.
        self._reload_in_flight = False
        self._reload_timer = QTimer()
        self._reload_timer.setSingleShot(True)
        self._reload_timer.timeout.connect(self._do_reload_whisper_model)

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
        self.streaming_runtime.setup_streaming()
        self._connect_signals()
        self.hotkey_runtime.setup_hook_watchdog()

    def _setup_transcription_backends(
        self, local_backend: Optional[LocalWhisperBackend] = None
    ) -> None:
        """Initialize transcription backends.

        Args:
            local_backend: Optional preloaded LocalWhisperBackend (e.g. loaded
                off the UI thread during the splash screen animation).
        """
        logger.info("Setting up transcription backends...")

        self.transcription_backends["local_whisper"] = (
            local_backend if local_backend is not None else LocalWhisperBackend()
        )
        self.transcription_backends["api_whisper"] = OpenAIBackend("api_whisper")
        self.transcription_backends["api_gpt4o"] = OpenAIBackend("api_gpt4o")
        self.transcription_backends["api_gpt4o_mini"] = OpenAIBackend("api_gpt4o_mini")

        saved_model = settings_manager.load_model_selection()
        self.current_backend = self.transcription_backends.get(
            saved_model, self.transcription_backends["local_whisper"]
        )
        logger.info(f"Using transcription backend: {saved_model}")

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
        self.ui_controller.on_hf_policy_changed = self.on_hf_policy_changed
        self.ui_controller.on_model_download_requested = self.request_model_download
        self.ui_controller.on_model_delete_requested = self.request_model_delete
        self.ui_controller.get_loaded_local_model = self.get_loaded_local_model
        self.ui_controller.on_dictation_transcribe = self.transcribe_clip
        self.ui_controller.get_meeting_active = self.is_meeting_active
        self.ui_controller.on_component_install = self.request_component_install
        self.ui_controller.on_component_cancel = self.cancel_component_install
        self.ui_controller.on_component_remove = self.request_component_uninstall
        self.ui_controller.on_meeting_start = self.meeting_runtime.start_meeting
        self.ui_controller.on_meeting_start_demo = (
            self.meeting_runtime.start_demo_meeting
        )
        self.ui_controller.on_meeting_end = self.meeting_runtime.end_meeting
        self.ui_controller.on_meeting_pause = self.meeting_runtime.pause_meeting
        self.ui_controller.on_meeting_resume = self.meeting_runtime.resume_meeting
        self.ui_controller.on_meeting_cancel = self.meeting_runtime.cancel_meeting
        self.ui_controller.on_meeting_open_dashboard = (
            self.meeting_runtime.open_dashboard
        )
        self.ui_controller.on_meeting_open_past = (
            self.meeting_runtime.open_past_meeting
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

    def reload_whisper_model(self) -> None:
        """Schedule a debounced, background reload of the local whisper model.

        Called by both the Settings dialog and the inline main-GUI engine
        controls. Rapid changes (e.g. flipping device then quant) are coalesced
        into a single reload, and the request is refused while a recording or
        transcription is in progress.
        """
        if self.is_meeting_active():
            logger.info("Ignoring whisper reload: a meeting is in progress")
            self.status_update.emit("End the meeting before changing the engine")
            self.engine_busy_changed.emit(False)
            return

        backend = self.current_backend
        if self.recorder.is_recording or getattr(backend, "is_transcribing", False):
            logger.info("Ignoring whisper reload: recording/transcribing in progress")
            self.status_update.emit("Finish recording before changing the engine")
            self.engine_busy_changed.emit(False)
            return

        self._reload_timer.start(config.WHISPER_RELOAD_DEBOUNCE_MS)

    def _do_reload_whisper_model(self) -> None:
        """Debounce fired: kick off the reload on a worker thread (UI thread)."""
        if self.is_meeting_active():
            logger.info("Canceling queued whisper reload: meeting owns the engine")
            self.status_update.emit("End the meeting before changing the engine")
            self.engine_busy_changed.emit(False)
            return
        if self._reload_in_flight:
            # A reload is already running; retry shortly so the newest settings win.
            self._reload_timer.start(config.WHISPER_RELOAD_DEBOUNCE_MS)
            return

        self._reload_in_flight = True
        self.engine_busy_changed.emit(True)
        self.status_update.emit("Reloading whisper engine...")
        self.executor.submit(self._reload_worker)

    def _reload_worker(self) -> None:
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
                self.device_info_update.emit(info)
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

    def notify_main_ui_ready(self) -> None:
        """Called by bootstrap once the main window is shown.

        For a new installation whose selected backend is Local Whisper with an
        uncached model, this is the moment the consent dialog may first appear
        — after the main UI is available, never during startup, and never for
        API-only users. The same deferral applies to a GPU fallback from the
        bootstrap-time model load: it is reported here, once there is a UI to
        report it to.
        """
        if isinstance(self.current_backend, LocalWhisperBackend):
            QTimer.singleShot(0, self.ensure_local_model_available)
            backend = self.transcription_backends.get("local_whisper")
            if backend is not None and getattr(backend, "gpu_fallback_note", None):
                self.gpu_fallback_detected.emit()
        # Meeting crash recovery: scan now that there is a UI to show the
        # recovery dialog over.
        QTimer.singleShot(0, self.meeting_runtime.setup)

    def on_hf_policy_changed(self, policy: str) -> None:
        """React to a Hugging Face access-policy change from Settings.

        Switching to ``always`` authorizes downloads without prompting, so a
        missing selected model can be fetched right away. Other policies take
        effect on the next model request without further action.
        """
        if policy == HuggingFaceAccessPolicy.ALWAYS and isinstance(
            self.current_backend, LocalWhisperBackend
        ):
            self.ensure_local_model_available()

    def ensure_local_model_available(self) -> None:
        """Make sure the local Whisper model is loaded, requesting consent if needed.

        Safe to call from any thread: consent dialogs are raised on the Qt
        main thread via ``hf_consent_requested``, and downloads run on the
        executor. Concurrent calls for the same model are deduplicated by the
        access coordinator so at most one dialog and one download exist.
        """
        backend = self.transcription_backends.get("local_whisper")
        if backend is None or backend.is_available():
            return
        if not getattr(backend, "is_model_missing", False):
            # Load failed for another reason (hardware, corrupt install);
            # downloading would not help.
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

    def get_loaded_local_model(self) -> Optional[str]:
        """Return the model name the local engine currently has loaded, if any.

        Used by the Model Manager to disable deletion of the in-use model
        (its files are memory-mapped by ctranslate2).
        """
        backend = self.transcription_backends.get("local_whisper")
        if backend is not None and backend.is_available():
            return getattr(backend, "last_loaded_model", None)
        return None

    def request_model_download(self, model_name: str) -> None:
        """Fetch a model into the local cache via the consent flow (Model Manager).

        Fetch-only: the download never changes the active model selection or
        touches the loaded engine (unless the model happens to be the missing
        selected one, in which case the worker also loads it). Routes through
        the same coordinator policy/grant/dedup machinery as
        ``ensure_local_model_available``.

        Args:
            model_name: Concrete faster-whisper model name (not ``"auto"``).
        """
        if not hf_access_coordinator.begin_request(model_name):
            return  # consent dialog or download already in flight

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
        elif decision == AccessDecision.DOWNLOAD_ALLOWED:
            self._start_hf_model_task(model_name, load_into_engine=False)
        elif decision == AccessDecision.BLOCKED_BY_ENV:
            self.hf_consent_requested.emit(model_name, True, False)
        else:  # NEEDS_CONSENT
            self.hf_consent_requested.emit(model_name, False, False)

    def request_model_delete(self, model_name: str) -> None:
        """Delete a model's files from the local HF cache (Model Manager).

        Refuses to delete the currently loaded model: ctranslate2 memory-maps
        the files, so removal would fail (Windows) or yank data out from under
        the engine. The coordinator's request slot also guards against a
        concurrent download of the same model.

        Args:
            model_name: Concrete faster-whisper model name (not ``"auto"``).
        """
        backend = self.transcription_backends.get("local_whisper")
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
        """Delete cached model files off the Qt thread; report via signals."""
        try:
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
                # A declined Model Manager download must not touch the
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

    # ------------------------------------------------------------------
    # Downloadable components
    # ------------------------------------------------------------------

    def request_component_install(self, component_id: str) -> None:
        """Start installing a component on the component worker thread.

        No-op when an install for this component is already in flight.

        Args:
            component_id: Component to install (see ``ComponentId``).
        """
        cancel_event = component_coordinator.begin_install(component_id)
        if cancel_event is None:
            return

        self.component_install_started.emit(component_id)
        self.component_executor.submit(
            self._component_install_worker, component_id, cancel_event
        )

    def cancel_component_install(self, component_id: str) -> None:
        """Request cancellation of an in-flight component install."""
        component_coordinator.cancel_install(component_id)

    def request_component_uninstall(self, component_id: str) -> None:
        """Remove an installed component from disk."""
        try:
            uninstall_component(component_id)
            self.status_update.emit("Component removed")
        except Exception as exc:
            logger.error(f"Failed to remove component '{component_id}': {exc}")
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

        Args:
            component_id: Component being installed.
            cancel_event: Event the download loop polls to abort.
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

    @staticmethod
    def _describe_gpu_fallback(backend) -> str:
        """Status message for a GPU fallback, naming the cause-specific fix."""
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
                "GPU Acceleration under Manage models to enable it."
            )
        if cause == GpuFallbackCause.MISSING_LIBRARIES:
            return (
                "GPU found, but its CUDA libraries are missing — using CPU. "
                "Install requirements-gpu.txt to enable GPU acceleration."
            )
        return "GPU failed to load — using CPU. See openwhisper.log for details."

    def _start_hf_model_task(self, model_name: str, load_into_engine: bool = True) -> None:
        """Run the approved download/load on a worker thread with busy states.

        Args:
            model_name: Concrete model to download and/or load.
            load_into_engine: True for the selected-model flow (download +
                load, engine busy); False for fetch-only Model Manager
                downloads that leave the engine alone.
        """
        if load_into_engine:
            self.engine_busy_changed.emit(True)
        self.model_download_started.emit(model_name)
        self.executor.submit(self._hf_model_worker, model_name, load_into_engine)

    def _hf_model_worker(self, model_name: str, load_into_engine: bool = True) -> None:
        """Download (if permitted) and load the model off the Qt thread.

        Re-evaluates access just before downloading so the one-time grant /
        policy check stays centralized in the coordinator. A failed download
        leaves the model unavailable — it is never treated as cached and never
        falls back to another model.

        Download progress is indeterminate today (coarse status strings only);
        a determinate bar would require hooking huggingface_hub's tqdm_class
        into a Qt signal relay.
        """
        backend = self.transcription_backends.get("local_whisper")
        success = False
        try:
            decision = hf_access_coordinator.evaluate_access(model_name)
            if not load_into_engine:
                if decision not in (
                    AccessDecision.LOAD_CACHED,
                    AccessDecision.DOWNLOAD_ALLOWED,
                ):
                    logger.warning(
                        f"Fetch of '{model_name}' aborted: access decision {decision}"
                    )
                    self.status_update.emit(f"Model '{model_name}' is unavailable")
                    return
                if decision == AccessDecision.DOWNLOAD_ALLOWED:
                    self.status_update.emit(
                        f"Downloading model '{model_name}' from Hugging Face..."
                    )
                    download_model_files(model_name)
                    self.status_update.emit(f"Model '{model_name}' downloaded")
                success = True
                # Bridge: fetching the currently-missing selected model also
                # revives the engine (now a pure cache hit, no consent re-entry).
                if (
                    backend is not None
                    and getattr(backend, "is_model_missing", False)
                    and backend.model_name == model_name
                ):
                    backend.reload_model(model_name)
                    if backend.is_available():
                        self.device_info_update.emit(backend.device_info)
                        self.status_update.emit("Whisper engine ready")
                    if getattr(backend, "gpu_fallback_note", None):
                        self.gpu_fallback_detected.emit()
                return

            if decision == AccessDecision.LOAD_CACHED:
                self.status_update.emit(f"Loading model '{model_name}'...")
                backend.reload_model(model_name)
            elif decision == AccessDecision.DOWNLOAD_ALLOWED:
                self.status_update.emit(
                    f"Downloading model '{model_name}' from Hugging Face..."
                )
                backend.download_and_load()
            else:
                logger.warning(
                    f"Model task for '{model_name}' aborted: access decision {decision}"
                )
                self.status_update.emit(f"Model '{model_name}' is unavailable")
                return

            if backend.is_available():
                success = True
                self.device_info_update.emit(backend.device_info)
                self.status_update.emit("Whisper engine ready")
                logger.info(f"Model '{model_name}' ready: {backend.device_info}")
            else:
                self.status_update.emit(f"Model '{model_name}' failed to load")
            if getattr(backend, "gpu_fallback_note", None):
                self.gpu_fallback_detected.emit()
        except Exception as exc:
            logger.error(f"Model download/load failed for '{model_name}': {exc}")
            self.status_update.emit(f"Model download failed: {exc}")
        finally:
            hf_access_coordinator.end_request(model_name)
            self.model_download_finished.emit(model_name, success)
            if success:
                self.model_cache_changed.emit()
            if load_into_engine:
                self.engine_busy_changed.emit(False)

    def change_audio_device(self, device_id: Optional[int]) -> None:
        """Change the audio input device."""
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

    def reconfigure_streaming(self) -> None:
        self.streaming_runtime.reconfigure_streaming()

    def start_recording(self) -> bool:
        """Start audio recording (UI callback target).

        Returns:
            False when the start was refused because Meeting Mode is active,
            so the caller can roll its recording UI back; True otherwise.
        """
        if self._refuse_dictation_during_meeting():
            return False
        self.transcription_runtime.start_recording()
        return True

    def stop_recording(self) -> None:
        """Stop recording and submit transcription (UI callback target)."""
        self.transcription_runtime.stop_recording()

    def toggle_recording(self) -> bool:
        """Toggle recording on/off (hotkey callback target).

        Returns:
            False when a start was refused because Meeting Mode is active;
            True otherwise.
        """
        if not self.recorder.is_recording and self._refuse_dictation_during_meeting():
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
        """Exclusive mode: block dictation starts while a meeting is active.

        Returns:
            True when the start was refused (meeting in progress).
        """
        if not self.is_meeting_active():
            return False
        logger.info("Dictation start refused: Meeting Mode is active")
        self.status_update.emit(
            "Meeting Mode is active — end the meeting to use dictation"
        )
        return True

    def cancel(self) -> None:
        """Cancel an active recording or transcription (UI/hotkey callback target)."""
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
            self.meeting_runtime.start_meeting()

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
        if getattr(backend, "is_transcribing", False):
            raise RuntimeError("Transcription engine is busy")
        return backend.transcribe(audio_path)

    def retranscribe_audio(self, audio_path: str) -> None:
        """Re-transcribe an existing audio file (UI callback target).

        Args:
            audio_path: Path to the saved recording.
        """
        if not self._refuse_dictation_during_meeting():
            self.transcription_runtime.retranscribe_audio(audio_path)

    def upload_audio_file(self, audio_path: str) -> None:
        """Transcribe an uploaded audio file (UI callback target)."""
        if not self._refuse_dictation_during_meeting():
            self.transcription_runtime.upload_audio_file(audio_path)

    def on_model_changed(self, model_name: str) -> None:
        """Switch the active transcription backend (UI callback target)."""
        if not self._refuse_dictation_during_meeting():
            self.transcription_runtime.on_model_changed(model_name)

    def update_status_with_auto_hide(self, status: str) -> None:
        """Emit a thread-safe status update (HotkeyManager callback target)."""
        self.hotkey_runtime.update_status_with_auto_hide(status)

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
        """Show the meeting recovery dialog for interrupted sessions (Qt main thread)."""
        try:
            self.ui_controller.show_meeting_recovery_dialog(
                list(meetings),
                on_finalize=self.meeting_runtime.finalize_recovered,
                on_discard=self.meeting_runtime.discard_recovered,
            )
        except Exception as exc:
            logger.error(f"Meeting recovery dialog failed: {exc}")

    def _connect_signals(self) -> None:
        """Connect Qt signals to UI controller methods."""
        self.transcription_completed.connect(self._on_transcription_complete)
        self.transcription_failed.connect(self._on_transcription_error)
        self.hf_consent_requested.connect(self._on_hf_consent_requested)
        self.status_update.connect(self.ui_controller.set_status)
        self.device_info_update.connect(self.ui_controller.set_device_info)
        self.engine_busy_changed.connect(self.ui_controller.set_engine_busy)
        self.model_download_started.connect(
            self.ui_controller.on_model_download_started
        )
        self.model_download_finished.connect(
            self.ui_controller.on_model_download_finished
        )
        self.model_deleted.connect(self.ui_controller.on_model_deleted)
        self.model_cache_changed.connect(self.ui_controller.refresh_model_manager)
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
        self.meeting_guest_link_ready.connect(
            self.ui_controller.copy_meeting_guest_link
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

    def _on_transcription_complete(
        self, transcript: str, raw_text=None, cleanup_info=None
    ) -> None:
        self.transcription_runtime.on_transcription_complete(
            transcript, raw_text, cleanup_info
        )

    def _on_transcription_error(self, error_message: str) -> None:
        self.transcription_runtime.on_transcription_error(error_message)

    def cleanup(self) -> None:
        """Cleanup resources."""
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

        for executor in (self.executor, self.component_executor):
            try:
                executor.shutdown(wait=True, cancel_futures=True)
            except TypeError:
                executor.shutdown(wait=False)
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

"""Local speech models: Hugging Face consent, downloads, deletes and the GPU offer.

Requests may arrive on any thread (engine reloads, hotkeys, Settings). The
consent and runtime dialogs are raised through this object's signals, which
it receives on the Qt thread it was created on; downloads run on the
controller's shared executor.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, List, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from services.components import (
    ComponentId,
    available_component_ids,
    component_coordinator,
)
from services.hf_access import (
    AccessDecision,
    ConsentAction,
    delete_model_from_cache,
    describe_model_download_error,
    download_model_files,
    hf_access_coordinator,
    is_hf_hub_offline_env_set,
    is_model_cached,
    resolve_model_repo,
)
from services.settings import (
    HuggingFaceAccessPolicy,
    SettingsKey,
    settings_manager,
)
from transcriber import GpuFallbackCause, LocalWhisperBackend

if TYPE_CHECKING:
    from services.application_controller import ApplicationController

logger = logging.getLogger(__name__)


class ModelRuntime(QObject):
    """Owns the model download, delete and consent flows for the controller."""

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
    # A local model load ended in a GPU→CPU fallback (emitted possibly from
    # worker threads); the connected slot reverts the persisted device setting
    # and raises a cause-specific warning on the Qt main thread.
    gpu_fallback_detected = pyqtSignal()

    def __init__(self, controller: "ApplicationController"):
        super().__init__()
        self.controller = controller
        # Set from the Qt thread, read in the batch download worker; a plain
        # bool flag is enough because stopping is only checked between models.
        self._batch_stop_requested = False
        self._runtime_prompted = set()
        # The "Use this GPU" offer: None until shown, then "shown" while it is
        # open and "answered" after. A missing model's download consent waits
        # for it, since the answer decides which model and device to fetch for.
        self._gpu_offer_state: Optional[str] = None

    def connect_signals(self) -> None:
        ui = self.controller.ui_controller
        self.hf_consent_requested.connect(self._on_hf_consent_requested)
        self.runtime_consent_requested.connect(self._prompt_for_model_runtime)
        self.model_download_started.connect(ui.on_model_download_started)
        self.model_download_progress.connect(ui.on_model_download_progress)
        self.model_download_finished.connect(ui.on_model_download_finished)
        self.batch_download_planned.connect(ui.on_model_batch_planned)
        self.batch_download_finished.connect(ui.on_model_batch_finished)
        self.model_download_finished.connect(self._on_model_download_finished)
        self.model_deleted.connect(ui.on_model_deleted)
        self.model_cache_changed.connect(ui.refresh_model_views)
        self.gpu_fallback_detected.connect(self._on_gpu_fallback)

    def forget_runtime_prompts(self) -> None:
        """Ask about a missing runtime again (the engine selection changed)."""
        self._runtime_prompted.clear()

    def on_hf_policy_changed(self, policy: str) -> None:
        """React to a Hugging Face access-policy change from Settings.

        Switching to ``always`` authorizes downloads without prompting, so a
        missing selected model can be fetched right away. Other policies take
        effect on the next model request without further action.
        """
        from transcriber.optional_backend import LocalSpeechBackend
        if policy == HuggingFaceAccessPolicy.ALWAYS and isinstance(
            self.controller.current_backend, (LocalWhisperBackend, LocalSpeechBackend)
        ):
            self.ensure_local_model_available()

    def _local_model_backend(self):
        """The engine whose local model is in play; Whisper while the API engine is selected."""
        controller = self.controller
        if controller._current_model_name != "api":
            return controller.current_backend
        return controller.transcription_backends.get("local_whisper")

    def ensure_local_model_available(self) -> None:
        """Make sure the local Whisper model is loaded, requesting consent if needed.

        Safe to call from any thread: consent dialogs are raised on the Qt
        main thread via ``hf_consent_requested``, and downloads run on the
        executor. Concurrent calls for the same model are deduplicated by the
        access coordinator so at most one dialog and one download exist.
        """
        backend = self._local_model_backend()
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

        backend = self.controller.current_backend
        if isinstance(backend, LocalSpeechBackend):
            component_id = backend.runtime_component
            if component_id and not is_installed(component_id):
                return component_id
        return None

    def get_loaded_local_model(self) -> Optional[str]:
        """Return the loaded model so its memory-mapped files cannot be deleted."""
        backend = self._local_model_backend()
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
        status_update = self.controller.status_update
        if is_hf_hub_offline_env_set():
            status_update.emit("Downloads are disabled by HF_HUB_OFFLINE")
            return
        if hf_access_coordinator.requests_in_flight:
            # Keep the app's one-download-at-a-time invariant even when the
            # request came from somewhere else (e.g. a single-model download).
            status_update.emit(
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
        self.controller.executor.submit(self._hf_batch_worker, pending)

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
        controller = self.controller
        if model_name in MODELS and controller.is_meeting_active():
            self.model_deleted.emit(model_name, False, "End the meeting before removing speech models")
            return
        backends = controller.transcription_backends
        backend = (backends.get(MODELS[model_name].backend)
                   if model_name in MODELS else backends.get("local_whisper"))
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

        controller.executor.submit(self._model_delete_worker, model_name)

    def _model_delete_worker(self, model_name: str) -> None:
        controller = self.controller
        try:
            with controller._engine_lock:
                from services.local_asr.catalog import MODELS
                if model_name in MODELS:
                    backend = controller.transcription_backends.get(MODELS[model_name].backend)
                    if controller.is_meeting_active() or (
                        backend and backend.is_available() and backend.last_loaded_model == model_name
                    ):
                        raise RuntimeError("Model is in use — switch models first")
                delete_model_from_cache(model_name)
        except (PermissionError, OSError) as exc:
            logger.error(f"Model delete failed for '{model_name}': {exc}")
            self.model_deleted.emit(
                model_name, False, "Files are in use by another process"
            )
        except Exception as exc:
            logger.error(f"Model delete failed for '{model_name}': {exc}", exc_info=True)
            self.model_deleted.emit(model_name, False, str(exc))
        else:
            controller.status_update.emit(f"Model '{model_name}' deleted")
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
        controller = self.controller
        policy = hf_access_coordinator.get_policy()
        try:
            action = controller.ui_controller.show_hf_consent_dialog(
                model_name, policy, env_blocked
            )
        except Exception:
            hf_access_coordinator.end_request(model_name)
            raise

        if env_blocked:
            hf_access_coordinator.end_request(model_name)
            controller.status_update.emit(
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
            controller.ui_controller.open_settings_dialog(focus_hf_policy=True)
        else:  # canceled: no network activity
            hf_access_coordinator.end_request(model_name)
            controller.status_update.emit(
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
        backend = self.controller.transcription_backends.get("local_whisper")
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
        # Background reload picks the reverted model up from settings; it is
        # cached, so this never re-enters the consent flow.
        self.controller.reload_whisper_model()

    # ---- GPU fallback and "Use this GPU" ----

    def _on_gpu_fallback(self) -> None:
        """Reflect a GPU→CPU fallback in settings and the UI (Qt main thread).

        Persists the device the engine actually uses, so the Device combo does
        not claim CUDA while transcription runs on the CPU, and raises a
        status message that names the fix instead of just the symptom.
        """
        backend = self.controller.transcription_backends.get("local_whisper")
        if backend is None or not getattr(backend, "gpu_fallback_note", None):
            return

        device = SettingsKey.WHISPER_DEVICE.read(settings_manager.load_all_settings())
        if device != "cpu":
            logger.info(
                f"GPU fallback: reverting whisper device setting "
                f"from '{device}' to 'cpu'"
            )
            settings_manager.save_setting(SettingsKey.WHISPER_DEVICE, "cpu")

        self.controller.status_update.emit(self._describe_gpu_fallback(backend))
        self._offer_gpu_setup(backend)

    def _gpu_offer_waiting(self, backend) -> bool:
        """The offer is due or open for this backend. Cheap; any thread."""
        return (
            self._gpu_offer_state != "answered"
            and backend is self.controller.transcription_backends.get("local_whisper")
            and getattr(backend, "gpu_fallback_cause", None) == GpuFallbackCause.MISSING_LIBRARIES
            and ComponentId.GPU_ACCEL in available_component_ids()
            and not SettingsKey.WHISPER_GPU_OFFER_DECLINED.read(
                settings_manager.load_all_settings())
        )

    def _offer_gpu_setup(self, backend) -> None:
        """Offer one action for the GPU the libraries are missing for (Qt thread).

        Shown once: "Keep using the CPU" is remembered, and GPU
        Acceleration stays in Downloads either way. A model download that
        waited on the answer carries on after "Keep using the CPU".
        """
        if self._gpu_offer_state is not None or not self._gpu_offer_waiting(backend):
            return
        controller = self.controller
        if controller.is_meeting_active() or controller.recorder.is_recording or controller.is_transcribing():
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
            accepted = controller.ui_controller.show_use_gpu_dialog(plan)
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
        libraries missing, installing them comes first, and the component
        runtime reloads the engine when it lands; the reload then finds the
        model missing and downloads it on this grant, and loads it on the GPU.
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
        if plan.install_component:
            self.controller.status_update.emit("Installing GPU Acceleration...")
            self.controller.components.request_install(ComponentId.GPU_ACCEL)
        else:
            self.controller.reload_whisper_model()

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

    # ---- downloads ----

    def _hf_download_progress(self, model_name: str):
        """Return a throttled byte-progress callback for one model download."""
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
        controller = self.controller
        if success:
            self._prompt_for_model_runtime(model_name)
        if success and isinstance(controller.current_backend, LocalSpeechBackend):
            # A remote engine's model_name is the host's, and downloading the
            # same model here doesn't change what the host runs.
            if controller.current_backend.model_name == model_name and controller._selected_remote() is None:
                controller.reload_whisper_model()
            return
        if not success or model_name != "tiny.en":
            return
        if not is_model_cached("tiny.en"):
            return
        settings = settings_manager.load_all_settings()
        if SettingsKey.STREAMING_ENABLED.read(settings):
            controller.streaming_runtime.reconfigure_streaming()

    def _start_hf_model_task(self, model_name: str, load_into_engine: bool = True) -> None:
        """Run an approved load or fetch-only download on a worker."""
        from services.local_asr.catalog import MODELS
        if model_name in MODELS:
            load_into_engine = False
            self.runtime_consent_requested.emit(model_name)
        if load_into_engine:
            self.controller.engine_busy_changed.emit(True)
        self.model_download_started.emit(model_name)
        self.controller.executor.submit(self._hf_model_worker, model_name, load_into_engine)

    def _prompt_for_model_runtime(self, model_name: str) -> None:
        """Ask once per runtime per session, on the Qt thread.

        On Auto with no runtime yet, an NVIDIA GPU that can run the model is
        offered first, with the CPU runtime Auto falls back to as the
        alternative. Installing either ends the question.
        """
        from services.local_asr.catalog import gpu_runtime_offer, missing_runtime

        controller = self.controller
        if (is_hf_hub_offline_env_set() or controller._reload_in_flight
                or controller.is_meeting_active() or controller.recorder.is_recording
                or controller.is_transcribing()):
            return
        settings = settings_manager.load_all_settings()
        component = missing_runtime(model_name, settings)
        if not component or component in self._runtime_prompted or component_coordinator.is_installing(component):
            return
        gpu_component = gpu_runtime_offer(model_name, settings)
        if gpu_component and component_coordinator.is_installing(gpu_component):
            return
        self._runtime_prompted.add(component)
        ui = controller.ui_controller
        if gpu_component:
            chosen = ui.show_gpu_runtime_dialog(model_name, gpu_component, component)
        else:
            chosen = component if ui.show_required_runtime_dialog(model_name, component) else None
        if chosen:
            ui.open_downloads(component_id=chosen)
            controller.components.request_install(chosen)

    def _hf_model_worker(self, model_name: str, load_into_engine: bool = True) -> None:
        from services.local_asr.catalog import MODELS
        controller = self.controller
        requested_load = load_into_engine
        load_into_engine = load_into_engine and model_name not in MODELS
        backend = controller.transcription_backends.get("local_whisper")
        success = False
        downloaded = False
        try:
            if controller._shutdown_cancel.is_set():
                return
            if not load_into_engine:
                success = self._download_model_to_cache(model_name)
                return
            if controller._refuse_engine_load_during_meeting():
                return
            if not self._fetch_model_for_engine(model_name):
                return
            downloaded = success = True
            # A download can finish after the user switched engines or started
            # a meeting. Only the selected engine may acquire resident weights.
            with controller._engine_lock:
                if (controller._shutting_down or controller._refuse_engine_load_during_meeting()
                        or controller.current_backend is not backend):
                    return
                backend.reload_model(model_name)
                success = backend.is_available()
                controller.device_info_update.emit(backend.device_info, success)
                controller.status_update.emit("Whisper engine ready" if success else "Model failed to load")
                if getattr(backend, "gpu_fallback_note", None):
                    self.gpu_fallback_detected.emit()
        except Exception as exc:
            logger.error("Model download/load failed for '%s': %s", model_name, exc,
                         exc_info=True)
            controller.status_update.emit(
                f"Model '{model_name}' failed to load (details in openwhisper.log)."
                if downloaded else describe_model_download_error(model_name, exc)
            )
        finally:
            hf_access_coordinator.end_request(model_name)
            self.model_download_finished.emit(model_name, success)
            if success:
                self.model_cache_changed.emit()
            if requested_load:
                controller.engine_busy_changed.emit(False)

    def _fetch_model_for_engine(self, model_name: str) -> bool:
        """Download ``model_name`` if access allows; False when it is unavailable."""
        decision = hf_access_coordinator.evaluate_access(model_name)
        if decision == AccessDecision.DOWNLOAD_ALLOWED:
            self.controller.status_update.emit(f"Downloading model '{model_name}'...")
            download_model_files(model_name, progress_callback=self._hf_download_progress(model_name),
                                 cancel=self.controller._shutdown_cancel)
        elif decision != AccessDecision.LOAD_CACHED:
            self.controller.status_update.emit(f"Model '{model_name}' is unavailable")
            return False
        return True

    def _download_model_to_cache(self, model_name: str) -> bool:
        """Fetch-only worker body shared by single and batched downloads.

        The caller owns the coordinator request slot and the
        started/finished signals; this only evaluates access (consuming any
        grant), downloads, and bridges the engine revival when the fetched
        model happens to be the missing selected one. Returns True when the
        files are present afterwards.
        """
        controller = self.controller
        if controller._shutdown_cancel.is_set():
            return False
        backend = controller.transcription_backends.get("local_whisper")
        decision = hf_access_coordinator.evaluate_access(model_name)
        if decision not in (
            AccessDecision.LOAD_CACHED,
            AccessDecision.DOWNLOAD_ALLOWED,
        ):
            logger.warning(
                f"Fetch of '{model_name}' aborted: access decision {decision}"
            )
            controller.status_update.emit(f"Model '{model_name}' is unavailable")
            return False
        if decision == AccessDecision.DOWNLOAD_ALLOWED:
            controller.status_update.emit(
                f"Downloading model '{model_name}' from Hugging Face..."
            )
            download_model_files(
                model_name,
                progress_callback=self._hf_download_progress(model_name),
                cancel=controller._shutdown_cancel,
            )
            controller.status_update.emit(f"Model '{model_name}' downloaded")
        # Bridge: fetching the currently-missing selected model also
        # revives the engine (now a pure cache hit, no consent re-entry).
        if (
            backend is not None
            and controller.current_backend is backend
            and getattr(backend, "is_model_missing", False)
            and backend.model_name == model_name
        ):
            # The download itself succeeded, so this still reports True; only
            # reviving the engine waits for the meeting to end.
            if controller._refuse_engine_load_during_meeting():
                return True
            with controller._engine_lock:
                if (controller._shutting_down or controller.is_meeting_active()
                        or controller.current_backend is not backend):
                    return True
                backend.reload_model(model_name)
            if backend.is_available():
                controller.device_info_update.emit(backend.device_info, True)
                controller.status_update.emit("Whisper engine ready")
            if getattr(backend, "gpu_fallback_note", None):
                self.gpu_fallback_detected.emit()
        return True

    def _hf_batch_worker(self, model_names: List[str]) -> None:
        """Download a confirmed queue of models strictly sequentially.

        Each model is granted and announced right before its own download;
        stopping between models releases the remaining slots so no consent
        or request claim outlives the queue.
        """
        controller = self.controller
        processed = 0
        succeeded = 0
        try:
            for model_name in model_names:
                if self._batch_stop_requested or controller._shutdown_cancel.is_set():
                    break
                hf_access_coordinator.grant_once(model_name)
                self.model_download_started.emit(model_name)
                success = False
                try:
                    success = self._download_model_to_cache(model_name)
                except Exception as exc:
                    logger.error(f"Model download failed for '{model_name}': {exc}",
                                 exc_info=True)
                    controller.status_update.emit(
                        describe_model_download_error(model_name, exc)
                    )
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

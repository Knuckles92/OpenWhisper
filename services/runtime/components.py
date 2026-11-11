"""Downloadable components (speech runtimes, GPU Acceleration, Meeting agent).

Installs run on the controller's dedicated component executor and report
through this object's signals; it is created on the Qt thread, so emits from
that worker are queued to the Qt thread.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from PyQt6.QtCore import QObject, pyqtSignal

from services.component_runtime import activate_component
from services.components import (
    ComponentCanceled,
    ComponentError,
    ComponentId,
    component_coordinator,
    gpu_runtime_available,
    install_component,
    uninstall_component,
)
from services.settings import SettingsKey, settings_manager

if TYPE_CHECKING:
    from services.application_controller import ApplicationController

logger = logging.getLogger(__name__)


class ComponentRuntime(QObject):
    """Owns component installs, removals and their UI notifications."""

    # Emitted possibly from worker threads.
    # (component_id, phase, done_units, total_units); throttled in the worker
    # so a multi-gigabyte download cannot flood the Qt event queue.
    component_progress = pyqtSignal(str, str, int, int)
    component_install_started = pyqtSignal(str)
    # (component_id, success, message). Unlike model downloads there are many
    # distinct user-facing failure causes, so the message is carried along.
    component_install_finished = pyqtSignal(str, bool, str)
    component_state_changed = pyqtSignal()

    def __init__(self, controller: "ApplicationController"):
        super().__init__()
        self.controller = controller

    def connect_signals(self) -> None:
        ui = self.controller.ui_controller
        self.component_progress.connect(ui.on_component_progress)
        self.component_install_started.connect(
            lambda _component_id: self.controller.ui_controller.on_component_state_changed()
        )
        self.component_install_finished.connect(ui.on_component_install_finished)
        self.component_install_finished.connect(self._on_install_finished)
        self.component_state_changed.connect(ui.on_component_state_changed)

    def _speech_job_running(self) -> bool:
        controller = self.controller
        return bool(controller.is_meeting_active() or controller.recorder.is_recording
                    or controller.is_transcribing() or controller._reload_in_flight)

    def request_install(self, component_id: str) -> None:
        """Start an install unless the same component is already in flight."""
        from services.local_asr.catalog import RUNTIME_IDS
        from transcriber.optional_backend import LocalSpeechBackend
        if component_id in RUNTIME_IDS:
            if self._speech_job_running():
                self.controller.status_update.emit("Finish the current speech job before installing its runtime")
                return
            for backend in self.controller.transcription_backends.values():
                if isinstance(backend, LocalSpeechBackend) and backend.runtime_component == component_id:
                    backend.cleanup()
        cancel_event = component_coordinator.begin_install(component_id)
        if cancel_event is None:
            return

        self.component_install_started.emit(component_id)
        self.controller.component_executor.submit(
            self._install_worker, component_id, cancel_event
        )

    def cancel_install(self, component_id: str) -> None:
        component_coordinator.cancel_install(component_id)

    def request_uninstall(self, component_id: str) -> None:
        from services.local_asr.catalog import RUNTIME_IDS
        if component_id in RUNTIME_IDS:
            if self._speech_job_running():
                self.controller.status_update.emit("Finish the current speech job before removing its runtime")
                return
            self.controller.component_executor.submit(self._uninstall_worker, component_id)
        else:
            self._uninstall_worker(component_id)

    def _uninstall_worker(self, component_id: str) -> None:
        controller = self.controller
        try:
            from services.local_asr.catalog import RUNTIME_IDS
            from transcriber.optional_backend import LocalSpeechBackend
            with controller._engine_lock:
                if component_id in RUNTIME_IDS:
                    if self._speech_job_running():
                        raise RuntimeError("Finish the current speech job before removing its runtime")
                    for backend in controller.transcription_backends.values():
                        if isinstance(backend, LocalSpeechBackend) and backend.runtime_component == component_id:
                            backend.cleanup()
                            controller.device_info_update.emit(backend.device_info, False)
                uninstall_component(component_id)
            controller.status_update.emit("Component removed")
        except Exception as exc:
            logger.error("Failed to remove component '%s': %s", component_id, exc, exc_info=True)
            controller.status_update.emit(f"Could not remove the component: {exc}")
        finally:
            self.component_state_changed.emit()

    def _install_worker(self, component_id: str, cancel_event) -> None:
        """Download and install a component off the Qt thread.

        Progress is emitted at most ~10 times per second: a 2 GB download in
        1 MiB chunks would otherwise queue thousands of cross-thread events
        and visibly stall the UI.

        """
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
            # _on_install_finished.
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

    def _on_install_finished(
        self, component_id: str, success: bool, _message: str
    ) -> None:
        """Put a freshly installed GPU component to use (Qt main thread).

        A persisted device of "cpu" is moved back to "auto" first: it is
        either the residue of an earlier GPU fallback (see
        ``ModelRuntime._on_gpu_fallback``) or a pre-existing choice the
        install supersedes — nobody downloads a ~1 GB CUDA runtime to keep
        transcribing on the CPU. "auto" rather than "cuda" so machines
        without a usable GPU degrade silently instead of falling back again.
        """
        from transcriber.optional_backend import LocalSpeechBackend
        from services.local_asr.catalog import RUNTIME_IDS
        controller = self.controller
        # A remote engine runs on the host, so nothing installed here changes it.
        remote = controller._selected_remote() is not None
        if success and component_id in RUNTIME_IDS and isinstance(controller.current_backend, LocalSpeechBackend):
            if not remote:
                controller.reload_whisper_model()
            return
        if not success or component_id != ComponentId.GPU_ACCEL:
            return
        if not gpu_runtime_available():
            return  # activation failed; the restart message already covers it

        device = SettingsKey.WHISPER_DEVICE.read(settings_manager.load_all_settings())
        if device == "cpu":
            logger.info(
                "GPU component installed — moving whisper device setting "
                "from 'cpu' back to 'auto'"
            )
            settings_manager.save_setting(SettingsKey.WHISPER_DEVICE, "auto")

        backend = controller.transcription_backends.get("local_whisper")
        if (
            backend is not None
            and backend.is_available()
            and getattr(backend, "device", None) == "cuda"
        ):
            return  # already on the GPU (e.g. a component update)
        if remote:
            return  # Local Whisper picks the GPU up when it next loads

        controller.reload_whisper_model()

"""The Remote engine: this computer as a client of, or host for, paired computers.

As a client it watches the selected Remote engine's connection and quietly
reconnects with a backoff, and lets the user switch the host's model. As a
host it answers paired computers' model switches and relays who is
connected. Requests and events arrive on connection, pairing and hotkey
threads; this object is created on the Qt thread, so its signals deliver them
there.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from config import config
from services.settings import SettingsKey, settings_manager

if TYPE_CHECKING:
    from services.application_controller import ApplicationController

logger = logging.getLogger(__name__)

#: How often the Remote engine's watch reads its connection. Reading sends
#: nothing; websockets keeps the socket's state current on its own thread.
REMOTE_WATCH_INTERVAL_MS = 1000
#: Seconds before each automatic reconnect to an unreachable host; the last
#: repeats until it answers.
REMOTE_RETRY_DELAYS_S = (2, 5, 15, 30, 60)


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


class RemoteRuntime(QObject):
    """Owns the Remote engine's link watch, reconnects and model switches."""

    # The host's catalog of models arrived (pairing, ready, catalog).
    remote_catalog_received = pyqtSignal(object, object, object)
    # The Remote engine's link may read differently (emitted from any thread;
    # the slot takes a fresh RemoteLink on the Qt thread).
    remote_link_poke = pyqtSignal()
    # The Remote engine settled: a background reconnect's restore_link()
    # outcome, or "reload" when a reload of it finished.
    remote_settled = pyqtSignal(str)
    # Reconnect now (emitted from any thread; hotkeys start recordings off it).
    remote_retry_requested = pyqtSignal()
    remote_job_retry_requested = pyqtSignal(object)  # the waiting worker's event
    # Host: the paired computers connected now, as connected_clients() lists them.
    remote_clients_changed = pyqtSignal(list)
    # The Remote engine's pairing changed (emitted from the pairing worker).
    remote_pairing_changed = pyqtSignal()
    # This computer's owner renamed the paired host; same pairing, new name.
    remote_host_renamed = pyqtSignal()
    # Records moved between this computer and its paired host, either way.
    records_changed = pyqtSignal()
    # A paired computer chose one of this computer's models (emitted from its
    # connection thread with a _ClientModelSwitch the slot answers).
    client_model_switch_requested = pyqtSignal(object)

    def __init__(self, controller: "ApplicationController"):
        super().__init__()
        self.controller = controller
        # The watch: a tick that reads the socket's state (nothing is sent)
        # while Remote is selected, and a backed-off quiet reconnect when the
        # host drops or can't be reached.
        self._watch_timer = QTimer()
        self._watch_timer.setInterval(REMOTE_WATCH_INTERVAL_MS)
        self._watch_timer.timeout.connect(self._on_watch_tick)
        self._retry_timer = QTimer()
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._retry)
        self._retry_step = 0
        self._retry_at: Optional[float] = None
        self._restoring = False
        self._job_retry: Optional[threading.Event] = None
        self._link = None
        # device_info the watch last reported, so repeat failures stay quiet.
        self._reported = ""

    def connect_signals(self) -> None:
        ui = self.controller.ui_controller
        self.remote_pairing_changed.connect(self._on_pairing_changed)
        self.remote_host_renamed.connect(self._on_host_renamed)
        self.records_changed.connect(self._on_records_changed)
        self.client_model_switch_requested.connect(self._on_client_model_switch)
        self.remote_link_poke.connect(self._publish_link)
        self.remote_catalog_received.connect(self._on_catalog)
        self.remote_settled.connect(self._on_settled)
        self.remote_retry_requested.connect(self._retry)
        self.remote_job_retry_requested.connect(self._retry_for_transcription)
        self.remote_clients_changed.connect(ui.set_remote_clients)
        ui.on_remote_retry = self.retry_now

    @property
    def restoring(self) -> bool:
        """A background reconnect is running."""
        return self._restoring

    def stop_timers(self) -> None:
        self._watch_timer.stop()
        self._retry_timer.stop()

    # ---- client: choosing the paired computer's model ----

    def remote_models(self):
        """What the Remote engine's Model field shows (a ``RemoteModels``)."""
        remote = self.controller.transcription_backends.get("remote")
        return remote.model_choices() if remote is not None else None

    def select_remote_model(self, family: str, model: str) -> None:
        """Client: switch the paired computer to one of its models.

        The choice rides on the next reload, which asks the host to load it
        and reconnects to what the host then runs, so the Model field's
        switch shows the same busy state as any other engine change.
        """
        controller = self.controller
        remote = controller.transcription_backends.get("remote")
        if remote is None or controller.current_backend is not remote:
            return
        current = remote.current_model
        if current is not None and current.key == (family, model):
            return
        refusal = None
        if controller.is_meeting_active():
            refusal = "End the meeting before changing the engine"
        elif controller.recorder.is_recording or controller.is_transcribing():
            refusal = "Finish recording before changing the engine"
        if refusal:
            controller.status_update.emit(refusal)
            controller.ui_controller.refresh_remote_models()
            return
        choice = remote.request_model(family, model)
        host = remote.host_name or "the remote computer"
        controller._reload_note = f"Switching {host} to {choice.label}..."
        controller.reload_whisper_model()

    def select_remote_runtime(self, family: str, model: str, changes: dict) -> None:
        controller = self.controller
        remote = controller._selected_remote()
        if remote is None:
            return
        if (controller.is_meeting_active() or controller.recorder.is_recording
                or controller.is_transcribing()
                or controller._reload_in_flight or controller._reload_pending):
            controller.status_update.emit("Finish the current activity before changing the remote runtime")
            controller.ui_controller.refresh_remote_models()
            return
        try:
            remote.request_runtime(family, model, changes)
        except ValueError as exc:
            controller.status_update.emit(str(exc))
            controller.ui_controller.refresh_remote_models()
            return
        controller._reload_note = f"Updating the runtime on {remote.host_name or 'the remote computer'}..."
        controller.reload_whisper_model()

    # ---- host: switching this computer's engine for a paired computer ----

    def switch_engine_for_client(
        self, family: str, model: str, device_name: str, device: Optional[str] = None
    ) -> None:
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

    def configure_engine_for_client(self, family: str, model: str, changes: dict, device_name: str) -> None:
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
                request.error = self._switch_engine_to(request.family, request.model, request.device_name,
                                                      runtime=request.runtime)
        except Exception as exc:
            logger.exception("Switching models for a paired computer failed")
            request.error = f"{socket.gethostname()} couldn't switch models: {exc}"
        finally:
            request.done.set()

    def select_host_model(self, family: str, model: str) -> Optional[str]:
        """Host Mode's engine picker: serve another model ready on this computer.

        Switches the way a paired computer's request does, so every view
        follows. Returns why it can't, or None once the switch is under way.
        """
        try:
            return self._switch_engine_to(family, model, None)
        except Exception as exc:
            logger.exception("Switching the served model failed")
            return f"Couldn't switch models: {exc}"

    def _switch_engine_to(self, family: str, model: str, device_name: Optional[str], *,
                          runtime: Optional[dict] = None,
                          target_device: Optional[str] = None) -> Optional[str]:
        """Select a model here as the user would; returns why not, or None once queued.

        Persists the model the way the engine fields do, selects its backend
        through the main window so every view follows, and queues the reload
        whose end the controller's ``_engine_settled`` reports.
        ``device_name`` is the paired computer that asked, or None when it
        was asked for on this computer.
        """
        controller = self.controller
        name = socket.gethostname() if device_name else "This computer"
        refusal = _engine_switch_refusal(controller, name)
        if refusal is not None:
            return refusal
        refusal = _apply_engine_overrides(controller, name, family, model, runtime, target_device)
        if refusal is not None:
            return refusal
        label = _save_engine_model(family, model)
        if label is None:
            return f"{name} can't run {model}."
        if device_name:
            logger.info("Paired computer %s switched this computer to %s", device_name, label)
        else:
            logger.info("Host Mode switched this computer to %s", label)
        if not _select_backend_family(controller, family):
            return f"{name} couldn't switch to {label} right now."
        if runtime is None and target_device is None and _whisper_already_serves(controller, family, model):
            return None  # Selecting the backend was all it took.
        controller._reload_note = (f"Switching to {label} for {device_name}..." if device_name
                                   else f"Switching to {label}...")
        controller.reload_whisper_model()
        return None

    # ---- pairing, host and record events ----

    def _on_pairing_changed(self) -> None:
        """Reconnect the Remote engine after pairing or forgetting a host."""
        controller = self.controller
        controller.record_sync.wake(now=True)
        self.records_changed.emit()
        if controller._current_model_name == "remote":
            self.cancel_retry()
            controller.reload_whisper_model()
            return
        remote = controller.transcription_backends.get("remote")
        if remote is not None:
            remote.cleanup()

    def _on_host_renamed(self) -> None:
        """Call the paired host its new name everywhere, without reconnecting."""
        controller = self.controller
        remote = controller.transcription_backends.get("remote")
        pairing = controller.remote_engine.client_pairing()
        if remote is not None and pairing is not None:
            remote.host_renamed(pairing)
        selected = controller._selected_remote()
        if selected is not None:
            self._reported = selected.device_info
            controller.device_info_update.emit(selected.device_info, selected.is_available())
        controller.ui_controller.refresh_remote_models()
        self.records_changed.emit()

    def on_engine_busy(self, busy: bool) -> None:
        """A load settled: paired computers learn if the shared engine changed."""
        remote_engine = getattr(self.controller, "remote_engine", None)
        if not busy and remote_engine is not None:
            remote_engine.engine_changed()

    def on_host_event(self, kind: str) -> None:
        """Host: which paired computers are connected, and busy (any thread)."""
        controller = self.controller
        if kind in ("clients", "activity", "state"):
            self.remote_clients_changed.emit(controller.remote_engine.connected_clients())
        if kind == "models":
            controller.models.model_cache_changed.emit()
        if kind == "components":
            controller.components.component_state_changed.emit()
            controller.models.model_cache_changed.emit()
        if kind == "records":
            # A paired computer stored or removed a record here.
            self.records_changed.emit()
        if kind == "host_renamed":
            self.remote_host_renamed.emit()

    def on_record_sync_event(self, kind: str) -> None:
        """Client: a record moved to, or came back from, the paired host (any thread)."""
        if kind == "records":
            self.records_changed.emit()

    def _on_records_changed(self) -> None:
        main_window = self.controller.ui_controller.main_window
        main_window.refresh_history()
        main_window.refresh_past_meetings()

    def _on_catalog(self, pairing, ready: dict, catalog: dict) -> None:
        remote = self.controller.transcription_backends.get("remote")
        if remote is not None and remote.refresh_host_catalog(pairing, ready, catalog):
            self.controller.ui_controller.refresh_remote_models()

    # ---- client: watching the connection ----

    def watch(self) -> None:
        """Start the link watch while Remote is selected; its tick stops it after."""
        if self.controller._selected_remote() is not None and not self._watch_timer.isActive():
            self._watch_timer.start()
        self._publish_link()

    def _on_watch_tick(self) -> None:
        remote = self.controller._selected_remote()
        if remote is None:
            self._watch_timer.stop()
            self.cancel_retry()
            self._link = None
            return
        if remote.check_link():
            # Closed between requests: say so now, not at the next recording.
            self.controller.device_info_update.emit(remote.device_info, False)
        if not remote.is_available():
            self._schedule_retry()
        self._publish_link()

    def _publish_link(self) -> None:
        """Hand the engine card a fresh RemoteLink when it reads differently."""
        remote = self.controller._selected_remote()
        if remote is None:
            return
        from dataclasses import replace

        link = replace(remote.link(), retry_at=self._retry_at)
        if link != self._link:
            self._link = link
            self.controller.ui_controller.set_remote_link(link)

    def _schedule_retry(self) -> None:
        """Try the host again after the next backoff delay, unless already due."""
        remote = self.controller._selected_remote()
        if (remote is None or remote.is_available() or self._restoring
                or self._retry_timer.isActive()):
            return
        if remote.link().state == "unpaired":
            return
        delays = REMOTE_RETRY_DELAYS_S
        delay = delays[min(self._retry_step, len(delays) - 1)]
        self._retry_step += 1
        self._retry_at = time.monotonic() + delay
        self._retry_timer.start(int(delay * 1000))
        self.watch()

    def cancel_retry(self) -> None:
        self._retry_timer.stop()
        self._retry_at = None
        self._retry_step = 0

    def retry_now(self) -> None:
        """Reconnect to the paired computer now. Safe from any thread."""
        self.remote_retry_requested.emit()

    def retry_for_transcription(self, settled: threading.Event) -> None:
        """Ask Qt to start or join a reconnect and signal when it settles."""
        self.remote_job_retry_requested.emit(settled)

    def _retry_for_transcription(self, settled: threading.Event) -> None:
        remote = self.controller._selected_remote()
        if remote is None or remote.is_available():
            settled.set()
            return
        self._job_retry = settled
        self._retry()

    def _retry(self) -> None:
        controller = self.controller
        remote = controller._selected_remote()
        if (remote is None or self._restoring or controller._reload_in_flight
                or (controller._reload_pending and self._job_retry is None)):
            return
        self._retry_timer.stop()
        self._retry_at = None
        if remote.is_available():
            self.cancel_retry()
            self._publish_link()
            return
        self._restoring = True
        self._publish_link()
        threading.Thread(
            target=self._restore_worker, args=(remote,),
            name="remote-reconnect", daemon=True,
        ).start()

    def _restore_worker(self, remote) -> None:
        try:
            outcome = remote.restore_link()
        except Exception:
            logger.exception("Remote engine reconnect failed")
            outcome = "offline"
        self.remote_settled.emit(outcome)

    def _on_settled(self, outcome: str) -> None:
        """A reconnect attempt or a reload of the Remote engine ended (Qt thread)."""
        controller = self.controller
        if outcome != "reload":
            self._restoring = False
        remote = controller._selected_remote()
        if self._job_retry is not None:
            self._job_retry.set()
            self._job_retry = None
        if remote is None:
            return
        available = remote.is_available()
        if outcome != "reload" and (available or outcome == "changed"
                                    or remote.device_info != self._reported):
            # A reload reports its own result; a quiet retry only when it
            # changed something, so a host that stays away doesn't clear the
            # status line every minute.
            controller.device_info_update.emit(remote.device_info, available)
        self._reported = remote.device_info
        if available:
            self.cancel_retry()
            if outcome != "reload":
                # The preview may be waiting for a first connection, or be
                # built for another engine than the host runs now.
                controller._pending_streaming_setup = True
                controller._flush_pending_streaming_setup()
        elif outcome != "unpaired":
            self._schedule_retry()
        self.watch()


# ---- host switch helpers taking the controller explicitly ----


def _engine_switch_refusal(controller, name: str) -> Optional[str]:
    if controller.is_meeting_active():
        return f"{name} is running a meeting. Change its model after the meeting ends."
    if controller.recorder.is_recording or controller.is_transcribing():
        return f"{name} is transcribing right now. Try again in a moment."
    return None


def _apply_engine_overrides(controller, name: str, family: str, model: str, runtime: Optional[dict],
                            target_device: Optional[str]) -> Optional[str]:
    """Save a requested device and runtime settings; returns why not, or None."""
    if target_device is not None:
        refusal = _save_engine_device(controller, name, family, model, target_device)
        if refusal is not None:
            return refusal
    if runtime is not None:
        from services.remote_asr.engines import host_engine_for
        from services.remote_asr.runtime import validate_runtime

        if controller._reload_in_flight or controller._reload_pending:
            return f"{name} is loading its engine. Try again in a moment."
        description = host_engine_for(controller.current_backend).describe()
        updates = validate_runtime(description, family, model, runtime)
        settings_manager.update_settings(updates)
    return None


def _save_engine_device(controller, name: str, family: str, model: str, target_device: str) -> Optional[str]:
    from services.local_asr.catalog import WHISPER_BACKEND
    from services.remote_asr.dependencies import validate_model_device

    if controller._reload_in_flight or controller._reload_pending:
        return f"{name} is loading its engine. Try again in a moment."
    validate_model_device(family, model, target_device)
    if family == WHISPER_BACKEND:
        settings_manager.update_settings({SettingsKey.WHISPER_DEVICE: target_device,
                                          SettingsKey.WHISPER_COMPUTE_TYPE: "auto"})
    else:
        devices = dict(settings_manager.get(SettingsKey.LOCAL_ASR_DEVICES, {}) or {})
        devices[family] = target_device
        settings_manager.save_setting(SettingsKey.LOCAL_ASR_DEVICES, devices)
    return None


def _save_engine_model(family: str, model: str) -> Optional[str]:
    """Persist ``model`` as ``family``'s model; returns its label, or None if it can't run it."""
    from services.local_asr.catalog import BACKENDS, MODELS, WHISPER_BACKEND

    if family in BACKENDS and model in MODELS and MODELS[model].backend == family:
        models = dict(settings_manager.load_all_settings().get(SettingsKey.LOCAL_ASR_MODELS) or {})
        models[family] = model
        settings_manager.save_setting(SettingsKey.LOCAL_ASR_MODELS, models)
        return MODELS[model].label
    if family == WHISPER_BACKEND and model in config.WHISPER_MODEL_CHOICES and model != "auto":
        settings_manager.save_setting(SettingsKey.WHISPER_MODEL, model)
        return f"Whisper {model}"
    return None


def _select_backend_family(controller, family: str) -> bool:
    """Select ``family``'s backend through the main window; False when it didn't take."""
    if controller._current_model_name == family:
        return True
    display = next(key for key, value in config.MODEL_VALUE_MAP.items() if value == family)
    controller.ui_controller.select_transcription_backend(display)
    return controller._current_model_name == family


def _whisper_already_serves(controller, family: str, model: str) -> bool:
    from services.local_asr.catalog import WHISPER_BACKEND

    whisper = controller.transcription_backends.get(WHISPER_BACKEND)
    return (family == WHISPER_BACKEND and whisper is not None and whisper.is_available()
            and whisper.last_loaded_model == model and not controller._reload_pending)

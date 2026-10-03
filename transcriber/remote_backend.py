"""The Remote engine: a paired host's speech engine, used like a local one.

``RemoteSpeechBackend`` is a ``LocalSpeechBackend`` whose worker is a
``RemoteConnection`` instead of a local process. Everything above the worker
call is inherited: 30 s windowing, the ``SpeechDecoder`` the meeting and
preview paths call, incremental dictation and native streaming. After
connecting, ``backend_id`` and ``model_name`` are the host's, so the engine
gates in config (incremental dictation, preview style) follow whichever
engine the host is running.

The host lists the models it can switch to, and ``request_model`` asks the
next reload to switch it: the host loads that model as though it were picked
there, and this backend reconnects to it.

Two things a local engine does make no sense here and are skipped: a warmup
decode (the host warmed its own worker) and closing the worker on cancel
(the host finishes the request anyway, so the connection is kept). The
controller also leaves this engine alone when it frees memory for a meeting,
since nothing is resident here. ``link()`` describes the
connection for the engine card, and ``check_link``/``restore_link`` let the
controller notice a drop between requests and reconnect without a reload.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

from transcriber.base import TranscriptionBackend
from transcriber.optional_backend import LocalSpeechBackend, SpeechDecoder

logger = logging.getLogger(__name__)

#: Backend key and ``backend_id`` before the host has told us its engine.
REMOTE_BACKEND = "remote"
#: Waiting for the host to load a model this computer chose. Longer than the
#: host's own limit (services/remote_asr/service.py), so its reason arrives.
SWITCH_REQUEST_TIMEOUT_S = 300


@dataclass(frozen=True)
class RemoteLink:
    """The connection to the paired computer, as the engine card shows it."""

    #: "unpaired", "connecting", "connected" or "offline".
    state: str
    host: str = ""
    #: "Tailscale" or "local network" while connected.
    route: str = ""
    latency_ms: Optional[float] = None
    #: A request is waiting on the host.
    busy: bool = False
    #: Replies received so far; each new one is a packet coming home.
    replies: int = 0
    #: Keepalive pongs so far on this connection; each new one is a
    #: heartbeat the card pulses on.
    beat: int = 0
    #: Why it's offline, as the backend last said.
    detail: str = ""
    #: ``time.monotonic()`` of the controller's next automatic retry.
    retry_at: Optional[float] = None
    #: What the host runs and on what, for the tooltip.
    engine_label: str = ""
    device: str = ""
    address: str = ""
    compute_type: str = ""
    runtime_status: str = ""
    gpu_name: str = ""
    gpu_memory_mib: int = 0


@dataclass(frozen=True)
class RemoteTiming:
    """Where one transcription's time went, from the requests it made."""

    host: str
    requests: int
    #: Sending each window and waiting for its reply, summed.
    round_trip_s: float
    #: The part the host says it spent; None from a host too old to say.
    host_s: Optional[float]

    @property
    def network_s(self) -> Optional[float]:
        if self.host_s is None:
            return None
        return max(0.0, self.round_trip_s - self.host_s)


@dataclass(frozen=True)
class HostModel:
    """One model the paired computer can run, as its ``ready`` lists it."""

    family: str
    model: str
    label: str

    @property
    def key(self) -> tuple:
        return (self.family, self.model)


@dataclass(frozen=True)
class RemoteModels:
    """What the Remote backend's Model field shows."""

    #: The paired computer's name; empty when this computer isn't paired.
    host: str = ""
    #: What it can switch to, or None until it says (not reached yet, or a
    #: version that doesn't list them).
    models: Optional[tuple] = None
    #: What it serves this computer now, while connected.
    current: Optional[HostModel] = None
    runtime: Optional[dict] = None
    engine: Optional[dict] = None


def parse_host_models(raw) -> Optional[tuple]:
    """``ready["models"]`` as HostModels, or None from a host too old to list them."""
    if not isinstance(raw, list):
        return None
    models = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        family, model = entry.get("family"), entry.get("model")
        if isinstance(family, str) and isinstance(model, str) and family and model:
            models.append(HostModel(family, model, str(entry.get("label") or model)))
    return tuple(models)


def _serves(ready: dict, model: HostModel) -> bool:
    engine = ready.get("engine") if isinstance(ready.get("engine"), dict) else {}
    return bool(engine.get("available")) and (engine.get("family"), engine.get("model")) == model.key


class RemoteSpeechBackend(LocalSpeechBackend):
    is_remote = True

    def __init__(self):
        # LocalSpeechBackend.__init__ resolves a local model from settings;
        # the host decides the model here, so set the same fields directly.
        TranscriptionBackend.__init__(self)
        self.backend_id = REMOTE_BACKEND
        self._model_override = None
        self._device_override = None
        self._process = None
        self._generation = 0
        self._state_lock = threading.RLock()
        self._decode_lock = threading.Lock()
        self.model = None
        self.last_error = ""
        self.runtime_component = None
        self.device = ""
        self.model_name = ""
        self.engine: dict = {}
        self.host_name = ""
        self._pairing = None
        #: What the host said it can switch to when this computer last
        #: reached it; None before then, or from a host that doesn't say.
        self.host_models: Optional[tuple] = None
        #: Why the host kept its model when the last reload asked it to switch.
        self.switch_error = ""
        self._requested: Optional[HostModel] = None
        self._requested_runtime: Optional[dict] = None
        self.runtime: Optional[dict] = None
        #: A connection still handshaking or switching, closed by cleanup.
        self._pending = None
        #: Called (on any thread) whenever ``link()`` may read differently.
        self.on_link_changed: Optional[Callable[[], None]] = None
        self._connecting = 0
        self._inflight = 0
        self._replies = 0
        #: None until a reload or restore has looked for a pairing.
        self._unpaired: Optional[bool] = None
        #: requests, round trip, host time, requests the host timed.
        self._timing = (0, 0.0, 0.0, 0)

    # ---- the link, for the engine card ----

    def link(self) -> RemoteLink:
        """A snapshot of the connection. Cheap: reads state, sends nothing."""
        with self._state_lock:
            process = self._process
            connecting = self._connecting > 0
            busy = self._inflight > 0
            replies = self._replies
            unpaired = self._unpaired
            host = self.host_name
            detail = self.last_error
            label = str(self.engine.get("label") or "")
        if unpaired is None:
            # Once, before the first reload: settings are read from disk, and
            # the controller asks for the link every second.
            from services.remote_asr.settings import load_client_pairing

            pairing = load_client_pairing()
            unpaired = pairing is None
            with self._state_lock:
                if self._unpaired is None:
                    self._unpaired = unpaired
                if pairing is not None and not self.host_name:
                    self.host_name = pairing.host_name
                host = self.host_name
        gpu = (self.runtime or {}).get("gpu") or {}
        common = dict(host=host, busy=busy, replies=replies, engine_label=label,
                      device=self.device, compute_type=str(self.engine.get("compute_type") or ""),
                      runtime_status=str(self.engine.get("status") or ""),
                      gpu_name=str(gpu.get("name") or ""), gpu_memory_mib=gpu.get("total_mib") or 0)
        if process is not None and self.model is not None and process.alive:
            latency = process.latency
            return RemoteLink(
                "connected",
                route="Tailscale" if process.via_tailscale else "local network",
                latency_ms=latency * 1000 if latency is not None else None,
                beat=process.beats,
                address=process.where,
                **common,
            )
        if connecting:
            return RemoteLink("connecting", **common)
        if unpaired:
            return RemoteLink("unpaired", detail=detail, **common)
        return RemoteLink("offline", detail=detail, **common)

    def _notify_link(self) -> None:
        callback = self.on_link_changed
        if callback is not None:
            try:
                callback()
            except Exception:
                logger.debug("Remote link listener raised", exc_info=True)

    def _set_connecting(self, connecting: bool) -> None:
        with self._state_lock:
            self._connecting += 1 if connecting else -1
        self._notify_link()

    # ---- timing, for the stats line ----

    def timing_mark(self) -> tuple:
        with self._state_lock:
            return self._timing

    def timing_since(self, mark: tuple) -> RemoteTiming:
        """The requests made since ``timing_mark()`` returned ``mark``."""
        with self._state_lock:
            now = self._timing
        requests, round_trip, host_time, timed = (a - b for a, b in zip(now, mark))
        return RemoteTiming(
            host=self.host_name,
            requests=requests,
            round_trip_s=round_trip,
            # Every request timed, or the split would be a guess.
            host_s=host_time if requests and timed == requests else None,
        )

    # ---- identity and status ----

    @property
    def name(self) -> str:
        label = self.engine.get("label")
        if label and self.host_name:
            return f"{label} on {self.host_name}"
        return "Remote engine"

    @property
    def model_label(self) -> str:
        """The host's model as its own UI names it; empty while not connected."""
        return str(self.engine.get("label") or "") if self.is_available() else ""

    @property
    def current_model(self) -> Optional[HostModel]:
        """The model the host is serving this computer, while connected."""
        if not self.is_available():
            return None
        return HostModel(self.backend_id, self.model_name, self.model_label or self.model_name)

    def model_choices(self) -> RemoteModels:
        from services.remote_asr.settings import load_client_pairing

        pairing = load_client_pairing()
        if pairing is None:
            return RemoteModels()
        available = self.is_available()
        return RemoteModels(pairing.host_name, self.host_models, self.current_model,
                            self.runtime if available else None, dict(self.engine) if available else None)

    def request_model(self, family: str, model: str) -> HostModel:
        """Have the next reload switch the host to ``model``; returns the choice."""
        choice = next(
            (entry for entry in self.host_models or () if entry.key == (family, model)),
            HostModel(family, model, model),
        )
        with self._state_lock:
            self._requested = choice
            self._requested_runtime = None
        return choice

    def request_runtime(self, family: str, model: str, changes: dict) -> None:
        current = self.current_model
        if current is None or current.key != (family, model):
            raise ValueError("The host changed engines. Reconnect before changing its runtime.")
        if not self.runtime or not self.runtime.get("can_configure"):
            raise ValueError("Update OpenWhisper on the host to change its runtime from here.")
        with self._state_lock:
            self._requested = current
            self._requested_runtime = dict(changes)

    def request_language(self) -> str:
        return (self.runtime or {}).get("selected", {}).get("language") or super().request_language()

    @property
    def is_model_missing(self) -> bool:
        return False

    @property
    def last_loaded_model(self):
        # The model lives on the host; nothing here is memory-mapped.
        return None

    @property
    def device_info(self) -> str:
        if self.is_available():
            device = self.engine.get("device")
            compute = self.engine.get("compute_type")
            if device and compute:
                device = f"{device} ({compute})"
            return f"{self.name} | {device}" if device else self.name
        return self.last_error or "Remote engine is not connected"

    def is_available(self) -> bool:
        process = self._process
        return process is not None and not process.closed and self.model is not None

    # ---- connection lifecycle ----

    def reload_model(self, model_name=None, *, cancel_event=None):
        from services.remote_asr.client import RemoteEngineError
        from services.remote_asr.settings import load_client_pairing, load_client_token

        with self._state_lock:
            if cancel_event is not None and cancel_event.is_set():
                return
            self.cleanup()
            self.reset_cancel_flag()
            generation = self._generation
            requested, self._requested = self._requested, None
            runtime, self._requested_runtime = self._requested_runtime, None
        self.switch_error = ""
        pairing = load_client_pairing()
        self._unpaired = pairing is None
        if pairing is None:
            self.last_error = "Pair with a host in Settings → Remote engine."
            self.host_models = None
            self._notify_link()
            return
        token = load_client_token()
        if not token:
            self.last_error = (
                f"This computer's pairing with {pairing.host_name} is missing its "
                "token. Pair again in Settings → Remote engine."
            )
            self.host_models = None
            self._notify_link()
            return
        self.host_name = self.host_name or pairing.host_name
        self._set_connecting(True)
        try:
            if cancel_event is not None and cancel_event.is_set():
                return
            connection, ready = self._connect(pairing, token, generation)
            if requested is not None and (runtime is not None or not _serves(ready, requested)):
                connection, ready = self._switch_host(connection, pairing, token, requested, generation, runtime)
        except RemoteEngineError as exc:
            with self._state_lock:
                if generation == self._generation:
                    self.host_name = pairing.host_name
                    self.last_error = str(exc)
                    self.host_models = None
            return
        except RuntimeError as exc:
            # Canceled by a cleanup, or a switch that outlasted its timeout.
            with self._state_lock:
                if generation == self._generation:
                    self.last_error = str(exc)
            return
        finally:
            with self._state_lock:
                self._pending = None
            self._set_connecting(False)
        with self._state_lock:
            if (generation != self._generation
                    or (cancel_event is not None and cancel_event.is_set())):
                connection.close()
                return
            self._adopt(pairing, connection, ready)
        self._notify_link()

    def check_link(self) -> bool:
        """Notice a connection that closed between requests; True when it just did.

        The controller calls this on a timer. It only reads the socket's
        state, which websockets keeps current on its own thread.
        """
        with self._state_lock:
            process = self._process
        if process is None or process.alive:
            return False
        host = self.host_name or process.where
        if getattr(process, "closed_for_engine_change", False):
            # The host pushes this when its engine changes (see
            # SpeechHost.engine_changed); the retry adopts the new one.
            logger.info("Remote engine %s switched engines; reconnecting", host)
            self._drop(process, f"{host} switched engines. Reconnecting...")
            return True
        logger.info("Remote engine connection to %s closed while idle", host)
        self._drop(process, f"{host} stopped answering.")
        return True

    def restore_link(self) -> str:
        """One quiet attempt to reconnect, off the Qt thread.

        Returns "connected" when the connection is up again (or never went
        down) with the engine it had, "changed" when the host came back
        running something else and this backend adopted it (the controller
        then does what a reload's end does), "offline" when the host still
        can't be reached or has nothing loaded, and "unpaired" when there is
        nothing to connect to. The controller decides when to try again.
        """
        from services.remote_asr.client import RemoteEngineError
        from services.remote_asr.settings import load_client_pairing, load_client_token

        with self._state_lock:
            if self.is_available():
                return "connected"
            generation = self._generation
            previous = self._pairing
            known = (self.backend_id, self.model_name) if previous is not None else None
        pairing = load_client_pairing()
        self._unpaired = pairing is None
        token = load_client_token() if pairing is not None else None
        if pairing is None or not token:
            self._notify_link()
            return "unpaired"
        self._set_connecting(True)
        try:
            connection, ready = self._connect(pairing, token, generation)
        except RemoteEngineError as exc:
            with self._state_lock:
                if generation == self._generation:
                    self.host_name = self.host_name or pairing.host_name
                    self.last_error = str(exc)
            return "offline"
        except RuntimeError:
            return "offline"  # a cleanup or reload took over
        finally:
            with self._state_lock:
                self._pending = None
            self._set_connecting(False)
        engine = ready.get("engine") if isinstance(ready.get("engine"), dict) else {}
        same = (
            known is not None and previous.fingerprint == pairing.fingerprint
            and bool(engine.get("available"))
            and (engine.get("family"), engine.get("model")) == known
        )
        with self._state_lock:
            if generation != self._generation:
                connection.close()
                return "offline"
            if same:
                self._process = connection
                self.model = SpeechDecoder(self)
                self.engine = engine
                self.device = str(engine.get("device") or "")
                self._adopt_runtime(ready)
                self.host_models = parse_host_models(ready.get("models"))
                self.last_error = ""
                outcome = "connected"
            else:
                self._adopt(pairing, connection, ready)
                outcome = "changed" if self.is_available() else "offline"
        logger.info("Remote engine reconnect: %s (%s)", outcome, connection.where)
        self._notify_link()
        return outcome

    def _connect(self, pairing, token, generation=None):
        """Open and authenticate. With ``generation``, a cleanup closes it midway."""
        from services.remote_asr.client import RemoteConnection

        connection = RemoteConnection(
            pairing.host, pairing.port, token, pairing.fingerprint,
            alternates=pairing.alternates,
        )
        if generation is not None:
            with self._state_lock:
                if generation != self._generation:
                    raise RuntimeError("Transcription canceled")
                self._pending = connection
        return connection, connection.connect()

    def _switch_host(self, connection, pairing, token, requested: HostModel, generation, runtime=None):
        """Ask the host to load ``requested``, then reconnect to what it runs.

        The host answers once the load has settled. Whether it switched or
        said why it couldn't, its engine may differ from the one this
        connection was opened on, so a fresh connection is what gets adopted.
        """
        from services.remote_asr.client import RemoteEngineError

        info = connection.ready.get("host")
        host = str(info.get("name") or "") if isinstance(info, dict) else ""
        host = host or pairing.host_name
        logger.info("Asking %s to switch to %s", host, requested.label)
        try:
            if runtime is not None and (connection.ready.get("capabilities") or {}).get("engine_controls") is not True:
                raise RuntimeError(f"Update OpenWhisper on {host} to change its runtime from here.")
            connection.request(
                "configure_runtime" if runtime is not None else "select_model",
                family=requested.family, model=requested.model,
                timeout=SWITCH_REQUEST_TIMEOUT_S,
                **({"settings": runtime} if runtime is not None else {}),
            )
        except RemoteEngineError:
            raise
        except RuntimeError as exc:
            if connection.closed:
                raise  # canceled, or the host never answered
            message = str(exc)
            if message.startswith("Unknown operation"):
                message = f"Update OpenWhisper on {host} to choose its model from here."
            self.switch_error = message
            logger.info("%s kept its model: %s", host, message)
        connection.close()
        return self._connect(pairing, token, generation)

    def cleanup(self):
        with self._state_lock:
            pending, self._pending = self._pending, None
        super().cleanup()
        if pending is not None:
            pending.close()
        self._notify_link()

    def cancel_transcription(self):
        """Stop waiting for the host, and keep the connection.

        A local engine's worker is closed to stop it. The host finishes the
        request it has either way, so closing here would only make the next
        dictation reconnect first. The flag this sets stops the canceled
        job's remaining windows; the next job clears it
        (TranscriptionRuntime._rearm_remote_engine).
        """
        TranscriptionBackend.cancel_transcription(self)
        with self._state_lock:
            process = self._process
        if process is not None:
            process.abandon()

    def warmup(self) -> bool:
        # The host warmed its own worker when it loaded the model. A decode
        # sent from here would only hold its engine, and this computer's
        # reload, for another round trip.
        return False

    def _adopt_runtime(self, ready: dict) -> None:
        runtime = ready.get("runtime")
        capabilities = ready.get("capabilities")
        self.runtime = ({**runtime, "can_configure": isinstance(capabilities, dict)
                         and capabilities.get("engine_controls") is True}
                        if isinstance(runtime, dict) and runtime else None)

    def refresh_host_catalog(self, pairing, ready: dict, catalog: dict) -> bool:
        """Refresh setup choices from the management connection without touching audio."""
        engine = catalog.get("engine") or {}
        with self._state_lock:
            if self._pairing != pairing or not self._process or not self._process.alive:
                return False
            # Engine changes still go through the normal reconnect/load path.
            if any(engine.get(key) != self.engine.get(key) for key in ("family", "model", "device", "compute_type")):
                return False
            self._adopt_runtime({**ready, "runtime": catalog.get("runtime")})
            self.host_models = parse_host_models([
                item for item in catalog.get("models", []) if item.get("cached") and item.get("runtime_ready")
            ]) if catalog.get("can_select") else ()
            return True

    def _adopt(self, pairing, connection, ready: dict) -> None:
        """Take a fresh connection's engine as ours. Caller holds the state lock."""
        engine = ready.get("engine") if isinstance(ready.get("engine"), dict) else {}
        host = ready.get("host") if isinstance(ready.get("host"), dict) else {}
        self._pairing = pairing
        self.engine = engine
        self._adopt_runtime(ready)
        self.host_models = parse_host_models(ready.get("models"))
        self.host_name = str(host.get("name") or pairing.host_name)
        self.backend_id = str(engine.get("family") or REMOTE_BACKEND)
        self.model_name = str(engine.get("model") or "")
        self.device = str(engine.get("device") or "")
        if engine.get("available"):
            self._process = connection
            self.model = SpeechDecoder(self)
            self.last_error = ""
            logger.info("Remote engine connected: %s", self.device_info)
        else:
            connection.close()
            status = engine.get("status") or "its speech engine is not loaded"
            self.last_error = f"{self.host_name}: {status}"
            logger.info("Remote engine host has no engine ready: %s", status)

    def download_and_load(self, progress_callback=None):
        self.reload_model()

    # ---- requests ----

    def _request_audio(self, op, audio, language=None, **options) -> dict:
        from services.remote_asr.client import RemoteConnectionLost

        if self.should_cancel:
            raise RuntimeError("Transcription canceled")
        language = language or self.request_language()
        samples = np.asarray(audio, dtype=np.float32)
        with self._state_lock:
            self._inflight += 1
        self._notify_link()
        started = time.perf_counter()
        try:
            for attempt in (0, 1):
                with self._state_lock:
                    process = self._process
                if process is None:
                    raise RuntimeError(self.last_error or "Remote engine is not connected")
                try:
                    result, host_s = process.request_timed(
                        op, audio=samples, language=language, timeout=300, **options
                    )
                    break
                except RemoteConnectionLost as exc:
                    # A connection that died while idle (the host restarted, the
                    # laptop slept) failed before the host saw anything, so one
                    # reconnect and resend is safe. A request the host may have
                    # started is not repeated.
                    if exc.sent or attempt or not self._reconnect(process):
                        self._drop(process, str(exc))
                        raise RuntimeError(str(exc)) from exc
            round_trip = time.perf_counter() - started
            with self._state_lock:
                requests, total, host_total, timed = self._timing
                self._timing = (
                    requests + 1, total + round_trip,
                    host_total + (host_s or 0.0), timed + (host_s is not None),
                )
                self._replies += 1
        finally:
            with self._state_lock:
                self._inflight -= 1
            self._notify_link()
        if self.should_cancel:
            raise RuntimeError("Transcription canceled")
        return result

    def _reconnect(self, stale) -> bool:
        from services.remote_asr.client import RemoteEngineError
        from services.remote_asr.settings import load_client_token

        with self._state_lock:
            if self._process is not stale:
                return self._process is not None
            generation = self._generation
            pairing = self._pairing
            family, model = self.backend_id, self.model_name
        token = load_client_token()
        if pairing is None or not token:
            return False
        try:
            connection, ready = self._connect(pairing, token)
        except (RemoteEngineError, RuntimeError) as exc:
            logger.info("Remote engine reconnect failed: %s", exc)
            return False
        engine = ready.get("engine") if isinstance(ready.get("engine"), dict) else {}
        # A different engine changes which features apply; that takes a full
        # reload, not a silent swap mid-dictation.
        if not engine.get("available") or engine.get("family") != family or engine.get("model") != model:
            connection.close()
            return False
        with self._state_lock:
            if generation != self._generation or self._process is not stale:
                connection.close()
                return self._process is not None
            self._process = connection
            self.engine = engine
            self.device = str(engine.get("device") or "")
            self._adopt_runtime(ready)
            self.host_models = parse_host_models(ready.get("models"))
        logger.info("Remote engine reconnected to %s", connection.where)
        return True

    def _drop(self, process, message: str) -> None:
        with self._state_lock:
            if self._process is process:
                self._process = None
                self.model = None
                self.last_error = message
        if process is not None:
            process.close()
        self._notify_link()

"""The Remote engine: a paired host's speech engine, used like a local one.

``RemoteSpeechBackend`` is a ``LocalSpeechBackend`` whose worker is a
``RemoteConnection`` instead of a local process. Everything above the worker
call is inherited: 30 s windowing, the ``SpeechDecoder`` the meeting and
preview paths call, incremental dictation, native streaming and warmup. After
connecting, ``backend_id`` and ``model_name`` are the host's, so the engine
gates in config (incremental dictation, preview style, warmup) follow
whichever engine the host is running.
"""
from __future__ import annotations

import logging
import threading

import numpy as np

from transcriber.base import TranscriptionBackend
from transcriber.optional_backend import LocalSpeechBackend, SpeechDecoder

logger = logging.getLogger(__name__)

#: Backend key and ``backend_id`` before the host has told us its engine.
REMOTE_BACKEND = "remote"


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

    # ---- identity and status ----

    @property
    def name(self) -> str:
        label = self.engine.get("label")
        if label and self.host_name:
            return f"{label} on {self.host_name}"
        return "Remote engine"

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
            return f"{self.name} | {device}" if device else self.name
        return self.last_error or "Remote engine is not connected"

    def is_available(self) -> bool:
        process = self._process
        return process is not None and not process.closed and self.model is not None

    # ---- connection lifecycle ----

    def reload_model(self, model_name=None):
        from services.remote_asr.client import RemoteEngineError
        from services.remote_asr.settings import load_client_pairing, load_client_token

        with self._state_lock:
            self.cleanup()
            self.reset_cancel_flag()
            generation = self._generation
        pairing = load_client_pairing()
        if pairing is None:
            self.last_error = "Pair with a host in Settings → Remote engine."
            return
        token = load_client_token()
        if not token:
            self.last_error = (
                f"This computer's pairing with {pairing.host_name} is missing its "
                "token. Pair again in Settings → Remote engine."
            )
            return
        try:
            connection, ready = self._connect(pairing, token)
        except RemoteEngineError as exc:
            with self._state_lock:
                if generation == self._generation:
                    self.host_name = pairing.host_name
                    self.last_error = str(exc)
            return
        except RuntimeError:
            return  # canceled while connecting
        with self._state_lock:
            if generation != self._generation:
                connection.close()
                return
            self._adopt(pairing, connection, ready)

    @staticmethod
    def _connect(pairing, token):
        from services.remote_asr.client import RemoteConnection

        connection = RemoteConnection(
            pairing.host, pairing.port, token, pairing.fingerprint,
            alternates=pairing.alternates,
        )
        return connection, connection.connect()

    def _adopt(self, pairing, connection, ready: dict) -> None:
        """Take a fresh connection's engine as ours. Caller holds the state lock."""
        engine = ready.get("engine") if isinstance(ready.get("engine"), dict) else {}
        host = ready.get("host") if isinstance(ready.get("host"), dict) else {}
        self._pairing = pairing
        self.engine = engine
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
        for attempt in (0, 1):
            with self._state_lock:
                process = self._process
            if process is None:
                raise RuntimeError(self.last_error or "Remote engine is not connected")
            try:
                result = process.request(op, audio=samples, language=language, timeout=300, **options)
                break
            except RemoteConnectionLost as exc:
                # A connection that died while idle (the host restarted, the
                # laptop slept) failed before the host saw anything, so one
                # reconnect and resend is safe. A request the host may have
                # started is not repeated.
                if exc.sent or attempt or not self._reconnect(process):
                    self._drop(process, str(exc))
                    raise RuntimeError(str(exc)) from exc
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

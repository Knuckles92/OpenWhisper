"""Serve this computer's speech engine to paired OpenWhisper clients.

``SpeechHost`` runs websockets' threaded server on a daemon thread, one
thread per connection, so a request simply calls the engine and blocks, the
same way the local backend waits on its worker. Nothing here imports Qt: the
desktop app starts it from a Settings toggle, and a headless entry point can
start it the same way later.
"""
from __future__ import annotations

import hmac
import json
import logging
import secrets
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from services.remote_asr import protocol, tailscale
from services.remote_asr.activity import HostActivity
from services.remote_asr.engines import HostEngine, UnavailableEngine
from services.remote_asr.tls import HostIdentity, server_context

logger = logging.getLogger(__name__)

#: How long a new connection has to send ``hello`` or ``pair``.
HANDSHAKE_TIMEOUT_S = 10.0
#: A pairing code stays valid this long, for one successful pairing.
PAIRING_TTL_S = 300.0
#: Wrong codes allowed before the host closes pairing.
MAX_PAIRING_FAILURES = 5
PAIRING_CODE_DIGITS = 6
MAX_DEVICE_NAME = 60
#: last_seen is persisted at most this often per device.
_LAST_SEEN_PERSIST_S = 60.0

HostEvent = Callable[[str, dict], None]


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean_device_name(name) -> str:
    text = "".join(ch for ch in str(name or "") if ch.isprintable()).strip()
    return text[:MAX_DEVICE_NAME] or "Unnamed computer"


class DeviceRegistry:
    """Paired devices, persisted through ``load``/``save`` callables.

    Each entry keeps the SHA-256 of the device's token, never the token, so
    reading the settings file does not let anyone connect.
    """

    def __init__(self, load: Callable[[], list], save: Callable[[list], None]):
        self._load = load
        self._save = save
        self._lock = threading.Lock()
        self._persisted_seen: Dict[str, float] = {}

    def _entries(self) -> List[dict]:
        raw = self._load()
        if not isinstance(raw, list):
            return []
        return [
            dict(entry) for entry in raw
            if isinstance(entry, dict)
            and isinstance(entry.get("id"), str)
            and isinstance(entry.get("token_sha256"), str)
        ]

    def list(self) -> List[dict]:
        """Entries without their token digests, for display."""
        with self._lock:
            return [
                {key: value for key, value in entry.items() if key != "token_sha256"}
                for entry in self._entries()
            ]

    def add(self, name: str, via: str = "code") -> tuple[dict, str]:
        token = secrets.token_urlsafe(32)
        entry = {
            "id": uuid.uuid4().hex,
            "name": clean_device_name(name),
            "token_sha256": protocol.token_digest(token),
            "paired_at": _now_iso(),
            "last_seen": _now_iso(),
            "via": via,
        }
        with self._lock:
            entries = self._entries()
            entries.append(entry)
            self._save(entries)
        return {k: v for k, v in entry.items() if k != "token_sha256"}, token

    def remove(self, device_id: str) -> bool:
        with self._lock:
            entries = self._entries()
            kept = [entry for entry in entries if entry["id"] != device_id]
            if len(kept) == len(entries):
                return False
            self._save(kept)
            return True

    def authenticate(self, token) -> Optional[dict]:
        if not isinstance(token, str) or not token:
            return None
        digest = protocol.token_digest(token)
        with self._lock:
            match = None
            # Compare against every entry so timing reveals nothing.
            for entry in self._entries():
                if hmac.compare_digest(entry["token_sha256"], digest):
                    match = entry
            if match is None:
                return None
            self._touch_locked(match["id"])
            return {k: v for k, v in match.items() if k != "token_sha256"}

    def _touch_locked(self, device_id: str) -> None:
        now = time.monotonic()
        if now - self._persisted_seen.get(device_id, -_LAST_SEEN_PERSIST_S) < _LAST_SEEN_PERSIST_S:
            return
        entries = self._entries()
        for entry in entries:
            if entry["id"] == device_id:
                entry["last_seen"] = _now_iso()
        self._save(entries)
        self._persisted_seen[device_id] = now


@dataclass
class _Pairing:
    code: str
    expires_at: float
    failures: int = 0


@dataclass
class _Client:
    device_id: str
    name: str
    address: str
    connected_at: float
    ws: object = None
    #: Decoding one of this client's requests right now.
    busy: bool = False
    #: The engine this client was told about in ``ready``.
    engine_identity: tuple = ()
    #: Handling any request of this client's, a model switch included.
    in_request: bool = False
    #: When it connected, by the wall clock (for the host dashboard).
    since: float = 0.0


class SpeechHost:
    """A TLS WebSocket server in front of ``engine_provider()``.

    ``engine_provider`` is called on every request so the host always serves
    the engine currently selected; when its identity changes, clients are
    told to reconnect and learn the new engine.

    ``tailscale_owner`` returns the Tailscale login whose own computers may
    pair without a code, or "" when that is off. ``addresses`` returns the
    other addresses this computer answers on (its LAN and Tailscale IPs);
    clients keep them to fall back on when the one they paired with is out
    of reach, such as a laptop away from home.

    ``models`` lists what a client may switch this computer to (see
    ``engines.host_models``), and ``select_model(family, model, device_name)``
    switches it, returning the new engine's ``describe()`` once it has
    loaded or raising with the reason it can't.
    """

    def __init__(
        self,
        *,
        engine_provider: Callable[[], HostEngine],
        registry: DeviceRegistry,
        identity: HostIdentity,
        host_name: Optional[str] = None,
        on_event: Optional[HostEvent] = None,
        tailscale_owner: Optional[Callable[[], str]] = None,
        addresses: Optional[Callable[[], List[str]]] = None,
        models: Optional[Callable[[], List[dict]]] = None,
        select_model: Optional[Callable[..., dict]] = None,
        model_management: Optional[Callable[[], bool]] = None,
        manage_models: Optional[Callable[[str, dict, str], dict]] = None,
        runtime: Optional[Callable[[], dict]] = None,
        configure_runtime: Optional[Callable[[str, str, dict, str], dict]] = None,
        records_enabled: Optional[Callable[[], bool]] = None,
        records: Optional[Callable[[str, dict, bytes, dict], dict]] = None,
        records_summary: Optional[Callable[[str], dict]] = None,
        history=None,
    ):
        self._engine_provider = engine_provider
        self.registry = registry
        self.identity = identity
        self.host_name = host_name or socket.gethostname()
        self._on_event = on_event
        self._tailscale_owner = tailscale_owner or (lambda: "")
        self._addresses = addresses or (lambda: [])
        self._models = models or (lambda: [])
        self._select_model = select_model
        self._model_management = model_management or (lambda: False)
        self._manage_models = manage_models
        self._runtime = runtime or (lambda: {})
        self._configure_runtime = configure_runtime
        self._records_enabled = records_enabled or (lambda: False)
        self._records = records
        self._records_summary = records_summary or (lambda _device_id: {})
        from services.remote_history.channel import HistoryBroker

        self.history = history if history is not None else HistoryBroker(registry)
        self._lock = threading.Lock()
        self._server = None
        self._thread: Optional[threading.Thread] = None
        self._pairing: Optional[_Pairing] = None
        self._clients: Dict[str, _Client] = {}
        self.port: Optional[int] = None
        #: What paired computers asked of this host since sharing started.
        self.activity = HostActivity()

    # ---- lifecycle ----

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self, port: int = protocol.DEFAULT_PORT, bind: str = "0.0.0.0") -> int:
        """Listen on ``bind:port`` (0 picks a free port). Raises OSError if taken."""
        from websockets.sync.server import serve

        with self._lock:
            if self._server is not None:
                return self.port
            server = serve(
                self._handle,
                bind,
                port,
                ssl=server_context(self.identity),
                process_request=self._check_path,
                max_size=max(protocol.MAX_REQUEST_BYTES, protocol.MAX_REPLY_BYTES),
                compression=None,
                open_timeout=HANDSHAKE_TIMEOUT_S,
                ping_interval=20,
                ping_timeout=20,
                logger=logging.getLogger("websockets.remote_engine"),
            )
            self._server = server
            self.history.start()
            self.port = server.socket.getsockname()[1]
            self._thread = threading.Thread(
                target=server.serve_forever, name="RemoteEngineHost", daemon=True
            )
            self._thread.start()
            self.activity.reset()
        logger.info("Remote engine host listening on %s:%s", bind, self.port)
        self._emit("state", {})
        return self.port

    def stop(self) -> None:
        self.history.remove()
        with self._lock:
            server, self._server = self._server, None
            thread, self._thread = self._thread, None
            self._pairing = None
        if server is None:
            return
        try:
            # Only closes the listening socket; connections keep running.
            server.shutdown()
        except Exception:
            logger.debug("Remote engine host shutdown raised", exc_info=True)
        if thread is not None:
            thread.join(timeout=5)
        # Turning sharing off must cut off computers already connected.
        with self._lock:
            sockets = [client.ws for client in self._clients.values()]
            self._clients.clear()
        for ws in sockets:
            try:
                ws.close(1001, "host stopped sharing")
            except Exception:
                logger.debug("Could not close a client connection", exc_info=True)
        logger.info("Remote engine host stopped")
        self._emit("state", {})

    # ---- pairing ----

    def open_pairing(self, ttl: float = PAIRING_TTL_S) -> str:
        code = f"{secrets.randbelow(10 ** PAIRING_CODE_DIGITS):0{PAIRING_CODE_DIGITS}d}"
        with self._lock:
            self._pairing = _Pairing(code, time.monotonic() + ttl)
        self._emit("pairing", {"open": True})
        return code

    def close_pairing(self) -> None:
        with self._lock:
            had = self._pairing is not None
            self._pairing = None
        if had:
            self._emit("pairing", {"open": False})

    def pairing_status(self) -> Optional[tuple[str, float]]:
        """``(code, seconds_left)`` while pairing is open, else None."""
        with self._lock:
            pairing = self._pairing
            if pairing is None:
                return None
            left = pairing.expires_at - time.monotonic()
            if left <= 0:
                self._pairing = None
                return None
            return pairing.code, left

    def connected_clients(self) -> List[dict]:
        with self._lock:
            return [
                {"device_id": c.device_id, "name": c.name, "address": c.address,
                 "busy": c.busy, "since": c.since}
                for c in self._clients.values()
            ]

    def _set_busy(self, connection_id: str, busy: bool) -> None:
        with self._lock:
            client = self._clients.get(connection_id)
            if client is None or client.busy == busy:
                return
            client.busy = busy
        self._emit("activity", {"name": client.name, "busy": busy})

    def _set_in_request(self, connection_id: str, value: bool) -> None:
        with self._lock:
            client = self._clients.get(connection_id)
            if client is not None:
                client.in_request = value

    def engine_changed(self) -> int:
        """Tell idle clients the engine changed, by closing their connections.

        A client otherwise learns at its next request, so its window kept
        naming the engine it connected to (Parakeet) after this host moved
        to Whisper turbo. Its link watch notices the close within a second
        and reconnects, and ``ready`` names the new engine. A client in the
        middle of a request is left alone; that request's reply tells it.
        Blocking (each close waits for the handshake), so not on the UI
        thread. Returns how many connections were closed.
        """
        current = self._engine().identity
        with self._lock:
            stale = [c.ws for c in self._clients.values()
                     if c.engine_identity != current and not c.in_request]
        for ws in stale:
            try:
                ws.close(protocol.CLOSE_ENGINE_CHANGED, "engine changed")
            except Exception:
                logger.debug("Could not close a client on an engine change", exc_info=True)
        if stale:
            logger.info("Engine changed; told %d paired computer(s) to reconnect", len(stale))
        return len(stale)

    def remove_device(self, device_id: str) -> bool:
        """Forget a device and drop its open connections."""
        removed = self.registry.remove(device_id)
        self.history.remove(device_id)
        with self._lock:
            sockets = [c.ws for c in self._clients.values() if c.device_id == device_id]
        for ws in sockets:
            try:
                ws.close(protocol.CLOSE_UNAUTHORIZED, "device removed")
            except Exception:
                logger.debug("Could not close a removed device's connection", exc_info=True)
        if removed:
            self._emit("devices", {})
        return removed

    # ---- connection handling ----

    def _emit(self, kind: str, detail: dict) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(kind, detail)
        except Exception:
            logger.debug("Remote engine host event listener raised", exc_info=True)

    @staticmethod
    def _check_path(connection, request):
        if request.path.split("?", 1)[0] != protocol.PATH:
            return connection.respond(404, "Not an OpenWhisper remote engine endpoint\n")
        return None

    def _engine(self) -> HostEngine:
        try:
            engine = self._engine_provider()
        except Exception as exc:
            logger.warning("Remote engine provider failed: %s", exc)
            return UnavailableEngine("The host's engine is unavailable")
        return engine if engine is not None else UnavailableEngine("No engine is selected on the host")

    @staticmethod
    def _send(ws, message: dict) -> None:
        ws.send(json.dumps(message, separators=(",", ":")))

    def _handle(self, ws) -> None:
        peer = ws.remote_address
        address = str(peer[0]) if isinstance(peer, tuple) and peer else "unknown"
        try:
            first = ws.recv(timeout=HANDSHAKE_TIMEOUT_S)
        except TimeoutError:
            ws.close(protocol.CLOSE_TIMEOUT, "handshake timeout")
            return
        except Exception:
            return
        try:
            message = json.loads(first) if isinstance(first, str) else None
        except ValueError:
            message = None
        if not isinstance(message, dict):
            ws.close(protocol.CLOSE_BAD_REQUEST, "expected hello")
            return
        if message.get("type") == "probe":
            # Answered before the version check, so discovery can say
            # "update OpenWhisper there" instead of skipping the host.
            self._probe(ws)
            return
        if message.get("protocol") != protocol.PROTOCOL_VERSION:
            self._send(ws, {
                "type": "error",
                "code": "protocol",
                "message": (
                    f"This host speaks remote engine protocol "
                    f"{protocol.PROTOCOL_VERSION}; update OpenWhisper on both computers."
                ),
            })
            ws.close(protocol.CLOSE_BAD_REQUEST, "protocol version")
            return
        kind = message.get("type")
        if kind == "pair" and message.get("tailscale") is True:
            self._pair_tailscale(ws, message, address)
        elif kind == "pair":
            self._pair(ws, message, address)
        elif kind == "hello":
            self._serve(ws, message, address)
        else:
            ws.close(protocol.CLOSE_BAD_REQUEST, "expected hello")

    def _host_info(self) -> dict:
        try:
            addresses = [str(a) for a in self._addresses() if a]
        except Exception:
            logger.debug("Remote engine host addresses lookup failed", exc_info=True)
            addresses = []
        return {
            "name": self.host_name,
            "fingerprint": self.identity.fingerprint,
            "addresses": addresses,
        }

    def _model_list(self) -> List[dict]:
        try:
            return [dict(entry) for entry in self._models() if isinstance(entry, dict)]
        except Exception:
            logger.warning("Remote engine host couldn't list its models", exc_info=True)
            return []

    def _can_manage_models(self) -> bool:
        try:
            return self._manage_models is not None and self._model_management() is True
        except Exception:
            logger.warning("Could not read model management permission", exc_info=True)
            return False

    def _keeps_records(self) -> bool:
        try:
            return self._records is not None and self._records_enabled() is True
        except Exception:
            logger.warning("Could not read the record storage permission", exc_info=True)
            return False

    def _stored_summary(self, device_id: str) -> dict:
        try:
            summary = self._records_summary(device_id)
            return dict(summary) if isinstance(summary, dict) else {}
        except Exception:
            logger.warning("Could not count a device's stored records", exc_info=True)
            return {}

    def _tailscale_pairing_owner(self) -> str:
        try:
            return str(self._tailscale_owner() or "")
        except Exception:
            logger.debug("Tailscale owner lookup failed", exc_info=True)
            return ""

    def _probe(self, ws) -> None:
        """Who this is and what it serves: enough for a discovery list, no secrets."""
        engine = self._engine().describe()
        self._send(ws, {
            "type": "probe",
            "protocol": protocol.PROTOCOL_VERSION,
            "host": {"name": self.host_name},
            "engine": {
                key: engine.get(key) for key in ("label", "device", "available", "family")
            },
            "tailscale_pairing": bool(self._tailscale_pairing_owner()),
        })
        ws.close()

    def _finish_pairing(self, ws, name: str, address: str, via: str) -> None:
        device, token = self.registry.add(name, via=via)
        logger.info("Paired remote engine device %r from %s (%s)", device["name"], address, via)
        self._send(ws, {
            "type": "paired",
            "token": token,
            "device_id": device["id"],
            "host": self._host_info(),
        })
        ws.close()
        self.activity.paired(device["id"], device["name"], via)
        self._emit("paired", {"name": device["name"]})
        self._emit("devices", {})

    def _pair_tailscale(self, ws, message: dict, address: str) -> None:
        """Pair one of the owner's own Tailscale computers without a code."""
        owner = self._tailscale_pairing_owner()
        local = ws.local_address
        local_ip = str(local[0]) if isinstance(local, tuple) and local else ""
        identity = tailscale.own_device(address, local_ip, owner) if owner else None
        if identity is None:
            reason = (
                "This host only lets its owner's own Tailscale computers pair "
                "without a code. Enter the pairing code shown there instead."
                if owner else
                "This host doesn't pair over Tailscale without a code. Enter the "
                "pairing code shown there instead."
            )
            logger.info("Refused Tailscale pairing from %s", address)
            self._send(ws, {"type": "error", "code": "tailscale_refused", "message": reason})
            ws.close(protocol.CLOSE_UNAUTHORIZED, "tailscale_refused")
            return
        name = clean_device_name(message.get("device_name") or identity.node)
        self._finish_pairing(ws, name, address, "tailscale")

    def _pair(self, ws, message: dict, address: str) -> None:
        code = str(message.get("code") or "").strip()
        name = clean_device_name(message.get("device_name"))
        error = None
        closed_now = False
        with self._lock:
            pairing = self._pairing
            if pairing is not None and pairing.expires_at <= time.monotonic():
                pairing = self._pairing = None
            if pairing is None:
                error = ("pairing_closed", "Pairing isn't open on the host. Click "
                         "\"Pair a device\" there and enter the code it shows.")
            elif not hmac.compare_digest(code, pairing.code):
                pairing.failures += 1
                if pairing.failures >= MAX_PAIRING_FAILURES:
                    self._pairing = None
                    closed_now = True
                    error = ("bad_code", "Too many wrong codes. Start pairing again on the host.")
                else:
                    error = ("bad_code", "That code doesn't match the one on the host.")
            else:
                self._pairing = None
        if error is not None:
            logger.info("Refused remote engine pairing from %s: %s", address, error[0])
            self._send(ws, {"type": "error", "code": error[0], "message": error[1]})
            ws.close(protocol.CLOSE_UNAUTHORIZED, error[0])
            if closed_now:
                self._emit("pairing", {"open": False})
            return
        self._finish_pairing(ws, name, address, "code")

    def _serve(self, ws, message: dict, address: str) -> None:
        device = self.registry.authenticate(message.get("token"))
        if device is None:
            self._send(ws, {
                "type": "error",
                "code": "unauthorized",
                "message": "This computer isn't paired with the host anymore. Pair it again.",
            })
            ws.close(protocol.CLOSE_UNAUTHORIZED, "unauthorized")
            return
        if message.get("purpose") == "history":
            self.history.serve(ws, device, message.get("token"), message.get("history_enabled"))
            return
        engine = self._engine()
        identity = engine.identity
        ready = {
            "type": "ready",
            "host": self._host_info(),
            "device": {"id": device["id"], "name": device["name"]},
            "engine": engine.describe(),
            "models": self._model_list(),
            "runtime": self._runtime(),
            "capabilities": {"model_management": self._can_manage_models(),
                             "runtime_installation": self._can_manage_models(),
                             "engine_controls": self._configure_runtime is not None},
        }
        if self._records is not None:
            ready["capabilities"]["records"] = self._keeps_records()
            # Counted even while storage is off, so a client can still find
            # (and bring back) what it stored before the owner turned it off.
            ready["records"] = self._stored_summary(device["id"])
        self._send(ws, ready)
        connection_id = uuid.uuid4().hex[:8]
        with self._lock:
            if self._server is None:
                # Sharing was turned off while this client was handshaking.
                ws.close(1001, "host stopped sharing")
                return
            self._clients[connection_id] = _Client(
                device["id"], device["name"], address, time.monotonic(), ws,
                engine_identity=identity, since=time.time(),
            )
        self.activity.connected(device["id"], device["name"])
        self._emit("clients", {})
        streams: set = set()
        try:
            for frame in ws:
                self._set_in_request(connection_id, True)
                try:
                    # Recheck revocation before accepting more work, including
                    # requests already buffered when the device was removed.
                    if self.registry.authenticate(message.get("token")) is None:
                        ws.close(protocol.CLOSE_UNAUTHORIZED, "device removed")
                        break
                    reply = self._dispatch(frame, engine, identity, connection_id, streams,
                                           device["name"], device["id"])
                    self._send(ws, reply)
                finally:
                    self._set_in_request(connection_id, False)
                if reply.get("code") == "engine_changed":
                    ws.close(protocol.CLOSE_ENGINE_CHANGED, "engine changed")
                    break
        except Exception as exc:
            logger.info("Remote engine client %s disconnected: %s", device["name"], exc)
        finally:
            for session in list(streams):
                try:
                    engine.cancel_stream(session)
                except Exception:
                    logger.debug("Could not cancel remote stream %s", session, exc_info=True)
            with self._lock:
                self._clients.pop(connection_id, None)
            self.activity.disconnected(device["id"], device["name"])
            self._emit("clients", {})

    def _dispatch(self, frame, engine: HostEngine, identity: tuple,
                  connection_id: str, streams: set, device_name: str = "",
                  device_id: str = "") -> dict:
        try:
            header, payload = protocol.unpack_frame(frame)
        except protocol.ProtocolError as exc:
            return {"id": None, "error": str(exc), "code": "bad_request"}
        request_id = header.get("id")
        op = header.get("op")
        if isinstance(op, str) and op.startswith("records_"):
            return self._records_request(request_id, header, payload,
                                         {"id": device_id, "name": device_name})
        try:
            audio = protocol.payload_audio(payload)
        except protocol.ProtocolError as exc:
            return {"id": request_id, "error": str(exc), "code": "bad_request"}
        if header.get("op") == "configure_runtime":
            return self._configure_runtime_request(request_id, header, device_name)
        if header.get("op") in ("model_catalog", "download_model", "install_runtime"):
            return self._manage_model_request(request_id, header, device_name)
        if header.get("op") == "select_model":
            # Asked for whatever this computer runs now, so a switch made
            # since this client connected doesn't turn it away.
            return self._switch_model(request_id, header, device_name)
        current = self._engine()
        if current.identity != identity:
            return {
                "id": request_id,
                "code": "engine_changed",
                "error": "The host switched speech engines. Reconnecting.",
            }
        op = header.get("op")
        language = header.get("language")
        language = language if isinstance(language, str) else None
        decodes = op in ("transcribe", "stream")
        if decodes:
            self._set_busy(connection_id, True)
        started = time.perf_counter()
        try:
            if op == "transcribe":
                result = current.transcribe(audio, language)
            elif op in ("stream", "cancel_stream"):
                session = header.get("session")
                if not isinstance(session, str) or not session:
                    raise ValueError("A stream needs a session id")
                # Sessions are per connection, so two clients (or the host's
                # own preview) never share one.
                scoped = f"remote-{connection_id}-{session}"
                if op == "stream":
                    finish = bool(header.get("finish"))
                    streams.add(scoped)
                    result = current.stream(scoped, audio, language, finish)
                    if finish:
                        streams.discard(scoped)
                else:
                    streams.discard(scoped)
                    current.cancel_stream(scoped)
                    result = {}
            elif op == "describe":
                result = current.describe()
            else:
                raise ValueError(f"Unknown operation: {op!r}")
            # Counted before the busy flag clears, so the "activity" event
            # that clearing sends already sees it.
            if op == "transcribe":
                self.activity.transcribed(device_id, device_name,
                                          len(audio) / protocol.SAMPLE_RATE,
                                          time.perf_counter() - started)
            elif op == "stream":
                self.activity.previewed(device_id, device_name)
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            if decodes:
                self.activity.failed(device_id, device_name, error)
            return {"id": request_id, "error": error}
        finally:
            if decodes:
                self._set_busy(connection_id, False)
        # Time spent here, including any wait behind this computer's own
        # dictation, so the client can tell it apart from the network's.
        host_ms = round((time.perf_counter() - started) * 1000, 1)
        return {"id": request_id, "result": result, "host_ms": host_ms}

    def _manage_model_request(self, request_id, header: dict, device_name: str) -> dict:
        # Checked for EVERY request, not just hello: turning the setting off
        # also denies already-connected clients. Existing model selection is
        # deliberately unchanged and still works without this opt-in.
        if not self._can_manage_models():
            return {"id": request_id, "code": "forbidden", "error":
                    "Model management is disabled on the host. Enable it in Settings → Remote engine there."}
        op = header["op"]
        fields = {"id", "op"} if op == "model_catalog" else {"id", "op", "family", "model"}
        if op == "install_runtime":
            fields.add("device")
        if (set(header) - fields or (op != "model_catalog" and not all(
            isinstance(header.get(key), str) and header[key] for key in ("family", "model")
        )) or (op == "install_runtime" and header.get("device") not in ("cpu", "cuda"))):
            return {"id": request_id, "code": "bad_request", "error": "Invalid model management request."}
        try:
            result = self._manage_models(op, header, device_name)
        except Exception as exc:
            return {"id": request_id, "error": str(exc) or type(exc).__name__}
        return {"id": request_id, "result": result}

    #: Operations that store something; the rest read back or delete.
    _RECORD_WRITES = frozenset({"records_begin", "records_put", "records_commit"})

    def _records_request(self, request_id, header: dict, payload: bytes, device: dict) -> dict:
        """A device's own stored records. Never another device's.

        Storing needs the owner's opt-in, rechecked on every request, so
        turning it off stops uploads already under way. Reading back and
        deleting don't: a computer can always retrieve or remove what it
        stored here while it stays paired.
        """
        op = header.get("op")
        if self._records is None:
            return {"id": request_id, "code": "unsupported",
                    "error": "Update OpenWhisper on the host to keep records there."}
        if op in self._RECORD_WRITES and not self._keeps_records():
            return {"id": request_id, "code": "forbidden", "error": (
                f"{self.host_name} isn't keeping records for other computers. Turn on "
                "\"Keep records for paired computers\" in Settings → Remote engine there."
            )}
        if not device.get("id"):
            return {"id": request_id, "code": "bad_request", "error": "Unknown device."}
        try:
            result = self._records(op, header, payload, device)
        except LookupError as exc:
            return {"id": request_id, "code": "not_found", "error": str(exc) or "No such record."}
        except ValueError as exc:
            return {"id": request_id, "code": "bad_request", "error": str(exc) or "Invalid record request."}
        except Exception as exc:
            logger.warning("Record request %s from %s failed", op, device.get("name"), exc_info=True)
            return {"id": request_id, "error": str(exc) or type(exc).__name__}
        if op in ("records_commit", "records_delete", "records_clear"):
            self._emit("records", {"device": device.get("name"), "op": op})
        return {"id": request_id, "result": result}

    def _configure_runtime_request(self, request_id, header: dict, device_name: str) -> dict:
        if self._configure_runtime is None:
            return {"id": request_id, "error": "Update OpenWhisper on the host to change its runtime."}
        if (set(header) - {"id", "op", "family", "model", "settings"}
                or not all(isinstance(header.get(key), str) and header[key] for key in ("family", "model"))
                or not isinstance(header.get("settings"), dict)):
            return {"id": request_id, "code": "bad_request", "error": "Invalid runtime request."}
        try:
            engine = self._configure_runtime(header["family"], header["model"], header["settings"], device_name)
        except Exception as exc:
            return {"id": request_id, "error": str(exc) or type(exc).__name__}
        self._emit("engine", {"by": device_name})
        return {"id": request_id, "result": {"engine": engine, "runtime": self._runtime()}}

    def _switch_model(self, request_id, header: dict, device_name: str) -> dict:
        """Load the model a client chose, as if it were picked on this computer.

        Blocks this connection until the load settles, which is what lets the
        client show one "Switching…" state and then the engine it switched to.
        """
        family, model = header.get("family"), header.get("model")
        if "device" in header:
            if not self._can_manage_models():
                return {"id": request_id, "code": "forbidden", "error": "Model management is disabled on the host."}
            if set(header) - {"id", "op", "family", "model", "device"} or header["device"] not in ("cpu", "cuda"):
                return {"id": request_id, "error": "Invalid model device request."}
        if not isinstance(family, str) or not isinstance(model, str) or not model:
            return {"id": request_id, "error": "Choose a model to switch to"}
        if self._select_model is None:
            return {"id": request_id,
                    "error": f"{self.host_name} doesn't let paired computers change its model."}
        try:
            engine = (self._select_model(family, model, device_name, header["device"]) if "device" in header
                      else self._select_model(family, model, device_name))
        except Exception as exc:
            return {"id": request_id, "error": str(exc) or type(exc).__name__}
        self.activity.switched(device_name, str((engine or {}).get("label") or model))
        self._emit("engine", {"family": family, "model": model, "by": device_name})
        return {"id": request_id, "result": {"engine": engine, "models": self._model_list()}}

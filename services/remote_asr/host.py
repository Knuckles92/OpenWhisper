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
        select_model: Optional[Callable[[str, str, str], dict]] = None,
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
        self._lock = threading.Lock()
        self._server = None
        self._thread: Optional[threading.Thread] = None
        self._pairing: Optional[_Pairing] = None
        self._clients: Dict[str, _Client] = {}
        self.port: Optional[int] = None

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
                max_size=protocol.MAX_REQUEST_BYTES,
                compression=None,
                open_timeout=HANDSHAKE_TIMEOUT_S,
                ping_interval=20,
                ping_timeout=20,
                logger=logging.getLogger("websockets.remote_engine"),
            )
            self._server = server
            self.port = server.socket.getsockname()[1]
            self._thread = threading.Thread(
                target=server.serve_forever, name="RemoteEngineHost", daemon=True
            )
            self._thread.start()
        logger.info("Remote engine host listening on %s:%s", bind, self.port)
        self._emit("state", {})
        return self.port

    def stop(self) -> None:
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
                 "busy": c.busy}
                for c in self._clients.values()
            ]

    def _set_busy(self, connection_id: str, busy: bool) -> None:
        with self._lock:
            client = self._clients.get(connection_id)
            if client is None or client.busy == busy:
                return
            client.busy = busy
        self._emit("activity", {"name": client.name, "busy": busy})

    def remove_device(self, device_id: str) -> bool:
        """Forget a device and drop its open connections."""
        removed = self.registry.remove(device_id)
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
        engine = self._engine()
        identity = engine.identity
        self._send(ws, {
            "type": "ready",
            "host": self._host_info(),
            "device": {"id": device["id"], "name": device["name"]},
            "engine": engine.describe(),
            "models": self._model_list(),
        })
        connection_id = uuid.uuid4().hex[:8]
        with self._lock:
            if self._server is None:
                # Sharing was turned off while this client was handshaking.
                ws.close(1001, "host stopped sharing")
                return
            self._clients[connection_id] = _Client(
                device["id"], device["name"], address, time.monotonic(), ws
            )
        self._emit("clients", {})
        streams: set = set()
        try:
            for frame in ws:
                reply = self._dispatch(frame, engine, identity, connection_id, streams, device["name"])
                self._send(ws, reply)
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
            self._emit("clients", {})

    def _dispatch(self, frame, engine: HostEngine, identity: tuple,
                  connection_id: str, streams: set, device_name: str = "") -> dict:
        try:
            header, audio = protocol.unpack_request(frame)
        except protocol.ProtocolError as exc:
            return {"id": None, "error": str(exc), "code": "bad_request"}
        request_id = header.get("id")
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
        except Exception as exc:
            return {"id": request_id, "error": str(exc) or type(exc).__name__}
        finally:
            if decodes:
                self._set_busy(connection_id, False)
        # Time spent here, including any wait behind this computer's own
        # dictation, so the client can tell it apart from the network's.
        host_ms = round((time.perf_counter() - started) * 1000, 1)
        return {"id": request_id, "result": result, "host_ms": host_ms}

    def _switch_model(self, request_id, header: dict, device_name: str) -> dict:
        """Load the model a client chose, as if it were picked on this computer.

        Blocks this connection until the load settles, which is what lets the
        client show one "Switching…" state and then the engine it switched to.
        """
        family, model = header.get("family"), header.get("model")
        if not isinstance(family, str) or not isinstance(model, str) or not model:
            return {"id": request_id, "error": "Choose a model to switch to"}
        if self._select_model is None:
            return {"id": request_id,
                    "error": f"{self.host_name} doesn't let paired computers change its model."}
        try:
            engine = self._select_model(family, model, device_name)
        except Exception as exc:
            return {"id": request_id, "error": str(exc) or type(exc).__name__}
        self._emit("engine", {"family": family, "model": model, "by": device_name})
        return {"id": request_id, "result": {"engine": engine, "models": self._model_list()}}

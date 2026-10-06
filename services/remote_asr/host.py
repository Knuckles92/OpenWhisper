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
import re
import secrets
import socket
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from services.remote_asr import discovery, protocol, tailscale
from services.remote_asr.activity import HostActivity
from services.remote_asr.engines import HostEngine, UnavailableEngine
from services.remote_asr.tls import HostIdentity, server_context
from services.remote_records.gate import RecordsBusy

logger = logging.getLogger(__name__)

#: How long a new connection has to send ``hello`` or ``pair``.
HANDSHAKE_TIMEOUT_S = 10.0
#: A pairing code stays valid this long, for one successful pairing.
PAIRING_TTL_S = 300.0
#: Wrong codes allowed before the host closes pairing.
MAX_PAIRING_FAILURES = 5
PAIRING_CODE_DIGITS = 6
#: A connection owns a server thread and may hold a history or speech stream.
MAX_CLIENT_CONNECTIONS = 12
#: Engine/model/record operations share a small fair work pool. Waiting work
#: is bounded too, so one overloaded host cannot accumulate unbounded threads.
MAX_ACTIVE_JOBS = 2
MAX_QUEUED_JOBS = 4
MAX_JOB_WAIT_S = 30.0
# Keep inbound frames buffered per connection small while a job waits; each
# individual frame is bounded by the protocol's WebSocket max_size below.
MAX_QUEUED_FRAMES_PER_CLIENT = 2
#: last_seen is persisted at most this often per device.
_LAST_SEEN_PERSIST_S = 60.0
#: How long a pairing request waits for this computer's owner to answer.
APPROVAL_TIMEOUT_S = 120.0
#: Pairing requests one address may make within APPROVAL_WINDOW_S.
MAX_APPROVAL_REQUESTS = 4
APPROVAL_WINDOW_S = 600.0

HostEvent = Callable[[str, dict], None]


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean_device_name(name) -> str:
    return protocol.clean_name(name) or "Unnamed computer"


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
        entries = [
            dict(entry) for entry in raw
            if isinstance(entry, dict)
            and isinstance(entry.get("id"), str)
            and isinstance(entry.get("token_sha256"), str)
        ]
        for entry in entries:
            owners = entry.get("record_owner_ids")
            if "record_owner_ids" in entry:
                entry["record_owner_ids"] = list(dict.fromkeys(
                    owner for owner in (owners if isinstance(owners, list) else [])
                    if isinstance(owner, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", owner)
                ))
        return entries

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

    def rename(self, device_id: str, name: str) -> Optional[dict]:
        """Call a device ``name`` here; an empty name goes back to the one it paired with.

        ``paired_name`` keeps that name while the device goes by another.
        The entry as ``list`` shows it, or None when the device isn't paired.
        """
        with self._lock:
            entries = self._entries()
            entry = next((entry for entry in entries if entry["id"] == device_id), None)
            if entry is None:
                return None
            paired_name = clean_device_name(entry.pop("paired_name", None) or entry.get("name"))
            entry["name"] = protocol.clean_name(name) or paired_name
            if entry["name"] != paired_name:
                entry["paired_name"] = paired_name
            self._save(entries)
        return {k: v for k, v in entry.items() if k != "token_sha256"}

    def remove(self, device_id: str) -> bool:
        with self._lock:
            entries = self._entries()
            kept = [entry for entry in entries if entry["id"] != device_id]
            if len(kept) == len(entries):
                return False
            self._save(kept)
            return True

    def attach_record_owner(self, device_id: str, owner_id: str) -> None:
        """Host-owner action; never exposed as a client pairing or RPC option."""
        if not isinstance(owner_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", owner_id):
            raise ValueError("Invalid stored record owner.")
        with self._lock:
            entries = self._entries()
            target = next((entry for entry in entries if entry["id"] == device_id), None)
            if target is None:
                raise ValueError("Pair the receiving computer again before recovering its records.")
            for entry in entries:
                if owner_id == entry["id"] or owner_id in entry.get("record_owner_ids", []):
                    raise ValueError("These records already belong to a paired computer.")
            target["record_owner_ids"] = [*target.get("record_owner_ids", []), owner_id]
            self._save(entries)

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
class PairRequest:
    """A computer on the network asking to pair, waiting for this one's owner.

    ``sas`` stays empty until both nonces are in; only then is the request
    shown, since the number is what the owner compares.
    """

    id: str
    name: str
    address: str
    expires_at: float
    sas: str = ""
    decision: Optional[bool] = None
    #: Sharing stopped while it waited.
    stopped: bool = False
    done: threading.Event = field(default_factory=threading.Event)


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


class _WorkAdmission:
    """FIFO admission for costly requests across all paired connections."""

    def __init__(self, active: int, queued: int, wait_s: float) -> None:
        self._active_limit = active
        self._queue_limit = queued
        self._wait_s = wait_s
        self._condition = threading.Condition()
        self._active = 0
        self._queue = deque()

    def acquire(self, still_connected: Callable[[], bool]) -> bool:
        with self._condition:
            if self._active < self._active_limit and not self._queue:
                self._active += 1
                return True
            if len(self._queue) >= self._queue_limit:
                return False
            ticket = object()
            self._queue.append(ticket)
            deadline = time.monotonic() + self._wait_s
            try:
                while True:
                    if not still_connected() or time.monotonic() >= deadline:
                        return False
                    if self._queue[0] is ticket and self._active < self._active_limit:
                        self._queue.popleft()
                        self._active += 1
                        return True
                    self._condition.wait(timeout=0.1)
            finally:
                if ticket in self._queue:
                    self._queue.remove(ticket)
                    self._condition.notify_all()

    def release(self) -> None:
        with self._condition:
            self._active -= 1
            self._condition.notify_all()


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
        mcp_enabled: Optional[Callable[[], bool]] = None,
        mcp: Optional[Callable[[str, dict, str], dict]] = None,
        history=None,
        max_connections: int = MAX_CLIENT_CONNECTIONS,
        max_active_jobs: int = MAX_ACTIVE_JOBS,
        max_queued_jobs: int = MAX_QUEUED_JOBS,
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
        self._mcp_enabled = mcp_enabled or (lambda: False)
        self._mcp = mcp
        from services.remote_history.channel import HistoryBroker

        self.history = history if history is not None else HistoryBroker(registry)
        self._lock = threading.Lock()
        self._server = None
        self._thread: Optional[threading.Thread] = None
        self._pairing: Optional[_Pairing] = None
        self._request: Optional[PairRequest] = None
        self._request_log: Dict[str, deque] = {}
        #: Answers discovery queries while sharing (see discovery.py), when
        #: start() was given a discovery port.
        self.discovery = discovery.DiscoveryResponder(self._discovery_info)
        self._clients: Dict[str, _Client] = {}
        self._connection_slots = threading.BoundedSemaphore(max(1, int(max_connections)))
        self._work_admission = _WorkAdmission(
            max(1, int(max_active_jobs)), max(0, int(max_queued_jobs)), MAX_JOB_WAIT_S,
        )
        self.port: Optional[int] = None
        #: What paired computers asked of this host since sharing started.
        self.activity = HostActivity()

    # ---- lifecycle ----

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self, port: int = protocol.DEFAULT_PORT, bind: str = "0.0.0.0",
              discovery_port: Optional[int] = None) -> int:
        """Listen on ``bind:port`` (0 picks a free port). Raises OSError if taken.

        With ``discovery_port``, also answer discovery queries on that UDP
        port; a taken one leaves ``discovery.error`` set and sharing running.
        """
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
                max_queue=MAX_QUEUED_FRAMES_PER_CLIENT,
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
        if discovery_port is not None:
            self.discovery.start(discovery_port, bind)
        self._emit("state", {})
        return self.port

    def stop(self) -> None:
        self.history.remove()
        self.discovery.stop()
        with self._lock:
            server, self._server = self._server, None
            thread, self._thread = self._thread, None
            self._pairing = None
            request = self._request
        if request is not None:
            request.stopped = True
            request.done.set()
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

    def pending_request(self) -> Optional[dict]:
        """The pairing request waiting for an answer here, if one is."""
        with self._lock:
            request = self._request
        if request is None or not request.sas or request.done.is_set():
            return None
        return {
            "id": request.id,
            "name": request.name,
            "address": request.address,
            "sas": request.sas,
            "seconds_left": max(0.0, request.expires_at - time.monotonic()),
        }

    def answer_request(self, request_id: str, allow: bool) -> bool:
        """Allow or deny the waiting request. False when it's gone already."""
        with self._lock:
            request = self._request
            if (request is None or request.id != request_id or not request.sas
                    or request.done.is_set()):
                return False
            request.decision = bool(allow)
        request.done.set()
        return True

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

    def rename_device(self, device_id: str, name: str) -> Optional[dict]:
        """Call a device ``name`` here, its open connections included (see DeviceRegistry.rename)."""
        device = self.registry.rename(device_id, name)
        if device is None:
            return None
        with self._lock:
            for client in self._clients.values():
                if client.device_id == device_id:
                    client.name = device["name"]
        self.activity.renamed(device_id, device["name"])
        self._emit("devices", {})
        self._emit("clients", {})
        return device

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
        if not self._connection_slots.acquire(blocking=False):
            try:
                self._send(ws, {"type": "error", "code": "busy",
                                "message": "The host is busy. Try again shortly.",
                                "retry_after_ms": 1000})
                ws.close(protocol.CLOSE_BUSY, "host busy")
            except Exception:
                logger.debug("Could not send connection limit response", exc_info=True)
            return
        try:
            self._handle_admitted(ws)
        finally:
            self._connection_slots.release()

    def _handle_admitted(self, ws) -> None:
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
        elif kind == "pair_request":
            self._pair_request(ws, message, address)
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

    def _controls_mcp(self) -> bool:
        try:
            return self._mcp is not None and self._mcp_enabled() is True
        except Exception:
            logger.warning("Could not read the MCP control permission", exc_info=True)
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
            "approval": True,
        })
        ws.close()

    def _discovery_info(self) -> dict:
        """What a discovery answer says: the probe's subset, plus where to connect."""
        engine = self._engine().describe()
        return {
            "name": self.host_name,
            "port": self.port,
            "fingerprint": self.identity.fingerprint,
            "engine": {key: engine.get(key) for key in ("label", "device", "available", "family")},
            "approval": True,
        }

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
                         "\"Show a pairing code\" there and enter the code it shows.")
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

    def _admit_request(self, name: str, address: str):
        """Reserve the one request slot: a PairRequest, or (code, message) refusing."""
        if not discovery.allowed_sender(address):
            return ("approval_refused", "This host only takes pairing requests from its own "
                    "network. Enter the pairing code shown there instead.")
        now = time.monotonic()
        with self._lock:
            log = self._request_log.setdefault(address, deque())
            while log and now - log[0] > APPROVAL_WINDOW_S:
                log.popleft()
            if len(log) >= MAX_APPROVAL_REQUESTS:
                return ("approval_limited", "Too many pairing requests from this computer. Wait "
                        "a few minutes, or enter the pairing code shown on the host.")
            current = self._request
            if current is not None and not current.done.is_set() and current.expires_at > now:
                return ("approval_busy", f"{self.host_name} is already asking about another "
                        "computer. Try again in a minute.")
            log.append(now)
            self._request = PairRequest(
                id=uuid.uuid4().hex[:12], name=name, address=address,
                expires_at=now + HANDSHAKE_TIMEOUT_S + APPROVAL_TIMEOUT_S,
            )
            return self._request

    def _pair_request(self, ws, message: dict, address: str) -> None:
        """Pair a computer found on the network once this computer's owner allows it.

        The nonce exchange (see protocol.pairing_sas) gives both screens the
        same six digits only when nobody is in the middle; the owner allows
        the request if they match.
        """
        from websockets.exceptions import ConnectionClosed

        name = clean_device_name(message.get("device_name"))
        admitted = self._admit_request(name, address)
        if not isinstance(admitted, PairRequest):
            logger.info("Refused a pairing request from %s: %s", address, admitted[0])
            self._send(ws, {"type": "error", "code": admitted[0], "message": admitted[1]})
            ws.close(protocol.CLOSE_UNAUTHORIZED, admitted[0])
            return
        request = admitted
        shown = False
        try:
            host_nonce = secrets.token_bytes(protocol.PAIRING_NONCE_BYTES)
            self._send(ws, {
                "type": "pair_commit",
                "commit": protocol.pairing_commitment(host_nonce),
                "timeout_s": int(APPROVAL_TIMEOUT_S),
            })
            try:
                raw = ws.recv(timeout=HANDSHAKE_TIMEOUT_S)
                reply = json.loads(raw) if isinstance(raw, str) else None
                client_nonce = (bytes.fromhex(str(reply.get("nonce") or ""))
                                if isinstance(reply, dict) and reply.get("type") == "pair_nonce"
                                else b"")
            except (TimeoutError, ValueError):
                client_nonce = b""
            except ConnectionClosed:
                return
            if len(client_nonce) != protocol.PAIRING_NONCE_BYTES:
                ws.close(protocol.CLOSE_BAD_REQUEST, "expected pair_nonce")
                return
            sas = protocol.pairing_sas(host_nonce, client_nonce, self.identity.fingerprint)
            self._send(ws, {"type": "pair_reveal", "nonce": host_nonce.hex()})
            with self._lock:
                request.expires_at = time.monotonic() + APPROVAL_TIMEOUT_S
                request.sas = sas
            shown = True
            logger.info("Pairing request from %r at %s is waiting for an answer here",
                        name, address)
            self._emit("pair_request", {"name": name, "address": address})
            outcome = self._await_answer(ws, request)
            logger.info("Pairing request from %r at %s: %s", name, address, outcome)
            if outcome == "allowed":
                self._finish_pairing(ws, name, address, "approval")
            elif outcome != "canceled":
                code, text = {
                    "denied": ("pair_denied", f"{self.host_name} declined the request."),
                    "stopped": ("pair_closed", f"{self.host_name} stopped sharing its engine."),
                    "timeout": ("pair_timeout", f"Nobody answered on {self.host_name}. Try again "
                                "while someone is at it, or pair with a code."),
                }[outcome]
                self._send(ws, {"type": "error", "code": code, "message": text})
                ws.close(protocol.CLOSE_UNAUTHORIZED, code)
        except ConnectionClosed:
            logger.info("Pairing request from %r at %s ended with the connection", name, address)
        finally:
            with self._lock:
                if self._request is request:
                    self._request = None
            request.done.set()
            if shown:
                self._emit("pair_request", {})

    def _await_answer(self, ws, request: PairRequest) -> str:
        """``allowed``, ``denied``, ``stopped``, ``timeout`` or ``canceled`` (the client left)."""
        from websockets.protocol import State

        while not request.done.wait(0.2):
            if ws.state is not State.OPEN:
                return "canceled"
            if time.monotonic() >= request.expires_at:
                return "timeout"
        if request.stopped:
            return "stopped"
        return "allowed" if request.decision else "denied"

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
                             "engine_controls": self._configure_runtime is not None,
                             "recognition_hints": getattr(engine, "accepts_phrases", False) is True},
        }
        if self._mcp is not None:
            ready["capabilities"]["mcp_control"] = self._controls_mcp()
        if self._records is not None:
            ready["capabilities"]["records"] = self._keeps_records()
            # Counted even while storage is off, so a client can still find
            # (and bring back) what it stored before the owner turned it off.
            ready["records"] = self._stored_summary(device["id"])
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
            # Register before ready so an immediate engine change can find
            # this client, and recheck changes made while preparing the reply.
            if self._engine().identity != identity:
                ws.close(protocol.CLOSE_ENGINE_CHANGED, "engine changed")
                return
            self._send(ws, ready)
            for frame in ws:
                self._set_in_request(connection_id, True)
                try:
                    # Recheck revocation before accepting more work, including
                    # requests already buffered when the device was removed.
                    current = self.registry.authenticate(message.get("token"))
                    if current is None:
                        ws.close(protocol.CLOSE_UNAUTHORIZED, "device removed")
                        break
                    reply = self._dispatch(frame, engine, identity, connection_id, streams,
                                           current["name"], device["id"], ws)
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
                client = self._clients.pop(connection_id, None)
            self.activity.disconnected(device["id"], client.name if client else device["name"])
            self._emit("clients", {})

    def _dispatch(self, frame, engine: HostEngine, identity: tuple,
                  connection_id: str, streams: set, device_name: str = "",
                   device_id: str = "", ws=None) -> dict:
        try:
            header, payload = protocol.unpack_frame(frame)
        except protocol.ProtocolError as exc:
            return {"id": None, "error": str(exc), "code": "bad_request"}
        request_id = header.get("id")
        op = header.get("op")
        costly = (op in ("transcribe", "stream", "select_model", "configure_runtime",
                         "model_catalog", "download_model", "install_runtime")
                  or isinstance(op, str) and op.startswith("records_"))
        if costly:
            from websockets.protocol import State

            def still_connected() -> bool:
                return (self.running and ws is not None and ws.state is State.OPEN)

            if not self._work_admission.acquire(still_connected):
                return {"id": request_id, "code": "busy",
                        "error": "The host is busy. Try again shortly.",
                        "retry_after_ms": 1000}
            try:
                return self._dispatch_unpacked(
                    header, payload, engine, identity, connection_id, streams,
                    device_name, device_id,
                )
            finally:
                self._work_admission.release()
        return self._dispatch_unpacked(
            header, payload, engine, identity, connection_id, streams,
            device_name, device_id,
        )

    def _dispatch_unpacked(self, header: dict, payload: bytes, engine: HostEngine,
                           identity: tuple, connection_id: str, streams: set,
                           device_name: str, device_id: str) -> dict:
        request_id = header.get("id")
        op = header.get("op")
        if isinstance(op, str) and op.startswith("records_"):
            return self._records_request(request_id, header, payload,
                                         {"id": device_id, "name": device_name})
        if op in self._MCP_OPS:
            return self._mcp_request(request_id, header, device_name)
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
                phrases = (
                    protocol.header_phrases(header)
                    if getattr(current, "accepts_phrases", False) is True else ()
                )
                # Engines without hints keep their two-argument call.
                if phrases:
                    result = current.transcribe(audio, language, phrases=phrases)
                else:
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
        devices = ("auto", "cpu") if header.get("family") == "parakeet_mlx" else ("cpu", "cuda")
        if (set(header) - fields or (op != "model_catalog" and not all(
            isinstance(header.get(key), str) and header[key] for key in ("family", "model")
        )) or (op == "install_runtime" and header.get("device") not in devices)):
            return {"id": request_id, "code": "bad_request", "error": "Invalid model management request."}
        try:
            result = self._manage_models(op, header, device_name)
        except Exception as exc:
            return {"id": request_id, "error": str(exc) or type(exc).__name__}
        return {"id": request_id, "result": result}

    _MCP_OPS = frozenset({"mcp_state", "mcp_configure"})

    def _mcp_request(self, request_id, header: dict, device_name: str) -> dict:
        """This computer's MCP server, for a paired computer its owner allowed.

        The permission is read on EVERY request, so turning it off also stops
        computers that are already connected. The state includes the access
        token, which is why this is an opt-in and not part of pairing.
        """
        if self._mcp is None:
            return {"id": request_id, "code": "unsupported",
                    "error": "Update OpenWhisper on the host to manage its MCP server."}
        if not self._controls_mcp():
            return {"id": request_id, "code": "forbidden", "error": (
                f"{self.host_name} isn't letting paired computers manage its MCP server. "
                "Turn on \"Allow paired computers to manage MCP\" in Settings → Remote engine there."
            )}
        op = header["op"]
        allowed = {"id", "op"} | ({"settings"} if op == "mcp_configure" else set())
        if set(header) - allowed or (op == "mcp_configure"
                                     and not isinstance(header.get("settings"), dict)):
            return {"id": request_id, "code": "bad_request", "error": "Invalid MCP request."}
        try:
            result = self._mcp(op, header, device_name)
        except ValueError as exc:
            # ControlError ("code: public text") carries nothing but a safe message.
            code, _, text = str(exc).partition(": ")
            return {"id": request_id, "code": code if text else "bad_request",
                    "error": text or str(exc)}
        except Exception as exc:
            logger.warning("MCP request %s from %s failed", op, device_name, exc_info=True)
            return {"id": request_id, "error": str(exc) or type(exc).__name__}
        if op == "mcp_configure":
            logger.info("%s changed this computer's MCP settings", device_name)
            self._emit("mcp", {"by": device_name})
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
        except RecordsBusy as exc:
            return {"id": request_id, "code": "busy", "retry_after_ms": 1000,
                    "error": str(exc)}
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
            devices = ("auto", "cpu") if family == "parakeet_mlx" else ("cpu", "cuda")
            if set(header) - {"id", "op", "family", "model", "device"} or header["device"] not in devices:
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

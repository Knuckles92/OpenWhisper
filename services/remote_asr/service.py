"""One object for both remote engine roles, owned by the app controller.

Host: starts and stops ``SpeechHost`` from settings and serves whichever
engine is selected on this computer. Client: pairs with a host and forgets
it. The Settings page calls in here and listens for changes; nothing here
imports Qt, so listeners are called on whatever thread the change happened
(the page re-posts them to the UI thread).
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from services.remote_asr import protocol
from services.remote_asr import settings as remote_settings
from services.remote_asr import tailscale
from services.remote_asr.engines import HostEngine, host_engine_for, host_models
from services.remote_records.gate import RecordGate

logger = logging.getLogger(__name__)

Listener = Callable[[str], None]

#: A cached ``tailscale status`` older than this is refreshed in the background.
TAILSCALE_STATUS_MAX_AGE_S = 30.0
#: How long a client's model switch may take to load here before the host
#: stops waiting. A cold large Whisper on CPU is the slow case.
SWITCH_TIMEOUT_S = 240.0
_SWITCH_POLL_S = 0.1
#: Peers probed at once while looking for hosts on the tailnet.
_PROBE_WORKERS = 12


@dataclass(frozen=True)
class TailnetHost:
    """A computer on the tailnet that answered the remote engine probe."""

    peer: tailscale.TailscalePeer
    port: int
    host_name: str
    engine: dict
    tailscale_pairing: bool
    compatible: bool

    @property
    def address(self) -> str:
        return protocol.format_address(self.peer.address, self.port)

    @property
    def can_pair_without_code(self) -> bool:
        return self.compatible and self.tailscale_pairing and self.peer.mine


@dataclass(frozen=True)
class TailnetScan:
    status: tailscale.TailscaleStatus
    hosts: Tuple[TailnetHost, ...] = ()


def _default_identity_dir() -> str:
    from config import user_data_path
    from services.remote_asr.tls import CERT_FILENAME

    return os.path.dirname(os.path.abspath(user_data_path(CERT_FILENAME)))


class RemoteEngineService:
    """``switch_engine(family, model, device_name[, device])`` selects one of this
    computer's models on its UI thread for a paired client, raising with the
    reason when it can't right now; ``engine_settled()`` says when the load
    that started has finished. Without them, clients can't change the model.
    """

    def __init__(
        self,
        backend_provider: Callable[[], object],
        *,
        identity_dir: Optional[str] = None,
        on_client_changed: Optional[Callable[[], None]] = None,
        bind: str = "0.0.0.0",
        switch_engine: Optional[Callable[..., None]] = None,
        engine_settled: Optional[Callable[[], bool]] = None,
        configure_engine: Optional[Callable[[str, str, dict, str], None]] = None,
        on_host_catalog: Optional[Callable[[object, dict, dict], None]] = None,
        records_root: Optional[str] = None,
    ):
        self._backend_provider = backend_provider
        self._records_root = records_root
        self._records = None
        self._mcp_control = None
        self._record_gate = RecordGate()
        from services.remote_asr.host import DeviceRegistry
        from services.remote_history.channel import HistoryBroker

        # History and speech connections mutate the same saved collection.
        # Keep their read-modify-write operations under one registry lock.
        self._registry = DeviceRegistry(
            remote_settings.load_host_devices, remote_settings.save_host_devices
        )
        self.history = HistoryBroker(self._registry)
        self._history_client = None
        self._switch_engine = switch_engine
        self._configure_engine = configure_engine
        self._runtime_lock = threading.Lock()
        self._engine_settled = engine_settled or (lambda: True)
        self._identity_dir = identity_dir
        # Every interface, so other computers can connect. Tests pass
        # 127.0.0.1, which also keeps Windows from asking about the firewall.
        self._bind = bind
        self.on_client_changed = on_client_changed
        self.on_host_catalog = on_host_catalog
        self._lock = threading.RLock()
        self._host = None
        self._host_error = ""
        self._listeners: List[Listener] = []
        self._engine_cache: tuple = (None, None)
        self._tailscale: Optional[tailscale.TailscaleStatus] = None
        self._tailscale_at = 0.0
        self._tailscale_refreshing = False
        from services.remote_asr.model_management import HostModelManager

        self._model_manager = HostModelManager(lambda: self._notify("models"))
        from services.remote_asr.dependencies import HostRuntimeInstaller

        self._runtime_installer = HostRuntimeInstaller(lambda: self._notify("components"))

    # ---- listeners ----

    def add_listener(self, listener: Listener) -> None:
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def remove_listener(self, listener: Listener) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def _notify(self, kind: str) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(kind)
            except Exception:
                logger.debug("Remote engine listener raised", exc_info=True)

    # ---- tailscale ----

    def tailscale_status(self, *, refresh: bool = False) -> Optional[tailscale.TailscaleStatus]:
        """The last known Tailscale status; None until the first check finishes.

        Never blocks: a stale or missing status is refreshed on a background
        thread and listeners hear "tailscale" when it lands. ``refresh``
        forces that check.
        """
        with self._lock:
            status = self._tailscale
            stale = time.monotonic() - self._tailscale_at > TAILSCALE_STATUS_MAX_AGE_S
            start = (refresh or stale or status is None) and not self._tailscale_refreshing
            if start:
                self._tailscale_refreshing = True
        if start:
            threading.Thread(
                target=self._refresh_tailscale, name="remote-engine-tailscale", daemon=True
            ).start()
        return status

    def _refresh_tailscale(self) -> tailscale.TailscaleStatus:
        try:
            status = tailscale.status()
        finally:
            with self._lock:
                self._tailscale_refreshing = False
        with self._lock:
            changed = status != self._tailscale
            self._tailscale = status
            self._tailscale_at = time.monotonic()
        if changed:
            self._notify("tailscale")
        return status

    def _current_tailscale(self, max_age: float = 120.0) -> tailscale.TailscaleStatus:
        """A status no older than ``max_age``, checking now if needed (blocks)."""
        with self._lock:
            status = self._tailscale
            fresh = status is not None and time.monotonic() - self._tailscale_at <= max_age
        return status if fresh else self._refresh_tailscale()

    def _trusted_tailscale_owner(self) -> str:
        """For the host: whose Tailscale computers may pair without a code."""
        if not remote_settings.host_tailscale_trust():
            return ""
        status = self._current_tailscale()
        return status.owner if status.running else ""

    def _host_addresses(self) -> List[str]:
        """Addresses a paired client can fall back on: LAN, then Tailscale."""
        addresses = []
        try:
            from services.lan_address import LAN, best_lan_address

            # Not a VPN's address: the computers at home can't reach it.
            lan = best_lan_address()
            if lan is not None and lan.kind == LAN:
                addresses.append(lan.address)
        except Exception:
            logger.debug("LAN address lookup failed", exc_info=True)
        status = self._current_tailscale()
        if status.running and status.address:
            addresses.append(status.address)
        return addresses

    def set_tailscale_trust(self, enabled: bool) -> None:
        from services.settings import SettingsKey, settings_manager

        settings_manager.save_setting(SettingsKey.REMOTE_HOST_TAILSCALE_TRUST, bool(enabled))
        self._notify("state")

    def scan_tailnet(self, port: Optional[int] = None) -> TailnetScan:
        """Find computers on the tailnet sharing their engine. Blocking.

        Only the default port is probed; a host on another port is paired
        by typing its address.
        """
        from services.remote_asr import client

        port = port or protocol.DEFAULT_PORT
        status = self._refresh_tailscale()
        if not status.running:
            return TailnetScan(status)
        peers = [peer for peer in status.peers if peer.online and peer.is_desktop]

        def probe(peer):
            try:
                result = client.probe_host(peer.address, port)
            except client.RemoteEngineError:
                return None
            except Exception:
                logger.debug("Probe of %s failed", peer.address, exc_info=True)
                return None
            return TailnetHost(
                peer=peer,
                port=port,
                host_name=result.host_name,
                engine=result.engine,
                tailscale_pairing=result.tailscale_pairing,
                compatible=result.compatible,
            )

        if not peers:
            return TailnetScan(status)
        with ThreadPoolExecutor(max_workers=min(_PROBE_WORKERS, len(peers))) as pool:
            found = [host for host in pool.map(probe, peers) if host is not None]
        return TailnetScan(status, tuple(found))

    # ---- host ----

    def _engine(self) -> HostEngine:
        backend = self._backend_provider()
        with self._lock:
            cached_backend, cached = self._engine_cache
            if cached is None or cached_backend is not backend:
                cached = host_engine_for(backend)
                self._engine_cache = (backend, cached)
            return cached

    def _ensure_host(self):
        from services.remote_asr.host import SpeechHost
        from services.remote_asr.tls import ensure_host_identity

        if self._host is None:
            identity = ensure_host_identity(self._identity_dir or _default_identity_dir())
            self._host = SpeechHost(
                engine_provider=self._engine,
                registry=self._registry,
                identity=identity,
                on_event=lambda kind, _detail: self._notify(kind),
                tailscale_owner=self._trusted_tailscale_owner,
                addresses=self._host_addresses,
                models=self.host_models,
                select_model=self.select_model if self._switch_engine is not None else None,
                model_management=remote_settings.host_model_management,
                manage_models=self.manage_host_models,
                runtime=self.runtime_state,
                configure_runtime=self.configure_runtime if self._configure_engine is not None else None,
                records_enabled=remote_settings.host_keeps_records,
                records=self.records_request,
                records_summary=self.records_summary,
                mcp_enabled=remote_settings.host_manages_mcp,
                mcp=self.mcp_request,
                history=self.history,
            )
        return self._host

    # ---- records paired computers keep here ----

    def record_store(self):
        """Where paired computers' records are kept (services/remote_records)."""
        with self._lock:
            if self._records is None:
                from config import user_data_path
                from services.remote_records.host_store import HostRecordStore
                from services.remote_records.kinds import DictationRecords, MeetingRecords

                self._records = HostRecordStore(
                    self._records_root or user_data_path("remote_records"),
                    {"dictation": DictationRecords(), "meeting": MeetingRecords()},
                )
            return self._records

    def records_request(self, op: str, header: dict, payload: bytes, device: dict) -> dict:
        with self._record_gate.request():
            grant = dict(device)
            grant["record_owner_ids"] = self._record_owners(device["id"])[1:]
            return self.record_store().handle(op, header, payload, grant)

    def paused_records_for_backup(self):
        return self._record_gate.paused()

    def _record_owners(self, device_id: str) -> list:
        device = next((entry for entry in self._registry.list() if entry["id"] == device_id), {})
        return [device_id, *device.get("record_owner_ids", [])]

    def records_summary(self, device_id: str) -> dict:
        """What a paired computer keeps here, per kind: ``{"count", "bytes"}``."""
        combined = {}
        store = self.record_store()
        for owner in self._record_owners(device_id):
            for kind, summary in store.summary(owner).items():
                total = combined.setdefault(kind, {"count": 0, "bytes": 0})
                for key in total:
                    total[key] += int(summary.get(key) or 0)
        return combined

    def recoverable_record_owners(self) -> list:
        paired = set()
        for device in self._registry.list():
            paired.update([device["id"], *device.get("record_owner_ids", [])])
        groups = {}
        for kind, handler in self.record_store().kinds.items():
            for owner in handler.owners():
                if owner["id"] in paired:
                    continue
                group = groups.setdefault(owner["id"], {
                    "id": owner["id"], "name": owner["name"], "counts": {},
                })
                group["counts"][kind] = owner["count"]
        return sorted(groups.values(), key=lambda group: (group["name"], group["id"]))

    def recover_device_records(self, owner_id: str, device_id: str) -> None:
        """Assign orphaned records only through the host owner's local UI."""
        with self._record_gate.paused():
            if not any(group["id"] == owner_id for group in self.recoverable_record_owners()):
                raise ValueError("These records are no longer available for recovery.")
            self._registry.attach_record_owner(device_id, owner_id)
        self._notify("records")

    def set_keep_records(self, enabled: bool) -> None:
        from services.settings import SettingsKey, settings_manager

        settings_manager.save_setting(SettingsKey.REMOTE_HOST_KEEP_RECORDS, bool(enabled))
        self._notify("state")

    def set_model_management(self, enabled: bool) -> None:
        from services.settings import SettingsKey, settings_manager

        settings_manager.save_setting(SettingsKey.REMOTE_HOST_MODEL_MANAGEMENT, bool(enabled))
        self._notify("state")

    def set_manage_mcp(self, enabled: bool) -> None:
        from services.settings import SettingsKey, settings_manager

        settings_manager.save_setting(SettingsKey.REMOTE_HOST_MANAGE_MCP, bool(enabled))
        self._notify("state")

    def mcp_request(self, op: str, header: dict, device_name: str) -> dict:
        """A paired computer's MCP request; the host re-checked its permission."""
        with self._lock:
            if self._mcp_control is None:
                from services.agent_mcp.host_control import HostMcpControl
                from services.agent_mcp.runtime import runtime
                from services.settings import settings_manager

                # One instance, so its lock serializes every paired computer.
                self._mcp_control = HostMcpControl(settings_manager, runtime)
            control = self._mcp_control
        if op == "mcp_state":
            return control.state()
        return control.configure(header["settings"])

    def manage_host_models(self, op: str, fields: dict, device_name: str) -> dict:
        """Authenticated host operations; permission remains host-controlled."""
        if not remote_settings.host_model_management():
            raise RuntimeError("Model management is disabled on the host.")
        if op == "model_catalog":
            result = self._model_manager.catalog()
            result["engine"] = self._engine().describe()
            result["can_select"] = self._switch_engine is not None
            result["can_install_runtime"] = True
            result["installation"] = self._runtime_installer.job()
            result["runtime"] = self.runtime_state()
            return result
        if op == "download_model":
            return self._model_manager.download(fields.get("family"), fields.get("model"), device_name)
        if op == "install_runtime":
            return self._runtime_installer.install(fields.get("family"), fields.get("model"),
                                                   fields.get("device"), device_name)
        raise ValueError("Unknown model management operation.")

    def host_models(self) -> List[dict]:
        """What paired clients may switch to; none when switching isn't wired up."""
        return host_models() if self._switch_engine is not None else []

    def runtime_state(self) -> dict:
        from services.remote_asr.runtime import runtime_state

        return runtime_state(self._engine().describe())

    def configure_runtime(self, family: str, model: str, changes: dict, device_name: str) -> dict:
        """Apply bounded runtime choices to the current host engine, then await load."""
        from services.remote_asr.runtime import validate_runtime

        if self._configure_engine is None:
            raise RuntimeError("The host doesn't support remote runtime controls.")
        if not self._runtime_lock.acquire(blocking=False):
            raise RuntimeError("The host is changing its runtime. Try again in a moment.")
        try:
            if not self._engine_settled():
                raise RuntimeError("The host is loading its engine. Try again in a moment.")
            validate_runtime(self._engine().describe(), family, model, changes)
            self._configure_engine(family, model, changes, device_name)
            deadline = time.monotonic() + SWITCH_TIMEOUT_S
            while not self._engine_settled():
                if time.monotonic() >= deadline:
                    raise RuntimeError("The host is still loading its runtime. Reconnect in a moment.")
                time.sleep(_SWITCH_POLL_S)
            engine = self._engine().describe()
            if not engine.get("available"):
                raise RuntimeError(engine.get("status") or "The host couldn't load this runtime.")
            if (engine.get("family"), engine.get("model")) != (family, model):
                raise RuntimeError("The host switched models while changing its runtime.")
            return engine
        finally:
            self._runtime_lock.release()

    def select_model(self, family: str, model: str, device_name: str, device: Optional[str] = None) -> dict:
        if not self._runtime_lock.acquire(blocking=False):
            raise RuntimeError("The host is changing its engine. Try again in a moment.")
        try:
            return self._select_model(family, model, device_name, device)
        finally:
            self._runtime_lock.release()

    def _select_model(self, family: str, model: str, device_name: str, device: Optional[str]) -> dict:
        """Switch this computer to ``model`` for a paired client. Blocking.

        Runs on the client's connection thread and returns the new engine's
        ``describe()`` once it has loaded. Raises RuntimeError with a reason
        the client shows as is.
        """
        name = socket.gethostname()
        choices = self.host_models()
        if device is not None:
            from services.remote_asr.dependencies import validate_model_device

            # Rechecked by the controller on the UI thread before saving anything.
            choices = [validate_model_device(family, model, device)]
        choice = next((entry for entry in choices
                       if entry["family"] == family and entry["model"] == model), None)
        if choice is None:
            raise RuntimeError(f"{model} isn't ready on {name}. Download it there first.")
        label = choice["label"]
        current = self._engine().describe()
        if device is None and current.get("available") and (current.get("family"), current.get("model")) == (family, model):
            return current
        # Raises with the reason when this computer can't switch right now.
        if device is None:
            self._switch_engine(family, model, device_name)
        else:
            self._switch_engine(family, model, device_name, device)
        deadline = time.monotonic() + SWITCH_TIMEOUT_S
        while not self._engine_settled():
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{name} is still loading {label}. Try again in a moment.")
            time.sleep(_SWITCH_POLL_S)
        engine = self._engine().describe()
        if not engine.get("available"):
            raise RuntimeError(engine.get("status") or f"{name} couldn't load {label}.")
        if (engine.get("family"), engine.get("model")) != (family, model):
            raise RuntimeError(f"{name} switched to another model while loading {label}.")
        logger.info("Switched to %s for paired computer %s", label, device_name)
        return engine

    def apply_host_settings(self) -> None:
        """Start or stop sharing to match settings. Errors land in host_state()."""
        with self._lock:
            enabled = remote_settings.host_enabled()
            port = remote_settings.host_port()
            host = self._host
            if not enabled:
                self._host_error = ""
                if host is not None:
                    host.stop()
                self._notify("state")
                return
            try:
                host = self._ensure_host()
                if host.running and host.port != port:
                    host.stop()
                if not host.running:
                    host.start(port, bind=self._bind)
                self._host_error = ""
            except OSError as exc:
                self._host_error = (
                    f"Couldn't listen on port {port} ({exc.strerror or exc}). "
                    "Another program may be using it; choose a different port."
                )
                logger.warning("Remote engine host could not start: %s", exc)
            except Exception as exc:
                self._host_error = f"Couldn't start sharing: {exc}"
                logger.exception("Remote engine host could not start")
        self._notify("state")

    def set_host_enabled(self, enabled: bool) -> None:
        from services.settings import SettingsKey, settings_manager

        settings_manager.save_setting(SettingsKey.REMOTE_HOST_ENABLED, bool(enabled))
        self.apply_host_settings()

    def set_host_port(self, port: int) -> None:
        from services.settings import SettingsKey, settings_manager

        settings_manager.save_setting(SettingsKey.REMOTE_HOST_PORT, int(port))
        if remote_settings.host_enabled():
            self.apply_host_settings()

    def open_pairing(self) -> Optional[str]:
        with self._lock:
            host = self._host
        if host is None or not host.running:
            return None
        return host.open_pairing()

    def cancel_pairing(self) -> None:
        with self._lock:
            host = self._host
        if host is not None:
            host.close_pairing()

    def remove_device(self, device_id: str, *, delete_records: bool = False) -> bool:
        """Forget a paired computer; with ``delete_records``, what it stored here too.

        Kept records stay in this computer's History and Past Meetings,
        badged with the computer they came from.
        """
        if delete_records:
            with self._record_gate.paused():
                for owner in self._record_owners(device_id):
                    self.record_store().delete_device(owner)
                removed = self.remove_device(device_id)
            self._notify("records")
            return removed
        with self._lock:
            host = self._host
        if host is not None:
            removed = host.remove_device(device_id)
        else:
            removed = self._registry.remove(device_id)
            self._notify("devices")
        return removed

    def engine_changed(self) -> None:
        """This computer's engine may have changed (it just finished loading).

        Refreshes the share line, and has the host tell connected clients
        still on the old engine, on a background thread since each close
        waits for its handshake. A no-op for clients already on this engine.
        """
        self._notify("engine")
        with self._lock:
            host = self._host
        if host is None or not host.running:
            return
        threading.Thread(
            target=host.engine_changed, name="remote-engine-changed", daemon=True
        ).start()

    def connected_clients(self) -> List[dict]:
        """The paired computers connected now, each with whether it's being served.

        What ``host_state()["clients"]`` lists, without the rest of the state,
        so it is cheap enough to ask on every ``activity`` event.
        """
        with self._lock:
            host = self._host
        return host.connected_clients() if host is not None and host.running else []

    def host_activity(self, events: Optional[int] = None) -> dict:
        """What paired computers asked of this host since sharing started.

        In memory and cheap, like ``connected_clients()``; the last session's
        counts stay readable after sharing stops, until it starts again.
        """
        from services.remote_asr.activity import empty_snapshot

        with self._lock:
            host = self._host
        return host.activity.snapshot(events) if host is not None else empty_snapshot()

    def engine_state(self) -> dict:
        """``describe()`` of the engine this computer would share, even while sharing is off."""
        return self._engine().describe()

    def host_state(self) -> dict:
        with self._lock:
            host = self._host
            error = self._host_error
        running = host is not None and host.running
        state = {
            "enabled": remote_settings.host_enabled(),
            "running": running,
            "port": host.port if running else remote_settings.host_port(),
            "error": error,
            "host_name": socket.gethostname(),
            "address": None,
            # "lan", or "vpn" when a VPN's is the only address there is.
            "address_kind": None,
            "fingerprint": host.identity.fingerprint if host is not None else "",
            "pairing": host.pairing_status() if running else None,
            "clients": host.connected_clients() if running else [],
            "devices": self._registry.list(),
            "engine": self._engine().describe() if running else None,
            "tailscale": self.tailscale_status(),
            "tailscale_trust": remote_settings.host_tailscale_trust(),
            "model_management": remote_settings.host_model_management(),
            "keep_records": remote_settings.host_keeps_records(),
            "manage_mcp": remote_settings.host_manages_mcp(),
        }
        if running:
            try:
                from services.lan_address import best_lan_address

                lan = best_lan_address()
                if lan is not None:
                    state["address"], state["address_kind"] = lan.address, lan.kind
            except Exception:
                logger.debug("LAN address lookup failed", exc_info=True)
        return state

    def shutdown(self) -> None:
        if self._history_client is not None:
            self._history_client.stop()
        with self._lock:
            host = self._host
        if host is not None:
            host.stop()

    def start_client_history(self):
        if self._history_client is None:
            from services.remote_history.client import HistoryClient

            self._history_client = HistoryClient()
        self._history_client.start()

    def set_share_history(self, enabled):
        from services.settings import SettingsKey, settings_manager

        settings_manager.save_setting(SettingsKey.REMOTE_CLIENT_HISTORY, enabled is True)
        if self._history_client is not None:
            self._history_client.refresh()
        self._notify("client")

    # ---- client ----

    def client_pairing(self) -> Optional[remote_settings.ClientPairing]:
        return remote_settings.load_client_pairing()

    def pair(self, address: str, code: Optional[str], *,
             tailscale: bool = False) -> remote_settings.ClientPairing:
        """Pair with the host at ``address``. Blocking; run it off the UI thread.

        ``tailscale=True`` pairs without a code, which the host allows only
        for its owner's own Tailscale computers.
        """
        from services.remote_asr.client import pair_with_host

        host, port = protocol.parse_address(address)
        result = pair_with_host(host, port, code, socket.gethostname(), tailscale=tailscale)
        pairing = remote_settings.save_client_pairing(host, port, result)
        if self._history_client is not None:
            self._history_client.refresh()
        logger.info("Paired with remote engine host %s at %s (%s)",
                    pairing.host_name, pairing.address, pairing.via)
        self._notify("client")
        if self.on_client_changed is not None:
            self.on_client_changed()
        return pairing

    def remote_model_request(self, op: str, *, expected_pairing=None, **fields) -> dict:
        """One off-UI-thread management request, separate from dictation.

        No automatic retry: a dropped reply may already have started a download
        or switched the engine. A fresh catalog read reconciles that state.
        """
        from services.remote_asr.client import RemoteConnection, RemoteEngineError

        if op not in ("model_catalog", "download_model", "install_runtime", "select_model"):
            raise ValueError("Unknown model management operation.")
        pairing = self.client_pairing()
        token = remote_settings.load_client_token()
        if pairing is None or not token:
            raise RemoteEngineError("Pair with a host before managing its models.")
        if expected_pairing is not None and pairing != expected_pairing:
            raise RemoteEngineError("The paired host changed. Close this window and open Manage host models again.")
        connection = RemoteConnection(
            pairing.host, pairing.port, token, pairing.fingerprint, alternates=pairing.alternates,
        )
        try:
            ready = connection.connect()
            capabilities = ready.get("capabilities")
            if not isinstance(capabilities, dict) or "model_management" not in capabilities:
                raise RemoteEngineError("Update OpenWhisper on the host to manage its models remotely.")
            if capabilities.get("model_management") is not True:
                raise RemoteEngineError(
                    "Model management is disabled on the host. Enable it in Settings → Remote engine there."
                )
            if (op == "install_runtime" or "device" in fields) and capabilities.get("runtime_installation") is not True:
                raise RemoteEngineError("Update OpenWhisper on the host to install runtimes and choose a model's device remotely.")
            result = connection.request(op, timeout=SWITCH_TIMEOUT_S + 10 if op == "select_model" else 30,
                                        **fields)
            if op == "model_catalog" and self.on_host_catalog is not None:
                self.on_host_catalog(pairing, ready, result)
            return result
        finally:
            connection.close()

    def remote_mcp_request(self, op: str, *, expected_pairing=None, **fields) -> dict:
        """One off-UI-thread request to the paired host's MCP server.

        A ``RemoteRequestError`` whose ``code`` is "unsupported" (an older host)
        or "forbidden" (its owner hasn't allowed it) says why the host won't
        answer; any other ``RemoteEngineError`` means it is out of reach.
        Writes are never retried:
        a dropped reply may already have applied, and a fresh ``mcp_state``
        read shows what the host now has.
        """
        from services.remote_asr.client import (
            RemoteConnection,
            RemoteEngineError,
            RemoteRequestError,
        )

        if op not in ("mcp_state", "mcp_configure"):
            raise ValueError("Unknown MCP operation.")
        pairing = self.client_pairing()
        token = remote_settings.load_client_token()
        if pairing is None or not token:
            raise RemoteEngineError("Pair with a host before managing its MCP server.")
        if expected_pairing is not None and pairing != expected_pairing:
            raise RemoteEngineError("The paired host changed.")
        connection = RemoteConnection(
            pairing.host, pairing.port, token, pairing.fingerprint, alternates=pairing.alternates,
        )
        try:
            ready = connection.connect()
            capabilities = ready.get("capabilities")
            if not isinstance(capabilities, dict) or "mcp_control" not in capabilities:
                raise RemoteRequestError(
                    "Update OpenWhisper on the host to manage its MCP server from here.",
                    code="unsupported",
                )
            if capabilities.get("mcp_control") is not True:
                raise RemoteRequestError(
                    f"{pairing.host_name} isn't letting paired computers manage its MCP server. "
                    "Turn on \"Allow paired computers to manage MCP\" in Settings → Remote engine there.",
                    code="forbidden",
                )
            return connection.request(op, timeout=30, **fields)
        finally:
            connection.close()

    def forget_host(self) -> None:
        remote_settings.forget_client_pairing()
        if self._history_client is not None:
            self._history_client.refresh()
        self._notify("client")
        if self.on_client_changed is not None:
            self.on_client_changed()

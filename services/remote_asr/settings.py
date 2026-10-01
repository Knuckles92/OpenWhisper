"""Persisted remote engine state for both roles.

Client: the paired host's address, pinned certificate fingerprint and name
live in settings; the device token lives in the OS credential store.
Host: whether sharing is on, the port, and the paired devices (token
digests only).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from services.remote_asr import protocol

logger = logging.getLogger(__name__)

TOKEN_CREDENTIAL = "OPENWHISPER_REMOTE_ENGINE_TOKEN"


@dataclass(frozen=True)
class ClientPairing:
    host: str
    port: int
    fingerprint: str
    host_name: str
    device_id: str = ""
    paired_at: str = ""
    #: The host's other addresses (LAN, Tailscale), tried when ``host`` isn't.
    alternates: tuple = ()
    #: "code" or "tailscale": how this computer paired.
    via: str = "code"

    @property
    def address(self) -> str:
        return protocol.format_address(self.host, self.port)

    @property
    def over_tailscale(self) -> bool:
        from services.remote_asr.tailscale import is_tailscale_address

        return is_tailscale_address(self.host)

    @property
    def tailscale_fallback(self) -> str:
        """A Tailscale address to fall back on, when ``host`` is a LAN one."""
        from services.remote_asr.tailscale import is_tailscale_address

        if self.over_tailscale:
            return ""
        return next((a for a in self.alternates if is_tailscale_address(a)), "")


def _settings(settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if settings is not None:
        return settings
    from services.settings import settings_manager

    return settings_manager.load_all_settings()


def load_client_pairing(settings: Optional[Dict[str, Any]] = None) -> Optional[ClientPairing]:
    from services.settings import SettingsKey

    raw = _settings(settings).get(SettingsKey.REMOTE_ENGINE_CLIENT)
    if not isinstance(raw, dict):
        return None
    host = raw.get("host")
    fingerprint = raw.get("fingerprint")
    try:
        port = int(raw.get("port", protocol.DEFAULT_PORT))
    except (TypeError, ValueError):
        return None
    if not isinstance(host, str) or not host or not isinstance(fingerprint, str) or not fingerprint:
        return None
    if not 1 <= port <= 65535:
        return None
    alternates = raw.get("alternates") if isinstance(raw.get("alternates"), list) else []
    return ClientPairing(
        host=host,
        port=port,
        fingerprint=fingerprint,
        host_name=str(raw.get("host_name") or host),
        device_id=str(raw.get("device_id") or ""),
        paired_at=str(raw.get("paired_at") or ""),
        alternates=tuple(a for a in alternates if isinstance(a, str) and a and a != host),
        via="tailscale" if raw.get("via") == "tailscale" else "code",
    )


def load_client_token() -> Optional[str]:
    from services.credentials import resolve_credential

    return resolve_credential(TOKEN_CREDENTIAL)


def save_client_pairing(host: str, port: int, result) -> ClientPairing:
    """Store a fresh pairing: token first, so settings never name a host we can't use."""
    from services.credentials import store
    from services.settings import SettingsKey, settings_manager

    store().set(TOKEN_CREDENTIAL, result.token)
    pairing = ClientPairing(
        host=host,
        port=port,
        fingerprint=result.fingerprint,
        host_name=result.host_name,
        device_id=result.device_id,
        paired_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        alternates=tuple(a for a in getattr(result, "alternates", ()) if a != host),
        via=getattr(result, "via", "code"),
    )
    settings_manager.save_setting(SettingsKey.REMOTE_ENGINE_CLIENT, {
        "host": pairing.host,
        "port": pairing.port,
        "fingerprint": pairing.fingerprint,
        "host_name": pairing.host_name,
        "device_id": pairing.device_id,
        "paired_at": pairing.paired_at,
        "alternates": list(pairing.alternates),
        "via": pairing.via,
    })
    return pairing


def forget_client_pairing() -> None:
    from services.credentials import store
    from services.settings import SettingsKey, settings_manager

    settings_manager.update_settings({}, remove=(SettingsKey.REMOTE_ENGINE_CLIENT,))
    try:
        store().delete(TOKEN_CREDENTIAL)
    except Exception as exc:
        logger.warning("Could not delete the remote engine token: %s", type(exc).__name__)


def host_enabled(settings: Optional[Dict[str, Any]] = None) -> bool:
    from services.settings import SettingsKey

    return _settings(settings).get(SettingsKey.REMOTE_HOST_ENABLED) is True


def host_model_management(settings: Optional[Dict[str, Any]] = None) -> bool:
    """Off by default; pairing alone never grants model administration."""
    from services.settings import SettingsKey

    return _settings(settings).get(SettingsKey.REMOTE_HOST_MODEL_MANAGEMENT) is True


def host_keeps_records(settings: Optional[Dict[str, Any]] = None) -> bool:
    """Off by default; pairing alone never lets a computer store files here."""
    from services.settings import SettingsKey

    return _settings(settings).get(SettingsKey.REMOTE_HOST_KEEP_RECORDS) is True


RECORD_LOCATIONS = ("local", "host", "both")


def client_shares_history(settings: Optional[Dict[str, Any]] = None) -> bool:
    from services.settings import SettingsKey

    return _settings(settings).get(SettingsKey.REMOTE_CLIENT_HISTORY) is True


def records_location(settings: Optional[Dict[str, Any]] = None) -> str:
    """Where this computer keeps its records while paired; "local" by default."""
    from services.settings import SettingsKey

    value = _settings(settings).get(SettingsKey.REMOTE_RECORDS_LOCATION)
    return value if value in RECORD_LOCATIONS else "local"


def host_port(settings: Optional[Dict[str, Any]] = None) -> int:
    from services.settings import SettingsKey

    raw = _settings(settings).get(SettingsKey.REMOTE_HOST_PORT, protocol.DEFAULT_PORT)
    try:
        port = int(raw)
    except (TypeError, ValueError):
        return protocol.DEFAULT_PORT
    return port if 1 <= port <= 65535 else protocol.DEFAULT_PORT


def host_tailscale_trust(settings: Optional[Dict[str, Any]] = None) -> bool:
    """On unless turned off: the owner's own Tailscale computers pair without a code."""
    from services.settings import SettingsKey

    return _settings(settings).get(SettingsKey.REMOTE_HOST_TAILSCALE_TRUST) is not False


def load_host_devices() -> list:
    from services.settings import SettingsKey

    raw = _settings().get(SettingsKey.REMOTE_HOST_DEVICES)
    return raw if isinstance(raw, list) else []


def save_host_devices(devices: list) -> None:
    from services.settings import SettingsKey, settings_manager

    settings_manager.save_setting(SettingsKey.REMOTE_HOST_DEVICES, list(devices))

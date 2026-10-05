"""What a paired computer may do to this computer's MCP server.

The Settings page does these things for the person at this keyboard; this
does them for a paired computer over the remote engine connection. Both end in
the same saved settings and the same runtime calls, so the two can't disagree.
Whether a paired computer may ask at all is the host owner's opt-in, checked
by the caller (services/remote_asr/host.py) on every request.
"""

import logging
import threading

from services.agent_mcp.controls import CONTROLS_BY_KEY, ControlError, writable_settings
from services.agent_mcp.runtime import DEFAULT_PORT
from services.settings import SettingsKey

logger = logging.getLogger(__name__)

#: Request field -> the saved setting it grants. Booleans only.
PERMISSIONS = {
    "retitle_transcriptions": SettingsKey.MCP_RETITLE_TRANSCRIPTIONS,
    "retitle_meetings": SettingsKey.MCP_RETITLE_MEETINGS,
    "settings_access": SettingsKey.MCP_SETTINGS_ACCESS,
}
CHANGEABLE = frozenset({"enabled", "port", "tailscale", "writable", *PERMISSIONS})


def _valid_port(port):
    return type(port) is int and 1 <= port <= 65535


class HostMcpControl:
    def __init__(self, settings, runtime):
        self._settings = settings
        self._runtime = runtime
        # One change at a time, so two paired computers can't interleave a
        # stop with a start and leave the saved switch disagreeing with the listener.
        self._lock = threading.Lock()

    def state(self):
        preferences = self._settings.load_all_settings()
        status = self._runtime.status()
        port = preferences.get(SettingsKey.MCP_PORT, DEFAULT_PORT)
        return {
            "enabled": preferences.get(SettingsKey.MCP_ENABLED) is True,
            "state": status.state,
            "message": status.message,
            "port": port if _valid_port(port) else DEFAULT_PORT,
            "tailscale": preferences.get(SettingsKey.MCP_TAILSCALE_ENABLED) is True,
            "url": status.url,
            "remote_url": status.remote_url,
            "token": self._runtime.token(),
            "permissions": {
                name: preferences.get(key) is True for name, key in PERMISSIONS.items()
            },
            "granted": sorted(writable_settings(preferences)),
            "controls": sorted(CONTROLS_BY_KEY),
        }

    def configure(self, changes):
        """Apply ``changes`` (see ``CHANGEABLE``) and return the new state.

        The whole request is checked first; nothing is applied unless all of it
        is acceptable.
        """
        if not isinstance(changes, dict):
            raise ControlError("invalid_value: MCP changes must be an object.")
        with self._lock:
            self._validate(changes)
            self._apply(changes)
            return self.state()

    def _validate(self, changes):
        if set(changes) - CHANGEABLE:
            raise ControlError("invalid_value: Unknown MCP setting.")
        for name in ("enabled", "tailscale", *PERMISSIONS):
            if name in changes and type(changes[name]) is not bool:
                raise ControlError(f"invalid_value: {name} must be true or false.")
        if "port" in changes and not _valid_port(changes["port"]):
            raise ControlError("invalid_value: MCP port must be between 1 and 65535.")
        grants = changes.get("writable", {})
        if not isinstance(grants, dict) or any(
            key not in CONTROLS_BY_KEY or type(value) is not bool
            for key, value in grants.items()
        ):
            raise ControlError("invalid_value: Choose supported preferences.")
        status = self._runtime.status()
        saved_on = self._settings.get(SettingsKey.MCP_ENABLED, False) is True
        if ("port" in changes or "tailscale" in changes) and (
            saved_on or status.state not in ("stopped", "error")
        ):
            raise ControlError("busy: Turn MCP off to change its port or Tailscale access.")
        if changes.get("enabled") is True and status.state == "stopping":
            raise ControlError("busy: MCP is still stopping. Try again in a moment.")

    def _apply(self, changes):
        try:
            for name, key in PERMISSIONS.items():
                if name in changes:
                    self._settings.save_setting(key, changes[name])
            if changes.get("writable"):
                grants = changes["writable"]

                def commit(saved):
                    granted = saved.get(SettingsKey.MCP_WRITABLE_SETTINGS, {})
                    granted = dict(granted) if isinstance(granted, dict) else {}
                    granted.update(grants)
                    saved[SettingsKey.MCP_WRITABLE_SETTINGS] = granted

                self._settings.mutate_settings(commit)
            if "port" in changes:
                self._settings.save_setting(SettingsKey.MCP_PORT, changes["port"])
            if "tailscale" in changes:
                self._settings.save_setting(
                    SettingsKey.MCP_TAILSCALE_ENABLED, changes["tailscale"]
                )
            if "enabled" in changes:
                self._settings.save_setting(SettingsKey.MCP_ENABLED, changes["enabled"])
        except Exception:
            logger.warning("Could not save a remote MCP change", exc_info=True)
            raise ControlError("save_failed: The host couldn't save that MCP setting.")
        if "enabled" in changes:
            if changes["enabled"]:
                self._start()
            else:
                self._runtime.stop()

    def _start(self):
        preferences = self._settings.load_all_settings()
        port = preferences.get(SettingsKey.MCP_PORT, DEFAULT_PORT)
        port = port if _valid_port(port) else DEFAULT_PORT
        if preferences.get(SettingsKey.MCP_TAILSCALE_ENABLED) is True:
            self._runtime.start(port, tailscale=True)
        else:
            self._runtime.start(port)

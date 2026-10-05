"""The paired host's MCP server, as the MCP settings page sees it.

The page reads its state on a timer and writes it from button handlers, both
on the UI thread, and neither may wait on a network. ``HostMcpLink`` keeps a
local view of the host's MCP state, refreshes it on a worker thread, and
sends changes the same way: a change shows in the view at once and is applied
to the host in the background. If the host refuses or can't be reached, the
view goes back to what the host last said and ``take_problem`` says why.

``HostMcpSettings`` and ``HostMcpServer`` give that view the shapes the page
already uses for this computer (``SettingsManager`` and ``McpRuntime``), so
the same page serves both. Nothing here imports Qt.
"""
from __future__ import annotations

import copy
import logging
import threading
import time
from typing import Callable, Optional

from services.agent_mcp.host_control import PERMISSIONS
from services.agent_mcp.runtime import DEFAULT_PORT, ServerStatus
from services.settings import SettingsKey

logger = logging.getLogger(__name__)

#: How stale the view may get while the host answers, and while it doesn't.
REFRESH_S = 2.0
RETRY_S = 5.0

LOADING, READY, FORBIDDEN, UNSUPPORTED, OFFLINE = (
    "loading", "ready", "forbidden", "unsupported", "offline",
)

_SETTING_FIELDS = {key: name for name, key in PERMISSIONS.items()}
_SETTING_FIELDS[SettingsKey.MCP_PORT] = "port"
_SETTING_FIELDS[SettingsKey.MCP_TAILSCALE_ENABLED] = "tailscale"


class HostMcpLink:
    def __init__(self, service, pairing=None, *, clock: Callable[[], float] = time.monotonic):
        self._service = service
        self._pairing = pairing
        self._clock = clock
        self._lock = threading.RLock()
        self._truth: Optional[dict] = None
        self._view: Optional[dict] = None
        self._availability = LOADING
        self._message = ""
        self._problem = ""
        self._pending: dict = {}
        self._running = False
        self._fetched_at = -1e9
        self._force = True

    # ---- what the page reads ----

    def availability(self) -> str:
        with self._lock:
            return self._availability

    def message(self) -> str:
        with self._lock:
            return self._message

    def state(self) -> Optional[dict]:
        with self._lock:
            return copy.deepcopy(self._view)

    def take_problem(self) -> str:
        """Why the last change didn't stick, once; empty when nothing went wrong."""
        with self._lock:
            problem, self._problem = self._problem, ""
            return problem

    # ---- what drives it ----

    def poll(self) -> None:
        """Refresh in the background if the view has gone stale."""
        with self._lock:
            age = self._clock() - self._fetched_at
            limit = REFRESH_S if self._availability == READY else RETRY_S
            if age < limit:
                return
            self._force = True
        self._kick()

    def refresh_now(self) -> None:
        with self._lock:
            self._force = True
        self._kick()

    def change(self, **changes) -> None:
        """Queue ``changes`` for the host and show them in the view straight away."""
        with self._lock:
            if self._view is None:
                return
            for name, value in changes.items():
                if name == "writable":
                    self._pending.setdefault("writable", {}).update(value)
                else:
                    self._pending[name] = value
            self._show(changes)
        self._kick()

    def _show(self, changes: dict) -> None:
        """Write ``changes`` into the view, as the host will once it applies them."""
        view = self._view
        for name, value in changes.items():
            if name == "writable":
                granted = set(view.get("granted", ()))
                for key, allowed in value.items():
                    (granted.add if allowed else granted.discard)(key)
                view["granted"] = sorted(granted)
            elif name in view.get("permissions", {}):
                view["permissions"][name] = value
            elif name in ("port", "tailscale", "enabled"):
                view[name] = value
            if name == "enabled":
                view["state"] = "starting" if value else "stopped"
                view["message"] = "Starting MCP…" if value else "MCP is off."
                if not value:
                    view["token"] = ""

    # ---- the worker ----

    def _kick(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
        threading.Thread(target=self._run, name="remote-mcp-link", daemon=True).start()

    def _run(self) -> None:
        try:
            while True:
                with self._lock:
                    changes, self._pending = self._pending, {}
                    fetch = not changes and self._force
                    if not changes and not fetch:
                        self._running = False
                        return
                    self._force = False
                if changes:
                    self._send(changes)
                else:
                    self._fetch()
        except BaseException:
            with self._lock:
                self._running = False
            raise

    def _ask(self, op: str, **fields):
        return self._service.remote_mcp_request(op, expected_pairing=self._pairing, **fields)

    def _fetch(self) -> None:
        try:
            self._take(self._ask("mcp_state"))
        except Exception as exc:
            self._fail(exc, write=False)

    def _send(self, changes: dict) -> None:
        try:
            self._take(self._ask("mcp_configure", settings=changes))
        except Exception as exc:
            self._fail(exc, write=True)

    def _take(self, state: dict) -> None:
        with self._lock:
            self._truth = state
            self._view = copy.deepcopy(state)
            # A change made while this request was in flight isn't in the
            # answer yet; keep showing it rather than flickering back.
            self._show(self._pending)
            self._availability, self._message = READY, ""
            self._fetched_at = self._clock()

    def _fail(self, exc: Exception, *, write: bool) -> None:
        code = getattr(exc, "code", None)
        text = str(exc) or type(exc).__name__
        with self._lock:
            self._fetched_at = self._clock()
            if code in ("forbidden", "unsupported"):
                self._availability = FORBIDDEN if code == "forbidden" else UNSUPPORTED
                self._message, self._view = text, None
                return
            if code is None:
                # No answer at all: the host is off, asleep or out of reach.
                self._availability, self._message = OFFLINE, text
            if write:
                # Back to what the host last said, so a refused change doesn't linger.
                self._problem = text
                self._view = copy.deepcopy(self._truth)
            logger.debug("Host MCP request failed: %s", text)


def _defaults() -> dict:
    return {
        "enabled": False, "state": "stopped", "message": "", "port": DEFAULT_PORT,
        "tailscale": False, "url": f"http://127.0.0.1:{DEFAULT_PORT}/mcp",
        "remote_url": "", "token": "", "granted": [], "controls": [],
        "permissions": {name: False for name in PERMISSIONS},
    }


class HostMcpSettings:
    """``SettingsManager``'s side of the page, answered from the host's view."""

    def __init__(self, link: HostMcpLink):
        self.link = link

    def load_all_settings(self) -> dict:
        state = self.link.state() or _defaults()
        saved = {
            SettingsKey.MCP_ENABLED: state["enabled"],
            SettingsKey.MCP_PORT: state["port"],
            SettingsKey.MCP_TAILSCALE_ENABLED: state["tailscale"],
            SettingsKey.MCP_WRITABLE_SETTINGS: {key: True for key in state["granted"]},
        }
        for name, key in PERMISSIONS.items():
            saved[key] = state["permissions"].get(name, False)
        return saved

    def get(self, key, default=None):
        return self.load_all_settings().get(key, default)

    def save_setting(self, key, value) -> None:
        if key == SettingsKey.MCP_ENABLED:
            return  # ``HostMcpServer.start``/``stop`` carry it, with the action.
        field = _SETTING_FIELDS.get(key)
        if field is None:
            raise KeyError(key)
        self.link.change(**{field: value})

    def mutate_settings(self, commit) -> None:
        before = self.load_all_settings()
        after = copy.deepcopy(before)
        commit(after)
        old = before.get(SettingsKey.MCP_WRITABLE_SETTINGS, {})
        new = after.get(SettingsKey.MCP_WRITABLE_SETTINGS, {})
        changed = {key: new.get(key) is True for key in set(old) | set(new)
                   if (old.get(key) is True) != (new.get(key) is True)}
        if changed:
            self.link.change(writable=changed)


class HostMcpServer:
    """``McpRuntime``'s side of the page, answered from the host's view."""

    def __init__(self, link: HostMcpLink):
        self.link = link

    def status(self) -> ServerStatus:
        self.link.poll()
        state = self.link.state() or _defaults()
        return ServerStatus(state["state"], state["message"], state["port"], state["remote_url"])

    def token(self) -> str:
        state = self.link.state()
        return state["token"] if state and state["state"] == "running" else ""

    def start(self, port=DEFAULT_PORT, *, tailscale=False) -> None:
        # The host starts with the port and Tailscale choice it has saved.
        self.link.change(enabled=True)

    def stop(self, *, wait=False) -> None:
        self.link.change(enabled=False)

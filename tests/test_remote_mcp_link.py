"""The page-facing view of the paired host's MCP server (no network, no Qt)."""
from __future__ import annotations

import copy
import threading
import time

import pytest

from services.agent_mcp.host_control import PERMISSIONS
from services.remote_asr.client import RemoteEngineError, RemoteRequestError
from services.remote_asr.mcp_link import (
    FORBIDDEN,
    LOADING,
    OFFLINE,
    READY,
    UNSUPPORTED,
    HostMcpLink,
    HostMcpServer,
    HostMcpSettings,
)
from services.settings import SettingsKey


def host_state(**overrides):
    state = {
        "enabled": False, "state": "stopped", "message": "MCP is off.", "port": 8767,
        "tailscale": False, "url": "http://127.0.0.1:8767/mcp", "remote_url": "",
        "token": "", "granted": [], "controls": [],
        "permissions": {name: False for name in PERMISSIONS},
    }
    state.update(overrides)
    return state


class FakeService:
    """Plays the host: applies configure requests to a state of its own."""

    def __init__(self):
        self.state = host_state()
        self.calls = []
        self.error = None
        self.gate = None
        self.lock = threading.Lock()

    def remote_mcp_request(self, op, *, expected_pairing=None, **fields):
        with self.lock:
            self.calls.append((op, copy.deepcopy(fields)))
        if self.gate is not None:
            assert self.gate.wait(5)
        if self.error is not None:
            raise self.error
        if op == "mcp_configure":
            changes = fields["settings"]
            for name, value in changes.items():
                if name == "enabled":
                    self.state.update(
                        enabled=value, state="running" if value else "stopped",
                        token="tok" if value else "")
                elif name == "writable":
                    granted = set(self.state["granted"])
                    for key, allowed in value.items():
                        (granted.add if allowed else granted.discard)(key)
                    self.state["granted"] = sorted(granted)
                elif name in self.state["permissions"]:
                    self.state["permissions"][name] = value
                else:
                    self.state[name] = value
        return copy.deepcopy(self.state)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def settle(link, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with link._lock:
            if not link._running and not link._pending and not link._force:
                return
        time.sleep(0.01)
    raise AssertionError("the link never went idle")


@pytest.fixture
def parts():
    service, clock = FakeService(), Clock()
    link = HostMcpLink(service, clock=clock)
    return service, link, clock


def test_the_first_poll_loads_the_hosts_state(parts):
    service, link, _ = parts
    assert link.availability() == LOADING and link.state() is None
    link.poll()
    settle(link)
    assert link.availability() == READY and link.state()["port"] == 8767
    assert service.calls == [("mcp_state", {})]


def test_polling_is_throttled_until_the_view_goes_stale(parts):
    service, link, clock = parts
    link.poll()
    settle(link)
    link.poll()
    settle(link)
    assert len(service.calls) == 1
    clock.now += 2.5
    link.poll()
    settle(link)
    assert len(service.calls) == 2


def test_a_change_shows_immediately_then_matches_the_host(parts):
    service, link, _ = parts
    link.poll()
    settle(link)
    service.gate = threading.Event()
    link.change(enabled=True)
    view = link.state()
    assert view["enabled"] is True and view["state"] == "starting" and view["token"] == ""
    service.gate.set()
    settle(link)
    view = link.state()
    assert view["state"] == "running" and view["token"] == "tok"
    assert service.calls[-1] == ("mcp_configure", {"settings": {"enabled": True}})


def test_changes_made_while_a_request_is_in_flight_are_not_lost(parts):
    service, link, _ = parts
    link.poll()
    settle(link)
    service.gate = threading.Event()
    link.change(retitle_meetings=True)
    deadline = time.monotonic() + 3
    while len(service.calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    link.change(retitle_transcriptions=True)
    link.change(settings_access=True)
    assert link.state()["permissions"]["retitle_transcriptions"] is True
    service.gate.set()
    settle(link)
    view = link.state()
    assert all(view["permissions"][name] for name in (
        "retitle_meetings", "retitle_transcriptions", "settings_access"))
    # The two later changes travelled together rather than one request each.
    assert len([c for c in service.calls if c[0] == "mcp_configure"]) == 2


def test_a_refused_change_rolls_back_and_says_why_once(parts):
    service, link, _ = parts
    link.poll()
    settle(link)
    service.error = RemoteRequestError("Turn MCP off to change its port.", code="busy")
    service.gate = threading.Event()
    link.change(port=9000)
    assert link.state()["port"] == 9000
    service.gate.set()
    settle(link)
    assert link.state()["port"] == 8767
    assert link.availability() == READY
    assert link.take_problem() == "Turn MCP off to change its port."
    assert link.take_problem() == ""


def test_the_hosts_opt_in_and_old_hosts_are_told_apart(parts):
    service, link, clock = parts
    service.error = RemoteRequestError("jed isn't letting paired computers…", code="forbidden")
    link.poll()
    settle(link)
    assert link.availability() == FORBIDDEN and link.state() is None
    assert "jed" in link.message()
    service.error = RemoteRequestError("Update OpenWhisper", code="unsupported")
    clock.now += 6
    link.poll()
    settle(link)
    assert link.availability() == UNSUPPORTED
    # The owner turns it on: the next retry notices.
    service.error = None
    clock.now += 6
    link.poll()
    settle(link)
    assert link.availability() == READY and link.state() is not None


def test_an_unreachable_host_goes_offline_and_recovers(parts):
    service, link, clock = parts
    link.poll()
    settle(link)
    service.error = RemoteEngineError("Couldn't reach jed.")
    link.change(retitle_meetings=True)
    settle(link)
    assert link.availability() == OFFLINE
    assert link.take_problem() == "Couldn't reach jed."
    assert link.state()["permissions"]["retitle_meetings"] is False
    service.error = None
    clock.now += 6
    link.poll()
    settle(link)
    assert link.availability() == READY


def test_the_settings_facade_maps_the_page_onto_host_changes(parts):
    service, link, _ = parts
    link.poll()
    settle(link)
    settings = HostMcpSettings(link)
    settings.save_setting(SettingsKey.MCP_RETITLE_MEETINGS, True)
    settings.save_setting(SettingsKey.MCP_TAILSCALE_ENABLED, True)
    settings.save_setting(SettingsKey.MCP_PORT, 9100)
    settle(link)
    assert settings.get(SettingsKey.MCP_RETITLE_MEETINGS) is True
    assert settings.get(SettingsKey.MCP_TAILSCALE_ENABLED) is True
    assert settings.get(SettingsKey.MCP_PORT) == 9100
    sent = {}
    for op, fields in service.calls:
        if op == "mcp_configure":
            sent.update(fields["settings"])
    assert sent == {"retitle_meetings": True, "tailscale": True, "port": 9100}
    with pytest.raises(KeyError):
        settings.save_setting(SettingsKey.REMOTE_HOST_MANAGE_MCP, True)


def test_individual_grants_send_only_what_changed(parts):
    service, link, _ = parts
    link.poll()
    settle(link)
    settings = HostMcpSettings(link)

    def grant(saved):
        granted = dict(saved.get(SettingsKey.MCP_WRITABLE_SETTINGS, {}))
        granted[SettingsKey.AUTO_PASTE] = True
        saved[SettingsKey.MCP_WRITABLE_SETTINGS] = granted

    settings.mutate_settings(grant)
    settle(link)
    assert service.calls[-1] == (
        "mcp_configure", {"settings": {"writable": {SettingsKey.AUTO_PASTE: True}}})
    calls = len(service.calls)
    settings.mutate_settings(grant)  # already granted: nothing to send
    settle(link)
    assert len(service.calls) == calls
    assert SettingsKey.AUTO_PASTE in settings.load_all_settings()[SettingsKey.MCP_WRITABLE_SETTINGS]


def test_the_server_facade_turns_the_host_on_and_off(parts):
    service, link, _ = parts
    link.poll()
    settle(link)
    server = HostMcpServer(link)
    assert server.status().state == "stopped" and server.token() == ""
    server.start(8767, tailscale=True)
    settle(link)
    status = server.status()
    assert status.state == "running" and server.token() == "tok"
    assert service.calls[-1] == ("mcp_configure", {"settings": {"enabled": True}})
    server.stop()
    settle(link)
    assert server.status().state == "stopped" and server.token() == ""

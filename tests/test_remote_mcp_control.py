"""A paired computer managing the host's MCP server, over real pinned TLS."""
from __future__ import annotations

import pytest

from services.agent_mcp.controls import SETTING_CONTROLS, ControlError
from services.agent_mcp.host_control import HostMcpControl
from services.agent_mcp.runtime import DEFAULT_PORT, ServerStatus
from services.remote_asr import settings as remote_settings
from services.remote_asr.client import PairingResult, RemoteRequestError
from services.remote_asr.service import RemoteEngineService
from services.settings import SettingsKey, SettingsManager, settings_manager
from tests.test_remote_engine import FakeEngine, _connect

TOKEN = "host-mcp-token-" + "b" * 30


class FakeRuntime:
    """Stands in for the listener; the real one would bind sockets."""

    def __init__(self):
        self.current = ServerStatus("stopped", "MCP is off.", DEFAULT_PORT)
        self.starts = []
        self.stops = 0

    def status(self):
        return self.current

    def token(self):
        return TOKEN if self.current.state == "running" else ""

    def start(self, port=DEFAULT_PORT, *, tailscale=False):
        self.starts.append((port, tailscale))
        remote = f"http://100.64.0.9:{port}/mcp" if tailscale else ""
        self.current = ServerStatus("running", "Ready", port, remote)

    def stop(self, *, wait=False):
        self.stops += 1
        self.current = ServerStatus("stopped", "MCP is off.", self.current.port)


@pytest.fixture
def control(tmp_path):
    settings = SettingsManager(str(tmp_path / "settings.json"))
    settings.save_all_settings({})
    runtime = FakeRuntime()
    return HostMcpControl(settings, runtime), settings, runtime


def test_state_reports_what_the_host_page_would_show(control):
    host, settings, runtime = control
    state = host.state()
    assert state["enabled"] is False and state["state"] == "stopped"
    assert state["port"] == DEFAULT_PORT and state["token"] == ""
    assert state["permissions"] == {
        "retitle_transcriptions": False,
        "retitle_meetings": False,
        "settings_access": False,
    }
    assert state["granted"] == [] and len(state["controls"]) == len(SETTING_CONTROLS)
    runtime.current = ServerStatus("running", "Ready", DEFAULT_PORT)
    assert host.state()["token"] == TOKEN


def test_enable_starts_with_the_saved_port_and_tailscale_choice(control):
    host, settings, runtime = control
    state = host.configure({"port": 9100, "tailscale": True, "enabled": True})
    assert runtime.starts == [(9100, True)]
    assert settings.get(SettingsKey.MCP_ENABLED) is True
    assert settings.get(SettingsKey.MCP_PORT) == 9100
    assert state["remote_url"] == "http://100.64.0.9:9100/mcp" and state["token"] == TOKEN
    host.configure({"enabled": False})
    assert runtime.stops == 1 and settings.get(SettingsKey.MCP_ENABLED) is False


def test_port_and_tailscale_cannot_change_while_mcp_is_on(control):
    host, settings, runtime = control
    host.configure({"enabled": True})
    for change in ({"port": 9000}, {"tailscale": True}):
        with pytest.raises(ControlError, match="busy"):
            host.configure(change)
    assert settings.get(SettingsKey.MCP_PORT) is None
    host.configure({"retitle_meetings": True})
    assert host.state()["permissions"]["retitle_meetings"] is True


def test_permissions_and_individual_grants_match_the_host_page(control):
    host, settings, _ = control
    state = host.configure({
        "retitle_transcriptions": True,
        "settings_access": True,
        "writable": {SettingsKey.AUTO_PASTE: True, SettingsKey.UI_THEME: True},
    })
    assert state["permissions"]["retitle_transcriptions"] is True
    assert state["granted"] == sorted([SettingsKey.AUTO_PASTE, SettingsKey.UI_THEME])
    state = host.configure({"writable": {SettingsKey.AUTO_PASTE: False}})
    assert state["granted"] == [SettingsKey.UI_THEME]


@pytest.mark.parametrize("changes", [
    {"unknown": True},
    {"enabled": "yes"},
    {"enabled": 1},
    {"port": 0},
    {"port": 70000},
    {"port": True},
    {"port": "8767"},
    {"writable": {"remote_host_manage_mcp": True}},
    {"writable": {SettingsKey.AUTO_PASTE: "true"}},
    {"writable": []},
    {"retitle_meetings": None},
])
def test_invalid_requests_change_nothing(control, changes):
    host, settings, runtime = control
    with pytest.raises(ControlError):
        host.configure({"retitle_transcriptions": True, **changes})
    assert settings.get(SettingsKey.MCP_RETITLE_TRANSCRIPTIONS) is None
    assert not runtime.starts


def test_a_remote_computer_cannot_reach_its_own_gate(control):
    """The opt-in that lets paired computers in is not itself something they can grant."""
    host, settings, _ = control
    with pytest.raises(ControlError):
        host.configure({"remote_host_manage_mcp": True})
    with pytest.raises(ControlError):
        host.configure({"writable": {SettingsKey.REMOTE_HOST_MANAGE_MCP: True}})
    assert settings.get(SettingsKey.REMOTE_HOST_MANAGE_MCP) is None


@pytest.fixture
def managed(tmp_path, monkeypatch):
    from services.remote_asr import tailscale

    monkeypatch.setattr(tailscale, "status", lambda: tailscale.TailscaleStatus("not_installed"))
    engine = FakeEngine()
    service = RemoteEngineService(lambda: None, identity_dir=str(tmp_path / "identity"))
    monkeypatch.setattr(service, "_engine", lambda: engine)
    monkeypatch.setattr(service, "_host_addresses", lambda: [])
    runtime = FakeRuntime()
    settings = SettingsManager(str(tmp_path / "host-settings.json"))
    settings.save_all_settings({})
    service._mcp_control = HostMcpControl(settings, runtime)
    host = service._ensure_host()
    host.start(port=0, bind="127.0.0.1")
    device, token = host.registry.add("laptop")
    pairing = PairingResult(token, device["id"], host.host_name, host.identity.fingerprint)
    remote_settings.save_client_pairing("127.0.0.1", host.port, pairing)
    yield type("Managed", (), {
        "service": service, "host": host, "pairing": pairing,
        "runtime": runtime, "settings": settings,
    })
    service.shutdown()


def test_pairing_alone_never_grants_mcp_control(managed):
    assert remote_settings.host_manages_mcp() is False
    connection = _connect(managed.host, managed.pairing)
    try:
        assert connection.ready["capabilities"]["mcp_control"] is False
        for op, fields in (("mcp_state", {}), ("mcp_configure", {"settings": {"enabled": True}})):
            with pytest.raises(RemoteRequestError, match="isn't letting paired") as caught:
                connection.request(op, **fields)
            assert caught.value.code == "forbidden"
        assert not managed.runtime.starts
        assert connection.request("describe")["available"]
    finally:
        connection.close()
    with pytest.raises(RemoteRequestError) as caught:
        managed.service.remote_mcp_request("mcp_state")
    assert caught.value.code == "forbidden"


def test_permission_is_rechecked_on_existing_connections(managed):
    connection = _connect(managed.host, managed.pairing)
    try:
        managed.service.set_manage_mcp(True)
        assert connection.request("mcp_state")["state"] == "stopped"
        managed.service.set_manage_mcp(False)
        with pytest.raises(RemoteRequestError, match="isn't letting paired"):
            connection.request("mcp_configure", settings={"enabled": True})
        assert not managed.runtime.starts
        assert settings_manager.load_all_settings()[SettingsKey.REMOTE_HOST_MANAGE_MCP] is False
    finally:
        connection.close()


def test_a_paired_computer_turns_the_host_mcp_on_and_reads_its_connection(managed):
    managed.service.set_manage_mcp(True)
    state = managed.service.remote_mcp_request(
        "mcp_configure", settings={"tailscale": True, "enabled": True, "retitle_meetings": True}
    )
    assert state["state"] == "running" and state["token"] == TOKEN
    assert managed.runtime.starts == [(DEFAULT_PORT, True)]
    assert managed.settings.get(SettingsKey.MCP_RETITLE_MEETINGS) is True
    again = managed.service.remote_mcp_request("mcp_state")
    assert again["remote_url"] == f"http://100.64.0.9:{DEFAULT_PORT}/mcp"
    managed.service.remote_mcp_request("mcp_configure", settings={"enabled": False})
    assert managed.runtime.stops == 1
    assert managed.service.remote_mcp_request("mcp_state")["token"] == ""


@pytest.mark.parametrize("fields", [
    {},
    {"settings": []},
    {"settings": {"enabled": True}, "extra": 1},
])
def test_malformed_configure_requests_are_refused(managed, fields):
    managed.service.set_manage_mcp(True)
    with pytest.raises(RemoteRequestError, match="Invalid MCP request") as caught:
        managed.service.remote_mcp_request("mcp_configure", **fields)
    assert caught.value.code == "bad_request"
    assert not managed.runtime.starts


def test_host_validation_errors_reach_the_client_without_internals(managed):
    managed.service.set_manage_mcp(True)
    with pytest.raises(RemoteRequestError, match="between 1 and 65535") as caught:
        managed.service.remote_mcp_request("mcp_configure", settings={"port": 0})
    assert caught.value.code == "invalid_value"
    managed.service.remote_mcp_request("mcp_configure", settings={"enabled": True})
    with pytest.raises(RemoteRequestError, match="Turn MCP off") as caught:
        managed.service.remote_mcp_request("mcp_configure", settings={"port": 9000})
    assert caught.value.code == "busy"


def test_revoked_devices_lose_mcp_control(managed):
    managed.service.set_manage_mcp(True)
    connection = _connect(managed.host, managed.pairing)
    try:
        managed.host.registry.remove(managed.pairing.device_id)
        with pytest.raises(RuntimeError):
            connection.request("mcp_configure", settings={"enabled": True})
        assert not managed.runtime.starts
    finally:
        connection.close()


def test_an_older_host_without_the_capability_is_reported_as_unsupported(managed):
    managed.host._mcp = None
    with pytest.raises(RemoteRequestError) as caught:
        managed.service.remote_mcp_request("mcp_state")
    assert caught.value.code == "unsupported"


def test_the_page_link_works_end_to_end_over_real_tls(managed):
    """HostMcpLink -> service -> pinned TLS -> host -> HostMcpControl, no fakes in between."""
    from services.remote_asr import mcp_link
    from tests.test_remote_mcp_link import settle

    link = mcp_link.HostMcpLink(managed.service)
    link.poll()
    settle(link)
    assert link.availability() == mcp_link.FORBIDDEN and "isn't letting paired" in link.message()
    managed.service.set_manage_mcp(True)
    link.refresh_now()
    settle(link)
    assert link.availability() == mcp_link.READY and link.state()["state"] == "stopped"
    settings, server = mcp_link.HostMcpSettings(link), mcp_link.HostMcpServer(link)
    settings.save_setting(SettingsKey.MCP_TAILSCALE_ENABLED, True)
    settle(link)
    server.start()
    settle(link)
    assert managed.runtime.starts == [(DEFAULT_PORT, True)]
    assert server.status().state == "running" and server.token() == TOKEN
    # A refusal from the real host rolls the view back and says why.
    settings.save_setting(SettingsKey.MCP_PORT, 9000)
    settle(link)
    assert link.state()["port"] == DEFAULT_PORT
    assert "Turn MCP off" in link.take_problem()
    # The owner revokes it: the very next request is turned away.
    managed.service.set_manage_mcp(False)
    link.refresh_now()
    settle(link)
    assert link.availability() == mcp_link.FORBIDDEN and link.state() is None


def test_restoring_a_backup_never_carries_the_opt_in_over():
    from services.backup import _sanitize_settings

    cleaned = _sanitize_settings({"remote_host_manage_mcp": True})
    assert cleaned["remote_host_manage_mcp"] is False

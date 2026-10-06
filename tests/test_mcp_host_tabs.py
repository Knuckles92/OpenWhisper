"""The MCP page's This computer / host tabs, over a fake host."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QApplication

from services.agent_mcp.runtime import DEFAULT_PORT, ServerStatus
from services.remote_asr.client import RemoteEngineError, RemoteRequestError
from services.settings import SettingsKey, SettingsManager
from tests.test_mcp_settings_page import FakeServer
from tests.test_remote_mcp_link import FakeService, settle
from ui_qt.dialogs.settings_mcp import McpSettingsView

PAIRING = SimpleNamespace(host_name="jed")


class FakeRemoteService(FakeService):
    def __init__(self, pairing=PAIRING):
        super().__init__()
        self.pairing = pairing
        self.listeners = []

    def client_pairing(self):
        return self.pairing

    def add_listener(self, listener):
        self.listeners.append(listener)

    def remove_listener(self, listener):
        self.listeners.remove(listener)


def pump(view, cycles=6):
    for _ in range(cycles):
        QApplication.processEvents()


def tick(view):
    """What the view's 500 ms timer does once the host has answered."""
    settle(view._link)
    view._sync()
    pump(view)


def make_view(tmp_path, service=None, *, local_running=False, settings=None):
    if settings is None:
        settings = SettingsManager(str(tmp_path / "settings.json"))
        settings.save_all_settings({})
    view = McpSettingsView(settings)
    # Replace the process-wide listener with a fake one.
    view.local.server = FakeServer()
    if local_running:
        view.local.server.current = ServerStatus("running", "Ready", DEFAULT_PORT)
    if service is not None:
        view.bind(service)
    return view


def close(view):
    view.close()
    view.deleteLater()


class HistoryService(FakeRemoteService):
    def __init__(self, settings, pairing=PAIRING):
        super().__init__(pairing)
        self.settings = settings
        self.history_changes = []

    def set_share_history(self, enabled):
        self.history_changes.append(enabled)
        self.settings.save_setting(SettingsKey.REMOTE_CLIENT_HISTORY, enabled)
        for listener in self.listeners:
            listener("client")


@pytest.mark.parametrize("mode", ["classic", "omarchy"])
def test_mcp_history_sharing_is_local_and_available_above_both_tabs(
    tmp_path, monkeypatch, mode
):
    monkeypatch.setenv("OPENWHISPER_UI", mode)
    settings = SettingsManager(str(tmp_path / "settings.json"))
    preferences = {
        SettingsKey.REMOTE_RECORDS_LOCATION: "local",
        SettingsKey.REMOTE_HOST_KEEP_RECORDS: False,
        SettingsKey.REMOTE_HOST_MANAGE_MCP: False,
        SettingsKey.MCP_ENABLED: False,
    }
    settings.save_all_settings(preferences)
    service = HistoryService(settings)
    service.error = RemoteRequestError("Not allowed", code="forbidden")
    view = make_view(tmp_path, service, settings=settings)
    try:
        view.resize(520, 740)
        view.show()
        pump(view)
        tile = view.share_history_tile
        assert tile.isVisible() and tile.isEnabled()
        assert not tile.checkbox.isChecked()
        assert tile.minimumSizeHint().width() <= 520
        tile.checkbox.click()
        assert settings.get(SettingsKey.REMOTE_CLIENT_HISTORY) is True
        view.tabs.buttons[1].click()
        tick(view)
        assert view.gate.isVisible()
        assert tile.isVisible() and tile.isEnabled() and tile.checkbox.isChecked()
        tile.checkbox.click()
        assert settings.get(SettingsKey.REMOTE_CLIENT_HISTORY) is False
        assert service.history_changes == [True, False]
        for key, value in preferences.items():
            assert settings.get(key) == value
        assert all(op != "mcp_configure" for op, _ in service.calls)
        assert not view.local.server.starts
    finally:
        close(view)


def test_history_sharing_preserves_existing_consent_and_refreshes_without_repairing(tmp_path):
    settings = SettingsManager(str(tmp_path / "settings.json"))
    settings.save_all_settings({SettingsKey.REMOTE_CLIENT_HISTORY: True})
    service = HistoryService(settings)
    view = make_view(tmp_path, service, settings=settings)
    try:
        assert view.share_history_tile.checkbox.isChecked()
        assert service.history_changes == []
        settings.save_setting(SettingsKey.REMOTE_CLIENT_HISTORY, False)
        view._on_service_event("client")
        assert not view.share_history_tile.checkbox.isChecked()
        assert service.history_changes == []
        service.pairing = None
        view._on_service_event("client")
        assert not view.share_history_tile.isEnabled()
        assert "Pair a host in Remote engine" in view.share_history_tile.description_label.text()
        service.pairing = PAIRING
        view._on_service_event("client")
        assert view.share_history_tile.isEnabled()
        assert settings.get(SettingsKey.REMOTE_CLIENT_HISTORY) is False
    finally:
        close(view)


def test_a_computer_with_no_host_gets_no_tabs(tmp_path):
    view = make_view(tmp_path, FakeRemoteService(pairing=None))
    try:
        view.show()
        pump(view)
        assert view.tabs is None and view.local.isVisible()
        assert not view.host_area.isVisible()
    finally:
        close(view)


def test_pairing_adds_tabs_and_unpairing_removes_them(tmp_path):
    service = FakeRemoteService(pairing=None)
    view = make_view(tmp_path, service)
    try:
        view.show()
        service.pairing = PAIRING
        view._on_service_event("client")
        pump(view)
        assert [b.text() for b in view.tabs.buttons] == ["This computer", "jed"]
        service.pairing = None
        view._on_service_event("client")
        pump(view)
        assert view.tabs is None and view.local.isVisible() and view._link is None
    finally:
        close(view)


def test_the_host_tab_manages_the_host_not_this_computer(tmp_path):
    service = FakeRemoteService()
    view = make_view(tmp_path, service)
    try:
        view.show()
        view.tabs.buttons[1].click()
        tick(view)
        page = view.host_page
        assert page.isVisible() and not view.local.isVisible() and not view.gate.isVisible()
        assert "jed" in page.status_label.text()
        page.enabled.setChecked(True)
        tick(view)
        assert service.state["enabled"] is True and service.state["state"] == "running"
        assert page.token.text() == "tok"
        # Nothing touched this computer's own MCP.
        assert view.local.server.starts == [] and view.local.server.current.state == "stopped"
        page.permission_checks[SettingsKey.MCP_RETITLE_MEETINGS].setChecked(True)
        settle(view._link)
        assert service.state["permissions"]["retitle_meetings"] is True
    finally:
        close(view)


def test_host_setup_names_the_host_and_points_at_its_tailscale_address(tmp_path):
    service = FakeRemoteService()
    service.state.update(
        enabled=True, state="running", token="tok", tailscale=True,
        remote_url="http://100.64.0.9:8767/mcp",
    )
    view = make_view(tmp_path, service)
    try:
        view.show()
        view.tabs.buttons[1].click()
        tick(view)
        page = view.host_page
        # The assistant lives on this computer, so it needs the host's Tailscale address.
        assert page.agent_location.currentIndex() == 0
        assert page.url.text() == "http://100.64.0.9:8767/mcp"
        assert "Tailscale" in page.url_label.text()
        page.agent_location.setCurrentIndex(1)
        assert page.url.text() == "http://127.0.0.1:8767/mcp"
        assert "jed" in page.setup_text.toPlainText()
        assert "jed" in page.url_label.text()
    finally:
        close(view)


@pytest.mark.parametrize("error, shown", [
    (RemoteRequestError("jed isn't letting paired computers", code="forbidden"),
     "hasn't allowed this yet"),
    (RemoteRequestError("Update", code="unsupported"), "needs an update"),
    (RemoteEngineError("Couldn't reach jed."), "Can't reach jed"),
])
def test_a_host_that_cant_be_managed_shows_why_instead_of_controls(tmp_path, error, shown):
    service = FakeRemoteService()
    service.error = error
    view = make_view(tmp_path, service)
    try:
        view.show()
        view.tabs.buttons[1].click()
        tick(view)
        assert view.gate.isVisible() and shown in view.gate.title.text()
        assert view.host_page is None or not view.host_page.isVisible()
        if "allowed" in shown:
            assert "Allow paired computers to manage MCP" in view.gate.body.text()
        # The owner fixes it; "Check again" finds out without reopening Settings.
        service.error = None
        view.gate.button.click()
        tick(view)
        assert not view.gate.isVisible() and view.host_page.isVisible()
    finally:
        close(view)


def test_tabs_report_each_sides_mcp_status(tmp_path):
    service = FakeRemoteService()
    service.state.update(enabled=True, state="running", token="tok")
    view = make_view(tmp_path, service, local_running=False)
    try:
        view.show()
        settle(view._link)
        view._sync()
        details = [button._detail.text() for button in view.tabs.buttons]
        assert details == ["MCP off", "Host · MCP on"]
        service.error = RemoteRequestError("no", code="forbidden")
        view._link.refresh_now()
        settle(view._link)
        view._sync()
        assert view.tabs.buttons[1]._detail.text() == "Host · not allowed"
    finally:
        close(view)


def test_it_opens_on_the_host_when_only_the_hosts_mcp_is_running(tmp_path):
    service = FakeRemoteService()
    service.state.update(enabled=True, state="running", token="tok")
    view = make_view(tmp_path, service)
    try:
        view.show()
        tick(view)
        assert view.tabs.currentIndex() == 1 and view.host_area.isVisible()
    finally:
        close(view)


@pytest.mark.parametrize("local_running, host_running", [(True, True), (False, False)])
def test_it_stays_on_this_computer_unless_only_the_host_is_running(
    tmp_path, local_running, host_running
):
    service = FakeRemoteService()
    if host_running:
        service.state.update(enabled=True, state="running", token="tok")
    view = make_view(tmp_path, service, local_running=local_running)
    try:
        view.show()
        tick(view)
        assert view.tabs.currentIndex() == 0 and view.local.isVisible()
    finally:
        close(view)


def test_a_choice_to_stay_is_respected(tmp_path):
    service = FakeRemoteService()
    service.state.update(enabled=True, state="running", token="tok")
    view = make_view(tmp_path, service)
    try:
        view.show()
        view.tabs.buttons[0].click()
        tick(view)
        view._link.refresh_now()
        tick(view)
        assert view.tabs.currentIndex() == 0 and view.local.isVisible()
    finally:
        close(view)


def test_a_change_the_host_refuses_is_reported_on_the_host_page(tmp_path):
    service = FakeRemoteService()
    service.state.update(enabled=True, state="running", token="tok")
    view = make_view(tmp_path, service)
    try:
        view.show()
        view.tabs.buttons[1].click()
        tick(view)
        service.error = RemoteRequestError("Turn MCP off to change its port.", code="busy")
        view.host_page.permission_checks[SettingsKey.MCP_RETITLE_MEETINGS].setChecked(True)
        settle(view._link)
        view.host_page.refresh()
        assert "Turn MCP off" in view.host_page.notice.text()
        assert not view.host_page.permission_checks[SettingsKey.MCP_RETITLE_MEETINGS].isChecked()
    finally:
        close(view)

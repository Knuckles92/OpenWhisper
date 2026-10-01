"""Settings controls must reflect the listener, not merely the saved preference."""

import json

from PyQt6.QtWidgets import QApplication, QLineEdit

from services.agent_mcp.runtime import DEFAULT_PORT, McpRuntime, ServerStatus
from services.settings import SettingsKey, SettingsManager
from ui_qt.dialogs.settings_mcp import McpSettingsPage

TOKEN = "test-token-" + "a" * 32


class FakeServer:
    def __init__(self):
        self.current = ServerStatus("stopped", "MCP is off.", DEFAULT_PORT)
        self.starts = []

    def status(self):
        return self.current

    def start(self, port):
        self.starts.append(port)
        self.current = ServerStatus("starting", "Starting MCP…", port)

    def stop(self):
        self.current = ServerStatus("stopped", "MCP is off.", self.current.port)

    def token(self):
        return TOKEN if self.current.state == "running" else ""


def make_page(tmp_path):
    settings = SettingsManager(str(tmp_path / "settings.json"))
    settings.save_all_settings({})
    server = FakeServer()
    return McpSettingsPage(settings, server=server), settings, server


def test_enable_status_setup_copy_and_stop(tmp_path):
    page, settings, server = make_page(tmp_path)
    try:
        assert not page.enabled.isChecked()
        assert not page.connection.isEnabled()
        assert not server.starts
        page.port.setValue(9123)
        page.enabled.setChecked(True)
        assert settings.get(SettingsKey.MCP_ENABLED) is True
        assert server.starts == [9123]
        assert "Starting" in page.status_label.text()
        assert not page.connection.isEnabled()

        server.current = ServerStatus("stopping", "Stopping MCP…", 9123)
        page.refresh()
        assert not page.enable_tile.isEnabled()

        server.current = ServerStatus("running", "Ready for agent connections.", 9123)
        page.refresh()
        assert page.connection.isEnabled()
        assert "Running" in page.status_label.text()
        assert page.enable_tile.isEnabled()
        assert page.url.text() == "http://127.0.0.1:9123/mcp"
        assert page.token.echoMode() == QLineEdit.EchoMode.Password
        assert TOKEN not in page.setup_text.toPlainText()
        page.copy_setup.click()
        assert "get_status" in QApplication.clipboard().text()
        page.setup_kind.setCurrentIndex(1)
        assert "claude mcp add" in page.setup_text.toPlainText()
        assert "<PASTE_TOKEN>" in page.setup_text.toPlainText()
        page.setup_kind.setCurrentIndex(2)
        config = json.loads(page.setup_text.toPlainText())
        assert config["mcpServers"]["openwhisper"]["url"] == page.url.text()
        page._copy(server.token())
        assert QApplication.clipboard().text() == TOKEN
        assert TOKEN not in (tmp_path / "settings.json").read_text()

        page.enabled.setChecked(False)
        assert settings.get(SettingsKey.MCP_ENABLED) is False
        assert not page.connection.isEnabled()
        assert not page.token.text()
        assert page.port.isEnabled()
        page._copy("should not copy when stopped")
        assert QApplication.clipboard().text() == TOKEN
    finally:
        page.close()
        page.deleteLater()


def test_failed_start_can_retry_and_failed_save_rolls_back(tmp_path, monkeypatch):
    page, settings, server = make_page(tmp_path)
    try:
        page.enabled.setChecked(True)
        server.current = ServerStatus("error", "Port is occupied.", DEFAULT_PORT)
        page.refresh()
        assert "Port is occupied" in page.status_label.text()
        assert not page.retry.isHidden()
        assert not page.connection.isEnabled()
        page.retry.click()
        assert len(server.starts) == 2
        server.stop()
        page.enabled.setChecked(False)

        def fail(*args):
            raise OSError("read only")

        monkeypatch.setattr(settings, "save_setting", fail)
        page.enabled.setChecked(True)
        assert not page.enabled.isChecked()
        assert "Could not save" in page.notice.text()
        assert len(server.starts) == 2
    finally:
        page.close()
        page.deleteLater()


def test_restore_is_opt_in_and_validates_port(tmp_path, monkeypatch):
    settings = SettingsManager(str(tmp_path / "settings.json"))
    settings.save_all_settings({})
    server = McpRuntime()
    starts = []
    monkeypatch.setattr(server, "start", starts.append)
    server.restore(settings)
    assert starts == []
    settings.update_settings(
        {SettingsKey.MCP_ENABLED: True, SettingsKey.MCP_PORT: "bad"}
    )
    server.restore(settings)
    assert starts == [DEFAULT_PORT]


def test_permissions_are_individual_persistent_and_configurable_while_off(tmp_path):
    page, settings, server = make_page(tmp_path)
    try:
        assert not any(check.isChecked() for check in page.permission_checks.values())
        assert not any(check.isChecked() for check in page.setting_checks.values())
        assert not page.settings_permissions.isEnabled()
        page.permission_checks[SettingsKey.MCP_RETITLE_TRANSCRIPTIONS].setChecked(True)
        page.permission_checks[SettingsKey.MCP_SETTINGS_ACCESS].setChecked(True)
        page.setting_checks[SettingsKey.AUTO_PASTE].setChecked(True)
        page.setting_checks[SettingsKey.UI_THEME].setChecked(True)
        assert not server.starts
        assert settings.get(SettingsKey.MCP_RETITLE_TRANSCRIPTIONS) is True
        assert settings.get(SettingsKey.MCP_RETITLE_MEETINGS) is None
        assert settings.get(SettingsKey.MCP_WRITABLE_SETTINGS) == {
            SettingsKey.AUTO_PASTE: True,
            SettingsKey.UI_THEME: True,
        }
        page.setting_checks[SettingsKey.AUTO_PASTE].setChecked(False)
        assert (
            settings.get(SettingsKey.MCP_WRITABLE_SETTINGS)[SettingsKey.UI_THEME]
            is True
        )
        page.permission_checks[SettingsKey.MCP_SETTINGS_ACCESS].setChecked(False)
        assert not page.settings_permissions.isEnabled()
        assert page.setting_checks[SettingsKey.UI_THEME].isChecked()
        page.permission_checks[SettingsKey.MCP_SETTINGS_ACCESS].setChecked(True)
        assert page.settings_permissions.isEnabled()
    finally:
        page.close()
        page.deleteLater()


def test_failed_permission_save_rolls_back_ui_and_preserves_other_grants(
    tmp_path, monkeypatch
):
    page, settings, _ = make_page(tmp_path)
    try:
        page.permission_checks[SettingsKey.MCP_SETTINGS_ACCESS].setChecked(True)
        page.setting_checks[SettingsKey.AUTO_PASTE].setChecked(True)

        def fail(*args):
            raise OSError("private-marker")

        monkeypatch.setattr(settings, "mutate_settings", fail)
        page.setting_checks[SettingsKey.UI_THEME].setChecked(True)
        assert not page.setting_checks[SettingsKey.UI_THEME].isChecked()
        assert page.setting_checks[SettingsKey.AUTO_PASTE].isChecked()
        assert "Could not save" in page.notice.text()
        assert "private-marker" not in page.notice.text()
        page.permission_checks[SettingsKey.MCP_RETITLE_MEETINGS].setChecked(True)
        assert not page.permission_checks[SettingsKey.MCP_RETITLE_MEETINGS].isChecked()
    finally:
        page.close()
        page.deleteLater()

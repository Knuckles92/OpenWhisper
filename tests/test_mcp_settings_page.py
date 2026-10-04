"""Settings controls must reflect the listener, not merely the saved preference."""

import json

import pytest
from PyQt6.QtCore import QPoint
from PyQt6.QtWidgets import QAbstractButton, QApplication, QLineEdit, QScrollArea

from services.agent_mcp.controls import SETTING_CONTROLS
from services.agent_mcp.runtime import DEFAULT_PORT, McpRuntime, ServerStatus
from services.settings import SettingsKey, SettingsManager
from ui_qt.dialogs.settings_mcp import McpSettingsPage

TOKEN = "test-token-" + "a" * 32


class FakeServer:
    def __init__(self):
        self.current = ServerStatus("stopped", "MCP is off.", DEFAULT_PORT)
        self.starts = []
        self.tailscale_starts = []

    def status(self):
        return self.current

    def start(self, port, *, tailscale=False):
        self.starts.append(port)
        self.tailscale_starts.append(tailscale)
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


def test_tailscale_setup_uses_host_url_and_separate_token(tmp_path):
    page, settings, server = make_page(tmp_path)
    try:
        assert not page.tailscale_enabled.isChecked()
        page.tailscale_enabled.setChecked(True)
        assert settings.get(SettingsKey.MCP_TAILSCALE_ENABLED) is True
        page.enabled.setChecked(True)
        assert server.tailscale_starts == [True]
        remote_url = "http://100.82.22.3:8767/mcp"
        server.current = ServerStatus("running", "Ready", DEFAULT_PORT, remote_url)
        page.refresh()
        assert not page.tailscale_tile.isEnabled()
        assert page.url.text() == remote_url
        assert "same Tailscale network" in page.setup_text.toPlainText()
        assert "on the computer running OpenWhisper" in page.setup_text.toPlainText()
        assert TOKEN not in page.setup_text.toPlainText()
        page.copy_setup.click()
        assert remote_url in QApplication.clipboard().text()
        page.setup_kind.setCurrentIndex(2)
        assert (
            json.loads(page.setup_text.toPlainText())["mcpServers"]["openwhisper"][
                "url"
            ]
            == remote_url
        )
        page.agent_location.setCurrentIndex(0)
        assert page.url.text() == server.current.url
        page.setup_kind.setCurrentIndex(0)
        assert (
            "Do not connect to or enable a different" in page.setup_text.toPlainText()
        )
    finally:
        page.close()
        page.deleteLater()


def test_other_computer_setup_is_not_copyable_until_access_enabled(tmp_path):
    page, _, server = make_page(tmp_path)
    try:
        server.current = ServerStatus("running", "Ready", DEFAULT_PORT)
        page.agent_location.setCurrentIndex(1)
        page.refresh()
        assert not page.copy_setup.isEnabled()
        assert not page.copy_url.isEnabled()
        assert "Allow agents over Tailscale" in page.setup_text.toPlainText()
        assert "Register a server" not in page.setup_text.toPlainText()
    finally:
        page.close()
        page.deleteLater()


def test_disclosures_keep_connection_formats_and_copy_actions_available(tmp_path):
    page, _, server = make_page(tmp_path)
    page.show()
    try:
        assert not page.advanced.body.isVisible()
        page.advanced.toggle.click()
        assert page.port.isEnabled()
        assert page.tailscale_enabled.isEnabled()
        page.enabled.click()
        server.current = ServerStatus("running", "Ready", DEFAULT_PORT)
        page.refresh()
        assert not page.port.isEnabled()
        assert not page.tailscale_enabled.isEnabled()
        page.copy_url.click()
        assert QApplication.clipboard().text() == server.current.url
        for index, label in enumerate(
            ("setup prompt", "Claude Code command", "Cursor JSON")
        ):
            page.setup_kind.buttons[index].click()
            page.preview.toggle.setChecked(True)
            assert page.setup_text.isVisible()
            assert label in page.copy_setup.text()
            assert TOKEN not in page.setup_text.toPlainText()
            page.copy_setup.click()
            assert QApplication.clipboard().text() == page.setup_text.toPlainText()
        page.copy_token.click()
        assert QApplication.clipboard().text() == TOKEN
        assert page.notice.isVisible()
        assert page.notice.text() == "Copied to clipboard."
    finally:
        page.close()
        page.deleteLater()


@pytest.mark.parametrize("ui_mode", ["classic", "omarchy"])
@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("font_percent", [100, 130])
def test_split_layout_and_individual_grants_survive_narrow_windows(
    tmp_path, monkeypatch, ui_mode, theme, font_percent
):
    from ui_qt.utils.font_scale import (
        apply_ui_font_scale,
        current_ui_font_scale_percent,
    )
    from ui_qt.utils.palette import current_palette, set_current_palette
    from ui_qt.utils.theme_manager import ThemeManager

    monkeypatch.setenv("OPENWHISPER_UI", ui_mode)
    app = QApplication.instance()
    previous_scale, previous_palette = (
        current_ui_font_scale_percent(),
        current_palette(),
    )
    previous_sheet = app.styleSheet()
    manager = ThemeManager(theme)
    page, settings, server = make_page(tmp_path)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(page)
    try:
        apply_ui_font_scale(font_percent, app=app, theme_manager=manager)
        scroll.resize(1300, 900)
        scroll.show()
        app.processEvents()
        assert page.permissions_card.x() > page.connection_card.x()
        assert not page.preview.body.isVisible()
        page.preferences.toggle.click()
        page.permission_checks[SettingsKey.MCP_SETTINGS_ACCESS].click()
        for group in page.setting_groups.values():
            group.toggle.click()
        assert set(page.setting_checks) == {control.key for control in SETTING_CONTROLS}
        assert all(
            check.isVisible() and check.isEnabled()
            for check in page.setting_checks.values()
        )
        page.setting_checks[SettingsKey.UI_THEME].click()
        assert "1 allowed to change" in page.permission_summary.text()
        page.advanced.toggle.click()
        for width in (480 if ui_mode == "omarchy" else 680, 1300):
            scroll.resize(width, 900)
            app.processEvents()
            assert scroll.horizontalScrollBar().maximum() == 0
            if width < 1000:
                assert page.permissions_card.y() > page.connection_card.y()
            else:
                assert page.permissions_card.x() > page.connection_card.x()
            for button in page.findChildren(QAbstractButton):
                if not button.isVisible():
                    continue
                position = button.mapTo(page, QPoint(0, 0))
                assert position.x() >= 0
                assert position.x() + button.width() <= page.width()
        assert settings.get(SettingsKey.MCP_WRITABLE_SETTINGS) == {
            SettingsKey.UI_THEME: True
        }
        assert not server.starts
        page.permission_checks[SettingsKey.MCP_SETTINGS_ACCESS].click()
        assert not page.setting_checks[SettingsKey.UI_THEME].isEnabled()
        assert page.setting_checks[SettingsKey.UI_THEME].isChecked()
    finally:
        scroll.close()
        scroll.deleteLater()
        apply_ui_font_scale(previous_scale, app=app)
        set_current_palette(previous_palette)
        app.setStyleSheet(previous_sheet)

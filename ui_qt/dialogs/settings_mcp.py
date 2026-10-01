"""Settings destination for the app-owned MCP server and agent onboarding."""

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from services.agent_mcp.controls import SETTING_CONTROLS, writable_settings
from services.agent_mcp.runtime import DEFAULT_PORT, runtime
from services.agent_mcp.setup import agent_prompt, claude_command, client_config
from services.settings import SettingsKey
from ui_qt.widgets import (
    Button,
    ElidingComboBox,
    FieldTile,
    NoWheelSpinBox,
    SettingTile,
    WrappedLabel,
)


class McpSettingsPage(QWidget):
    def __init__(self, settings, *, server=None, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.server = server if server is not None else runtime
        if server is None:
            self.server.bind_settings(settings)
        self._notice = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self.enable_tile = SettingTile(
            "Enable MCP",
            "Let agents on this computer search your saved dictations and meetings. "
            "Starts automatically with OpenWhisper when enabled; quitting the app stops access.",
        )
        self.enabled = self.enable_tile.checkbox
        self.enabled.setAccessibleName("Enable MCP")
        self.enabled.toggled.connect(self._toggle)
        layout.addWidget(self.enable_tile)

        status_row = QHBoxLayout()
        self.status_label = WrappedLabel("")
        self.status_label.setAccessibleName("MCP server status")
        status_row.addWidget(self.status_label, 1)
        self.retry = Button("Retry")
        self.retry.clicked.connect(self._start)
        status_row.addWidget(self.retry)
        layout.addLayout(status_row)

        self.port = NoWheelSpinBox()
        self.port.setRange(1, 65535)
        self.port.setKeyboardTracking(False)
        port = settings.get(SettingsKey.MCP_PORT, DEFAULT_PORT)
        self.port.setValue(
            port if type(port) is int and 1 <= port <= 65535 else DEFAULT_PORT
        )
        self.port.valueChanged.connect(self._port_changed)
        layout.addWidget(
            FieldTile(
                "Local port",
                "Turn MCP off to change its port. Reconnect your agent after changing it.",
                self.port,
                compact=True,
            )
        )

        self.connection = QWidget()
        connection_layout = QVBoxLayout(self.connection)
        connection_layout.setContentsMargins(0, 0, 0, 0)
        connection_layout.setSpacing(10)
        connection_layout.addWidget(QLabel("Connect your agent"))
        self.url = QLineEdit()
        self.url.setReadOnly(True)
        self.url.setAccessibleName("MCP server URL")
        self._copy_row(
            connection_layout,
            "Server URL",
            self.url,
            "Copy URL",
            lambda: self.url.text(),
        )

        self.token = QLineEdit()
        self.token.setReadOnly(True)
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setAccessibleName("MCP access token")
        self._copy_row(
            connection_layout,
            "Access token",
            self.token,
            "Copy token",
            self.server.token,
        )
        connection_layout.addWidget(
            WrappedLabel(
                "The token grants access to your saved history and the actions you allow below. It is saved in your system "
                "credential store. Share it only with agents you trust.",
            )
        )

        self.setup_kind = ElidingComboBox()
        self.setup_kind.addItems(
            ["Agent setup prompt", "Claude Code command", "Client JSON (Cursor)"]
        )
        self.setup_kind.setAccessibleName("Agent setup method")
        self.setup_kind.currentIndexChanged.connect(self._render_setup)
        connection_layout.addWidget(self.setup_kind)
        self.setup_text = QPlainTextEdit()
        self.setup_text.setReadOnly(True)
        self.setup_text.setMinimumHeight(160)
        self.setup_text.setMaximumHeight(210)
        self.setup_text.setAccessibleName("MCP setup instructions")
        connection_layout.addWidget(self.setup_text)
        setup_row = QHBoxLayout()
        setup_row.addWidget(
            WrappedLabel(
                "Paste the prompt into your agent, or use a manual option and replace "
                "<PASTE_TOKEN> with the access token above.",
            ),
            1,
        )
        self.copy_setup = Button("Copy setup")
        self.copy_setup.clicked.connect(
            lambda: self._copy(self.setup_text.toPlainText())
        )
        setup_row.addWidget(self.copy_setup)
        connection_layout.addLayout(setup_row)
        layout.addWidget(self.connection)

        layout.addWidget(QLabel("Agent permissions"))
        layout.addWidget(
            WrappedLabel(
                "Choose what connected agents may change. New permissions are off by default. "
                "Turning one off blocks future changes immediately."
            )
        )
        self.permission_checks = {}
        for key, title, description in (
            (
                SettingsKey.MCP_RETITLE_TRANSCRIPTIONS,
                "Retitle transcription history",
                "Change display titles while keeping the original text and source filename.",
            ),
            (
                SettingsKey.MCP_RETITLE_MEETINGS,
                "Retitle saved meetings",
                "Change titles of finished meetings saved on this computer.",
            ),
            (
                SettingsKey.MCP_SETTINGS_ACCESS,
                "Allow settings access",
                "Read supported preferences. Choose each preference agents may change below.",
            ),
        ):
            tile = SettingTile(title, description)
            check = tile.checkbox
            check.setAccessibleName(title)
            check.toggled.connect(
                lambda checked, key=key: self._save_permission(key, checked)
            )
            self.permission_checks[key] = check
            layout.addWidget(tile)

        self.settings_permissions = QWidget()
        permissions_layout = QVBoxLayout(self.settings_permissions)
        permissions_layout.setContentsMargins(0, 0, 0, 0)
        self.setting_checks = {}
        groups = {}
        for control in SETTING_CONTROLS:
            if control.group not in groups:
                permissions_layout.addWidget(QLabel(control.group))
                grid = QGridLayout()
                grid.setColumnStretch(0, 1)
                grid.setColumnStretch(1, 1)
                permissions_layout.addLayout(grid)
                groups[control.group] = (grid, 0)
            grid, index = groups[control.group]
            check = QCheckBox(control.label)
            check.setAccessibleName(f"Allow agent changes: {control.label}")
            check.setToolTip(control.effect)
            check.toggled.connect(
                lambda checked, key=control.key: self._save_setting_permission(
                    key, checked
                )
            )
            grid.addWidget(check, index // 2, index % 2)
            groups[control.group] = (grid, index + 1)
            self.setting_checks[control.key] = check
        layout.addWidget(self.settings_permissions)

        layout.addWidget(
            WrappedLabel(
                "Agents can read saved text and meeting insights, and use the permissions selected above. "
                "They cannot start recordings, delete history, edit transcript text, or change their permissions. "
                "A cloud-powered agent may send retrieved text to its provider. "
                "The local URL works only for agents running on this computer.",
            )
        )
        self.notice = WrappedLabel("")
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.notice)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.refresh)
        self.refresh()

    def _copy_row(self, layout, title, field, button_text, value):
        layout.addWidget(QLabel(title))
        row = QHBoxLayout()
        row.addWidget(field, 1)
        button = Button(button_text)
        button.clicked.connect(lambda: self._copy(value()))
        row.addWidget(button)
        layout.addLayout(row)

    def _copy(self, text):
        if self.server.status().state != "running" or not text:
            return
        QApplication.clipboard().setText(text)
        self._notice = "Copied to clipboard."
        self.notice.setText(self._notice)

    def _toggle(self, checked):
        try:
            self.settings.save_setting(SettingsKey.MCP_ENABLED, checked)
        except Exception:
            self._notice = "Could not save the MCP setting. Try again."
        else:
            self._notice = ""
            if checked:
                self._start()
            else:
                self.server.stop()
        self.refresh()

    def _start(self):
        self.server.start(self.port.value())
        self.refresh()

    def _save_permission(self, key, checked):
        try:
            self.settings.save_setting(key, checked)
            self._notice = ""
        except Exception:
            self._notice = "Could not save the agent permission. Try again."
        self.refresh()

    def _save_setting_permission(self, key, checked):
        def commit(saved):
            permissions = saved.get(SettingsKey.MCP_WRITABLE_SETTINGS, {})
            permissions = dict(permissions) if isinstance(permissions, dict) else {}
            permissions[key] = checked
            saved[SettingsKey.MCP_WRITABLE_SETTINGS] = permissions

        try:
            self.settings.mutate_settings(commit)
            self._notice = ""
        except Exception:
            self._notice = "Could not save the setting permission. Try again."
        self.refresh()

    def _port_changed(self, value):
        try:
            self.settings.save_setting(SettingsKey.MCP_PORT, value)
            self._notice = ""
        except Exception:
            self._notice = "Could not save the MCP port. Try again."
            self.port.blockSignals(True)
            self.port.setValue(self.settings.get(SettingsKey.MCP_PORT, DEFAULT_PORT))
            self.port.blockSignals(False)
        self.refresh()

    def _render_setup(self):
        url = self.url.text()
        options = (
            agent_prompt(url),
            claude_command(url),
            client_config(url, "<PASTE_TOKEN>"),
        )
        self.setup_text.setPlainText(options[self.setup_kind.currentIndex()])

    def refresh(self):
        status = self.server.status()
        preferences = self.settings.load_all_settings()
        saved = preferences.get(SettingsKey.MCP_ENABLED, False) is True
        for key, check in self.permission_checks.items():
            check.blockSignals(True)
            check.setChecked(preferences.get(key) is True)
            check.blockSignals(False)
        granted = writable_settings(preferences)
        for key, check in self.setting_checks.items():
            check.blockSignals(True)
            check.setChecked(key in granted)
            check.blockSignals(False)
        self.settings_permissions.setEnabled(
            preferences.get(SettingsKey.MCP_SETTINGS_ACCESS) is True
        )
        self.enabled.blockSignals(True)
        self.enabled.setChecked(saved)
        self.enabled.blockSignals(False)
        self.enable_tile.setEnabled(status.state != "stopping")
        self.port.setEnabled(status.state in {"stopped", "error"} and not saved)
        self.retry.setVisible(status.state == "error" and saved)
        labels = {
            "stopped": "Off",
            "starting": "Starting",
            "running": "Running",
            "stopping": "Stopping",
            "error": "Could not start",
        }
        self.status_label.setText(f"{labels[status.state]} · {status.message}")
        ready = status.state == "running"
        self.connection.setEnabled(ready)
        url = status.url if ready else "Enable MCP to get your connection URL"
        if self.url.text() != url:
            self.url.setText(url)
            self._render_setup()
        self.token.setText(self.server.token() if ready else "")
        if not ready:
            self.setup_text.setPlainText(
                "Connection instructions appear when MCP is running."
            )
        self.notice.setText(self._notice)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self.timer.start()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

"""Settings → Automate → API & MCP: the app-owned MCP server, agent onboarding,
and this computer's live API, MCP Events and Claude Code channel."""

import html
import itertools
import threading
from typing import Optional

from PyQt6.QtCore import QEvent, QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from services.agent_mcp.controls import SETTING_CONTROLS, writable_settings
from services.agent_mcp.runtime import DEFAULT_PORT, runtime
from services.agent_mcp.setup import (
    TOKEN_PLACEHOLDER,
    agent_prompt,
    chatgpt_config,
    claude_command,
    client_config,
)
from services.settings import SettingsKey
from ui_qt.widgets.settings_switch import SettingsSwitch
from ui_qt.utils.font_scale import current_ui_font_scale
from ui_qt.utils.icons import design_icon
from ui_qt.utils.restyle import set_style_property
from ui_qt.widgets import (
    Button,
    FieldTile,
    NoWheelSpinBox,
    SettingTile,
    WrappedLabel,
)
from ui_qt.widgets.engine_field import EngineStatus, StatusDot
from ui_qt.widgets.setting_tile import TileBase
from ui_qt.widgets.segmented_bar import SegmentButton, SegmentedBar

# Stands in for the access token in the on-screen preview; copies get the real one.
_HIDDEN_TOKEN = "•" * 12


class _ServiceRelay(QObject):
    """Brings remote engine changes to the Qt thread for whichever MCP views exist then.

    It lives for the session, so the service's threads never hold or emit on
    a view. Holding one let a thread drop the last reference and delete the
    view off the Qt thread, and emitting on one raced Settings closing; both
    crash the process. Qt drops a deleted view's connection.
    """

    service_event = pyqtSignal(int, str)


_relay: Optional[_ServiceRelay] = None
#: View tokens, unique across views because they share the relay.
_tokens = itertools.count(1)


def _service_relay() -> _ServiceRelay:
    global _relay
    if _relay is None:
        _relay = _ServiceRelay()
    return _relay


class _ActionButton(Button):
    """Respect the styled text height even inside a compressed settings card."""

    def _refresh_size(self):
        super()._refresh_size()
        self.setMinimumHeight(max(self._base_min_height, self.sizeHint().height()))

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (
            QEvent.Type.FontChange,
            QEvent.Type.StyleChange,
        ) and hasattr(self, "_base_min_height"):
            self._refresh_size()


# The chooser lives in ui_qt/widgets so other pages can use it; these names
# are kept for the MCP page and its tests.
_ChoiceButton = SegmentButton
_ChoiceBar = SegmentedBar


class _AdaptiveRow(QWidget):
    """Side-by-side while the widgets fit, stacked when they do not.

    ``items`` are ``(widget, stretch)`` pairs; ``None`` is a flexible gap that
    only exists in the side-by-side arrangement.
    """

    def __init__(self, items, *, spacing=8, parent=None):
        super().__init__(parent)
        self.setObjectName("mcpAdaptiveRow")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._items = [item for item in items if item is not None]
        self._stretches = []
        layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(spacing)
        for item in items:
            if item is None:
                layout.addStretch(1)
                self._stretches.append(1)
            else:
                layout.addWidget(item[0], item[1])
                self._stretches.append(item[1])
        self._reflow_timer = QTimer(self)
        self._reflow_timer.setSingleShot(True)
        self._reflow_timer.timeout.connect(self.reflow)

    def reflow(self):
        layout = self.layout()
        widgets = [widget for widget, _ in self._items if not widget.isHidden()]
        required = sum(widget.sizeHint().width() for widget in widgets)
        required += layout.spacing() * max(0, len(widgets) - 1)
        narrow = self.width() < required
        layout.setDirection(
            QBoxLayout.Direction.TopToBottom
            if narrow
            else QBoxLayout.Direction.LeftToRight
        )
        for index, stretch in enumerate(self._stretches):
            layout.setStretch(index, 0 if narrow else stretch)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.reflow()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (
            QEvent.Type.FontChange,
            QEvent.Type.StyleChange,
        ) and hasattr(self, "_reflow_timer"):
            self._reflow_timer.start(0)


class _Disclosure(QWidget):
    """A collapsible section.

    ``inline`` leaves the toggle for the caller to place beside other actions;
    the section then occupies no space until it is expanded. ``trailing`` puts
    a short status label beside the toggle.
    """

    def __init__(self, title, *, inline=False, trailing=None, parent=None):
        super().__init__(parent)
        self.setObjectName("mcpDisclosure")
        self._inline = inline
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.toggle = QToolButton()
        self.toggle.setObjectName("mcpDisclosureToggle")
        self.toggle.setText(title)
        self.toggle.setAccessibleName(title)
        self.toggle.setCheckable(True)
        self.toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setSizePolicy(
            QSizePolicy.Policy.Preferred
            if inline or trailing is not None
            else QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self.toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        if trailing is not None:
            layout.addWidget(_AdaptiveRow([(self.toggle, 0), None, (trailing, 0)]))
        elif not inline:
            layout.addWidget(self.toggle)
        self.body = QWidget()
        self.body.setObjectName("mcpDisclosureBody")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(10)
        layout.addWidget(self.body)
        self.body.hide()
        if inline:
            self.hide()
        self.toggle.toggled.connect(self._expand)

    def _expand(self, expanded):
        self.toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self.body.setVisible(expanded)
        if self._inline:
            self.setVisible(expanded)


class _CopyField(QWidget):
    """Keep copy actions readable and stack them when the field needs more room."""

    def __init__(self, field, button):
        super().__init__()
        self.setObjectName("mcpCopyField")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.field = field
        self.button = button
        field.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(field, 1)
        layout.addWidget(button)
        self._reflow_timer = QTimer(self)
        self._reflow_timer.setSingleShot(True)
        self._reflow_timer.timeout.connect(self._reflow)

    def _reflow(self):
        self.field.setMinimumHeight(self.field.sizeHint().height())
        narrow = (
            self.width()
            < round(180 * current_ui_font_scale()) + self.button.sizeHint().width() + 8
        )
        self.layout().setDirection(
            QBoxLayout.Direction.TopToBottom
            if narrow
            else QBoxLayout.Direction.LeftToRight
        )
        self.layout().setStretch(0, 0 if narrow else 1)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (
            QEvent.Type.FontChange,
            QEvent.Type.StyleChange,
        ) and hasattr(self, "_reflow_timer"):
            self._reflow_timer.start(0)


class _PermissionRow(QFrame):
    def __init__(self, title, description, icon):
        super().__init__()
        self.setObjectName("mcpPermissionRow")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(12)
        mark = QLabel()
        mark.setObjectName("settingsTileIcon")
        mark.setPixmap(design_icon(icon).pixmap(20, 20))
        layout.addWidget(mark, alignment=Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(2)
        title_label = WrappedLabel(title)
        title_label.setObjectName("settingsTileTitle")
        text.addWidget(title_label)
        detail = WrappedLabel(description)
        detail.setObjectName("settingsTileDescription")
        text.addWidget(detail)
        layout.addLayout(text, 1)
        self.checkbox = SettingsSwitch()
        self.checkbox.setAccessibleName(title)
        self.checkbox.setAccessibleDescription(description)
        layout.addWidget(self.checkbox)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.checkbox.isEnabled():
            self.checkbox.toggle()
            event.accept()
        else:
            super().mousePressEvent(event)


class McpSettingsPage(QWidget):
    """The MCP controls for one computer.

    By default that is this one. With ``host_name`` it is a paired host's, read
    and changed through ``settings``/``server`` stand-ins (services/remote_asr/
    mcp_link.py) and ``link``, which reports changes the host refused. Nothing
    else differs except which "where is your assistant" choice means the
    Tailscale address: for a host that is "this computer", since the host is
    the other machine.
    """

    def __init__(self, settings, *, server=None, parent=None, host_name=None, link=None):
        super().__init__(parent)
        self.setObjectName("mcpSettingsPage")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.settings = settings
        self.server = server if server is not None else runtime
        if server is None:
            self.server.bind_settings(settings)
        self.host_name = host_name
        self.link = link
        # Index of the chooser option that uses the Tailscale address.
        self._remote_index = 0 if host_name else 1
        self._local_index = 1 - self._remote_index
        self._notice = ""
        self._wide = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        self.enable_tile = QFrame()
        self.enable_tile.setObjectName("mcpServerBar")
        self.enable_tile.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        status_row = QHBoxLayout(self.enable_tile)
        status_row.setContentsMargins(18, 14, 18, 14)
        status_row.setSpacing(12)
        self.status_dot = StatusDot(diameter=10)
        status_row.addWidget(self.status_dot)
        self.status_label = WrappedLabel("")
        self.status_label.setObjectName("mcpStatusText")
        self.status_label.setTextFormat(Qt.TextFormat.RichText)
        self.status_label.setAccessibleName("MCP server status")
        status_row.addWidget(self.status_label, 1)
        self.retry = _ActionButton("Retry")
        self.retry.set_base_minimum_size(70, 34)
        self.retry.clicked.connect(self._start)
        status_row.addWidget(self.retry)
        self.enabled = SettingsSwitch()
        self.enabled.setAccessibleName("Enable MCP")
        self.enabled.setToolTip(
            "Starts with OpenWhisper when enabled. Quitting the app stops access."
        )
        self.enabled.toggled.connect(self._toggle)
        status_row.addWidget(self.enabled)
        layout.addWidget(self.enable_tile)

        self.columns = QGridLayout()
        self.columns.setContentsMargins(0, 0, 0, 0)
        self.columns.setSpacing(14)
        layout.addLayout(self.columns)
        self.connection_card, connection_layout = self._card(
            "Connection",
            "Connect your assistant in two simple steps.",
            "server-blue.svg",
        )
        self.permissions_card, permissions_layout = self._card(
            "Permissions",
            "Read-only by default. Optional changes are off until you allow them.",
            "key-blue.svg",
        )

        self.connection = QWidget()
        self.connection.setObjectName("mcpConnection")
        setup_layout = QVBoxLayout(self.connection)
        setup_layout.setContentsMargins(0, 0, 0, 0)
        setup_layout.setSpacing(8)
        setup_layout.addWidget(
            self._label("1. Where is your assistant?", "mcpSectionLabel")
        )
        self.agent_location = _ChoiceBar(
            (
                ("This computer", ""),
                (f"On {host_name}", ""),
            )
            if host_name
            else (
                ("This computer", ""),
                ("Another computer", ""),
            )
        )
        self.agent_location.setAccessibleName("Agent computer")
        tailscale_on = settings.get(SettingsKey.MCP_TAILSCALE_ENABLED, False) is True
        self.agent_location.setCurrentIndex(
            self._remote_index if tailscale_on or host_name else self._local_index
        )
        self.agent_location.currentIndexChanged.connect(self._render_setup)
        setup_layout.addWidget(self.agent_location)
        address_layout = QVBoxLayout()
        address_layout.setSpacing(4)
        self.url_label = self._label("MCP server address", "mcpFieldLabel")
        address_layout.addWidget(self.url_label)
        self.url = QLineEdit()
        self.url.setReadOnly(True)
        self.url.setAccessibleName("MCP server address")
        self.copy_url = self._copy_row(
            address_layout,
            "",
            self.url,
            "Copy address",
            lambda: self.url.text(),
        )
        self.copy_url.setAccessibleName("Copy MCP server address")
        setup_layout.addLayout(address_layout)
        setup_layout.addSpacing(8)
        setup_layout.addWidget(
            self._label("2. Set up your assistant", "mcpSectionLabel")
        )
        self.setup_kind = _ChoiceBar(
            (
                ("Setup prompt", ""),
                ("Claude Code", ""),
                ("Cursor", ""),
                ("ChatGPT", ""),
            )
        )
        self.setup_kind.setAccessibleName("Agent setup method")
        self.setup_kind.currentIndexChanged.connect(self._render_setup)
        setup_layout.addWidget(self.setup_kind)
        self.setup_hint = self._detail("")
        setup_layout.addWidget(self.setup_hint)
        self.copy_setup = _ActionButton("Copy setup prompt")
        self.copy_setup.setObjectName("primaryButton")
        self.copy_setup.set_base_minimum_size(0, 38)
        self.copy_setup.setToolTip(
            "Includes the access token, which grants saved-history access and the "
            "permissions you choose. Share only with trusted assistants."
        )
        self.copy_setup.clicked.connect(self._copy_setup)
        self.preview = _Disclosure("Preview prompt", inline=True)
        self.copy_actions = _AdaptiveRow(
            [(self.copy_setup, 0), None, (self.preview.toggle, 0)], spacing=10
        )
        setup_layout.addWidget(self.copy_actions)
        self.setup_text = QPlainTextEdit()
        self.setup_text.setReadOnly(True)
        self.setup_text.setMinimumHeight(160)
        self.setup_text.setMaximumHeight(210)
        self.setup_text.setAccessibleName("MCP setup instructions")
        self.preview.body_layout.addWidget(self.setup_text)
        setup_layout.addWidget(self.preview)
        setup_layout.addSpacing(8)
        token_layout = QVBoxLayout()
        token_layout.setSpacing(4)
        token_label = self._label(
            "<b>Access token</b> · already included when you copy the setup above",
            "mcpFieldLabel",
        )
        token_label.setTextFormat(Qt.TextFormat.RichText)
        token_layout.addWidget(token_label)
        self.token = QLineEdit()
        self.token.setReadOnly(True)
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setAccessibleName("MCP access token")
        self.token.setPlaceholderText("Available when MCP is running")
        self.token.setToolTip(
            "Saved in your system credential store. Copy it here if an assistant asks for it."
        )
        self.copy_token = self._copy_row(
            token_layout, "", self.token, "Copy token", self.server.token
        )
        self.copy_token.setAccessibleName("Copy access token")
        self.copy_token.setToolTip(
            "Grants saved-history access and the permissions you choose. Share only with trusted assistants."
        )
        setup_layout.addLayout(token_layout)
        connection_layout.addWidget(self.connection)

        self.advanced = _Disclosure("Advanced connection")
        self.advanced.body_layout.addWidget(
            self._detail(
                "Turn MCP off to change these settings. Remote assistants must be on the same Tailscale network."
            )
        )
        self.port = NoWheelSpinBox()
        self.port.setAccessibleName("MCP local port")
        self.port.setRange(1, 65535)
        self.port.setKeyboardTracking(False)
        port = settings.get(SettingsKey.MCP_PORT, DEFAULT_PORT)
        self.port.setValue(
            port if type(port) is int and 1 <= port <= 65535 else DEFAULT_PORT
        )
        self.port.valueChanged.connect(self._port_changed)
        self.advanced.body_layout.addWidget(
            FieldTile("Local port", "", self.port, compact=True)
        )
        self.tailscale_tile = SettingTile(
            "Allow agents over Tailscale",
            "Connect from another computer on the same Tailscale network. Local access stays available.",
            design_icon("world-blue.svg"),
        )
        self.tailscale_enabled = self.tailscale_tile.checkbox
        self.tailscale_enabled.setAccessibleName("Allow agents over Tailscale")
        self.tailscale_enabled.toggled.connect(self._save_tailscale)
        self.advanced.body_layout.addWidget(self.tailscale_tile)
        connection_layout.addWidget(self.advanced)
        connection_layout.addStretch()

        baseline = _PermissionRow(
            "Read saved text",
            "Search saved dictations, transcripts and meeting insights whenever MCP is on.",
            "file-music-blue.svg",
        )
        baseline.checkbox.hide()
        baseline.setCursor(Qt.CursorShape.ArrowCursor)
        baseline.checkbox.setEnabled(False)
        included = QLabel("Included")
        included.setObjectName("mcpIncluded")
        baseline.layout().addWidget(included, alignment=Qt.AlignmentFlag.AlignVCenter)
        permissions_layout.addWidget(baseline)
        self.permission_checks = {}
        for key, title, description, icon in (
            (
                SettingsKey.MCP_RETITLE_TRANSCRIPTIONS,
                "Rename dictations",
                "Keep the original text and source filename.",
                "typography-blue.svg",
            ),
            (
                SettingsKey.MCP_RETITLE_MEETINGS,
                "Rename finished meetings",
                "Change titles of local saved meetings.",
                "box-blue.svg",
            ),
            (
                SettingsKey.MCP_SETTINGS_ACCESS,
                "Read app preferences",
                "Read supported preferences. Allow individual changes below.",
                "layout-grid-blue.svg",
            ),
        ):
            row = _PermissionRow(title, description, icon)
            check = row.checkbox
            check.toggled.connect(
                lambda checked, key=key: self._save_permission(key, checked)
            )
            self.permission_checks[key] = check
            permissions_layout.addWidget(row)

        self.permission_summary = QLabel()
        self.permission_summary.setObjectName("settingsTileDescription")
        self.preferences = _Disclosure(
            "Choose individual preferences", trailing=self.permission_summary
        )
        self.preferences.body_layout.addWidget(
            self._detail(
                "Each checkbox allows changes to that preference. "
                "Permissions apply to every assistant using this token; "
                "turning one off blocks future changes immediately."
            )
        )
        self.settings_permissions = QWidget()
        self.settings_permissions.setObjectName("mcpSettingsPermissions")
        preferences_layout = QVBoxLayout(self.settings_permissions)
        preferences_layout.setContentsMargins(0, 0, 0, 0)
        preferences_layout.setSpacing(10)
        self.setting_checks = {}
        self.setting_groups = {}
        for control in SETTING_CONTROLS:
            if control.group not in self.setting_groups:
                group = _Disclosure(control.group)
                group.toggle.setProperty("group", True)
                self.setting_groups[control.group] = group
                preferences_layout.addWidget(group)
            check = QCheckBox(control.label)
            check.setAccessibleName(f"Allow agent changes: {control.label}")
            check.setToolTip(control.effect)
            check.toggled.connect(
                lambda checked, key=control.key: self._save_setting_permission(
                    key, checked
                )
            )
            self.setting_groups[control.group].body_layout.addWidget(check)
            self.setting_checks[control.key] = check
        self.preferences.body_layout.addWidget(self.settings_permissions)
        permissions_layout.addWidget(self.preferences)
        limits = self._detail(
            "Never allowed: deletion, transcript edits, audio access or permission changes. "
            "A cloud-powered assistant may send saved text to its provider."
        )
        limits.setObjectName("mcpNote")
        permissions_layout.addWidget(limits)
        permissions_layout.addStretch()

        self.notice = WrappedLabel("")
        self.notice.setObjectName("mcpNotice")
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.notice)
        layout.addStretch()
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.refresh)
        self._arrange_columns()
        self.refresh()

    @staticmethod
    def _label(text, name):
        label = WrappedLabel(text)
        label.setObjectName(name)
        return label

    @classmethod
    def _detail(cls, text):
        return cls._label(text, "settingsTileDescription")

    @classmethod
    def _card(cls, title, description, icon):
        card = QFrame()
        card.setObjectName("mcpCard")
        card.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        # The standard Settings card heading: a bare icon beside a small
        # bold title, as on every other page's tiles.
        header = QHBoxLayout()
        header.setSpacing(12)
        glyph = QLabel()
        glyph.setObjectName("settingsTileIcon")
        glyph.setFixedSize(TileBase.ICON_SIZE, TileBase.ICON_SIZE)
        glyph.setPixmap(design_icon(icon).pixmap(TileBase.ICON_SIZE, TileBase.ICON_SIZE))
        header.addWidget(glyph, alignment=Qt.AlignmentFlag.AlignTop)
        titles = QVBoxLayout()
        titles.setSpacing(3)
        titles.addWidget(cls._label(title, "settingsTileTitle"))
        titles.addWidget(cls._detail(description))
        header.addLayout(titles, 1)
        layout.addLayout(header)
        return card, layout

    def _arrange_columns(self):
        wide = self.width() >= round(740 * current_ui_font_scale())
        if self._wide == wide:
            return
        self._wide = wide
        self.columns.removeWidget(self.connection_card)
        self.columns.removeWidget(self.permissions_card)
        top = Qt.AlignmentFlag.AlignTop
        self.columns.addWidget(self.connection_card, 0, 0, alignment=top)
        self.columns.addWidget(
            self.permissions_card, 0 if wide else 1, 1 if wide else 0, alignment=top
        )
        self.columns.setColumnStretch(0, 6 if wide else 1)
        self.columns.setColumnStretch(1, 5 if wide else 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrange_columns()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (
            QEvent.Type.FontChange,
            QEvent.Type.StyleChange,
        ) and hasattr(self, "permissions_card"):
            self._arrange_columns()

    def _copy_row(self, layout, title, field, button_text, value):
        if title:
            layout.addWidget(self._label(title, "settingsTileTitle"))
        button = _ActionButton(button_text)
        button.setObjectName("mcpCopyButton")
        button.set_base_minimum_size(0, 38)
        button.setAccessibleName(button_text)
        button.clicked.connect(lambda: self._copy(value()))
        layout.addWidget(_CopyField(field, button))
        return button

    def _copy(self, text):
        if self.server.status().state != "running" or not text:
            return
        QApplication.clipboard().setText(text)
        self._notice = "Copied to clipboard."
        self.notice.setText(self._notice)
        self.notice.show()

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
        if self.settings.get(SettingsKey.MCP_TAILSCALE_ENABLED, False) is True:
            self.server.start(self.port.value(), tailscale=True)
        else:
            self.server.start(self.port.value())
        self.refresh()

    def _save_tailscale(self, checked):
        try:
            self.settings.save_setting(SettingsKey.MCP_TAILSCALE_ENABLED, checked)
            self._notice = ""
            self.agent_location.setCurrentIndex(
                self._remote_index if checked else self._local_index
            )
        except Exception:
            self._notice = "Could not save Tailscale access. Try again."
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

    def _setup_url(self, status):
        if self.agent_location.currentIndex() == self._remote_index:
            return getattr(status, "remote_url", "")
        return status.url

    def _setup_for(self, url, token):
        """The selected setup format; an empty ``token`` leaves the placeholder."""
        return (
            agent_prompt(url, self.host_name, token),
            claude_command(url, token or TOKEN_PLACEHOLDER),
            client_config(url, token or TOKEN_PLACEHOLDER),
            chatgpt_config(url, token or TOKEN_PLACEHOLDER),
        )[self.setup_kind.currentIndex()]

    def _copy_setup(self):
        # Built at click time: the preview only ever holds the masked token.
        url = self._setup_url(self.server.status())
        if url:
            self._copy(self._setup_for(url, self.server.token()))

    def _render_setup(self):
        status = self.server.status()
        url = self._setup_url(status)
        remote = self.agent_location.currentIndex() == self._remote_index
        self.url_label.setText(
            "Tailscale MCP address"
            if remote
            else f"MCP address on {self.host_name}"
            if self.host_name
            else "Local MCP address"
        )
        ready = status.state == "running" and bool(url)
        self.url.setToolTip(
            url if ready else "The address appears when this connection is available."
        )
        self.copy_setup.setEnabled(ready)
        self.copy_url.setEnabled(ready)
        self.url.setEnabled(ready)
        kinds = ("setup prompt", "Claude Code command", "Cursor JSON", "ChatGPT config")
        kind = self.setup_kind.currentIndex()
        self.copy_setup.setText(f"Copy {kinds[kind]}")
        self.preview.toggle.setText(
            f"Preview {('prompt', 'command', 'JSON', 'config')[kind]}"
        )
        self.preview.toggle.setAccessibleName(f"Preview {kinds[kind]}")
        self.preview.toggle.setToolTip(f"Preview {kinds[kind]}")
        self.copy_actions.reflow()
        has_token = bool(self.server.token())
        hints = (
            "Paste the setup prompt into your assistant.",
            "Paste the command into your terminal.",
            "Merge this JSON into Cursor's MCP configuration.",
            "For the desktop app, merge this into ~/.codex/config.toml.",
        )
        self.setup_hint.setText(
            hints[kind]
            + (
                " Your access token is already filled in."
                if has_token
                else " Copy the access token separately when asked."
                if kind == 0
                else f" Replace {TOKEN_PLACEHOLDER} with the access token."
            )
        )
        if not ready:
            self.url.setText(
                "Tailscale access is not enabled"
                if status.state == "running"
                else "Enable MCP to get your connection URL"
            )
            self.setup_text.setPlainText(
                "To connect an agent on another computer, turn MCP off, enable "
                "Allow agents over Tailscale, then turn MCP on again."
                if status.state == "running"
                else "Connection instructions appear when MCP is running."
            )
            self.setup_hint.setText(self.setup_text.toPlainText())
            return
        if self.url.text() != url:
            self.url.setText(url)
            self.url.setCursorPosition(0)
        text = self._setup_for(url, _HIDDEN_TOKEN if has_token else "")
        if self.setup_text.toPlainText() != text:
            self.setup_text.setPlainText(text)

    def refresh(self):
        status = self.server.status()
        preferences = self.settings.load_all_settings()
        saved = preferences.get(SettingsKey.MCP_ENABLED, False) is True
        if self.link is not None:
            # The host answers after the click, so what it refused arrives here.
            self._notice = self.link.take_problem() or self._notice
            port = preferences.get(SettingsKey.MCP_PORT)
            if type(port) is int and port != self.port.value() and not self.port.hasFocus():
                self.port.blockSignals(True)
                self.port.setValue(port)
                self.port.blockSignals(False)
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
        access = self.settings_permissions.isEnabled()
        self.permission_summary.setText(
            f"{len(granted)} allowed to change"
            if access
            else "Needs read access"
        )
        self.enabled.blockSignals(True)
        self.enabled.setChecked(saved)
        self.enabled.blockSignals(False)
        self.tailscale_enabled.blockSignals(True)
        self.tailscale_enabled.setChecked(
            preferences.get(SettingsKey.MCP_TAILSCALE_ENABLED) is True
        )
        self.tailscale_enabled.blockSignals(False)
        self.tailscale_tile.setEnabled(
            status.state in {"stopped", "error"} and not saved
        )
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
        where = self.host_name or ""
        details = {
            "running": f"Keep OpenWhisper open on {where}."
            if where
            else "Keep OpenWhisper open.",
            "stopped": f"Turn on to let an assistant search {where}'s saved dictations and meetings."
            if where
            else "Turn on to let an assistant search your saved dictations and meetings.",
        }
        detail = details.get(status.state, status.message)
        self.status_label.setText(
            f"<b>{labels[status.state]}</b> · {html.escape(detail)}"
        )
        self.status_label.setToolTip(status.message)
        set_style_property(
            self.enable_tile,
            "state",
            "running"
            if status.state == "running"
            else "error"
            if status.state == "error"
            else "off",
        )
        self.status_dot.set_status(
            EngineStatus.READY
            if status.state == "running"
            else EngineStatus.ATTENTION
            if status.state == "error"
            else EngineStatus.UNKNOWN
        )
        self.status_dot.set_busy(status.state in {"starting", "stopping"})
        ready = status.state == "running"
        self.connection.setEnabled(ready)
        self._render_setup()
        self.token.setText(self.server.token() if ready else "")
        if not ready:
            self.setup_text.setPlainText(
                "Connection instructions appear when MCP is running."
            )
        self.notice.setText(self._notice)
        self.notice.setVisible(bool(self._notice))

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self.timer.start()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)


class _LiveRelay(QObject):
    """Brings background answers (clearing subscriptions) to the Qt thread.

    Session-lived like ``_ServiceRelay``: the worker never holds or emits on
    a view, and Qt drops a deleted view's connection.
    """

    answered = pyqtSignal(int, object, object)  # token, result, error text or None


_live_relay: Optional[_LiveRelay] = None


def _live_answers() -> _LiveRelay:
    global _live_relay
    if _live_relay is None:
        _live_relay = _LiveRelay()
    return _live_relay


#: Event-stream listeners by kind (``events.bus().subscribers(label)``). The
#: live API labels a plain SSE client "sse" and a Claude Code channel session
#: "claude-channel" (docs/design/automation/design.md, the API contract).
SSE_LISTENER_LABEL = "sse"
CHANNEL_LISTENER_LABEL = "claude-channel"

#: What the live API offers, for the endpoint list: (method, path, what it does).
LIVE_ENDPOINTS = (
    ("GET", "/v1/events", "Live events as they happen"),
    ("GET", "/v1/live/state", "What's happening right now"),
    ("GET", "/v1/actions", "Your saved actions"),
    ("POST", "/v1/actions/{id}/run", "Run an action with text"),
    ("POST", "/v1/recording/start", "Start dictating; also /stop and /cancel"),
    ("POST", "/v1/filters", "Change the words for a minute; renew or DELETE /v1/filters/{id}"),
    ("POST", "/v1/notify", "Show a short notice"),
)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


class LiveAutomationSection(QWidget):
    """Live API, MCP Events and the Claude Code channel, for this computer only.

    They share the MCP listener, so their addresses come from ``page``'s
    server. ``refresh_live`` reads counters only (in memory) and runs on the
    page's 500 ms timer; saved choices are re-read when shown and after a write.
    """

    #: A setting the app caches changed: "mcp_events".
    settings_changed = pyqtSignal(str)

    def __init__(self, settings, page, parent=None):
        super().__init__(parent)
        self.setObjectName("mcpLiveSection")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.settings = settings
        self.page = page
        self._wide = None
        self._waiting = {}
        _live_answers().answered.connect(self._on_answer)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(14)
        heading = QLabel("Live events and automation")
        heading.setObjectName("settingsTileGroupTitle")
        layout.addWidget(heading)
        self.columns = QGridLayout()
        self.columns.setContentsMargins(0, 0, 0, 0)
        self.columns.setSpacing(14)
        layout.addLayout(self.columns)
        self._build_live_api()
        self._build_mcp_events()
        self._build_channel()
        self.notice = WrappedLabel("")
        self.notice.setObjectName("mcpNotice")
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.hide()
        layout.addWidget(self.notice)
        self._arrange_columns()
        self.refresh()

    # ---- building ----

    def _card(self, title, description, icon, chip=""):
        card, layout = McpSettingsPage._card(title, description, icon)
        if chip:
            badge = QLabel(chip)
            badge.setObjectName("mcpPreviewChip")
            layout.itemAt(0).layout().addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
        return card, layout

    def _status_line(self, layout, accessible_name):
        row = QWidget()
        row.setObjectName("mcpLiveRow")
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(8)
        dot = StatusDot(diameter=8)
        line.addWidget(dot, 0, Qt.AlignmentFlag.AlignVCenter)
        label = WrappedLabel("")
        label.setObjectName("mcpLiveStatus")
        label.setAccessibleName(accessible_name)
        line.addWidget(label, 1)
        layout.addWidget(row)
        return dot, label

    def _copy_button(self, text):
        button = _ActionButton(text)
        button.setObjectName("mcpCopyButton")
        button.set_base_minimum_size(0, 38)
        button.setAccessibleName(text)
        return button

    def _build_live_api(self):
        self.live_api_card, layout = self._card(
            "Live API",
            "Your own programs can follow along as you dictate and, if you allow it, act for you.",
            "broadcast-green.svg",
        )
        self.listeners_dot, self.listeners_label = self._status_line(
            layout, "Programs listening to live events"
        )
        self.api_permission_checks = {}
        for key, title, description, icon in (
            (
                SettingsKey.API_LIVE_EVENTS,
                "Follow live events",
                "A program can read your finished dictations, the actions you run and meeting "
                "lines as they happen, like reading history.",
                "file-music-blue.svg",
            ),
            (
                SettingsKey.API_RUN_ACTIONS,
                "Run actions",
                "Start your saved actions with text a program sends, and show its notices.",
                "bolt-green.svg",
            ),
            (
                SettingsKey.API_CONTROL_RECORDING,
                "Start recordings",
                "Turn on the microphone to dictate, and stop or cancel a recording.",
                "microphone-blue.svg",
            ),
            (
                SettingsKey.API_TEXT_FILTERS,
                "Change dictated text",
                "Let a program on this computer rewrite or hold back words before they're pasted "
                "or sent to an action. Words for coding agents also need the switch below.",
                "filter-purple.svg",
            ),
            (
                SettingsKey.API_AGENT_PROMPTS,
                "Send words to my coding agents",
                "Lets a program run your Agent hotkey and Claude Code session actions with its "
                "own words. Needs Run actions too.",
                "send-purple.svg",
            ),
        ):
            row = _PermissionRow(title, description, icon)
            row.checkbox.toggled.connect(
                lambda checked, key=key: self._save(key, checked)
            )
            self.api_permission_checks[key] = row.checkbox
            layout.addWidget(row)
        layout.addWidget(McpSettingsPage._detail(
            "These also apply to assistants connected over MCP. Each is checked on every request."
        ))
        address_layout = QVBoxLayout()
        address_layout.setSpacing(4)
        address_layout.addWidget(McpSettingsPage._label("Live API address", "mcpFieldLabel"))
        self.api_url = QLineEdit()
        self.api_url.setReadOnly(True)
        self.api_url.setAccessibleName("Live API address")
        self.copy_api_url = self._copy_button("Copy address")
        self.copy_api_url.clicked.connect(lambda: self._copy(self.api_url.text(), "Address copied."))
        address_layout.addWidget(_CopyField(self.api_url, self.copy_api_url))
        layout.addLayout(address_layout)

        self.endpoints = _Disclosure("Endpoints")
        self.endpoints.body_layout.addWidget(McpSettingsPage._detail(
            "Send the MCP access token as a Bearer header. Click an address to copy it."
        ))
        grid_host = QWidget()
        grid_host.setObjectName("mcpEndpoints")
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(4)
        self.endpoint_buttons = []
        for row_index, (method, path, what) in enumerate(LIVE_ENDPOINTS):
            button = QPushButton(f"{method}  {path}")
            button.setObjectName("mcpEndpoint")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip("Copy this address")
            button.setAccessibleName(f"Copy the {path} address")
            button.clicked.connect(lambda _checked=False, path=path: self._copy_endpoint(path))
            grid.addWidget(button, row_index, 0, Qt.AlignmentFlag.AlignLeft)
            label = WrappedLabel(what)
            label.setObjectName("settingsTileDescription")
            grid.addWidget(label, row_index, 1)
            self.endpoint_buttons.append(button)
        grid.setColumnStretch(1, 1)
        self.endpoints.body_layout.addWidget(grid_host)
        self.copy_example = self._copy_button("Copy a curl example")
        self.copy_example.setToolTip(
            "Includes the access token. Paste it into a terminal to watch events live."
        )
        self.copy_example.clicked.connect(self._copy_example)
        self.endpoints.body_layout.addWidget(self.copy_example, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.endpoints)
        layout.addStretch()

    def _build_mcp_events(self):
        self.mcp_events_card, layout = self._card(
            "MCP Events",
            "Agents subscribe to events over MCP, and OpenWhisper sends them signed webhooks.",
            "bell-amber.svg",
            chip="Preview",
        )
        row = _PermissionRow(
            "Let agents subscribe",
            "An assistant over MCP chooses the web address, and it receives what you dictate "
            "(the events it subscribes to).",
            "key-blue.svg",
        )
        self.mcp_events_switch = row.checkbox
        self.mcp_events_switch.toggled.connect(self._save_mcp_events)
        layout.addWidget(row)
        line = QWidget()
        line.setObjectName("mcpLiveRow")
        count_row = QHBoxLayout(line)
        count_row.setContentsMargins(0, 0, 0, 0)
        count_row.setSpacing(10)
        self.subscriptions_label = WrappedLabel("")
        self.subscriptions_label.setObjectName("mcpLiveStatus")
        self.subscriptions_label.setAccessibleName("MCP event subscriptions")
        count_row.addWidget(self.subscriptions_label, 1)
        self.clear_subscriptions = _ActionButton("Clear")
        self.clear_subscriptions.setObjectName("mcpCopyButton")
        self.clear_subscriptions.set_base_minimum_size(0, 34)
        self.clear_subscriptions.setAccessibleName("Clear MCP event subscriptions")
        self.clear_subscriptions.setToolTip("Forget every subscription and its signing secret")
        self.clear_subscriptions.clicked.connect(self._clear_subscriptions)
        count_row.addWidget(self.clear_subscriptions, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(line)
        layout.addStretch()

    def _build_channel(self):
        from services.agent_mcp.setup import claude_channel_command, claude_channel_config

        self.channel_card, layout = self._card(
            "Claude Code channel",
            "Send words into a running Claude Code session with a “Tell my Claude Code "
            "session” action.",
            "send-purple.svg",
            chip="Research preview",
        )
        self.sessions_dot, self.sessions_label = self._status_line(
            layout, "Claude Code sessions connected to the channel"
        )
        layout.addWidget(McpSettingsPage._label("1. Add OpenWhisper to a project's .mcp.json", "mcpSectionLabel"))
        # A session that loads the entry without the channel flag still
        # connects (and counts) but Claude Code drops what it is sent.
        self.channel_scope = McpSettingsPage._detail(
            "Only in projects you start with the command below, not in your user-wide "
            "settings. A session started without it connects but can't receive messages."
        )
        layout.addWidget(self.channel_scope)
        self.channel_config = QPlainTextEdit()
        self.channel_config.setObjectName("mcpChannelConfig")
        self.channel_config.setReadOnly(True)
        self.channel_config.setAccessibleName("Claude Code channel .mcp.json entry")
        self.channel_config.setPlainText(claude_channel_config())
        self.channel_config.setMinimumHeight(160)
        self.channel_config.setMaximumHeight(230)
        self.copy_channel_config = self._copy_button("Copy .mcp.json entry")
        self.copy_channel_config.clicked.connect(
            lambda: self._copy(
                self.channel_config.toPlainText(),
                "Entry copied. Merge it into that project's .mcp.json.",
            )
        )
        self.channel_preview = _Disclosure("Preview entry", inline=True)
        self.channel_preview.body_layout.addWidget(self.channel_config)
        layout.addWidget(_AdaptiveRow(
            [(self.copy_channel_config, 0), None, (self.channel_preview.toggle, 0)], spacing=10
        ))
        layout.addWidget(self.channel_preview)
        layout.addWidget(McpSettingsPage._label("2. Start Claude Code with the channel", "mcpSectionLabel"))
        self.channel_command = WrappedLabel(claude_channel_command(), break_long_words=True)
        self.channel_command.setObjectName("mcpCommandBox")
        self.channel_command.setTextFormat(Qt.TextFormat.PlainText)
        self.channel_command.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.channel_command.setAccessibleName("Claude Code channel command")
        self.copy_channel_command = self._copy_button("Copy command")
        self.copy_channel_command.clicked.connect(
            lambda: self._copy(self.channel_command.text(), "Command copied. Run it in that project's folder.")
        )
        # Full width, so the command wraps between words rather than inside them.
        layout.addWidget(self.channel_command)
        layout.addWidget(self.copy_channel_command, 0, Qt.AlignmentFlag.AlignLeft)
        self.channel_programs = McpSettingsPage._detail(
            "A program can send words here too once you allow Send words to my coding "
            "agents. Claude sees they came from a program, not from you."
        )
        layout.addWidget(self.channel_programs)
        layout.addStretch()

    # ---- layout ----

    def _arrange_columns(self):
        wide = self.width() >= round(740 * current_ui_font_scale())
        if self._wide == wide:
            return
        self._wide = wide
        for card in (self.live_api_card, self.mcp_events_card, self.channel_card):
            self.columns.removeWidget(card)
        top = Qt.AlignmentFlag.AlignTop
        if wide:
            self.columns.addWidget(self.live_api_card, 0, 0, 2, 1, alignment=top)
            self.columns.addWidget(self.mcp_events_card, 0, 1, alignment=top)
            self.columns.addWidget(self.channel_card, 1, 1, alignment=top)
            self.columns.setRowStretch(1, 1)
        else:
            for row, card in enumerate((self.live_api_card, self.mcp_events_card, self.channel_card)):
                self.columns.addWidget(card, row, 0, alignment=top)
            self.columns.setRowStretch(1, 0)
        self.columns.setColumnStretch(0, 6 if wide else 1)
        self.columns.setColumnStretch(1, 5 if wide else 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrange_columns()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (
            QEvent.Type.FontChange,
            QEvent.Type.StyleChange,
        ) and hasattr(self, "channel_card"):
            self._arrange_columns()

    # ---- state ----

    def _base_url(self):
        status = self.page.server.status()
        if status.state != "running":
            return ""
        return status.url.rsplit("/mcp", 1)[0]

    def refresh(self):
        """Re-read saved choices, then the live counters."""
        try:
            preferences = self.settings.load_all_settings()
        except Exception:
            preferences = {}
        from services.automation import permissions

        for key, check in self.api_permission_checks.items():
            blocked = check.blockSignals(True)
            check.setChecked(permissions.granted(preferences, key))
            check.blockSignals(blocked)
        blocked = self.mcp_events_switch.blockSignals(True)
        self.mcp_events_switch.setChecked(
            preferences.get(SettingsKey.MCP_EVENTS_ENABLED) is True
        )
        self.mcp_events_switch.blockSignals(blocked)
        self.refresh_live()

    def refresh_live(self):
        """Counters and the address; cheap enough for the page's 500 ms timer."""
        from services.agent_mcp import events_ext
        from services.automation import events

        url = self._base_url()
        running = bool(url)
        if self.api_url.text() != (url or "Turn on MCP above to start the live API"):
            self.api_url.setText(url or "Turn on MCP above to start the live API")
            self.api_url.setCursorPosition(0)
        for widget in (self.api_url, self.copy_api_url, self.copy_example, *self.endpoint_buttons):
            widget.setEnabled(running)
        bus = events.bus()
        listeners = bus.subscribers(SSE_LISTENER_LABEL)
        sessions = bus.subscribers(CHANNEL_LISTENER_LABEL)
        self._show_count(
            self.listeners_dot, self.listeners_label, running, listeners,
            _plural(listeners, "program") + " listening" if listeners else "No programs listening yet",
            "Off while MCP is off",
        )
        self._show_count(
            self.sessions_dot, self.sessions_label, running, sessions,
            # "Connected", not "listening": a session started without the
            # channel flag connects too, and can't receive anything.
            _plural(sessions, "session") + " connected" if sessions else "No sessions connected",
            "Needs MCP on",
        )
        try:
            count = int(events_ext.subscription_count())
        except Exception:
            count = 0
        enabled = self.mcp_events_switch.isChecked()
        text = (
            _plural(count, "subscription") if count
            else "No subscriptions yet" if enabled
            else "Off. Agents can't subscribe."
        )
        if self.subscriptions_label.text() != text:
            self.subscriptions_label.setText(text)
        # A Clear with nothing to clear is only noise.
        self.clear_subscriptions.setVisible(count > 0)
        self.clear_subscriptions.setEnabled("clear" not in self._waiting.values())

    @staticmethod
    def _show_count(dot, label, running, count, text, off_text):
        text = text if running else off_text
        if label.text() != text:
            label.setText(text)
        status = (
            EngineStatus.READY if running and count
            else EngineStatus.UNKNOWN
        )
        if dot.status() != status:
            dot.set_status(status)

    # ---- writes ----

    def _write(self, key, value):
        def commit(saved):
            saved[key] = value

        self.settings.mutate_settings(commit)

    def _save(self, key, checked):
        try:
            self._write(key, bool(checked))
        except Exception:
            self._show_notice("Couldn't save that permission. Try again.")
            self.refresh()
            return
        self._show_notice("")

    def _save_mcp_events(self, checked):
        try:
            self._write(SettingsKey.MCP_EVENTS_ENABLED, bool(checked))
        except Exception:
            self._show_notice("Couldn't save MCP Events. Try again.")
            self.refresh()
            return
        self._show_notice("")
        self.settings_changed.emit("mcp_events")
        self.refresh_live()

    def _clear_subscriptions(self):
        from services.agent_mcp import events_ext

        token = next(_tokens)
        self._waiting[token] = "clear"
        self.clear_subscriptions.setEnabled(False)
        answered = _live_answers().answered
        clear = events_ext.clear_subscriptions

        def worker():
            # Forgetting secrets touches the credential store, which can be slow.
            try:
                result = clear()
            except Exception as exc:
                answered.emit(token, None, str(exc) or type(exc).__name__)
            else:
                answered.emit(token, result, None)

        threading.Thread(target=worker, name="settings-mcp-events-clear", daemon=True).start()

    def _on_answer(self, token, result, error):
        if self._waiting.pop(token, None) != "clear":
            return
        if error:
            self._show_notice(f"Couldn't clear subscriptions: {error}")
        else:
            count = int(result or 0)
            self._show_notice(
                f"Cleared {_plural(count, 'subscription')}." if count else "Nothing to clear."
            )
        self.refresh_live()

    # ---- copying ----

    def _show_notice(self, text):
        self.notice.setText(text)
        self.notice.setVisible(bool(text))

    def _copy(self, text, done):
        if not text:
            return
        QApplication.clipboard().setText(text)
        self._show_notice(done)

    def _copy_endpoint(self, path):
        base = self._base_url()
        if base:
            self._copy(base + path, f"Copied {base + path}")

    def _copy_example(self):
        base = self._base_url()
        token = self.page.server.token()
        if not base or not token:
            return
        # Built at click time, so the token is never on screen.
        self._copy(
            f'curl -N -H "Authorization: Bearer {token}" {base}/v1/events',
            "Copied. Paste it into a terminal to watch events live.",
        )

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()


class _HostGate(QFrame):
    """Stands in for the host's controls while it can't be managed from here."""

    retry = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("mcpGate")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(8)
        self.title = WrappedLabel("")
        self.title.setObjectName("mcpCardTitle")
        self.body = WrappedLabel("")
        self.body.setObjectName("settingsTileDescription")
        self.button = _ActionButton("Check again")
        self.button.setObjectName("mcpCopyButton")
        self.button.set_base_minimum_size(0, 38)
        self.button.clicked.connect(self.retry)
        layout.addWidget(self.title)
        layout.addWidget(self.body)
        layout.addSpacing(4)
        layout.addWidget(self.button, alignment=Qt.AlignmentFlag.AlignLeft)

    def show_message(self, title, body, *, can_retry):
        self.title.setText(title)
        self.body.setText(body)
        self.body.setVisible(bool(body))
        self.button.setVisible(can_retry)


class McpSettingsView(QWidget):
    """MCP settings for this computer and, once paired, for its host.

    Most people with a host will want the host's: that is where an assistant
    should connect, and where their records live if they keep them there. The
    tabs show up only while paired, and open on the host's when its MCP is
    already running and this computer's is not.

    The live API, MCP Events and the Claude Code channel (``live``) belong to
    this computer only: the host tab hides them and says where to find them.
    """

    #: A setting the app caches changed (``SettingsDialog.notify_changed`` kinds).
    settings_changed = pyqtSignal(str)

    def __init__(self, settings, *, parent=None):
        super().__init__(parent)
        self.setObjectName("mcpSettingsView")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.settings = settings
        self.local = McpSettingsPage(settings)
        self.host_page = None
        self.tabs = None
        self._service = None
        self._pairing = None
        self._link = None
        self._chosen = False
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(14)
        self.share_history_tile = SettingTile(
            "Share this computer's history through the paired host's MCP",
            "",
            design_icon("server-blue.svg"),
        )
        self.share_history_tile.setProperty("tileId", "mcpShareHistoryTile")
        self.share_history_tile.checkbox.toggled.connect(self._toggle_history_sharing)
        # This permission belongs to this computer, even when the host's MCP
        # tab is selected or the host has denied remote MCP administration.
        self._layout.addWidget(self.share_history_tile)
        self._layout.addWidget(self.local)
        self.live = LiveAutomationSection(settings, self.local)
        self.live.settings_changed.connect(self.settings_changed)
        # Counters refresh on this computer's page timer, which runs while it shows.
        self.local.timer.timeout.connect(self.live.refresh_live)
        self._layout.addWidget(self.live)
        self.host_area = QWidget()
        self.host_area.setObjectName("mcpHostArea")
        self.host_layout = QVBoxLayout(self.host_area)
        self.host_layout.setContentsMargins(0, 0, 0, 0)
        self.gate = _HostGate()
        self.gate.retry.connect(self._retry)
        self.host_layout.addWidget(self.gate)
        self.host_note = WrappedLabel("")
        self.host_note.setObjectName("mcpNotice")
        self.host_note.setTextFormat(Qt.TextFormat.PlainText)
        self.host_layout.addWidget(self.host_note)
        self.host_area.hide()
        self._layout.addWidget(self.host_area)
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._sync)
        relay = _service_relay()
        relay.service_event.connect(self._on_service_event)
        self._token = token = next(_tokens)
        event = relay.service_event

        def listener(kind):
            # Called on whatever thread the change happened; the relay re-posts it.
            event.emit(token, kind)

        self._listener = listener
        self._refresh_history_sharing()

    # ---- the service ----

    def bind(self, service):
        """Attach the controller's remote engine service (built after the dialog)."""
        if service is not self._service:
            if self._service is not None:
                self._service.remove_listener(self._listener)
            self._service = service
            if service is not None:
                listener = self._listener
                service.add_listener(listener)
                self.destroyed.connect(lambda *_: service.remove_listener(listener))
        self._refresh_pairing()

    def _on_service_event(self, token, kind):
        if token != self._token:
            return
        if kind in ("client", "host_renamed"):
            self._refresh_pairing()

    def _refresh_pairing(self):
        pairing = self._service.client_pairing() if self._service is not None else None
        self._refresh_history_sharing(pairing)
        renamed = getattr(pairing, "host_name", "") != getattr(self._pairing, "host_name", "")
        if pairing == self._pairing and not renamed and (pairing is None) == (self._link is None):
            return
        self._pairing = pairing
        if self.host_page is not None:
            self.host_layout.removeWidget(self.host_page)
            self.host_page.hide()
            self.host_page.deleteLater()
            self.host_page = None
        if self.tabs is not None:
            self._layout.removeWidget(self.tabs)
            self.tabs.hide()
            self.tabs.deleteLater()
            self.tabs = None
        self._link = None
        self._chosen = False
        if pairing is None:
            self._show_tab(0)
            return
        from services.remote_asr.mcp_link import HostMcpLink

        self._link = HostMcpLink(self._service, pairing)
        self.tabs = _ChoiceBar((("This computer", ""), (self._host_name(), "")))
        self.tabs.setAccessibleName("Whose MCP to manage")
        self.tabs.currentIndexChanged.connect(self._show_tab)
        self.tabs.activated.connect(self._choose)
        self._layout.insertWidget(0, self.tabs)
        self._show_tab(0)
        self._link.poll()
        self._sync()

    def _refresh_history_sharing(self, pairing=None):
        description = (
            "Assistants connected to the paired host's MCP can search this computer's "
            "saved dictations and meetings and read their transcripts and insights "
            "while OpenWhisper is running. Copies already stored on the host remain "
            "readable there when this is off."
        )
        if pairing is None:
            description += " Pair a host in Remote engine to enable this setting."
        tile = self.share_history_tile
        tile.set_description(description)
        tile.checkbox.setAccessibleDescription(description)
        tile.setEnabled(self._service is not None and pairing is not None)
        blocked = tile.checkbox.blockSignals(True)
        tile.checkbox.setChecked(
            self.settings.get(SettingsKey.REMOTE_CLIENT_HISTORY, False) is True
        )
        tile.checkbox.blockSignals(blocked)

    def _toggle_history_sharing(self, checked):
        if self._service is not None and self._service.client_pairing() is not None:
            self._service.set_share_history(checked)

    def _host_name(self):
        return getattr(self._pairing, "host_name", "") or "Host"

    # ---- what shows ----

    def _choose(self, _index):
        self._chosen = True

    def _show_tab(self, index):
        host = index == 1 and self._link is not None
        self.local.setVisible(not host)
        self.live.setVisible(not host)
        self.host_area.setVisible(host)
        if host:
            self.host_note.setText(
                "The live API, MCP Events and the Claude Code channel are set on each "
                f"computer. Change this computer's under This computer, and {self._host_name()}'s "
                f"in Settings on {self._host_name()}."
            )
            self._link.refresh_now()
            self._ensure_host_page()
        self._sync()

    def _ensure_host_page(self):
        if self.host_page is not None or self._link is None:
            return
        from services.remote_asr.mcp_link import HostMcpServer, HostMcpSettings

        self.host_page = McpSettingsPage(
            HostMcpSettings(self._link),
            server=HostMcpServer(self._link),
            host_name=self._host_name(),
            link=self._link,
        )
        # Above the note that says where this computer's live controls are.
        self.host_layout.insertWidget(self.host_layout.indexOf(self.host_note), self.host_page)
        self.host_page.hide()

    def _retry(self):
        if self._link is not None:
            self._link.refresh_now()
            self._sync()

    def _sync(self):
        link = self._link
        if link is None or self.tabs is None:
            return
        from services.remote_asr import mcp_link

        link.poll()
        availability = link.availability()
        state = link.state() if availability == mcp_link.READY else None
        local_running = self.local.server.status().state == "running"
        self.tabs.set_detail(0, "MCP on" if local_running else "MCP off")
        host = self._host_name()
        host_running = state is not None and state["state"] == "running"
        self.tabs.set_detail(1, {
            mcp_link.READY: "Host · MCP on" if host_running else "Host · MCP off",
            mcp_link.FORBIDDEN: "Host · not allowed",
            mcp_link.UNSUPPORTED: "Host · needs update",
            mcp_link.OFFLINE: "Host · unreachable",
        }.get(availability, "Host · checking…"))
        if (not self._chosen and host_running and not local_running
                and self.tabs.currentIndex() == 0):
            self.tabs.setCurrentIndex(1)
        if self.tabs.currentIndex() != 1:
            return
        ready = availability == mcp_link.READY
        if self.host_page is not None:
            self.host_page.setVisible(ready)
        self.gate.setVisible(not ready)
        if ready:
            return
        if availability == mcp_link.FORBIDDEN:
            self.gate.show_message(
                f"{host} hasn't allowed this yet",
                "To manage its MCP server from here, turn on “Allow paired computers "
                f"to manage MCP” in Settings → Remote engine on {host}. It's off by "
                "default because it lets paired computers turn MCP on and read its access token.",
                can_retry=True,
            )
        elif availability == mcp_link.UNSUPPORTED:
            self.gate.show_message(
                f"{host} needs an update",
                f"{host} is running a version of OpenWhisper from before this feature, so "
                "there's nothing to switch on there yet. Update and restart OpenWhisper on "
                f"{host}, then turn on “Allow paired computers to manage MCP” in Settings → "
                "Remote engine on it.",
                can_retry=True,
            )
        elif availability == mcp_link.OFFLINE:
            self.gate.show_message(
                f"Can't reach {host}", link.message(), can_retry=True
            )
        else:
            self.gate.show_message(f"Checking {host}…", "", can_retry=False)

    def showEvent(self, event):
        super().showEvent(event)
        if self._link is not None:
            self._link.refresh_now()
        self._sync()
        self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

"""Settings destination for the app-owned MCP server and agent onboarding."""

import html

from PyQt6.QtCore import QEvent, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QButtonGroup,
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QStylePainter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from services.agent_mcp.controls import SETTING_CONTROLS, writable_settings
from services.agent_mcp.runtime import DEFAULT_PORT, runtime
from services.agent_mcp.setup import (
    agent_prompt,
    chatgpt_config,
    claude_command,
    client_config,
)
from services.settings import SettingsKey
from ui_qt.dialogs.settings_basic import SettingsSwitch
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


class _ChoiceButton(QPushButton):
    """A native, keyboard-accessible button with independently sized text lines."""

    def __init__(self, title, detail):
        super().__init__(title)
        self.setObjectName("mcpChoice")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(title)
        self.setAccessibleDescription(detail)
        self.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(2)
        for text, name in ((title, "mcpChoiceTitle"), (detail, "mcpChoiceDetail")):
            if not text:
                continue
            label = QLabel(text)
            label.setObjectName(name)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            layout.addWidget(label)
        self.toggled.connect(
            lambda checked: set_style_property(
                layout.itemAt(0).widget(), "selected", checked
            )
        )

    def sizeHint(self):
        return self.layout().sizeHint().expandedTo(QSize(0, 36))

    def minimumSizeHint(self):
        return self.sizeHint()

    def paintEvent(self, event):
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = ""  # Child labels provide the title and supporting text.
        painter = QStylePainter(self)
        painter.drawControl(QStyle.ControlElement.CE_PushButton, option)


class _ChoiceBar(QWidget):
    """A segmented tray. Equal-width choices wrap into rows instead of one tall stack."""

    currentIndexChanged = pyqtSignal(int)

    def __init__(self, choices, *, parent=None):
        super().__init__(parent)
        self.setObjectName("mcpChoiceBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout = QGridLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        self._group = QButtonGroup(self)
        self._index = -1
        self._columns = 0
        self.buttons = []
        for index, (title, detail) in enumerate(choices):
            button = _ChoiceButton(title, detail)
            self._group.addButton(button, index)
            self.buttons.append(button)
        self._place(len(self.buttons))
        self._reflow_timer = QTimer(self)
        self._reflow_timer.setSingleShot(True)
        self._reflow_timer.timeout.connect(self._reflow)
        self._group.idClicked.connect(self.setCurrentIndex)
        self.setCurrentIndex(0)

    def _place(self, columns):
        if columns == self._columns:
            return
        self._columns = columns
        layout = self.layout()
        for button in self.buttons:
            layout.removeWidget(button)
        for index, button in enumerate(self.buttons):
            layout.addWidget(button, index // columns, index % columns)
        for column in range(len(self.buttons)):
            layout.setColumnStretch(column, 1 if column < columns else 0)

    def _reflow(self):
        layout = self.layout()
        margins = layout.contentsMargins()
        gap = layout.horizontalSpacing()
        cell = max(button.sizeHint().width() for button in self.buttons)
        for button in self.buttons:
            button.setMinimumWidth(cell)
        room = self.width() - margins.left() - margins.right() + gap
        fits = max(1, min(len(self.buttons), room // (cell + gap)))
        rows = -(-len(self.buttons) // fits)
        # Balance the rows so four choices wrap 2 + 2, never 3 + 1.
        self._place(-(-len(self.buttons) // rows))

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

    def currentIndex(self):
        return self._index

    def setCurrentIndex(self, index):
        if index == self._index or not 0 <= index < len(self.buttons):
            return
        self._index = index
        self.buttons[index].setChecked(True)
        self.currentIndexChanged.emit(index)


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
    def __init__(self, settings, *, server=None, parent=None):
        super().__init__(parent)
        self.setObjectName("mcpSettingsPage")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.settings = settings
        self.server = server if server is not None else runtime
        if server is None:
            self.server.bind_settings(settings)
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
                ("Another computer", ""),
            )
        )
        self.agent_location.setAccessibleName("Agent computer")
        self.agent_location.setCurrentIndex(
            1 if settings.get(SettingsKey.MCP_TAILSCALE_ENABLED, False) is True else 0
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
        self.copy_setup.clicked.connect(
            lambda: self._copy(self.setup_text.toPlainText())
        )
        self.preview = _Disclosure("Preview setup prompt", inline=True)
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
            "<b>Access token</b> · copy it separately when your assistant asks",
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
            "Saved in your system credential store. Copy it separately when your assistant asks."
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
                "Each checkbox allows changes to that preference. Permissions apply to every assistant using this token; turning one off blocks future changes immediately."
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
            "Never allowed: recording, deletion, transcript edits, audio access or permission changes. A cloud-powered assistant may send saved text to its provider."
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
        header = QHBoxLayout()
        header.setSpacing(12)
        tile = QLabel()
        tile.setObjectName("mcpCardIcon")
        tile.setFixedSize(34, 34)
        tile.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tile.setPixmap(design_icon(icon).pixmap(20, 20))
        header.addWidget(tile, alignment=Qt.AlignmentFlag.AlignTop)
        titles = QVBoxLayout()
        titles.setSpacing(1)
        titles.addWidget(cls._label(title, "mcpCardTitle"))
        titles.addWidget(cls._detail(description))
        header.addLayout(titles, 1)
        layout.addLayout(header)
        return card, layout

    def _arrange_columns(self):
        wide = self.width() >= round(780 * current_ui_font_scale())
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
            self.agent_location.setCurrentIndex(1 if checked else 0)
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

    def _render_setup(self):
        status = self.server.status()
        url = status.url
        remote = self.agent_location.currentIndex() == 1
        if remote:
            url = getattr(status, "remote_url", "")
        self.url_label.setText(
            "Tailscale MCP address" if remote else "Local MCP address"
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
        self.preview.toggle.setText(f"Preview {kinds[kind]}")
        self.preview.toggle.setAccessibleName(self.preview.toggle.text())
        self.copy_actions.reflow()
        hints = (
            "Paste the setup prompt into your assistant. Copy the access token separately when asked.",
            "Paste the command into your terminal. Replace <PASTE_TOKEN> with the access token.",
            "Merge this JSON into Cursor's MCP configuration. Replace <PASTE_TOKEN> with the access token.",
            "For the desktop app, merge this into ~/.codex/config.toml. Replace <PASTE_TOKEN> with the access token.",
        )
        self.setup_hint.setText(hints[kind])
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
        options = (
            agent_prompt(url),
            claude_command(url),
            client_config(url, "<PASTE_TOKEN>"),
            chatgpt_config(url),
        )
        text = options[kind]
        if self.setup_text.toPlainText() != text:
            self.setup_text.setPlainText(text)

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
        details = {
            "running": "Keep OpenWhisper open.",
            "stopped": "Turn on to let an assistant search your saved dictations and meetings.",
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

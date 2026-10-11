"""Fields the Actions page shares between the Agent hotkey card and the action editor."""

from __future__ import annotations

from typing import Mapping, Optional

from PyQt6.QtCore import QEasingCurve, QEvent, QObject, QSize, Qt, QVariantAnimation, pyqtSignal
from PyQt6.QtWidgets import (
    QBoxLayout,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QStylePainter,
    QVBoxLayout,
    QWidget,
)

from services.actions.model import (
    AGENT_NAMES,
    AGENTS,
    AgentMode,
    AgentSurface,
    Output,
    SHAPING_CLEANUP,
    SHAPING_RAW,
)
from ui_qt.utils.font_scale import current_ui_font_scale
from ui_qt.utils.restyle import set_style_property
from ui_qt.widgets.action_copy import MODE_LABELS, OUTPUT_LABELS, SURFACE_HINTS, SURFACE_LABELS
from ui_qt.widgets.button_row import ButtonRow
from ui_qt.widgets.buttons import Button, fit_compact_button, neutral_button
from ui_qt.widgets.command_settings import CommandShortcutField
from ui_qt.widgets.eliding_label import ElidingLabel
from ui_qt.widgets.no_wheel import ElidingComboBox
from ui_qt.widgets.profile_hotkey_input import ProfileHotkeyInput
from ui_qt.widgets.reflow import Reflow
from ui_qt.widgets.segmented_bar import SegmentedBar
from ui_qt.widgets.settings_switch import SettingsSwitch
from ui_qt.widgets.wrapped_label import WrappedLabel

_SHORTCUT_TIP = "Click, then press a shortcut. Escape cancels. Press it once to start, again to send."
#: One agent's arrival once a scan finds it: the mark warms to its colour.
ARRIVAL_MS = 380
#: The widest the choosers get at 100% font scale: equal cells stretched
#: across a wide card read as one heavy bar.
CHOOSER_MAX_WIDTH = 660
BAR_MAX_WIDTH = 460
COMBO_MAX_WIDTH = 340
SHORTCUT_MAX_WIDTH = 380
#: A tile narrower than this (at 100%) gives its body the full width.
NARROW_TILE_WIDTH = 360


def cap_widths(caps) -> None:
    """Hold each ``(widget, width at 100%)`` to its width at the current font scale."""
    scale = current_ui_font_scale()
    for widget, width in caps:
        widget.setMaximumWidth(round(width * scale))


def _font_changed(event) -> bool:
    return event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange)


def field_label(text: str, buddy: Optional[QWidget] = None) -> QLabel:
    label = QLabel(text)
    label.setObjectName("textModelFieldLabel")
    if buddy is not None:
        label.setBuddy(buddy)
    return label


def hint_label(text: str = "") -> WrappedLabel:
    """A one-line hint under a field; ``tone`` ("ok", "warning", "error") colours it."""
    label = WrappedLabel(text)
    label.setObjectName("actionHint")
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


_ICON_TONES = (("-purple", "purple"), ("-green", "green"), ("-gray", "gray"), ("-blue", "blue"))


class _BodyIndent(QObject):
    """Lines a tile's body up with its title, or gives it the full width when narrow."""

    def __init__(self, tile, indent: int):
        super().__init__(tile)
        self._tile = tile
        self._indent = indent
        tile.installEventFilter(self)
        self._apply(tile.width())

    def _apply(self, width: int) -> None:
        narrow = width < round(NARROW_TILE_WIDTH * current_ui_font_scale())
        margin = 0 if narrow or not self._indent else self._indent
        layout = self._tile._body_layout
        if layout.contentsMargins().left() != margin:
            layout.setContentsMargins(margin, 0, 0, 0)

    def eventFilter(self, obj, event):
        if obj is self._tile and event.type() == QEvent.Type.Resize:
            self._apply(event.size().width())
        return False


def tint_icon(tile, icon: str, *, side: int = 32, indent_body: bool = True) -> None:
    """Sit a tile's icon in a soft rounded square of its own colour, like the MCP cards."""
    from ui_qt.utils.icons import design_icon

    stem = icon.removesuffix(".svg")
    tone = next((tone for suffix, tone in _ICON_TONES if stem.endswith(suffix)), "blue")
    label = tile.icon_label
    label.setFixedSize(side, side)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setPixmap(design_icon(icon).pixmap(18, 18))
    set_style_property(label, "iconTone", tone)
    if getattr(tile, "_action_body_indent", None) is None:
        tile._action_body_indent = _BodyIndent(tile, side + 12 if indent_body else 0)


def input_field(widget: QWidget) -> QWidget:
    """Give a text box the Settings field look (theme block automation:G)."""
    widget.setProperty("tileId", "actionInput")
    return widget


def page_link(text: str, tooltip: str = "") -> QPushButton:
    """A flat accent link, like the other Settings pages' "Open …" links."""
    link = QPushButton(text)
    link.setObjectName("actionLink")
    link.setFlat(True)
    link.setCursor(Qt.CursorShape.PointingHandCursor)
    if tooltip:
        link.setToolTip(tooltip)
    return link


def plain_row(*widgets, spacing: int = 8, stretch_last: bool = False) -> QWidget:
    """A transparent row of widgets, so it never paints the page colour inside a card."""
    row = QWidget()
    row.setProperty("tileId", "actionRow")
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    for widget in widgets:
        if widget is None:
            layout.addStretch(1)
        else:
            layout.addWidget(widget)
    if stretch_last:
        layout.addStretch(1)
    return row


class ActionButtons(ButtonRow):
    """Buttons at their own width in one row, wrapping into more rows when narrow."""

    def __init__(self, buttons, *, spacing: int = 8, parent=None):
        super().__init__(buttons, spacing=spacing, parent=parent)
        # Its own width when there's room, so a footer never stretches across a card.
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

    def sizeHint(self) -> QSize:
        # Always the one-row width, so whoever places the row (a Reflow, a
        # stretch) decides the same way however it is wrapped right now.
        visible = [button for button in self._buttons if not button.isHidden()]
        width = sum(max(b.minimumWidth(), b.sizeHint().width()) for b in visible)
        width += self._spacing * max(0, len(visible) - 1)
        return QSize(width, super().sizeHint().height())


def button_row(*buttons, align: str = "left") -> QWidget:
    """``buttons`` side by side at their own width, wrapping when the column is narrow."""
    for button in buttons:
        fit_compact_button(button)
    row = ActionButtons(buttons)
    box = QWidget()
    box.setProperty("tileId", "actionRow")
    layout = QHBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    if align == "right":
        layout.addStretch(1)
    layout.addWidget(row)
    if align == "left":
        layout.addStretch(1)
    box.buttons = row
    return box


def section_heading(text: str) -> QWidget:
    """A hairline and a small heading that open a part of a long card."""
    box = QWidget()
    box.setProperty("tileId", "actionRow")
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 8, 0, 0)
    layout.setSpacing(10)
    line = QFrame()
    line.setObjectName("actionDivider")
    line.setFixedHeight(1)
    layout.addWidget(line)
    label = QLabel(text)
    label.setObjectName("actionSectionLabel")
    layout.addWidget(label)
    return box


def stack(*widgets, spacing: int = 6) -> QWidget:
    """A transparent column of widgets: a label, its field and its hint."""
    box = QWidget()
    box.setProperty("tileId", "actionRow")
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    for widget in widgets:
        layout.addWidget(widget)
    return box


class Columns(QWidget):
    """Two columns side by side while each gets ``min_column`` px (at 100%), else stacked."""

    def __init__(self, left: QWidget, right: QWidget, *, min_column: int = 260,
                 spacing: int = 18, parent=None):
        super().__init__(parent)
        self.setProperty("tileId", "actionRow")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._min_column = min_column
        self._layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(spacing)
        self._layout.addWidget(left, 1, Qt.AlignmentFlag.AlignTop)
        self._layout.addWidget(right, 1, Qt.AlignmentFlag.AlignTop)

    @property
    def stacked(self) -> bool:
        return self._layout.direction() == QBoxLayout.Direction.TopToBottom

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        needed = 2 * round(self._min_column * current_ui_font_scale()) + self._layout.spacing()
        direction = (QBoxLayout.Direction.TopToBottom if self.width() < needed
                     else QBoxLayout.Direction.LeftToRight)
        if self._layout.direction() != direction:
            self._layout.setDirection(direction)


# ---- file pickers (patched by tests) ----

def choose_folder(parent: QWidget, title: str, start: str) -> str:
    return QFileDialog.getExistingDirectory(parent, title, start)


def choose_program(parent: QWidget, title: str, start: str) -> str:
    path, _filter = QFileDialog.getOpenFileName(parent, title, start)
    return path


class PathField(QWidget):
    """A full path, chosen with a picker: a folder or a program."""

    changed = pyqtSignal(str)

    def __init__(self, title: str, *, folder: bool, placeholder: str, parent=None):
        super().__init__(parent)
        self.setProperty("tileId", "actionPathField")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._title = title
        self._folder = folder
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.line = input_field(QLineEdit())
        self.line.setReadOnly(True)
        self.line.setPlaceholderText(placeholder)
        self.line.setAccessibleName(title)
        self.line.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.choose_button = neutral_button(Button("Choose…"))
        self.choose_button.setAccessibleName(f"Choose {title.lower()}")
        self.choose_button.clicked.connect(self._choose)
        self.clear_button = neutral_button(Button("Clear"))
        self.clear_button.setAccessibleName(f"Clear {title.lower()}")
        self.clear_button.clicked.connect(lambda: self._set(""))
        # The buttons move under the path when the column is narrow.
        self.buttons = button_row(self.choose_button, self.clear_button)
        layout.addWidget(Reflow(self.line, self.buttons, lead_width=160,
                                stacked_alignment=Qt.AlignmentFlag.AlignLeft))
        self.set_path("")

    def path(self) -> str:
        return self.line.text()

    def set_path(self, path: str) -> None:
        self.line.setText(path or "")
        self.line.setToolTip(path or "")
        self.line.setCursorPosition(0)
        self.clear_button.setVisible(bool(path))

    def _set(self, path: str) -> None:
        if path != self.path():
            self.set_path(path)
            self.changed.emit(path)

    def _choose(self) -> None:
        pick = choose_folder if self._folder else choose_program
        chosen = pick(self.window(), self._title, self.path())
        if chosen:
            import os

            self._set(os.path.normpath(chosen))


class ShortcutField(QWidget):
    """A shortcut box with Clear; the owner checks conflicts and saves."""

    captured = pyqtSignal(str)
    capture_changed = pyqtSignal(bool)

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self.setProperty("tileId", "actionShortcutField")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.input = ProfileHotkeyInput()
        self.input.setAccessibleName(name)
        self.input.setToolTip(_SHORTCUT_TIP)
        self.input.setPlaceholderText("Click to set a shortcut")
        self.input.setMinimumWidth(0)
        self.input.capture_changed.connect(self._on_capture_changed)
        self.input.captured.connect(self.captured)
        self.clear_button = neutral_button(Button("Clear"))
        fit_compact_button(self.clear_button, 64)
        self.clear_button.setAccessibleName(f"Clear {name.lower()}")
        self.clear_button.clicked.connect(lambda: self.captured.emit(""))
        # Clear moves under the box when the column is narrow.
        self.row = Reflow(self.input, self.clear_button, lead_width=170,
                          stacked_alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.row)
        self._caps = ((self.row, SHORTCUT_MAX_WIDTH),)
        cap_widths(self._caps)
        self._before = ""

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if _font_changed(event) and hasattr(self, "_caps"):
            cap_widths(self._caps)

    @property
    def hotkey(self) -> str:
        return self.input.hotkey

    def set_hotkey(self, hotkey: str) -> None:
        self.input.set_hotkey(hotkey or "")
        self.clear_button.setEnabled(bool(hotkey))

    def restore(self) -> None:
        """Put back the shortcut from before the last capture (it was refused)."""
        self.set_hotkey(self._before)

    def begin_capture(self) -> None:
        self.input.setFocus()
        self.input.begin_capture()

    def cancel_capture(self) -> None:
        self.input.cancel_capture()

    def _on_capture_changed(self, capturing: bool) -> None:
        if capturing:
            self._before = self.input.hotkey
        self.capture_changed.emit(capturing)


class RouteShortcutField(CommandShortcutField):
    """The Route by voice shortcut, saved like any standard one (dialog.set_standard_hotkey)."""

    ACTION = "route_record"

    def __init__(self, dialog, parent=None):
        super().__init__(dialog, parent=parent)
        self.setProperty("tileId", "actionRouteShortcut")
        self.input.setAccessibleName("Route by voice shortcut")
        self.input.setToolTip("Click, then press a shortcut. Escape cancels.")
        self.clear_button.setToolTip("Remove the Route by voice shortcut")

    def hotkey(self) -> str:
        hotkeys = self.dialog.current_hotkeys or {}
        if not hotkeys:
            stored = self.dialog._settings_snapshot().get("hotkeys")
            hotkeys = stored if isinstance(stored, dict) else {}
        value = hotkeys.get(self.ACTION, "")
        return value if isinstance(value, str) else ""

    def _save(self, hotkey: str) -> None:
        error = self.dialog.set_standard_hotkey(self.ACTION, hotkey)
        self.show_error(error)
        self.sync()


class SwitchRow(QFrame):
    """A title, one line of detail and a switch; the whole row toggles it."""

    def __init__(self, title: str, detail: str, parent=None):
        super().__init__(parent)
        self.setObjectName("actionSwitchRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(12)
        text = QVBoxLayout()
        text.setSpacing(1)
        self.title_label = WrappedLabel(title)
        self.title_label.setObjectName("settingsTileTitle")
        text.addWidget(self.title_label)
        self.detail_label = WrappedLabel(detail)
        self.detail_label.setObjectName("settingsTileDescription")
        text.addWidget(self.detail_label)
        layout.addLayout(text, 1)
        self.switch = SettingsSwitch()
        self.switch.setAccessibleName(title)
        self.switch.setAccessibleDescription(detail)
        layout.addWidget(self.switch, alignment=Qt.AlignmentFlag.AlignTop)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.switch.isEnabled():
            self.switch.toggle()
            event.accept()
            return
        super().mousePressEvent(event)


class OutputCombo(ElidingComboBox):
    """Where an answer, reply or output goes."""

    MINIMUM_WIDTH = 110

    def __init__(self, name: str, *, allow_none: bool = True, parent=None):
        super().__init__(parent)
        self.setObjectName("actionOutputCombo")
        self.setAccessibleName(name)
        self.setMinimumHeight(36)
        for value in Output.ALL:
            if value == Output.NONE and not allow_none:
                continue
            self.addItem(OUTPUT_LABELS[value], value)
        cap_widths(((self, COMBO_MAX_WIDTH),))

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if _font_changed(event):
            cap_widths(((self, COMBO_MAX_WIDTH),))

    def value(self) -> str:
        return self.currentData() or Output.NONE

    def set_value(self, value: str) -> None:
        index = self.findData(value)
        self.setCurrentIndex(max(0, index))


# ---- the agent chooser ----

def _agent_spec(agent_id: str):
    from services.installed_agents import AGENT_SPECS

    return AGENT_SPECS.get(agent_id)


class AgentChoice(QPushButton):
    """One installed agent: its mark, its name, and whether the scan found it."""

    def __init__(self, agent_id: str, parent=None):
        from ui_qt.widgets.agent_picker import AgentMark

        super().__init__(parent)
        self.agent_id = agent_id
        self.setObjectName("actionAgentChoice")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        name = AGENT_NAMES.get(agent_id, agent_id)
        self.setAccessibleName(name)
        spec = _agent_spec(agent_id)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 7, 14, 7)
        layout.setSpacing(10)
        self.mark = AgentMark(spec.monogram if spec else name[:2], spec.accent if spec else "#0a84ff")
        layout.addWidget(self.mark, alignment=Qt.AlignmentFlag.AlignVCenter)
        text = QVBoxLayout()
        text.setSpacing(0)
        self.name_label = ElidingLabel(name)
        self.name_label.setObjectName("actionAgentName")
        self.detail_label = ElidingLabel("Checking…")
        self.detail_label.setObjectName("actionAgentDetail")
        for label in (self.name_label, self.detail_label):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            text.addWidget(label)
        layout.addLayout(text, 1)
        self.found: Optional[bool] = None
        self._arrival = QVariantAnimation(self)
        self._arrival.setDuration(ARRIVAL_MS)
        self._arrival.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._arrival.valueChanged.connect(self._on_arrival)
        self.toggled.connect(lambda checked: set_style_property(self.name_label, "selected", checked))

    def set_found(self, found: Optional[bool], *, animate: bool = False) -> None:
        """None while checking; the mark lights up in the agent's colour once found."""
        self.found = found
        self.detail_label.setText(
            "Checking…" if found is None else "Found" if found else "Not found")
        set_style_property(self.detail_label, "tone",
                           "found" if found else "missing" if found is False else "")
        self.setAccessibleDescription(self.detail_label.text())
        self._arrival.stop()
        self.mark.set_missing(1.0 if found is False else 0.0)
        if found and animate:
            self._arrival.setStartValue(self.mark.warmth)
            self._arrival.setEndValue(1.0)
            self._arrival.start()
        else:
            self.mark.set_warmth(1.0 if found else 0.0)

    def _on_arrival(self, value) -> None:
        self.mark.set_warmth(float(value))

    def sizeHint(self) -> QSize:
        return self.layout().sizeHint().expandedTo(QSize(0, 44))

    def minimumSizeHint(self) -> QSize:
        return QSize(self.layout().minimumSize().width(), self.sizeHint().height())

    def paintEvent(self, event) -> None:
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = ""  # The child labels carry the text.
        painter = QStylePainter(self)
        painter.drawControl(QStyle.ControlElement.CE_PushButton, option)


class AgentChooser(QWidget):
    """Claude Code, Codex and OpenCode; only the ones found can be chosen."""

    chosen = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("tileId", "actionAgentChooser")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.choices = {agent_id: AgentChoice(agent_id) for agent_id in AGENTS}
        for choice in self.choices.values():
            choice.clicked.connect(lambda _checked=False, c=choice: self._on_clicked(c.agent_id))
        # Side by side while their names fit, else one above the other.
        self._layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(8)
        for choice in self.choices.values():
            self._layout.addWidget(choice, 1)
        self._current = AGENTS[0]
        self._available: Optional[dict] = None
        self._sync()

    def current(self) -> str:
        return self._current

    @property
    def stacked(self) -> bool:
        return self._layout.direction() == QBoxLayout.Direction.TopToBottom

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        needed = sum(choice.sizeHint().width() for choice in self.choices.values())
        needed += self._layout.spacing() * (len(self.choices) - 1)
        direction = (QBoxLayout.Direction.TopToBottom if self.width() < needed
                     else QBoxLayout.Direction.LeftToRight)
        if self._layout.direction() != direction:
            self._layout.setDirection(direction)

    def set_current(self, agent_id: str) -> None:
        self._current = agent_id if agent_id in self.choices else AGENTS[0]
        self._sync()

    def available(self) -> Optional[dict]:
        return None if self._available is None else dict(self._available)

    def set_available(self, available: Optional[Mapping], *, animate: bool = False) -> None:
        """``None`` while looking; otherwise ``{agent_id: found}``."""
        self._available = None if available is None else {
            agent_id: bool(available.get(agent_id)) for agent_id in AGENTS
        }
        for agent_id, choice in self.choices.items():
            found = None if self._available is None else self._available[agent_id]
            choice.set_found(found, animate=animate)
        self._sync()

    def found(self) -> list[str]:
        return [agent_id for agent_id in AGENTS if (self._available or {}).get(agent_id)]

    def _sync(self) -> None:
        for agent_id, choice in self.choices.items():
            choice.setChecked(agent_id == self._current)
            # The chosen agent stays shown even when it's missing, so the
            # card never claims a different agent than the one saved.
            choice.setEnabled(bool((self._available or {}).get(agent_id)))

    def _on_clicked(self, agent_id: str) -> None:
        if agent_id != self._current:
            self._current = agent_id
            self._sync()
            self.chosen.emit(agent_id)
        else:
            self._sync()


class AgentFields(QWidget):
    """What an agent action does: which agent, a session or an answer, where, and with what."""

    changed = pyqtSignal()
    styles_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("tileId", "actionAgentFields")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._loading = False
        self._extra: dict = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.chooser = AgentChooser()
        self.chooser.chosen.connect(self._emit_changed)
        layout.addWidget(field_label("Agent"))
        layout.addWidget(self.chooser)
        self.agent_note = hint_label("")
        self.agent_note.setProperty("tone", "warning")
        self.agent_note.hide()
        layout.addWidget(self.agent_note)

        # Everything after the agent, hidden while the card only invites setup.
        self.details = QWidget()
        self.details.setProperty("tileId", "actionRow")
        outer, layout = layout, QVBoxLayout(self.details)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(6)
        outer.addWidget(self.details)
        layout.addWidget(field_label("When you finish speaking"))
        self.mode_bar = SegmentedBar(
            tuple((MODE_LABELS[mode], "") for mode in AgentMode.ALL), compact=True)
        self.mode_bar.setAccessibleName("Agent mode")
        self.mode_bar.currentIndexChanged.connect(self._on_mode_changed)
        layout.addWidget(self.mode_bar)
        self.mode_hint = hint_label("")
        layout.addWidget(self.mode_hint)

        self.session_box = QWidget()
        self.session_box.setProperty("tileId", "actionRow")
        session = QVBoxLayout(self.session_box)
        session.setContentsMargins(0, 4, 0, 0)
        session.setSpacing(6)
        session.addWidget(field_label("Opens in"))
        self.surface_bar = SegmentedBar(
            tuple((SURFACE_LABELS[surface], "") for surface in AgentSurface.ALL), compact=True)
        self.surface_bar.setAccessibleName("Where the session opens")
        self.surface_bar.currentIndexChanged.connect(self._on_surface_changed)
        session.addWidget(self.surface_bar)
        self.surface_hint = hint_label("")
        session.addWidget(self.surface_hint)
        layout.addWidget(self.session_box)

        self.answer_box = QWidget()
        self.answer_box.setProperty("tileId", "actionRow")
        answer = QHBoxLayout(self.answer_box)
        answer.setContentsMargins(0, 4, 0, 0)
        answer.setSpacing(12)
        output_column = QVBoxLayout()
        output_column.setSpacing(6)
        self.output_combo = OutputCombo("Where the answer goes", allow_none=False)
        self.output_combo.activated.connect(self._emit_changed)
        output_column.addWidget(field_label("Answer goes to", self.output_combo))
        output_column.addWidget(self.output_combo)
        answer.addLayout(output_column, 1)
        model_column = QVBoxLayout()
        model_column.setSpacing(6)
        self.model_edit = input_field(QLineEdit())
        self.model_edit.setPlaceholderText("The agent's default")
        self.model_edit.setAccessibleName("Model for quick answers")
        self.model_edit.setMaxLength(120)
        self.model_edit.editingFinished.connect(self._on_model_edited)
        model_column.addWidget(field_label("Model (optional)", self.model_edit))
        model_column.addWidget(self.model_edit)
        answer.addLayout(model_column, 1)
        layout.addWidget(self.answer_box)

        layout.addSpacing(4)
        layout.addWidget(field_label("Working folder"))
        self.folder = PathField("Working folder", folder=True,
                                placeholder="Optional · where the agent starts")
        self.folder.changed.connect(self._emit_changed)
        layout.addWidget(self.folder)

        layout.addSpacing(4)
        self.selection_row = SwitchRow(
            "Include what I've selected",
            "Sends the selected text and the app's name along with your words.",
        )
        self.selection_row.switch.toggled.connect(self._emit_changed)
        layout.addWidget(self.selection_row)
        self.selection_note = QWidget()
        self.selection_note.setProperty("tileId", "actionRow")
        note = QHBoxLayout(self.selection_note)
        note.setContentsMargins(0, 0, 0, 0)
        note.setSpacing(8)
        self.selection_hint = hint_label("Needs “Know which app I'm dictating into” on Styles.")
        self.selection_hint.setProperty("tone", "warning")
        note.addWidget(self.selection_hint, 1)
        self.styles_link = page_link("Open Styles", "Turn on app awareness")
        self.styles_link.clicked.connect(self.styles_requested)
        note.addWidget(self.styles_link, alignment=Qt.AlignmentFlag.AlignTop)
        self.selection_note.hide()
        layout.addWidget(self.selection_note)
        self._app_awareness = True
        self._caps = ((self.chooser, CHOOSER_MAX_WIDTH), (self.mode_bar, BAR_MAX_WIDTH),
                      (self.surface_bar, BAR_MAX_WIDTH), (self.answer_box, CHOOSER_MAX_WIDTH),
                      (self.folder, CHOOSER_MAX_WIDTH), (self.selection_row, CHOOSER_MAX_WIDTH),
                      (self.selection_note, CHOOSER_MAX_WIDTH))
        cap_widths(self._caps)
        self.set_params({})

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if _font_changed(event) and hasattr(self, "_caps"):
            cap_widths(self._caps)

    # ---- values ----

    def params(self) -> dict:
        """The agent parameters, keeping any this form doesn't show."""
        mode = AgentMode.ALL[max(0, self.mode_bar.currentIndex())]
        return {
            **self._extra,
            "agent": self.chooser.current(),
            "mode": mode,
            "surface": AgentSurface.ALL[max(0, self.surface_bar.currentIndex())],
            "folder": self.folder.path(),
            "include_selection": self.selection_row.switch.isChecked(),
            "model": self.model_edit.text().strip(),
            "output": self.output_combo.value(),
        }

    def set_params(self, params: Mapping) -> None:
        from services.actions.model import PARAMS, ActionKind

        values = {**PARAMS[ActionKind.AGENT], **dict(params or {})}
        self._extra = {k: v for k, v in values.items() if k not in PARAMS[ActionKind.AGENT]}
        self._loading = True
        try:
            self.chooser.set_current(values["agent"])
            mode = values["mode"] if values["mode"] in AgentMode.ALL else AgentMode.HANDOFF
            self.mode_bar.setCurrentIndex(AgentMode.ALL.index(mode))
            surface = values["surface"] if values["surface"] in AgentSurface.ALL else AgentSurface.AUTO
            self.surface_bar.setCurrentIndex(AgentSurface.ALL.index(surface))
            output = values["output"] if values["output"] != Output.NONE else Output.SCRATCHPAD
            self.output_combo.set_value(output)
            self.model_edit.setText(values["model"] or "")
            self.folder.set_path(values["folder"] or "")
            self.selection_row.switch.setChecked(bool(values["include_selection"]))
        finally:
            self._loading = False
        self._sync()

    def set_available(self, available: Optional[Mapping], *, animate: bool = False) -> None:
        self.chooser.set_available(available, animate=animate)
        self._sync()

    def set_app_awareness(self, on: bool) -> None:
        self._app_awareness = bool(on)
        self._sync()

    # ---- reactions ----

    def _sync(self) -> None:
        handoff = self.mode_bar.currentIndex() != AgentMode.ALL.index(AgentMode.ASK)
        name = AGENT_NAMES.get(self.chooser.current(), "the agent")
        self.mode_hint.setText(
            f"{name} opens with your words, ready for you to send." if handoff
            else f"{name} answers with no tools, so nothing on this computer changes.")
        self.session_box.setVisible(handoff)
        self.answer_box.setVisible(not handoff)
        surface = AgentSurface.ALL[max(0, self.surface_bar.currentIndex())]
        self.surface_hint.setText(SURFACE_HINTS[surface])
        self.selection_note.setVisible(
            self.selection_row.switch.isChecked() and not self._app_awareness)
        available = self.chooser.available()
        missing = available is not None and not available.get(self.chooser.current())
        if missing and self.chooser.found():
            self.agent_note.setText(f"{name} wasn't found on this computer. Choose another agent.")
        elif missing:
            self.agent_note.setText(f"{name} wasn't found on this computer.")
        self.agent_note.setVisible(missing)

    def _emit_changed(self, *_args) -> None:
        self._sync()
        if not self._loading:
            self.changed.emit()

    def _on_mode_changed(self, _index: int) -> None:
        self._emit_changed()

    def _on_surface_changed(self, _index: int) -> None:
        self._emit_changed()

    def _on_model_edited(self) -> None:
        if self.model_edit.isModified():
            self.model_edit.setModified(False)
            self._emit_changed()


# ---- how the words are prepared ----

_SHAPING_KINDS = ("cleanup", "raw", "profile", "transform")
_SHAPING_HINTS = {
    "cleanup": "Your usual AI cleanup, whenever it's on.",
    "raw": "Exactly what you said, after your dictionary and snippets.",
    "profile": "Formatted by one of your cleanup profiles.",
    "transform": "Cleaned up, then rewritten by one of your transforms.",
}


class ShapingField(QWidget):
    """Raw, cleaned up, a cleanup profile, or a transform."""

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("tileId", "actionShapingField")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.bar = SegmentedBar(
            (("Cleaned up", ""), ("Raw", ""), ("Profile", ""), ("Transform", "")), compact=True)
        self.bar.setAccessibleName("How the words are prepared")
        self.bar.currentIndexChanged.connect(self._on_kind_changed)
        layout.addWidget(self.bar)
        self.combo = ElidingComboBox()
        self.combo.setObjectName("actionShapingCombo")
        self.combo.setMinimumHeight(36)
        self.combo.activated.connect(lambda _index: self.changed.emit())
        layout.addWidget(self.combo)
        self.hint = hint_label("")
        layout.addWidget(self.hint)
        self._profiles: list = []
        self._transforms: list = []
        self._loading = False
        self._caps = ((self.bar, BAR_MAX_WIDTH + 80), (self.combo, COMBO_MAX_WIDTH))
        cap_widths(self._caps)
        self._sync()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if _font_changed(event) and hasattr(self, "_caps"):
            cap_widths(self._caps)

    def set_choices(self, settings: Mapping) -> None:
        from services.cleanup_profiles import load_cleanup_profiles
        from services.text_transforms import load_transforms

        value = self.value()
        self._profiles = [(p.id, p.name) for p in load_cleanup_profiles(dict(settings))]
        self._transforms = [(t.id, t.name) for t in load_transforms(settings)]
        self.bar.buttons[2].setEnabled(bool(self._profiles))
        self.bar.buttons[3].setEnabled(bool(self._transforms))
        self.bar.buttons[2].setToolTip("" if self._profiles else "You have no cleanup profiles yet.")
        self.bar.buttons[3].setToolTip("" if self._transforms else "You have no transforms yet.")
        self.set_value(value)

    def kind(self) -> str:
        return _SHAPING_KINDS[max(0, self.bar.currentIndex())]

    def value(self) -> str:
        kind = self.kind()
        if kind == "raw":
            return SHAPING_RAW
        if kind in ("profile", "transform"):
            item = self.combo.currentData()
            return f"{kind}:{item}" if item else SHAPING_CLEANUP
        return SHAPING_CLEANUP

    def set_value(self, shaping: str) -> None:
        shaping = shaping or SHAPING_CLEANUP
        kind, _sep, item = shaping.partition(":")
        if shaping == SHAPING_RAW:
            kind = "raw"
        elif kind not in ("profile", "transform"):
            kind = "cleanup"
        self._loading = True
        try:
            self.bar.setCurrentIndex(_SHAPING_KINDS.index(kind))
            self._fill_combo(kind, item)
        finally:
            self._loading = False
        self._sync()

    def _fill_combo(self, kind: str, selected: str = "") -> None:
        self.combo.clear()
        items = self._profiles if kind == "profile" else self._transforms if kind == "transform" else []
        for item_id, name in items:
            self.combo.addItem(name, item_id)
        if selected and self.combo.findData(selected) < 0:
            # Deleted since it was chosen: say so instead of quietly picking
            # another; saving asks for a new choice.
            noun = "Profile" if kind == "profile" else "Transform"
            self.combo.insertItem(0, f"{noun} no longer exists", selected)
            self.combo.setCurrentIndex(0)
        elif selected:
            self.combo.setCurrentIndex(self.combo.findData(selected))
        self.combo.setAccessibleName("Cleanup profile" if kind == "profile" else "Transform")

    def _on_kind_changed(self, _index: int) -> None:
        if self._loading:
            return
        self._fill_combo(self.kind())
        self._sync()
        self.changed.emit()

    def _sync(self) -> None:
        kind = self.kind()
        self.combo.setVisible(kind in ("profile", "transform"))
        self.hint.setText(_SHAPING_HINTS[kind])

"""The Walkthrough tab: its settings down the left, the walkthrough on the right.

Two panes that never fold or swap. The left column is one form read top to
bottom: Start and the two shortcuts, then what happens while recording
(Auto Mode, the capture delay, Minimize to tray), who writes it up and how
(the AI write-up, its agent and style, whether it follows spoken notes, and
the prompt itself in a popover), and what is copied when it finishes. These
are the choices people change between walkthroughs; the rest (shortcuts, how
the document looks) stays in Settings → Walkthroughs, one link away.

The right side is a stage with one slot. It shows the storyboard of the
newest walkthrough, or the one picked in the side panel, to fix, reorder and
copy again (``walkthrough_review.py``); a Recording card while one records;
or, with none open, how a walkthrough works and the three newest to open.
Changing a setting never moves the stage, and the stage changing never moves
the settings or resizes the window. In a window too narrow for both, the
column goes above the stage. See docs/walkthrough.md.
"""
from __future__ import annotations

import itertools
import logging
import sys
import threading
from datetime import date

from PyQt6.QtCore import QEvent, QObject, QPoint, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QFont, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from services.installed_agents import AGENT_SPECS
from services.settings import (
    SettingsKey,
    WalkthroughCopyFormat,
    resolve_walkthrough_auto_mode,
    resolve_walkthrough_capture_delay,
    resolve_walkthrough_copy_format,
    resolve_walkthrough_intro_seen,
    resolve_walkthrough_minimize_to_tray,
    resolve_walkthrough_write_up,
    resolve_walkthrough_write_up_agent,
    resolve_walkthrough_write_up_instructions,
    resolve_walkthrough_write_up_notes,
    settings_manager,
)
from services.walkthrough import library
from services.walkthrough.storage import captures_root
from services.walkthrough.styles import (
    DEFAULT_STYLE,
    delete_style,
    load_styles,
    save_style,
    style_for,
)
from services.walkthrough.write_up import AGENTS, DEFAULT_INSTRUCTIONS
from ui_qt.dialogs.settings_destinations import WALKTHROUGHS
from ui_qt.utils.palette import token_color
from ui_qt.widgets import past_walkthroughs
from ui_qt.widgets.agent_picker import FOUND, describe_agent
from ui_qt.widgets.buttons import SuccessButton, WarningButton
from ui_qt.widgets.capture_delay import CaptureDelay
from ui_qt.widgets.context_menu import CONTEXT_MENU_STYLESHEET
from ui_qt.widgets.eliding_label import ElidingLabel
from ui_qt.widgets.feature_badge import FeatureBadge
from ui_qt.widgets.no_wheel import NoWheelComboBox
from ui_qt.widgets.settings_switch import SettingsSwitch
from ui_qt.widgets.studio import (
    COLUMN_WIDTH,
    GAP,
    STAGE_WIDTH,
    Stage,
    StudioSplit,
)
from ui_qt.widgets.studio import scaled as _scaled
from ui_qt.widgets.walkthrough_copy_tips import set_copy_tip
from ui_qt.widgets.walkthrough_review import WalkthroughReview, _day, _time

logger = logging.getLogger(__name__)

#: The style menu's item while the prompt is the user's own words.
OWN_PROMPT = "Your own"
#: The style menu's last item, which makes a style from scratch.
NEW_STYLE = "New style…"
#: Its item data: not a style id (hex or a slug) and not "", the user's own.
_NEW_STYLE_DATA = ":new"
#: How long typing in the prompt pauses before it is saved.
PROMPT_SAVE_DELAY_MS = 400

#: Copy choices in the order the menu lists them.
COPY_CHOICES = (
    (WalkthroughCopyFormat.PDF, "PDF"),
    (WalkthroughCopyFormat.AGENT_TASK, "Agent"),
    (WalkthroughCopyFormat.BOTH, "PDF + Markdown"),
    (WalkthroughCopyFormat.AGENT, "Markdown"),
    (WalkthroughCopyFormat.WORD, "Word"),
)

# Sizes at 100% type; each grows with the font scale.
#: The values down the column's right edge, so they line up as one form.
FIELD_WIDTH = 164
FIELD_HEIGHT = 34
ROW_HEIGHT = 36
#: Under Start: the two shortcut rows, or Cancel while a walkthrough records.
KEYS_SLOT_HEIGHT = 68
#: How many walkthroughs the empty stage offers to open.
RECENT_SHOWN = 3
RECENT_THUMB = QSize(76, 48)
#: The prompt popover.
PROMPT_POPOVER_WIDTH = 520

_STYLE = """
    QFrame#walkthroughColumn {
        background-color: @surface;
        border: 1px solid @border;
        border-radius: 14px;
    }
    QFrame#walkthroughStagePane {
        background-color: @surface-sunken;
        border: 1px solid @border-subtle;
        border-radius: 14px;
    }
    QWidget#walkthroughStudio, QWidget#walkthroughStage, QWidget#walkthroughPlain {
        background: transparent;
        border: none;
    }
    QFrame#walkthroughRule {
        background-color: @border;
        border: none;
    }
    QLabel#walkthroughOverline {
        color: @text-secondary;
        font-size: 11px;
        font-weight: 700;
        background: transparent;
    }
    QLabel#walkthroughRowName {
        color: @text;
        font-size: 13px;
        background: transparent;
    }
    QLabel#walkthroughRowName:disabled {
        color: @text-muted;
    }
    QLabel#walkthroughKeyName {
        color: @text-secondary;
        font-size: 12px;
        background: transparent;
    }
    QLabel#walkthroughKey {
        color: @text;
        background-color: @slate-field;
        border: 1px solid @slate-border;
        border-bottom-width: 2px;
        border-radius: 6px;
        padding: 1px 7px;
        font-size: 12px;
        font-weight: 600;
    }
    QLabel#walkthroughKey[unset="true"] {
        color: @text-muted;
        font-weight: 400;
    }
    QPushButton#walkthroughField {
        color: @text;
        background-color: @surface;
        border: 1px solid @border;
        border-radius: 10px;
        padding: 0 30px 0 12px;
        font-size: 12px;
        font-weight: 500;
        min-height: 0;
        text-align: left;
    }
    QPushButton#walkthroughField:hover {
        background-color: @surface-hover;
    }
    QPushButton#walkthroughField:focus {
        border-color: @accent-tint-border;
    }
    QPushButton#walkthroughField[tone="warning"] {
        color: @warning-text;
        border-color: rgba(@warning-rgb, 0.55);
    }
    QPushButton#walkthroughField:disabled {
        color: @text-muted;
        border-color: @border-subtle;
    }
    QPushButton#walkthroughSettingsLink:disabled, QPushButton#walkthroughPromptLink:disabled {
        color: @text-muted;
    }
    QPushButton#walkthroughPromptLink {
        color: @accent;
        background-color: transparent;
        border: none;
        font-size: 12px;
        font-weight: 600;
        padding: 2px 0;
        min-height: 0;
    }
    QPushButton#walkthroughPromptLink:hover, QPushButton#walkthroughPromptLink:focus {
        color: @accent-soft;
        text-decoration: underline;
    }
    QLabel#walkthroughHeading {
        color: @text-heading;
        font-size: 18px;
        font-weight: 700;
        background: transparent;
    }
    QLabel#walkthroughTitle {
        color: @text;
        font-size: 14px;
        font-weight: 700;
        background: transparent;
    }
    QLabel#walkthroughHint {
        color: @text-secondary;
        font-size: 12px;
        background: transparent;
    }
    QLabel#walkthroughStep {
        color: @accent-soft;
        background-color: rgba(@accent-rgb, 0.16);
        border-radius: 11px;
        font-size: 12px;
        font-weight: 700;
        min-width: 22px; max-width: 22px;
        min-height: 22px; max-height: 22px;
    }
    QFrame#walkthroughRecent {
        background-color: transparent;
        border: 1px solid transparent;
        border-radius: 10px;
    }
    QFrame#walkthroughRecent:hover, QFrame#walkthroughRecent:focus {
        background-color: rgba(@overlay-rgb, 0.06);
        border-color: @border;
    }
    QLabel#walkthroughThumb {
        background-color: @slate-field;
        border-radius: 6px;
    }
    QLabel#walkthroughPill {
        color: @accent-soft;
        background-color: rgba(@accent-rgb, 0.12);
        border-radius: 8px;
        padding: 1px 8px;
        font-size: 11px;
        font-weight: 600;
    }
    QLabel#walkthroughPill[tone="warning"] {
        color: @warning-text;
        background-color: rgba(@warning-rgb, 0.16);
    }
    QLabel#walkthroughRecordingDot {
        background-color: @danger;
        border-radius: 6px;
        min-width: 12px; max-width: 12px;
        min-height: 12px; max-height: 12px;
    }
    QLabel#walkthroughRecordingKeys {
        color: @text;
        font-size: 14px;
        background: transparent;
    }
    QFrame#walkthroughPopover {
        background-color: @surface;
        border: 1px solid @border-strong;
        border-radius: 14px;
    }
    QWidget#walkthroughPopoverFrame {
        background: transparent;
    }
    QLabel#walkthroughPromptTitle {
        color: @text;
        font-size: 13px;
        font-weight: 700;
        background: transparent;
    }
    QComboBox#walkthroughPreset {
        padding: 4px 10px;
        font-size: 12px;
        min-width: 150px;
    }
    QPushButton#walkthroughStyleLink {
        color: @accent;
        background-color: transparent;
        border: none;
        font-size: 12px;
        font-weight: 600;
        padding: 2px 0;
        min-height: 0;
    }
    QPushButton#walkthroughStyleLink:hover,
    QPushButton#walkthroughStyleLink:focus {
        color: @accent-soft;
        text-decoration: underline;
    }
    QPlainTextEdit#walkthroughPrompt {
        color: @text;
        background-color: @slate-field;
        border: 1px solid @slate-border;
        border-radius: 10px;
        padding: 6px 8px;
        font-size: 13px;
    }
    QPlainTextEdit#walkthroughPrompt:focus {
        border-color: @accent;
    }
"""


def _label(text: str, name: str, *, wrap: bool = False) -> QLabel:
    label = QLabel(text)
    label.setObjectName(name)
    label.setWordWrap(wrap)
    if name == "walkthroughOverline":
        font = label.font()
        font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 108)
        label.setFont(font)
    return label


def _plain() -> QWidget:
    widget = QWidget()
    widget.setObjectName("walkthroughPlain")
    return widget


def _rule() -> QFrame:
    rule = QFrame()
    rule.setObjectName("walkthroughRule")
    rule.setFixedHeight(1)
    return rule


def _key_chip(text: str) -> QLabel:
    chip = QLabel(text)
    chip.setObjectName("walkthroughKey")
    chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return chip


def _restyle(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def _menu(parent: QWidget) -> QMenu:
    menu = QMenu(parent)
    # Opaque: these open over the form and the storyboard, which would show through.
    menu.setStyleSheet(CONTEXT_MENU_STYLESHEET.replace("rgba(@surface-rgb, 0.95)", "@surface"))
    menu.setToolTipsVisible(True)
    return menu


class Field(QPushButton):
    """One value in the form: its text at a fixed width, a menu to change it.

    ``build_menu`` makes the menu each time it opens, from the choices as they
    are then, so a scan, a saved style or a change in Settings never leaves
    it stale. The chevron is drawn to match the capture delay's own chip,
    which shares the column.
    """

    def __init__(self, build_menu, name: str):
        super().__init__()
        self.setObjectName("walkthroughField")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.build_menu = build_menu
        self._name = name
        self.value = ""
        self.clicked.connect(self._open)

    def set_value(self, text: str, *, tip: str = "", tone: str = "") -> None:
        self.value = text
        self._elide()
        self.setToolTip(tip or (text if self.text() != text else ""))
        self.setAccessibleName(f"{self._name}: {text}")
        if (self.property("tone") or "") != tone:
            self.setProperty("tone", tone)
            _restyle(self)

    def _elide(self) -> None:
        # 12 padding on the left, and room for the chevron on the right.
        self.setText(self.fontMetrics().elidedText(
            self.value, Qt.TextElideMode.ElideRight, max(0, self.width() - 42)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def _open(self) -> None:
        menu = self.build_menu()
        menu.setMinimumWidth(max(menu.sizeHint().width(), self.width()))
        menu.exec(self.mapToGlobal(QPoint(0, self.height() + 4)))

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(1.0 if self.isEnabled() else 0.4)
        painter.setPen(QPen(token_color("text-secondary"), 1.4, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap))
        x, y = self.width() - 17, self.height() / 2
        painter.drawLine(int(x - 3), int(y - 2), int(x), int(y + 1))
        painter.drawLine(int(x), int(y + 1), int(x + 3), int(y - 2))


class _RecentRelay(QObject):
    """Carries a worker's recent walkthroughs back to the UI thread: (token, summaries)."""

    loaded = pyqtSignal(int, object)


_relay_instance: _RecentRelay | None = None
_tokens = itertools.count(1)


def _relay() -> _RecentRelay:
    # Made on first use, on the UI thread: a module-level QObject would live
    # on whichever thread imported this module.
    global _relay_instance
    if _relay_instance is None:
        _relay_instance = _RecentRelay()
    return _relay_instance


class RecentRow(QFrame):
    """One walkthrough on the empty stage: click, Enter or Space opens it."""

    clicked = pyqtSignal(object)

    def __init__(self, summary, today: date):
        super().__init__()
        self.folder = summary.folder
        self.setObjectName("walkthroughRecent")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 8, 12, 8)
        row.setSpacing(12)
        thumb = QLabel()
        thumb.setObjectName("walkthroughThumb")
        thumb.setFixedSize(RECENT_THUMB)
        if summary.thumb is not None:
            thumb.setPixmap(QPixmap.fromImage(summary.thumb))
        row.addWidget(thumb)
        words = QVBoxLayout()
        words.setSpacing(2)
        self.title_label = ElidingLabel(summary.title)
        self.title_label.setObjectName("walkthroughTitle")
        words.addWidget(self.title_label)
        when = summary.when
        meta = summary.meta if when is None else (
            f"{_day(when, today)} · {_time(when)} · {summary.meta}")
        words.addWidget(_label(meta, "walkthroughHint"))
        row.addLayout(words, 1)
        if summary.pill:
            pill = _label(summary.pill, "walkthroughPill")
            if summary.pill_tone:
                pill.setProperty("tone", summary.pill_tone)
            row.addWidget(pill, 0, Qt.AlignmentFlag.AlignVCenter)
        self.setAccessibleName(f"Open {summary.title}. {meta}")

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
                event.position().toPoint()):
            self.clicked.emit(self.folder)
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit(self.folder)
            return
        super().keyPressEvent(event)


class PromptPopover(QWidget):
    """The prompt, opened beside the column; a click outside or Esc closes it."""

    closed = pyqtSignal()

    def __init__(self, owner: QWidget, content: QWidget):
        super().__init__(owner, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.setObjectName("walkthroughPopoverFrame")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("walkthroughPopover")
        outer.addWidget(card)
        body = QVBoxLayout(card)
        body.setContentsMargins(16, 14, 16, 14)
        body.addWidget(content)

    def open_beside(self, column: QWidget, anchor: QWidget) -> None:
        """Open to the right of ``column``, level with ``anchor`` where the screen allows."""
        self.setFixedWidth(_scaled(PROMPT_POPOVER_WIDTH))
        # Content shown or hidden while this was closed is only measured on
        # the second pass, once the first has let it lay out.
        for _ in range(2):
            self.layout().activate()
            self.adjustSize()
        at = QPoint(column.mapToGlobal(QPoint(column.width() + 8, 0)).x(),
                    anchor.mapToGlobal(QPoint(0, anchor.height())).y() - self.height() // 2)
        screen = (column.screen() or QApplication.primaryScreen()).availableGeometry()
        at.setX(max(screen.left() + 8, min(at.x(), screen.right() - self.width() - 8)))
        at.setY(max(screen.top() + 8, min(at.y(), screen.bottom() - self.height() - 8)))
        self.move(at)
        self.show()
        self.activateWindow()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.closed.emit()


class WalkthroughTab(QWidget):
    """Start or finish a walkthrough, set up what follows it, and work with the last one."""

    #: Start a walkthrough, or finish the one recording.
    walkthrough_requested = pyqtSignal()
    #: Throw the recording walkthrough away.
    cancel_requested = pyqtSignal()
    settings_requested = pyqtSignal(str)
    #: The storyboard saved, rewrote or deleted a walkthrough: lists of them are stale.
    walkthroughs_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        # Before the stylesheet: setting it sends a StyleChange (changeEvent).
        self._built = False
        self.setObjectName("walkthroughTab")
        self.setStyleSheet(_STYLE)
        self.is_recording = False
        self.walkthrough_active = False
        self._scan_token = 0
        self._recent_token = 0
        self._stage_state: str | None = None
        self._agent = resolve_walkthrough_write_up_agent(settings_manager.load_all_settings())
        self._copy_index = 0
        #: The user's own prompt, kept while a style is tried in its place.
        self._own_prompt = ""
        self._styles = load_styles(settings_manager.load_all_settings())
        self._prompt_timer = QTimer(self)
        self._prompt_timer.setSingleShot(True)
        self._prompt_timer.setInterval(PROMPT_SAVE_DELAY_MS)
        self._prompt_timer.timeout.connect(self._save_prompt)
        _relay().loaded.connect(self._on_recent_loaded)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("transcriptionTabScrollArea")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(self.scroll_area)
        content = QWidget()
        content.setObjectName("walkthroughTabContent")
        self.scroll_area.setWidget(content)
        page = QVBoxLayout(content)
        page.setContentsMargins(24, 6, 24, 16)
        page.setSpacing(0)

        self.stage = Stage()
        self.split = StudioSplit(self._column(), self.stage)
        page.addWidget(self.split, 1)
        self._build_stage()
        self.prompt_popover = PromptPopover(self, self._prompt_area())
        self.prompt_popover.closed.connect(self._on_prompt_closed)
        self._built = True
        self._apply_sizes()

        self.set_hotkeys("", "")
        self.load_settings()
        self._show_agents()
        self.show_walkthrough()

    # ---- the control column ----

    def _column(self) -> QFrame:
        column = self.column = QFrame()
        column.setObjectName("walkthroughColumn")
        body = QVBoxLayout(column)
        body.setContentsMargins(16, 16, 16, 12)
        body.setSpacing(2)

        self.walkthrough_button = SuccessButton("Start Walkthrough")
        self.walkthrough_button.setProperty("buttonId", "walkthrough")
        self.walkthrough_button.setSizePolicy(QSizePolicy.Policy.Expanding,
                                              QSizePolicy.Policy.Fixed)
        self.walkthrough_button.clicked.connect(self.walkthrough_requested)
        body.addWidget(self.walkthrough_button)

        # One slot under Start holds the shortcuts, or Cancel while a
        # walkthrough records (the stage shows the shortcuts then), so the
        # rows below never shift. Cancel goes under Finish, not beside it:
        # "Finish Walkthrough" needs the column's width.
        self.keys_slot = _plain()
        under = QVBoxLayout(self.keys_slot)
        under.setContentsMargins(0, 8, 0, 0)
        under.setSpacing(0)
        self.cancel_button = WarningButton("Cancel")
        self.cancel_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.cancel_button.clicked.connect(self.cancel_requested)
        self.cancel_button.hide()
        under.addWidget(self.cancel_button)
        self.keys_box = _plain()
        keys = QVBoxLayout(self.keys_box)
        keys.setContentsMargins(0, 0, 0, 0)
        keys.setSpacing(4)
        self.walkthrough_badge = FeatureBadge(
            None,
            "Talk while you take screenshots, then get one document with your "
            "words and pictures in order. The copy choice below controls how it's delivered.",
            column,
        )
        self.walkthrough_badge.set_new(
            not resolve_walkthrough_intro_seen(settings_manager.load_all_settings()))
        self.start_key = _key_chip("")
        self.snap_key = _key_chip("")
        keys.addWidget(self._key_row("Start / finish", self.start_key, self.walkthrough_badge))
        keys.addWidget(self._key_row("Screenshot", self.snap_key))
        under.addWidget(self.keys_box)
        under.addStretch(1)
        body.addWidget(self.keys_slot)

        self._section(body, "CAPTURE")
        self.auto_switch = SettingsSwitch()
        self.auto_switch.setAccessibleName("Auto Mode: capture clicks and drawings")
        self.auto_switch.toggled.connect(
            lambda on: settings_manager.save_setting(SettingsKey.WALKTHROUGH_AUTO_MODE, on))
        self.auto_row = self._row(
            "Auto Mode", self.auto_switch,
            "Save screenshots after clicks and drawings. Use the toolbar at the top to draw.")
        body.addWidget(self.auto_row)
        self.capture_delay = CaptureDelay()
        self.delay_row = self._row(
            "Capture delay", self.capture_delay,
            "Wait before each screenshot, to add marks or let the screen settle.")
        body.addWidget(self.delay_row)
        # Clicks can be watched only here.
        auto_here = sys.platform in ("win32", "darwin")
        self.auto_row.setVisible(auto_here)
        self.delay_row.setVisible(auto_here)
        self.minimize_switch = SettingsSwitch()
        self.minimize_switch.setAccessibleName("Minimize to tray when a walkthrough starts")
        self.minimize_switch.toggled.connect(self._on_minimize_toggled)
        body.addWidget(self._row(
            "Minimize to tray", self.minimize_switch,
            "Gets this window out of the way of what you're showing. "
            "Open it again from the tray icon."))

        self._section(body, "WRITE-UP")
        self.write_up_switch = SettingsSwitch()
        self.write_up_switch.setAccessibleName("Write walkthroughs up with an AI agent")
        self.write_up_switch.toggled.connect(self._on_write_up_toggled)
        self.write_up_row = self._row("AI write-up", self.write_up_switch)
        body.addWidget(self.write_up_row)
        self.agent_field = Field(self.agent_menu, "Written by")
        self.style_field = Field(self.style_menu, "Style")
        self.notes_switch = SettingsSwitch()
        self.notes_switch.setAccessibleName("Follow notes left for the agent")
        self.notes_switch.toggled.connect(
            lambda on: settings_manager.save_setting(SettingsKey.WALKTHROUGH_WRITE_UP_NOTES, on))
        self.prompt_link = QPushButton("Edit the prompt…")
        self.prompt_link.setObjectName("walkthroughPromptLink")
        self.prompt_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.prompt_link.setToolTip("See and change exactly what the agent is asked for")
        self.prompt_link.clicked.connect(self.edit_prompt)
        link_row = _plain()
        links = QHBoxLayout(link_row)
        links.setContentsMargins(0, 2, 0, 2)
        links.addStretch(1)
        links.addWidget(self.prompt_link)
        # Without a write-up these have nobody to act on. Dimmed rather than
        # hidden, so the column keeps its shape.
        self.write_up_rows = [
            self._row("Written by", self.agent_field),
            self._row("Style", self.style_field),
            self._row("Follow my notes", self.notes_switch,
                      "Say \"note for the agent: skip the setup part\" while you talk, and "
                      "the agent follows it instead of writing it into the document."),
            link_row,
        ]
        for row in self.write_up_rows:
            body.addWidget(row)

        self._section(body, "COPY")
        self.copy_field = Field(self.copy_menu, "Copy when it finishes")
        body.addWidget(self._row("On finish, copy", self.copy_field))

        body.addStretch(1)
        foot = QHBoxLayout()
        foot.setContentsMargins(0, 10, 0, 0)
        self.settings_link = QPushButton("Walkthrough settings  →")
        self.settings_link.setObjectName("walkthroughSettingsLink")
        self.settings_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_link.setToolTip("Shortcuts and how the document looks")
        self.settings_link.clicked.connect(lambda: self.settings_requested.emit(WALKTHROUGHS))
        foot.addWidget(self.settings_link)
        foot.addStretch(1)
        body.addLayout(foot)
        return column

    @staticmethod
    def _section(body: QVBoxLayout, title: str) -> None:
        body.addSpacing(10)
        body.addWidget(_rule())
        body.addSpacing(10)
        body.addWidget(_label(title, "walkthroughOverline"))
        body.addSpacing(2)

    def _row(self, text: str, control: QWidget, tip: str = "") -> QWidget:
        """A form row: what it is on the left, its control at the right edge."""
        row = _plain()
        row.setProperty("formRow", True)
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(8)
        name = _label(text, "walkthroughRowName")
        if tip:
            name.setToolTip(tip)
        line.addWidget(name)
        line.addStretch(1)
        line.addWidget(control)
        row.name_label = name
        return row

    @staticmethod
    def _key_row(text: str, chip: QWidget, badge: QWidget | None = None) -> QWidget:
        row = _plain()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 2, 0, 2)
        line.setSpacing(6)
        line.addWidget(_label(text, "walkthroughKeyName"))
        if badge is not None:
            line.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
        line.addStretch(1)
        line.addWidget(chip)
        return row

    def _apply_sizes(self) -> None:
        """Size the column and its values for the type size in use."""
        if not self._built:
            return
        self.split.set_column_width(_scaled(COLUMN_WIDTH))
        self.keys_slot.setFixedHeight(_scaled(KEYS_SLOT_HEIGHT))
        for field in (self.agent_field, self.style_field, self.copy_field):
            field.setFixedSize(_scaled(FIELD_WIDTH), _scaled(FIELD_HEIGHT))
        self.capture_delay.setFixedWidth(_scaled(FIELD_WIDTH))
        for row in self.column.findChildren(QWidget):
            if row.property("formRow"):
                row.setMinimumHeight(_scaled(ROW_HEIGHT))

    # ---- the prompt (in its popover) ----

    def _prompt_area(self) -> QWidget:
        """What the agent is asked for, and the styles to start from or keep."""
        self.prompt_area = QWidget()
        self.prompt_area.setObjectName("walkthroughPlain")
        area = QVBoxLayout(self.prompt_area)
        area.setContentsMargins(0, 0, 0, 0)
        area.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel("Prompt")
        title.setObjectName("walkthroughPromptTitle")
        head.addWidget(title)
        head.addStretch(1)
        start = QLabel("Start from")
        start.setObjectName("walkthroughKeyName")
        head.addWidget(start)
        self.preset_combo = NoWheelComboBox()
        self.preset_combo.setObjectName("walkthroughPreset")
        self.preset_combo.setAccessibleName("Write-up style")
        self.preset_combo.activated.connect(self._on_preset_chosen)
        head.addWidget(self.preset_combo)
        area.addLayout(head)

        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setObjectName("walkthroughPrompt")
        self.prompt_edit.setAccessibleName("Write-up prompt")
        self.prompt_edit.setPlaceholderText(DEFAULT_INSTRUCTIONS)
        self.prompt_edit.setTabChangesFocus(True)
        self.prompt_edit.setMinimumHeight(150)
        self.prompt_edit.setMaximumHeight(260)
        self.prompt_edit.textChanged.connect(self._on_prompt_edited)
        area.addWidget(self.prompt_edit)

        options = QHBoxLayout()
        options.setSpacing(16)
        hint = QLabel("Saved as you type.")
        hint.setObjectName("walkthroughKeyName")
        options.addWidget(hint)
        options.addStretch(1)
        self.save_style_button = self._style_link(
            "Save as style…", "Keep these words as a style to pick here and in Redo as",
            self._save_as_style)
        options.addWidget(self.save_style_button)
        self.delete_style_button = self._style_link(
            "Delete style", "Remove this style from the list; the words stay in the box",
            self._delete_style)
        options.addWidget(self.delete_style_button)
        area.addLayout(options)
        return self.prompt_area

    def _style_link(self, text: str, tip: str, slot) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("walkthroughStyleLink")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setToolTip(tip)
        button.clicked.connect(slot)
        return button

    def edit_prompt(self) -> None:
        """Open the prompt beside the column; asking for it turns the write-up on."""
        if not self.write_up_switch.isChecked():
            self.write_up_switch.setChecked(True)
        self.prompt_popover.open_beside(self.column, self.prompt_link)
        self.prompt_edit.setFocus()

    def _on_prompt_closed(self) -> None:
        # Closing is done typing: save now rather than when the pause ends.
        if self._prompt_timer.isActive():
            self._save_prompt()

    # ---- the stage ----

    def _build_stage(self) -> None:
        self.review = WalkthroughReview(closable=True)
        self.review.changed.connect(self._on_review_changed)
        self.review.close_requested.connect(self.close_walkthrough)
        self.stage.add(self.review)
        self.recording_pane = self.stage.add(self._recording_pane())
        self.empty_pane = self.stage.add(self._empty_pane())

    @staticmethod
    def _pane() -> QFrame:
        pane = QFrame()
        pane.setObjectName("walkthroughStagePane")
        return pane

    def _recording_pane(self) -> QFrame:
        pane = self._pane()
        outer = QVBoxLayout(pane)
        outer.setContentsMargins(32, 32, 32, 32)
        outer.addStretch(2)
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addStretch(1)
        dot = QLabel()
        dot.setObjectName("walkthroughRecordingDot")
        head.addWidget(dot, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(_label("Recording your walkthrough", "walkthroughHeading"))
        head.addStretch(1)
        outer.addLayout(head)
        outer.addSpacing(10)
        self.recording_keys = _label("", "walkthroughRecordingKeys")
        self.recording_keys.setTextFormat(Qt.TextFormat.RichText)
        self.recording_keys.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self.recording_keys)
        outer.addSpacing(4)
        after = _label("It opens here as a storyboard when you finish.", "walkthroughHint")
        after.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(after)
        outer.addStretch(3)
        return pane

    def _empty_pane(self) -> QFrame:
        pane = self._pane()
        outer = QHBoxLayout(pane)
        outer.setContentsMargins(28, 24, 28, 24)
        outer.addStretch(1)
        column = _plain()
        column.setMaximumWidth(460)
        column.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        body = QVBoxLayout(column)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addStretch(2)
        body.addWidget(_label("No walkthrough open", "walkthroughHeading"))
        body.addSpacing(4)
        self.empty_hint = _label("Start one on the left.", "walkthroughHint", wrap=True)
        body.addWidget(self.empty_hint)
        body.addSpacing(22)
        for number, (title, text) in enumerate((
            ("Talk it through", "Press Start and explain what's on screen."),
            ("Snap as you go", "Each screenshot lands beside what you were saying."),
            ("Paste the result", "One document, already on your clipboard."),
        ), 1):
            step = QHBoxLayout()
            step.setSpacing(12)
            badge = _label(str(number), "walkthroughStep")
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            step.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
            words = QVBoxLayout()
            words.setSpacing(1)
            words.addWidget(_label(title, "walkthroughTitle"))
            words.addWidget(_label(text, "walkthroughHint", wrap=True))
            step.addLayout(words, 1)
            body.addLayout(step)
            body.addSpacing(14)

        self.recent_box = _plain()
        recent = QVBoxLayout(self.recent_box)
        recent.setContentsMargins(0, 10, 0, 0)
        recent.setSpacing(4)
        recent.addWidget(_rule())
        recent.addSpacing(12)
        recent.addWidget(_label("RECENT", "walkthroughOverline"))
        recent.addSpacing(2)
        self._recent_layout = QVBoxLayout()
        self._recent_layout.setSpacing(2)
        recent.addLayout(self._recent_layout)
        self.recent_box.hide()
        body.addWidget(self.recent_box)
        body.addStretch(3)
        outer.addWidget(column, 4)
        outer.addStretch(1)
        self.recent_rows: list[RecentRow] = []
        return pane

    def _load_recent(self) -> None:
        """Read the newest few walkthroughs for the empty stage, off the UI thread."""
        self._recent_token = token = next(_tokens)
        root, today = captures_root(), date.today()
        ratio = self.devicePixelRatioF() or 1.0
        loaded = _relay().loaded

        def load() -> None:
            try:
                folders = library.entries(root)[:RECENT_SHOWN]
            except OSError:
                folders = []
            summaries = []
            for folder in folders:
                try:
                    summaries.append(past_walkthroughs.summarize(
                        folder, today, ratio, thumb_size=RECENT_THUMB, thumb_radius=6))
                except Exception:
                    logger.warning("Could not read walkthrough %s", folder, exc_info=True)
            loaded.emit(token, summaries)

        threading.Thread(target=load, name="walkthrough-recent-load", daemon=True).start()

    def _on_recent_loaded(self, token: int, summaries) -> None:
        if token != self._recent_token:
            return
        self._recent_token = 0
        for row in self.recent_rows:
            row.hide()
            row.setParent(None)
            row.deleteLater()
        today = date.today()
        self.recent_rows = []
        for summary in summaries:
            row = RecentRow(summary, today)
            row.clicked.connect(lambda folder: self.show_walkthrough(folder, ask=True))
            self._recent_layout.addWidget(row)
            self.recent_rows.append(row)
        self.recent_box.setVisible(bool(summaries))
        self.empty_hint.setText("Start one on the left, or open a recent one below."
                                if summaries else "Start one on the left.")

    @property
    def recent_loading(self) -> bool:
        """True while the empty stage's list is being read."""
        return bool(self._recent_token)

    @property
    def stage_state(self) -> str:
        """What the stage shows: "review", "recording" or "empty"."""
        return self._stage_state or "empty"

    def _show_stage(self) -> None:
        # A recording takes the stage even over an open storyboard, which
        # comes back if it is cancelled.
        state = ("recording" if self.walkthrough_active
                 else "review" if self.review.folder is not None else "empty")
        if state == "empty" and self._stage_state != "empty":
            self._load_recent()
        self._stage_state = state
        self.stage.show_only({"recording": self.recording_pane, "review": self.review,
                              "empty": self.empty_pane}[state])

    # ---- menus ----

    @staticmethod
    def _choice(menu: QMenu, group: QActionGroup, text: str, on: bool, slot) -> QAction:
        action = menu.addAction(text)
        action.setCheckable(True)
        action.setChecked(on)
        group.addAction(action)
        action.triggered.connect(lambda _checked=False: slot())
        return action

    def copy_menu(self) -> QMenu:
        menu, group = _menu(self), QActionGroup(self)
        for index, (value, text) in enumerate(COPY_CHOICES):
            action = self._choice(menu, group, text, index == self._copy_index,
                                  lambda i=index: self._on_copy_chosen(i))
            set_copy_tip(action, value, on_finish=True)
        return menu

    def agent_menu(self) -> QMenu:
        menu, group = _menu(self), QActionGroup(self)
        for agent in AGENTS:
            status, _found, detail = self._agent_state(agent)
            action = self._choice(menu, group, f"{AGENT_SPECS[agent].name}\t{status}",
                                  agent == self._agent, lambda a=agent: self._choose_agent(a))
            if detail:
                action.setToolTip(detail)
        return menu

    def style_menu(self) -> QMenu:
        menu, group = _menu(self), QActionGroup(self)
        current = self._current_style()
        for style in self._styles:
            self._choice(menu, group, style.name, current is not None and style.id == current.id,
                         lambda s=style: self._use_style(s.id))
        if self._own_prompt:
            self._choice(menu, group, OWN_PROMPT, current is None, lambda: self._use_style(""))
        menu.addSeparator()
        menu.addAction("Edit the prompt…").triggered.connect(
            lambda _checked=False: self.edit_prompt())
        menu.addAction(NEW_STYLE).triggered.connect(lambda _checked=False: self._new_style())
        return menu

    @staticmethod
    def _agent_state(agent: str) -> tuple[str, bool, str]:
        """(what the last scan said, found and usable, detail) for ``agent``."""
        from ui_qt.widgets import agent_picker

        found = agent_picker.cached_agents()
        if found is None:
            return "Checking…", True, ""
        state = describe_agent(agent, found.get(agent))
        return state.pill, state.tone == FOUND, state.detail

    # ---- state ----

    def load_settings(self) -> None:
        """Show the saved choices without saving them back.

        Typing not yet saved is saved first: it is newer than anything here.
        """
        if self._prompt_timer.isActive():
            self._save_prompt()
        settings = settings_manager.load_all_settings()
        self._styles = load_styles(settings)
        quiet = (self.write_up_switch, self.notes_switch, self.prompt_edit,
                 self.minimize_switch, self.auto_switch)
        for widget in quiet:
            widget.blockSignals(True)
        self.write_up_switch.setChecked(resolve_walkthrough_write_up(settings))
        self.notes_switch.setChecked(resolve_walkthrough_write_up_notes(settings))
        self.minimize_switch.setChecked(resolve_walkthrough_minimize_to_tray(settings))
        self.auto_switch.setChecked(resolve_walkthrough_auto_mode(settings))
        self.capture_delay.set_seconds(resolve_walkthrough_capture_delay(settings))
        prompt = resolve_walkthrough_write_up_instructions(settings) or DEFAULT_INSTRUCTIONS
        if self.prompt_edit.toPlainText().strip() != prompt:
            # Only when it differs: setting it moves the cursor to the start.
            self.prompt_edit.setPlainText(prompt)
        for widget in quiet:
            widget.blockSignals(False)
            widget.update()
        self._show_preset()
        self._agent = resolve_walkthrough_write_up_agent(settings)
        copy = resolve_walkthrough_copy_format(settings)
        self._copy_index = next(
            i for i, (value, _label) in enumerate(COPY_CHOICES) if value == copy)
        self._sync_write_up()
        self._sync_copy()

    def _on_write_up_toggled(self, on: bool) -> None:
        settings_manager.save_setting(SettingsKey.WALKTHROUGH_WRITE_UP, on)
        self._sync_write_up()

    def _choose_agent(self, agent: str) -> None:
        self._agent = agent
        settings_manager.save_setting(SettingsKey.WALKTHROUGH_WRITE_UP_AGENT, agent)
        if not self.write_up_switch.isChecked():
            # Picking an agent is asking for a write-up.
            self.write_up_switch.setChecked(True)
        self._sync_write_up()

    def _on_minimize_toggled(self, on: bool) -> None:
        settings_manager.save_setting(SettingsKey.WALKTHROUGH_MINIMIZE_TO_TRAY, on)

    def _on_copy_chosen(self, index: int) -> None:
        self._copy_index = index
        settings_manager.save_setting(SettingsKey.WALKTHROUGH_COPY_FORMAT, COPY_CHOICES[index][0])
        self._sync_copy()

    def _sync_copy(self) -> None:
        value, text = COPY_CHOICES[self._copy_index]
        self.copy_field.set_value(text)
        set_copy_tip(self.copy_field, value, on_finish=True)

    def _sync_write_up(self) -> None:
        on = self.write_up_switch.isChecked()
        name = AGENT_SPECS[self._agent].name
        self.write_up_row.name_label.setToolTip(
            f"{name} lays out a clean document from it. The as-spoken PDF is kept too."
            if on else "Off: you get it as spoken.")
        status, found, detail = self._agent_state(self._agent)
        self.agent_field.set_value(
            name, tip=f"{name}: {status}" + (f". {detail}" if detail else ""),
            tone="" if found else "warning")
        for row in self.write_up_rows:
            row.setEnabled(on)

    # ---- the prompt ----

    def _on_prompt_edited(self) -> None:
        self._prompt_timer.start()
        self._show_preset()

    def _save_prompt(self) -> None:
        """Save the prompt; the default's own words save as "", like an empty box."""
        self._prompt_timer.stop()
        text = self.prompt_edit.toPlainText().strip()
        settings_manager.save_setting(
            SettingsKey.WALKTHROUGH_WRITE_UP_INSTRUCTIONS,
            "" if text == DEFAULT_INSTRUCTIONS else text)

    def _current_style(self):
        return style_for(self.prompt_edit.toPlainText(), self._styles)

    def _show_preset(self) -> None:
        """Point the style menus at the style the prompt is, or at the user's own words.

        The combo is rebuilt each time: styles are saved and deleted here and
        from the storyboard's Redo with a new prompt.
        """
        text = self.prompt_edit.toPlainText().strip()
        style = style_for(text, self._styles)
        if style is None:
            self._own_prompt = text
        combo = self.preset_combo
        combo.clear()
        for each in self._styles:
            combo.addItem(each.name, each.id)
        if self._own_prompt:
            combo.addItem(OWN_PROMPT, "")
        combo.insertSeparator(combo.count())
        combo.addItem(NEW_STYLE, _NEW_STYLE_DATA)
        combo.setCurrentIndex(combo.findData(style.id if style else ""))
        self.save_style_button.setVisible(style is None and bool(text))
        self.delete_style_button.setVisible(style is not None and style != DEFAULT_STYLE)
        self.style_field.set_value(style.name if style else OWN_PROMPT)

    def _use_style(self, style_id: str) -> None:
        """Use a saved style's words, or ("") the user's own."""
        index = self.preset_combo.findData(style_id)
        if index >= 0:
            self._on_preset_chosen(index)

    def _on_preset_chosen(self, index: int) -> None:
        # Past the styles: the user's own words, then New style….
        style_id = self.preset_combo.itemData(index)
        if style_id == _NEW_STYLE_DATA:
            self._new_style()
            return
        words = next((style.instructions for style in self._styles if style.id == style_id),
                     self._own_prompt)
        if words != self.prompt_edit.toPlainText().strip():
            self.prompt_edit.setPlainText(words)
        self._save_prompt()

    def _new_style(self) -> None:
        """Make a style from scratch and use it; cancelled, the menu goes back."""
        from ui_qt.dialogs.new_style_dialog import NewStyleDialog

        # A dialog over an open popover would close it under the user's hand.
        self.prompt_popover.hide()
        dialog = NewStyleDialog(self)
        if dialog.exec() != NewStyleDialog.DialogCode.Accepted:
            self._show_preset()
            return
        self._styles = load_styles(settings_manager.load_all_settings())
        # Its words fill the box, which points the menu at it, as picking a style does.
        self.prompt_edit.setPlainText(dialog.saved.instructions)
        self._save_prompt()

    def _save_as_style(self) -> None:
        text = self.prompt_edit.toPlainText().strip()
        self.prompt_popover.hide()
        name, ok = QInputDialog.getText(
            self, "Save as style", "Name the style. A name already saved is replaced.")
        if not ok or not name.strip():
            return
        try:
            settings_manager.mutate_settings(lambda settings: save_style(settings, name, text))
        except ValueError as exc:
            QMessageBox.warning(self, "Save as style", str(exc))
            return
        self._styles = load_styles(settings_manager.load_all_settings())
        self._show_preset()

    def _delete_style(self) -> None:
        style = style_for(self.prompt_edit.toPlainText(), self._styles)
        if style is None or style == DEFAULT_STYLE:
            return
        self.prompt_popover.hide()
        if QMessageBox.question(
                self, "Delete style", f"Delete the style \"{style.name}\"? Its words stay.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        settings_manager.mutate_settings(lambda settings: delete_style(settings, style.id))
        self._styles = load_styles(settings_manager.load_all_settings())
        self._show_preset()

    # ---- the window ----

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            # Settings → Walkthroughs edits the same choices, and the library
            # can delete walkthroughs: show both as they are on return.
            self.load_settings()
            if self._stage_state == "empty":
                self._load_recent()
        elif event.type() == QEvent.Type.StyleChange:
            # A new type size restyles every widget; the column follows it.
            self._apply_sizes()

    def _show_agents(self) -> None:
        """Label the agents with the last scan; scan once if none has run."""
        from ui_qt.widgets import action_relay, agent_picker

        if agent_picker.cached_agents() is None:
            if not self._scan_token:
                action_relay.relay().scan_done.connect(self._on_scan_done)
                self._scan_token = token = action_relay.next_token()
                action_relay.start_agent_scan(token)
            return
        self._sync_write_up()

    def _on_scan_done(self, token: int) -> None:
        if token == self._scan_token:
            self._show_agents()

    def set_hotkeys(self, walkthrough_key: str, screenshot_key: str) -> None:
        for chip, key in ((self.start_key, walkthrough_key), (self.snap_key, screenshot_key)):
            chip.setText(key or "Not set")
            chip.setProperty("unset", not key)
            _restyle(chip)
            chip.setMinimumWidth(0)
        # Both at one width, so the pair reads as a column.
        widest = max(self.start_key.sizeHint().width(), self.snap_key.sizeHint().width())
        for chip in (self.start_key, self.snap_key):
            chip.setMinimumWidth(widest)
        self.walkthrough_button.set_hotkey(walkthrough_key)
        self.recording_keys.setText(
            f"Finish with <b>{self.start_key.text()}</b>  ·  "
            f"Screenshot with <b>{self.snap_key.text()}</b>")

    def set_recording_state(self, is_recording: bool, walkthrough_active: bool) -> None:
        """A walkthrough records: Finish and Cancel. A dictation records: wait for it."""
        self.is_recording = is_recording
        self.walkthrough_active = walkthrough_active
        if walkthrough_active and self.walkthrough_badge.text() == FeatureBadge.NEW:
            settings_manager.save_setting(SettingsKey.WALKTHROUGH_INTRO_SEEN, True)
            self.walkthrough_badge.set_new(False)
        self.walkthrough_button.setText(
            "Finish Walkthrough" if walkthrough_active else "Start Walkthrough")
        self.walkthrough_button.set_active(walkthrough_active or not is_recording)
        self.cancel_button.setVisible(walkthrough_active)
        self.keys_box.setVisible(not walkthrough_active)
        # Set before a walkthrough starts; it reads them as it starts.
        idle = not is_recording and not walkthrough_active
        self.auto_switch.setEnabled(idle)
        self.capture_delay.setEnabled(idle)
        self._show_stage()

    # ---- the storyboard ----

    def show_walkthrough(self, folder=None, *, ask: bool = False) -> bool:
        """Show ``folder``, or the newest walkthrough unless it was closed, as the storyboard.

        Unsaved edits stay unless ``ask`` is set and the user lets them go;
        False when they stayed, or while the storyboard is saving.
        """
        if folder is None:
            newest = self._newest()
            closed = SettingsKey.WALKTHROUGH_CLOSED.read(settings_manager.load_all_settings())
            folder = newest if newest is not None and newest.name != closed else None
        return self._show(folder, ask=ask)

    def close_walkthrough(self) -> None:
        """Put the storyboard away until a newer walkthrough finishes."""
        newest = self._newest()
        if self._show(None, ask=True) and newest is not None:
            settings_manager.save_setting(SettingsKey.WALKTHROUGH_CLOSED, newest.name)

    @staticmethod
    def _newest():
        folders = library.entries(captures_root())
        return folders[0] if folders else None

    def _show(self, folder, *, ask: bool) -> bool:
        review = self.review
        if review._busy or (review._dirty and not (ask and review.discard_changes())):
            return False
        review.show_folder(folder)
        self._show_stage()
        return True

    def fills_height(self) -> bool:
        """The stage takes whatever height the window is given, whatever it shows."""
        return True

    def preferred_width(self) -> int:
        """Room for the column beside a storyboard you can read, at any type size."""
        return self.split.column_width + GAP + _scaled(STAGE_WIDTH)

    def _on_review_changed(self, folder) -> None:
        self.show_walkthrough(folder)
        self.walkthroughs_changed.emit()

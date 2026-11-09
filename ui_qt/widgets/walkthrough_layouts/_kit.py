"""What the prototype layouts share: the tab's real controls, and a few small widgets.

``LayoutTab`` builds the real ``WalkthroughTab`` (so every control exists and
is wired to its setting), puts the shipped layout away unseen, and hands an
empty page to ``build``. A layout places the real controls where it wants
them and adds its own chips, menus and labels, which read and change the
same controls through the helpers here. ``refresh`` runs after anything
changes, so a layout's own labels never go stale.

The real controls a layout can place (each is used at most once):

``walkthrough_button`` ``cancel_button`` ``auto_check`` ``capture_delay``
``minimize_check`` ``start_key`` ``snap_key`` ``walkthrough_badge``
``copy_bar`` ``write_up_switch`` ``write_up_text`` ``agent_choices``
``prompt_area`` (or its parts: ``preset_combo`` ``prompt_edit``
``notes_switch`` ``notes_label`` ``save_style_button``
``delete_style_button``) ``settings_link`` ``review``
"""
from __future__ import annotations

import sys

from PyQt6.QtCore import QPoint, QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QFont
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QLabel,
    QLayout,
    QMenu,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from services.installed_agents import AGENT_SPECS
from services.settings import WALKTHROUGH_CAPTURE_DELAYS
from services.walkthrough.styles import style_for
from services.walkthrough.write_up import AGENTS
from ui_qt.widgets import agent_picker
from ui_qt.widgets.context_menu import CONTEXT_MENU_STYLESHEET
from ui_qt.widgets.settings_switch import SettingsSwitch
from ui_qt.widgets.walkthrough_copy_tips import set_copy_tip
from ui_qt.widgets.walkthrough_tab import _STYLE, COPY_CHOICES, WalkthroughTab

#: Auto Mode and the capture delay exist where clicks can be watched.
AUTO_MODE_HERE = sys.platform in ("win32", "darwin")

KIT_STYLE = """
    QFrame#wlCard {
        background-color: @surface;
        border: 1px solid @border;
        border-radius: 14px;
    }
    QFrame#wlCard[tone="sunken"] {
        background-color: @surface-sunken;
        border-color: @border-subtle;
    }
    QFrame#wlCard[tone="accent"] {
        background-color: @accent-tint;
        border-color: @accent-tint-border;
    }
    QWidget#wlPlain {
        background: transparent;
        border: none;
    }
    QLabel#wlHeading {
        color: @text-heading;
        font-size: 18px;
        font-weight: 700;
        background: transparent;
    }
    QLabel#wlTitle2 {
        color: @text;
        font-size: 14px;
        font-weight: 700;
        background: transparent;
    }
    QLabel#wlBody {
        color: @text;
        font-size: 13px;
        background: transparent;
    }
    QLabel#wlHint {
        color: @text-secondary;
        font-size: 12px;
        background: transparent;
    }
    QLabel#wlOverline {
        color: @text-secondary;
        font-size: 11px;
        font-weight: 700;
        background: transparent;
    }
    QLabel#wlStep {
        color: @on-accent;
        background-color: @accent;
        border-radius: 11px;
        font-size: 12px;
        font-weight: 700;
        min-width: 22px; max-width: 22px;
        min-height: 22px; max-height: 22px;
    }
    QLabel#wlStep[tone="quiet"] {
        color: @accent-soft;
        background-color: rgba(@accent-rgb, 0.16);
    }
    QPushButton#wlChip {
        color: @accent-soft;
        background-color: rgba(@accent-rgb, 0.13);
        border: 1px solid rgba(@accent-rgb, 0.0);
        border-radius: 8px;
        padding: 3px 9px;
        font-size: 13px;
        font-weight: 600;
        min-height: 0;
        text-align: left;
    }
    QPushButton#wlChip:hover, QPushButton#wlChip:focus {
        background-color: rgba(@accent-rgb, 0.22);
        border-color: rgba(@accent-rgb, 0.45);
    }
    QPushButton#wlChip[tone="field"] {
        color: @text;
        background-color: @slate-field;
        border: 1px solid @slate-border;
        padding: 6px 11px;
        font-weight: 500;
    }
    QPushButton#wlChip[tone="field"]:hover, QPushButton#wlChip[tone="field"]:focus {
        border-color: @slate-border-hover;
        background-color: @slate-field;
    }
    QPushButton#wlChip[tone="quiet"] {
        color: @text-secondary;
        background-color: rgba(@overlay-rgb, 0.06);
    }
    QPushButton#wlChip[tone="quiet"]:hover {
        color: @text;
        background-color: rgba(@overlay-rgb, 0.10);
        border-color: transparent;
    }
    QPushButton#wlChip::menu-indicator { image: none; width: 0; }
    QPushButton#wlLink {
        color: @accent;
        background-color: transparent;
        border: none;
        font-size: 12px;
        font-weight: 600;
        padding: 2px 0;
        min-height: 0;
    }
    QPushButton#wlLink:hover, QPushButton#wlLink:focus {
        color: @accent-soft;
        text-decoration: underline;
    }
    QPushButton#wlGhost {
        color: @text;
        background-color: rgba(@overlay-rgb, 0.06);
        border: 1px solid @border;
        border-radius: 9px;
        padding: 6px 12px;
        font-size: 12px;
        font-weight: 600;
        min-height: 0;
    }
    QPushButton#wlGhost:hover, QPushButton#wlGhost:focus {
        background-color: rgba(@overlay-rgb, 0.11);
        border-color: @border-hover;
    }
    QPushButton#wlGhost:checked {
        color: @accent-soft;
        background-color: @accent-tint;
        border-color: @accent-tint-border;
    }
    QFrame#wlRule {
        background-color: @border;
        border: none;
    }
    QFrame#wlPopoverCard {
        background-color: @surface;
        border: 1px solid @border-strong;
        border-radius: 14px;
    }
"""


# ---- small widgets ----

def label(text: str, name: str = "wlBody", *, wrap: bool = False) -> QLabel:
    """A label in one of the kit's text styles: wlHeading, wlTitle2, wlBody, wlHint, wlOverline."""
    widget = QLabel(text)
    widget.setObjectName(name)
    widget.setWordWrap(wrap)
    if name == "wlOverline":
        font = widget.font()
        font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 108)
        widget.setFont(font)
    return widget


def step_badge(number: int | str, *, quiet: bool = False) -> QLabel:
    """A numbered dot for a step."""
    badge = QLabel(str(number))
    badge.setObjectName("wlStep")
    badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
    if quiet:
        badge.setProperty("tone", "quiet")
    return badge


def rule(vertical: bool = False) -> QFrame:
    line = QFrame()
    line.setObjectName("wlRule")
    if vertical:
        line.setFixedWidth(1)
    else:
        line.setFixedHeight(1)
    return line


def card(tone: str = "") -> QFrame:
    """A rounded surface: "" (raised), "sunken" or "accent"."""
    frame = QFrame()
    frame.setObjectName("wlCard")
    if tone:
        frame.setProperty("tone", tone)
    return frame


def plain() -> QWidget:
    """A see-through container."""
    widget = QWidget()
    widget.setObjectName("wlPlain")
    return widget


def link(text: str, slot=None, tip: str = "") -> QPushButton:
    button = QPushButton(text)
    button.setObjectName("wlLink")
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if tip:
        button.setToolTip(tip)
    if slot is not None:
        button.clicked.connect(slot)
    return button


def ghost(text: str, slot=None, *, checkable: bool = False) -> QPushButton:
    """A quiet secondary button."""
    button = QPushButton(text)
    button.setObjectName("wlGhost")
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setCheckable(checkable)
    if slot is not None:
        (button.toggled if checkable else button.clicked).connect(slot)
    return button


def restyle(widget: QWidget) -> None:
    """Apply a changed dynamic property."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def bound_switch(check: QCheckBox, name: str = "") -> SettingsSwitch:
    """A switch that is ``check``: flipping either flips the other (and saves once)."""
    switch = SettingsSwitch()
    switch.setChecked(check.isChecked())
    switch.setAccessibleName(name or check.text())
    switch.setToolTip(check.toolTip())
    switch.toggled.connect(lambda on: check.isChecked() != on and check.click())

    def follow(on: bool) -> None:
        if switch.isChecked() != on:
            switch.blockSignals(True)
            switch.setChecked(on)
            switch.blockSignals(False)
            switch.update()

    check.toggled.connect(follow)
    return switch


class Chip(QPushButton):
    """A value you can click to change: shows it, and opens a menu or calls back.

    ``tone``: "" (accent, for a value inside a sentence), "field" (a quiet
    drop-down) or "quiet". ``menu`` is a callable returning the QMenu to open
    under the chip; without one, ``clicked`` is yours to connect.
    """

    def __init__(self, text: str = "", *, menu=None, tone: str = "", caret: bool = True,
                 parent=None):
        super().__init__(parent)
        self.setObjectName("wlChip")
        if tone:
            self.setProperty("tone", tone)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._caret = caret
        self._menu = menu
        self.set_value(text)
        if menu is not None:
            self.clicked.connect(self.open_menu)

    def set_value(self, text: str) -> None:
        self.setText(f"{text}  ▾" if self._caret else text)

    def open_menu(self) -> None:
        menu = self._menu()
        menu.setMinimumWidth(max(menu.sizeHint().width(), self.width()))
        menu.exec(self.mapToGlobal(QPoint(0, self.height() + 4)))


class Popover(QFrame):
    """A floating card under a button, for controls that are too big for a menu.

    Add content to ``body``. It closes on a click outside or Esc.
    """

    closed = pyqtSignal()

    def __init__(self, owner: QWidget, *, width: int = 420):
        super().__init__(owner, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setObjectName("wlPlain")
        self._width = width
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.card = QFrame()
        self.card.setObjectName("wlPopoverCard")
        outer.addWidget(self.card)
        self.body = QVBoxLayout(self.card)
        self.body.setContentsMargins(16, 14, 16, 16)
        self.body.setSpacing(10)

    def open_under(self, anchor: QWidget, *, align_right: bool = False) -> None:
        self.setFixedWidth(self._width)
        self.adjustSize()
        at = anchor.mapToGlobal(QPoint(0, anchor.height() + 6))
        if align_right:
            at.setX(at.x() + anchor.width() - self._width)
        screen = (anchor.screen() or QApplication.primaryScreen()).availableGeometry()
        at.setX(max(screen.left() + 8, min(at.x(), screen.right() - self._width - 8)))
        at.setY(max(screen.top() + 8, min(at.y(), screen.bottom() - self.height() - 8)))
        self.move(at)
        self.show()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.closed.emit()


class FlowLayout(QLayout):
    """Items left to right, wrapping onto new lines; its height follows its width."""

    def __init__(self, parent=None, *, h_gap: int = 6, v_gap: int = 6):
        super().__init__(parent)
        self._items = []
        self._h_gap = h_gap
        self._v_gap = v_gap
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._place(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._place(rect, apply=True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def _place(self, rect: QRect, *, apply: bool) -> int:
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x, y, line = area.x(), area.y(), 0
        row = []

        def flush():
            # Centre each item on its line.
            for item, left in row:
                hint = item.sizeHint()
                item.setGeometry(QRect(QPoint(left, y + (line - hint.height()) // 2), hint))

        for item in self._items:
            if item.widget() is not None and item.widget().isHidden():
                continue
            hint = item.sizeHint()
            if x + hint.width() > area.right() + 1 and row:
                if apply:
                    flush()
                x, y, line, row = area.x(), y + line + self._v_gap, 0, []
            row.append((item, x))
            x += hint.width() + self._h_gap
            line = max(line, hint.height())
        if apply:
            flush()
        return y + line - rect.y() + margins.bottom()


# ---- the tab ----

class LayoutTab(WalkthroughTab):
    """The real Walkthrough tab with its page left for a subclass to lay out."""

    #: Extra stylesheet for the layout's own widgets.
    STYLE = ""
    #: How wide the page asks to be with and without a storyboard (0: as the window is).
    WIDTH = 0
    WIDTH_WITH_STORYBOARD = 900
    _built = False

    def __init__(self, parent=None):
        super().__init__(parent)
        page = self.scroll_area.widget().layout()
        while page.count():
            page.takeAt(0)
        # The shipped layout, kept alive (the base class still talks to its
        # labels) but never seen. Controls a layout places leave it.
        self._attic = QWidget(self)
        self._attic.hide()
        self.split.parentWidget().setParent(self._attic)
        self.review_rule.setParent(self._attic)
        self.review.setParent(self._attic)
        self.setStyleSheet(_STYLE + KIT_STYLE + self.STYLE)
        self.page = page
        self.build(page)
        self._built = True
        # Reparenting loses what was shown and hidden: say it all again.
        self.auto_check.setVisible(AUTO_MODE_HERE)
        self.capture_delay.setVisible(AUTO_MODE_HERE)
        self._sync_write_up()
        self._show_preset()
        self.set_recording_state(self.is_recording, self.walkthrough_active)
        self._show(self.review.folder, ask=False)

    # ---- for a layout to fill in ----

    def build(self, page: QVBoxLayout) -> None:
        """Lay the page out. ``page`` is the scrolling column, empty."""
        raise NotImplementedError

    def refresh(self) -> None:
        """Something changed (a choice, the recording, the storyboard): update your labels."""

    # ---- what's chosen, and choosing it ----

    @property
    def copy_index(self) -> int:
        return self.copy_bar.currentIndex()

    @property
    def copy_label(self) -> str:
        return COPY_CHOICES[self.copy_index][1]

    def choose_copy(self, index: int) -> None:
        self.copy_bar.setCurrentIndex(index)
        self._on_copy_chosen(index)

    @property
    def write_up_on(self) -> bool:
        return self.write_up_switch.isChecked()

    def set_write_up(self, on: bool) -> None:
        self.write_up_switch.setChecked(on)

    @property
    def agent(self) -> str:
        return self._agent

    @property
    def agent_name(self) -> str:
        return AGENT_SPECS[self._agent].name

    def choose_agent(self, agent: str) -> None:
        """Pick who writes it up (which also turns the write-up on)."""
        self._choose_agent(agent)

    @staticmethod
    def agent_status(agent: str) -> tuple[str, bool]:
        """("Found" / "Not installed" / …, usable) from the last scan."""
        found = agent_picker.cached_agents()
        if found is None:
            return "Checking…", True
        state = agent_picker.describe_agent(agent, found.get(agent))
        return state.pill, state.tone == agent_picker.FOUND

    @property
    def styles(self) -> list:
        return list(self._styles)

    @property
    def style(self):
        """The write-up style the prompt is, or None for the user's own words."""
        return style_for(self.prompt_edit.toPlainText(), self._styles)

    @property
    def style_name(self) -> str:
        style = self.style
        return style.name if style else "my own prompt"

    def choose_style(self, style_id: str) -> None:
        """Use a saved style's prompt ("" is the user's own words)."""
        index = self.preset_combo.findData(style_id)
        if index >= 0:
            self._on_preset_chosen(index)

    @property
    def auto_on(self) -> bool:
        return self.auto_check.isChecked()

    @property
    def minimize_on(self) -> bool:
        return self.minimize_check.isChecked()

    @property
    def delay_text(self) -> str:
        seconds = self.capture_delay.seconds
        return f"after {seconds} s" if seconds else "instantly"

    def choose_delay(self, seconds: int) -> None:
        self.capture_delay._choose(seconds)
        self._refresh()

    @property
    def has_walkthrough(self) -> bool:
        return not self.review.isHidden()

    def recipe(self) -> list[str]:
        """What a walkthrough started now would do, as short phrases."""
        parts = ["Auto capture" if self.auto_on and AUTO_MODE_HERE else "Manual capture"]
        parts.append(f"{self.agent_name} · {self.style_name}" if self.write_up_on
                     else "As spoken")
        parts.append(f"Copies {self.copy_label}")
        if self.minimize_on:
            parts.append("Minimizes to tray")
        return parts

    # ---- menus over the same choices ----

    def _menu(self) -> QMenu:
        menu = QMenu(self)
        # Opaque: these open over a sentence or a form, which would show through.
        menu.setStyleSheet(CONTEXT_MENU_STYLESHEET.replace("rgba(@surface-rgb, 0.95)", "@surface"))
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        menu.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        menu.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)
        menu.setToolTipsVisible(True)
        return menu

    @staticmethod
    def _choice(menu: QMenu, group: QActionGroup, text: str, on: bool, slot) -> QAction:
        action = menu.addAction(text)
        action.setCheckable(True)
        action.setChecked(on)
        group.addAction(action)
        action.triggered.connect(lambda _checked=False: slot())
        return action

    def copy_menu(self) -> QMenu:
        menu, group = self._menu(), QActionGroup(self)
        for index, (value, text) in enumerate(COPY_CHOICES):
            action = self._choice(menu, group, text, index == self.copy_index,
                                  lambda i=index: self.choose_copy(i))
            set_copy_tip(action, value, on_finish=True)
        return menu

    def agent_menu(self, *, with_off: bool = True) -> QMenu:
        menu, group = self._menu(), QActionGroup(self)
        for agent in AGENTS:
            status, _usable = self.agent_status(agent)
            self._choice(menu, group, f"{AGENT_SPECS[agent].name}   ·   {status}",
                         self.write_up_on and agent == self.agent,
                         lambda a=agent: self.choose_agent(a))
        if with_off:
            menu.addSeparator()
            self._choice(menu, group, "Nobody: keep it as spoken", not self.write_up_on,
                         lambda: self.set_write_up(False))
        return menu

    def style_menu(self, *, edit=None) -> QMenu:
        """Styles to write it up as; ``edit`` adds "Edit the prompt…" calling it."""
        menu, group = self._menu(), QActionGroup(self)
        current = self.style
        for style in self._styles:
            self._choice(menu, group, style.name, current is not None and style.id == current.id,
                         lambda s=style: self.choose_style(s.id))
        if self._own_prompt:
            self._choice(menu, group, "My own prompt", current is None,
                         lambda: self.choose_style(""))
        menu.addSeparator()
        if edit is not None:
            menu.addAction("Edit the prompt…").triggered.connect(lambda _checked=False: edit())
        menu.addAction("New style…").triggered.connect(lambda _checked=False: self._new_style())
        return menu

    def delay_menu(self) -> QMenu:
        menu, group = self._menu(), QActionGroup(self)
        for seconds in WALKTHROUGH_CAPTURE_DELAYS:
            self._choice(menu, group, f"After {seconds} seconds" if seconds else "Instantly",
                         seconds == self.capture_delay.seconds,
                         lambda s=seconds: self.choose_delay(s))
        return menu

    def capture_menu(self) -> QMenu:
        """Who takes the screenshots: you, or Auto Mode."""
        menu, group = self._menu(), QActionGroup(self)
        self._choice(menu, group, "Myself, with the shortcut", not self.auto_on,
                     lambda: self.auto_on and self.auto_check.click())
        self._choice(menu, group, "Automatically, on clicks and drawings", self.auto_on,
                     lambda: self.auto_on or self.auto_check.click())
        return menu

    def window_menu(self) -> QMenu:
        menu, group = self._menu(), QActionGroup(self)
        self._choice(menu, group, "Stays open", not self.minimize_on,
                     lambda: self.minimize_on and self.minimize_check.click())
        self._choice(menu, group, "Goes to the tray", self.minimize_on,
                     lambda: self.minimize_on or self.minimize_check.click())
        return menu

    # ---- keeping a layout's labels current ----

    def _refresh(self) -> None:
        if self._built:
            self.refresh()

    def _sync_setup_summary(self) -> None:
        super()._sync_setup_summary()
        self._refresh()

    def _show_preset(self) -> None:
        super()._show_preset()
        self._refresh()

    def set_recording_state(self, is_recording: bool, walkthrough_active: bool) -> None:
        super().set_recording_state(is_recording, walkthrough_active)
        self._refresh()

    def set_hotkeys(self, walkthrough_key: str, screenshot_key: str) -> None:
        super().set_hotkeys(walkthrough_key, screenshot_key)
        self._refresh()

    def _show(self, folder, *, ask: bool) -> bool:
        shown = super()._show(folder, ask=ask)
        self._refresh()
        return shown

    def preferred_width(self) -> int:
        return self.WIDTH_WITH_STORYBOARD if self.fills_height() else self.WIDTH

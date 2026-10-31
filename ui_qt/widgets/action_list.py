"""Your saved actions as cards: what each does, how to start it, on or off."""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QEvent, QSignalBlocker, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QBoxLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from services.actions.model import Action
from services.hotkey_manager import format_hotkey_display
from ui_qt.utils.font_scale import current_ui_font_scale
from ui_qt.utils.icons import design_icon
from ui_qt.utils.restyle import set_style_property
from ui_qt.widgets.action_copy import KIND_ICONS, spoken_hint, summary
from ui_qt.widgets.action_fields import page_link, tint_icon
from ui_qt.widgets.buttons import Button, compact_primary_button, fit_compact_button
from ui_qt.widgets.empty_state import EmptyState
from ui_qt.widgets.reflow import Reflow
from ui_qt.widgets.setting_tile import InfoTile
from ui_qt.widgets.settings_switch import SettingsSwitch

#: Cards sit two to a row from this page width (at 100% font scale).
TWO_COLUMNS_FROM = 640


class ChipRow(QWidget):
    """Chips side by side at their own width, or one under another when they don't fit."""

    def __init__(self, chips, parent=None):
        super().__init__(parent)
        self.setProperty("tileId", "actionRow")
        self._chips = list(chips)
        self._layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)
        for item in self._chips:
            self._layout.addWidget(item, 0, Qt.AlignmentFlag.AlignLeft)
        self._layout.addStretch(1)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        needed = sum(item.sizeHint().width() for item in self._chips)
        needed += self._layout.spacing() * (len(self._chips) - 1)
        direction = (QBoxLayout.Direction.TopToBottom if self.width() < needed
                     else QBoxLayout.Direction.LeftToRight)
        if self._layout.direction() != direction:
            self._layout.setDirection(direction)


def chip(text: str, tone: str = "") -> QLabel:
    label = QLabel(text)
    label.setObjectName("actionChip")
    label.setProperty("tone", tone)
    return label


class ActionCard(InfoTile):
    """One action: icon, name, what it does, how to start it, a switch, Edit and Delete."""

    edit_requested = pyqtSignal(str)
    delete_requested = pyqtSignal(str)
    enabled_toggled = pyqtSignal(str, bool)

    def __init__(self, action: Action, parent=None):
        super().__init__(action.name, summary(action), design_icon(KIND_ICONS[action.kind]), parent)
        self.setProperty("tileId", "actionCard")
        tint_icon(self, KIND_ICONS[action.kind])
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.action = action
        self.switch = SettingsSwitch()
        self.switch.setAccessibleName(f"{action.name} on")
        self.switch.setChecked(action.enabled)
        self.switch.toggled.connect(lambda on: self.enabled_toggled.emit(self.action.id, bool(on)))
        self.add_trailing(self.switch)

        shortcut = format_hotkey_display(action.hotkey)
        self.shortcut_chip = chip(shortcut or "No shortcut", "key" if shortcut else "muted")
        self.shortcut_chip.setAccessibleName(
            f"Shortcut {shortcut}" if shortcut else "No shortcut")
        spoken = spoken_hint(action)
        self.spoken_chip = chip(spoken, "voice") if spoken else None
        chips = ChipRow([c for c in (self.shortcut_chip, self.spoken_chip) if c is not None])

        links = QWidget()
        links.setProperty("tileId", "actionRow")
        link_row = QHBoxLayout(links)
        link_row.setContentsMargins(0, 0, 0, 0)
        link_row.setSpacing(14)
        self.edit_link = page_link("Edit", f"Change {action.name}")
        self.edit_link.setAccessibleName(f"Edit {action.name}")
        self.edit_link.clicked.connect(lambda: self.edit_requested.emit(self.action.id))
        self.delete_link = page_link("Delete", f"Delete {action.name}")
        self.delete_link.setAccessibleName(f"Delete {action.name}")
        self.delete_link.setProperty("tone", "danger")
        self.delete_link.clicked.connect(lambda: self.delete_requested.emit(self.action.id))
        link_row.addWidget(self.edit_link)
        link_row.addWidget(self.delete_link)
        # Cards in a row share its height; the chips and links stay on the bottom line.
        self.layout().insertStretch(1, 1)
        self.add_body(Reflow(chips, links, stacked_alignment=Qt.AlignmentFlag.AlignLeft))
        set_style_property(self, "off", not action.enabled)

    def set_enabled_shown(self, on: bool) -> None:
        with QSignalBlocker(self.switch):
            self.switch.setChecked(on)
        set_style_property(self, "off", not on)

    def focus_order(self) -> tuple[QWidget, ...]:
        """Its controls in the order they read: the switch at the top, then Edit and Delete."""
        return (self.switch, self.edit_link, self.delete_link)


class ActionList(QWidget):
    """The cards, two to a row when there's room, or an invitation to add one."""

    add_requested = pyqtSignal()
    edit_requested = pyqtSignal(str)
    delete_requested = pyqtSignal(str)
    enabled_toggled = pyqtSignal(str, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("tileId", "actionList")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.empty = EmptyState(
            "No actions yet",
            "Send what you say to a chat, a webhook or your own program, "
            "with its own shortcut or by name.",
        )
        self.empty_add = compact_primary_button(Button("Add an action"))
        fit_compact_button(self.empty_add)
        self.empty_add.clicked.connect(self.add_requested)
        self.empty.add_action(self.empty_add)
        layout.addWidget(self.empty)

        self.grid_box = QWidget()
        self.grid_box.setProperty("tileId", "actionRow")
        self.grid = QGridLayout(self.grid_box)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(12)
        self.grid.setVerticalSpacing(12)
        layout.addWidget(self.grid_box)
        self.cards: list[ActionCard] = []
        #: The actions the cards were made from; None before the first.
        self._actions: Optional[list[Action]] = None
        self._columns = 0
        self._reflow_timer = QTimer(self)
        self._reflow_timer.setSingleShot(True)
        self._reflow_timer.timeout.connect(self._arrange)
        self.set_actions([])

    def card(self, action_id: str) -> Optional[ActionCard]:
        return next((card for card in self.cards if card.action.id == action_id), None)

    def set_actions(self, actions: list[Action]) -> None:
        if list(actions) == self._actions:
            # Nothing changed: the cards keep their place, their focus and any glow.
            return
        self._actions = list(actions)
        for card in self.cards:
            self.grid.removeWidget(card)
            card.hide()
            card.deleteLater()
        self.cards = []
        for action in actions:
            card = ActionCard(action)
            card.edit_requested.connect(self.edit_requested)
            card.delete_requested.connect(self.delete_requested)
            card.enabled_toggled.connect(self.enabled_toggled)
            self.cards.append(card)
        self.empty.setVisible(not self.cards)
        self.grid_box.setVisible(bool(self.cards))
        self._columns = 0
        self._arrange()
        self._chain_focus()

    def _chain_focus(self) -> None:
        """Tab reaches the cards in reading order, right after what comes before the list.

        Cards are made after the rest of the page, so Tab would otherwise
        reach them last, after Route by voice.
        """
        previous = self.empty_add
        for card in self.cards:
            for widget in card.focus_order():
                QWidget.setTabOrder(previous, widget)
                previous = widget

    def _arrange(self) -> None:
        columns = 2 if self.width() >= round(TWO_COLUMNS_FROM * current_ui_font_scale()) else 1
        columns = min(columns, max(1, len(self.cards)))
        if columns == self._columns and all(self.grid.indexOf(c) >= 0 for c in self.cards):
            return
        self._columns = columns
        for card in self.cards:
            self.grid.removeWidget(card)
        for index, card in enumerate(self.cards):
            self.grid.addWidget(card, index // columns, index % columns)
            card.show()
        for column in range(2):
            self.grid.setColumnStretch(column, 1 if column < columns else 0)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._arrange()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange) and hasattr(
                self, "_reflow_timer"):
            self._columns = 0
            self._reflow_timer.start(0)

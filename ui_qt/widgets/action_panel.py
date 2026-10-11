"""Your actions: the cards, the Add action gallery and the editor, one at a time."""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Mapping, Optional

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtWidgets import QLabel, QMessageBox, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from services.actions.model import (
    AGENT_ACTION_ID,
    MAX_ACTIONS,
    Action,
    ActionKind,
    Preset,
    action_secret_name,
    delete_action,
    find_action,
    load_actions,
    new_id,
    save_action,
)
from services.hotkey_manager import format_hotkey_display
from ui_qt.utils.restyle import set_style_property
from ui_qt.widgets.action_editor import ActionEditor
from ui_qt.widgets.action_fields import hint_label, plain_row
from ui_qt.widgets.action_gallery import PresetGallery
from ui_qt.widgets.action_list import ActionList
from ui_qt.widgets.buttons import Button, compact_primary_button, fit_compact_button

LIST, GALLERY, EDITOR = "list", "gallery", "editor"
#: How long a just-saved card glows.
FLASH_MS = 1600


def unique_name(name: str, settings: Mapping) -> str:
    """``name``, or "name 2", "name 3"… when an action already has it."""
    taken = {action.name.casefold() for action in load_actions(settings)}
    candidate, suffix = name, 2
    while candidate.casefold() in taken:
        candidate = f"{name} {suffix}"
        suffix += 1
    return candidate


def saved_note(action: Action) -> str:
    shortcut = format_hotkey_display(action.hotkey)
    if shortcut:
        return f"Saved {action.name}. Press {shortcut}, speak, then press it again."
    if action.spoken_names:
        return f"Saved {action.name}. With Route by voice, say “{action.spoken_names[0]}, …”."
    return f"Saved {action.name}. Give it a shortcut or a spoken name to use it."


class ActionsPanel(QWidget):
    """Lists actions and adds or edits one; saves through ``manager.mutate_settings``."""

    actions_changed = pyqtSignal()
    capture_changed = pyqtSignal(bool)
    destination_requested = pyqtSignal(str)

    def __init__(self, *, manager, try_action: Callable, parent=None):
        super().__init__(parent)
        self.setProperty("tileId", "actionsPanel")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.manager = manager
        self.mode = LIST
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        title = QLabel("Your actions")
        title.setObjectName("settingsTileGroupTitle")
        self.add_button = compact_primary_button(Button("Add action"))
        fit_compact_button(self.add_button, 0)
        self.add_button.clicked.connect(self.add_action)
        self.header = plain_row(title, None, self.add_button)
        layout.addWidget(self.header)
        self.notice = hint_label("")
        self.notice.setProperty("tileId", "actionsNotice")
        self.notice.hide()
        layout.addWidget(self.notice)

        self.list = ActionList()
        self.list.add_requested.connect(self.add_action)
        self.list.edit_requested.connect(self.edit)
        self.list.delete_requested.connect(self.delete)
        self.list.enabled_toggled.connect(self.set_enabled)
        layout.addWidget(self.list)

        self.gallery = PresetGallery()
        self.gallery.chosen.connect(self.start_from_preset)
        self.gallery.cancelled.connect(self.show_list)
        self.gallery.hide()
        layout.addWidget(self.gallery)

        self.editor = ActionEditor(manager=manager, try_action=try_action)
        self.editor.saved.connect(self._on_saved)
        self.editor.cancelled.connect(self.show_list)
        self.editor.capture_changed.connect(self.capture_changed)
        self.editor.destination_requested.connect(self.destination_requested)
        self.editor.hide()
        layout.addWidget(self.editor)

        self._flash_card_id = ""
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.setInterval(FLASH_MS)
        self._flash_timer.timeout.connect(self._end_flash)
        self._reveal_target: Optional[QWidget] = None
        self._reveal_timer = QTimer(self)
        self._reveal_timer.setSingleShot(True)
        self._reveal_timer.setInterval(0)
        self._reveal_timer.timeout.connect(self._reveal)
        self.refresh()

    # ---- what shows ----

    def _settings(self) -> dict:
        return self.manager.load_all_settings()

    def refresh(self, settings: Optional[Mapping] = None) -> None:
        settings = self._settings() if settings is None else settings
        actions = [a for a in load_actions(settings) if a.id != AGENT_ACTION_ID]
        self.list.set_actions(actions)
        self.add_button.setEnabled(len(load_actions(settings)) < MAX_ACTIONS)
        # With no actions the empty card's own button invites the first one.
        self.add_button.setVisible(self.mode == LIST and bool(actions))
        self.add_button.setToolTip(
            "" if self.add_button.isEnabled()
            else f"You can keep up to {MAX_ACTIONS} actions. Delete one to add another.")
        if self.mode == EDITOR:
            self.editor.refresh_choices(settings)

    def _show(self, mode: str) -> None:
        self.mode = mode
        self.list.setVisible(mode == LIST)
        self.gallery.setVisible(mode == GALLERY)
        self.editor.setVisible(mode == EDITOR)
        self.add_button.setVisible(mode == LIST and bool(self.list.cards))

    def show_list(self) -> None:
        self._show(LIST)
        self.refresh()

    def add_action(self) -> None:
        self.show_notice("")
        self._show(GALLERY)
        self._scroll_to(self.gallery)

    def start_from_preset(self, preset: Preset) -> None:
        settings = self._settings()
        action = Action(
            new_id(), unique_name(preset.name, settings), preset.kind, dict(preset.params),
            shaping=preset.shaping, description=preset.description.rstrip("."),
        )
        self._open_editor(action, is_new=True, note=preset.note)

    def edit(self, action_id: str) -> None:
        action = find_action(self._settings(), action_id)
        if action is None:
            self.refresh()
            return
        self._open_editor(action, is_new=False)

    def _open_editor(self, action: Action, *, is_new: bool, note: str = "") -> None:
        self.show_notice("")
        self.editor.open(action, is_new=is_new, note=note)
        self._show(EDITOR)
        self._scroll_to(self.editor)
        self.editor.name_edit.setFocus()

    # ---- changes ----

    def _on_saved(self, action: Action) -> None:
        self._show(LIST)
        self.refresh()
        self.show_notice(saved_note(action), "ok")
        self._flash(action.id)
        self.actions_changed.emit()

    def set_enabled(self, action_id: str, on: bool) -> None:
        action = find_action(self._settings(), action_id)
        if action is None:
            return
        try:
            self.manager.mutate_settings(
                lambda settings: save_action(settings, replace(action, enabled=bool(on))))
        except ValueError as exc:
            self.show_notice(str(exc), "error")
            card = self.list.card(action_id)
            if card is not None:
                card.set_enabled_shown(action.enabled)
            return
        card = self.list.card(action_id)
        if card is not None:
            card.set_enabled_shown(bool(on))
        self.show_notice("")
        self.actions_changed.emit()

    def delete(self, action_id: str) -> None:
        action = find_action(self._settings(), action_id)
        if action is None:
            return
        answer = QMessageBox.question(
            self, "Delete action?", f"Delete {action.name} and its shortcut?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.manager.mutate_settings(lambda settings: delete_action(settings, action_id))
        except Exception as exc:
            self.show_notice(f"Couldn't delete {action.name}: {exc}", "error")
            return
        if action.kind == ActionKind.WEBHOOK:
            self._forget_secret(action_id)
        self.refresh()
        self.show_notice(f"Deleted {action.name}.", "")
        self.actions_changed.emit()

    @staticmethod
    def _forget_secret(action_id: str) -> None:
        from services import credentials

        try:
            credentials.store().delete(action_secret_name(action_id))
        except Exception:
            pass  # Nothing stored, or no store: the secret signs nothing now.

    # ---- drafts and capture ----

    def save_draft(self, *, ask: bool = False) -> bool:
        if self.mode != EDITOR:
            return True
        return self.editor.save_draft(ask=ask)

    def cancel_capture(self) -> None:
        self.editor.cancel_capture()

    # ---- small touches ----

    def show_notice(self, text: str, tone: str = "") -> None:
        self.notice.setText(text)
        set_style_property(self.notice, "tone", tone)
        self.notice.setVisible(bool(text))

    def _flash(self, action_id: str) -> None:
        self._end_flash()
        card = self.list.card(action_id)
        if card is None:
            return
        self._flash_card_id = action_id
        set_style_property(card, "searchHit", True)
        self._flash_timer.start()
        self._scroll_to(card)

    def _end_flash(self) -> None:
        card = self.list.card(self._flash_card_id) if self._flash_card_id else None
        self._flash_card_id = ""
        if card is not None:
            set_style_property(card, "searchHit", False)

    def _scroll_to(self, widget: QWidget) -> None:
        self._reveal_target = widget
        self._reveal_timer.start()

    def _reveal(self) -> None:
        target, self._reveal_target = self._reveal_target, None
        if target is None or not target.isVisible():
            return
        area = self.parentWidget()
        while area is not None and not isinstance(area, QScrollArea):
            area = area.parentWidget()
        if area is not None:
            area.ensureWidgetVisible(target, 0, 24)

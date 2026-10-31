"""The Hotkeys page's rows for the shortcuts you give your own things.

Profiles, transforms and actions each carry an optional shortcut. Their own
editors (Profiles, Commands, Actions) set it too; a row here saves through the
same validation, so a shortcut is refused here exactly when it would be there.
"""

from dataclasses import dataclass, replace

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from services.actions.model import find_action, load_actions, save_action
from services.cleanup_profiles import (
    find_cleanup_profile,
    load_cleanup_profiles,
    save_cleanup_profile,
)
from services.hotkey_conflicts import ACTION, PROFILE, TRANSFORM, hotkey_conflict
from services.hotkey_manager import format_hotkey_display
from services.text_transforms import find_transform, load_transforms, save_transform
from ui_qt.dialogs.settings_destinations import ACTIONS, CLEANUP_PROFILES, COMMANDS
from ui_qt.utils.restyle import set_style_property
from ui_qt.widgets.action_copy import summary
from ui_qt.widgets.buttons import Button, fit_compact_button
from ui_qt.widgets.hotkey_capture import mouse_shortcut_note
from ui_qt.widgets.hotkey_row import ReflowCard
from ui_qt.widgets.profile_hotkey_input import ProfileHotkeyInput
from ui_qt.widgets.wrapped_label import WrappedLabel

#: A profile's or transform's instructions are quoted up to this many characters.
_INSTRUCTION_CHARS = 80


@dataclass(frozen=True)
class Binding:
    """One profile, transform or action, as its row shows it."""

    kind: str
    id: str
    name: str
    hotkey: str
    detail: str
    enabled: bool = True


@dataclass(frozen=True)
class _Group:
    kind: str
    title: str
    destination: str
    link: str
    #: What pressing one of the group's shortcuts does.
    caption: str
    empty: str


GROUPS = (
    _Group(
        PROFILE, "Profiles", CLEANUP_PROFILES, "Open Profiles",
        "Press a profile's shortcut to record and again to stop; what you say "
        "comes out in its format.",
        "No profiles yet. A profile records straight into a format, like an email or a ticket.",
    ),
    _Group(
        TRANSFORM, "Transforms", COMMANDS, "Open Commands",
        "Select text in any app, then press a transform's shortcut to rewrite it.",
        "No transforms yet. A transform rewrites the text you select.",
    ),
    _Group(
        ACTION, "Actions", ACTIONS, "Open Actions",
        "Press an action's shortcut, say what you need, and press it again to send it.",
        "No actions yet. An action sends what you say to a chat, your agent or your own tools.",
    ),
)

#: How to find and save each kind of binding: (find, save) on loaded settings.
_STORE = {
    PROFILE: (find_cleanup_profile, save_cleanup_profile),
    TRANSFORM: (find_transform, save_transform),
    ACTION: (find_action, save_action),
}


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _INSTRUCTION_CHARS else text[: _INSTRUCTION_CHARS - 1].rstrip() + "…"


def bindings(settings) -> dict:
    """Every profile, transform and action, by kind, in their saved order."""
    return {
        PROFILE: [
            Binding(PROFILE, p.id, p.name, p.hotkey, _clip(p.instructions))
            for p in load_cleanup_profiles(settings)
        ],
        TRANSFORM: [
            Binding(TRANSFORM, t.id, t.name, t.hotkey, _clip(t.instruction))
            for t in load_transforms(settings)
        ],
        ACTION: [
            Binding(ACTION, a.id, a.name, a.hotkey, summary(a) + ".", a.enabled)
            for a in load_actions(settings)
        ],
    }


class _Row(ReflowCard):
    """A binding's name and what it does, beside its shortcut and Clear."""

    def __init__(self, binding: Binding, parent=None):
        super().__init__(parent)
        self.setObjectName("hotkeyShortcutCard")
        self.binding = binding
        self.name = WrappedLabel(binding.name)
        self.name.setObjectName("hotkeyShortcutName")
        self.copy.addWidget(self.name)
        self.detail = WrappedLabel(binding.detail)
        self.detail.setObjectName("hotkeyShortcutDescription")
        self.copy.addWidget(self.detail)
        self.note = WrappedLabel("")
        self.note.setObjectName("hotkeyShortcutNote")
        self.note.setVisible(False)
        self.copy.addWidget(self.note)

        self.field = ProfileHotkeyInput()
        self.field.setAccessibleName(f"{binding.name} shortcut")
        self.field.setToolTip("Click, then press a shortcut. Escape cancels.")
        self.field.setMinimumWidth(190)
        self.field.setMaximumWidth(220)
        self.field.notice.connect(lambda text: self.show_note(text, warning=True))
        self.add_control(self.field)
        self.clear = Button("Clear")
        self.clear.setObjectName("hotkeyClearButton")
        self.clear.setAccessibleName(f"Clear {binding.name} shortcut")
        fit_compact_button(self.clear, 68)
        self.add_control(self.clear)

    def set_binding(self, binding: Binding, settings) -> None:
        """Show ``binding``'s saved state, and why its shortcut won't work, if it won't."""
        self.binding = binding
        self.name.setText(binding.name)
        self.detail.setText(binding.detail)
        self.field.setAccessibleName(f"{binding.name} shortcut")
        self.clear.setAccessibleName(f"Clear {binding.name} shortcut")
        if not self.field._capturing:
            self.field.set_hotkey(binding.hotkey)
        self.clear.setEnabled(bool(binding.hotkey))
        # The app skips a shortcut that anything else also uses
        # (HotkeyRuntime.refresh_profile_hotkeys), and an action that is off.
        conflict = hotkey_conflict(
            binding.hotkey, settings, exclude=(binding.kind, binding.id)
        )
        if conflict:
            self.show_note(f"{conflict} uses this shortcut too, so it does nothing here.",
                           warning=True)
        elif binding.hotkey and not binding.enabled:
            self.show_note("This action is turned off, so its shortcut does nothing.")
        else:
            self.show_note(mouse_shortcut_note(binding.hotkey))

    def show_note(self, text: str, *, warning: bool = False) -> None:
        self.note.setText(text)
        set_style_property(self.note, "tone", "warning" if warning else "info")
        self.note.setVisible(bool(text))


class BindingShortcuts(QWidget):
    """Profiles, transforms and actions, each with its shortcut to change or clear."""

    #: A row's shortcut box started (True) or stopped (False) waiting for keys.
    capture_changed = pyqtSignal(bool)
    #: (kind, message): a shortcut of that kind was saved.
    saved = pyqtSignal(str, str)
    #: A group's link: the Settings destination to show.
    open_requested = pyqtSignal(str)

    def __init__(self, *, manager, parent=None):
        super().__init__(parent)
        self.manager = manager
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        self._rows: dict = {kind: [] for kind in _STORE}
        self._lists: dict = {}
        self._captions: dict = {}
        self._empty: dict = {}
        for group in GROUPS:
            header = QHBoxLayout()
            header.setContentsMargins(0, 0, 0, 0)
            title = QLabel(group.title)
            title.setObjectName("hotkeyGroupTitle")
            header.addWidget(title)
            header.addStretch()
            link = QPushButton(group.link)
            link.setObjectName("settingsPageLink")
            link.setFlat(True)
            link.setCursor(Qt.CursorShape.PointingHandCursor)
            link.setToolTip(f"Create and edit {group.title.lower()} on their own page")
            link.clicked.connect(lambda _checked=False, d=group.destination: self.open_requested.emit(d))
            header.addWidget(link, alignment=Qt.AlignmentFlag.AlignBottom)
            root.addLayout(header)
            caption = WrappedLabel(group.caption)
            caption.setObjectName("infoLabel")
            root.addWidget(caption)
            self._captions[group.kind] = caption
            rows = QVBoxLayout()
            rows.setContentsMargins(0, 0, 0, 0)
            rows.setSpacing(10)
            root.addLayout(rows)
            self._lists[group.kind] = rows
            empty = WrappedLabel(group.empty)
            empty.setObjectName("hotkeyShortcutDescription")
            rows.addWidget(empty)
            self._empty[group.kind] = empty

    def capturing(self) -> bool:
        return any(row.field._capturing for rows in self._rows.values() for row in rows)

    def cancel_capture(self) -> None:
        for rows in self._rows.values():
            for row in rows:
                row.field.cancel_capture()

    def load(self, settings) -> None:
        """Show what is saved; a box waiting for keys keeps waiting."""
        if self.capturing():
            return
        for kind, items in bindings(settings).items():
            rows = self._rows[kind]
            if [r.binding.id for r in rows] != [b.id for b in items]:
                self._rebuild(kind, items)
                rows = self._rows[kind]
            for row, binding in zip(rows, items):
                row.set_binding(binding, settings)
            # An empty group says what it's for instead.
            self._captions[kind].setVisible(bool(items))
            self._empty[kind].setVisible(not items)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # Another page may have changed a shortcut, or added or removed a binding.
        self.load(self.manager.load_all_settings())

    def _rebuild(self, kind: str, items: list) -> None:
        layout = self._lists[kind]
        for row in self._rows[kind]:
            layout.removeWidget(row)
            # Off the page now, not when deleteLater gets to it.
            row.setParent(None)
            row.deleteLater()
        self._rows[kind] = []
        for binding in items:
            row = _Row(binding)
            row.field.capture_changed.connect(self.capture_changed)
            row.field.captured.connect(lambda hotkey, row=row: self._save(row, hotkey))
            row.clear.clicked.connect(lambda _checked=False, row=row: self._save(row, ""))
            layout.addWidget(row)
            self._rows[kind].append(row)

    def _save(self, row: _Row, hotkey: str) -> None:
        binding = row.binding
        find, save = _STORE[binding.kind]

        def mutate(settings):
            item = find(settings, binding.id)
            if item is None:
                raise ValueError(f"{binding.name} no longer exists.")
            save(settings, replace(item, hotkey=hotkey))

        try:
            self.manager.mutate_settings(mutate)
        except ValueError as exc:  # The same reason its own editor would give.
            row.field.set_hotkey(binding.hotkey)
            row.show_note(str(exc), warning=True)
            return
        except Exception as exc:  # The settings file couldn't be written.
            row.field.set_hotkey(binding.hotkey)
            row.show_note(f"Couldn't save the shortcut: {exc}", warning=True)
            return
        self.load(self.manager.load_all_settings())
        shortcut = format_hotkey_display(hotkey)
        message = (f"{binding.name} shortcut set to {shortcut}." if shortcut
                   else f"{binding.name} shortcut cleared.")
        self.saved.emit(binding.kind, message)

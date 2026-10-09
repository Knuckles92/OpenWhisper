"""Controls for Settings → Commands: the Command Mode shortcut and the transforms library."""

from uuid import uuid4

from PyQt6.QtCore import QEvent, QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QBoxLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from services.hotkey_manager import format_hotkey_display
from services.settings import RecordingTriggerMode, resolve_recording_trigger_mode
from services.text_transforms import (
    NAME_MAX_CHARS,
    STARTER_TRANSFORMS,
    Transform,
    delete_transform,
    load_transforms,
    save_transform,
    transform_hotkey_conflict,
)
from ui_qt.utils.font_scale import current_ui_font_scale
from ui_qt.widgets.button_row import ButtonRow
from ui_qt.widgets.buttons import (
    Button,
    compact_primary_button,
    fit_compact_button,
    neutral_button,
)
from ui_qt.widgets.profile_hotkey_input import ProfileHotkeyInput
from ui_qt.widgets.wrapped_label import WrappedLabel

COMMAND_ACTION = "command_mode"
#: The transforms list's width beside the editor, unless its buttons need more.
LIBRARY_WIDTH = 210
#: The Command Mode shortcut box's narrowest width beside its buttons, at 100%.
INPUT_MIN_WIDTH = 140
_CAPTURE_TIP = "Click, then press a shortcut. Escape cancels."


def command_mode_hint(settings) -> str:
    """How the Command Mode shortcut is used in the current recording mode."""
    if resolve_recording_trigger_mode(settings) == RecordingTriggerMode.PUSH_HOLD:
        return "Hold the shortcut while you speak, then let go."
    return "Press the shortcut, speak, then press it again."


def _shortcut_input(name: str) -> ProfileHotkeyInput:
    field = ProfileHotkeyInput()
    field.setAccessibleName(name)
    field.setToolTip(_CAPTURE_TIP)
    return field


class ShowWatcher(QObject):
    """Calls ``callback`` whenever ``widget`` is shown again."""

    def __init__(self, widget: QWidget, callback):
        super().__init__(widget)
        self._callback = callback
        widget.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Show:
            self._callback()
        return False


class CommandShortcutField(QWidget):
    """Captures the Command Mode shortcut and saves it through Settings' hotkey path."""

    def __init__(self, dialog, *, change_button: bool = False, clear_button: bool = True,
                 inline_errors: bool = True, parent=None):
        super().__init__(parent)
        self.setProperty("tileId", "commandShortcutField")
        self.dialog = dialog
        self._inline_errors = inline_errors
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        # Settings pages ignore minimum widths, so a narrow tile would draw
        # the buttons over the box; resizeEvent stacks them instead.
        row = self._row = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.input = _shortcut_input("Command Mode shortcut")
        self.input.setMinimumWidth(INPUT_MIN_WIDTH)
        self.input.setMaximumWidth(240)
        self.input.capture_changed.connect(dialog.set_hotkey_capture_suspended)
        self.input.captured.connect(self._save)
        row.addWidget(self.input, 1)
        self.change_button = None
        if change_button:
            # Plain, like the Recording shortcut's Change on the Basic tab.
            self.change_button = QPushButton("Change")
            self.change_button.setObjectName("basicSettingsChangeShortcut")
            self.change_button.clicked.connect(self.begin_capture)
            row.addWidget(self.change_button)
        self.clear_button = None
        if clear_button:
            self.clear_button = neutral_button(Button("Clear"))
            self.clear_button.set_base_minimum_size(64, 34)
            self.clear_button.setToolTip("Remove the Command Mode shortcut")
            self.clear_button.clicked.connect(lambda: self._save(""))
            row.addWidget(self.clear_button)
        row.addStretch()
        column.addLayout(row)
        self.message = WrappedLabel("")
        self.message.setObjectName("infoLabel")
        self.message.setProperty("tileId", "commandShortcutError")
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.hide()
        column.addWidget(self.message)
        self.sync()

    def hotkey(self) -> str:
        hotkeys = self.dialog.current_hotkeys or {}
        if not hotkeys:
            stored = self.dialog._settings_snapshot().get("hotkeys")
            hotkeys = stored if isinstance(stored, dict) else {}
        value = hotkeys.get(COMMAND_ACTION, "")
        return value if isinstance(value, str) else ""

    def sync(self) -> None:
        if not self.input._capturing:
            self.input.set_hotkey(self.hotkey())
        if self.clear_button is not None:
            self.clear_button.setEnabled(bool(self.hotkey()))

    def begin_capture(self) -> None:
        self.input.setFocus()
        self.input.begin_capture()

    def cancel_capture(self) -> None:
        self.input.cancel_capture()

    def show_error(self, text: str) -> None:
        self.message.setText(text)
        self.message.setVisible(bool(text) and self._inline_errors)

    def _save(self, hotkey: str) -> None:
        error = self.dialog.set_standard_hotkey(COMMAND_ACTION, hotkey)
        self.show_error(error)
        self.sync()

    def showEvent(self, event):
        super().showEvent(event)
        self.sync()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        box = round(INPUT_MIN_WIDTH * current_ui_font_scale())
        buttons = [b for b in (self.change_button, self.clear_button) if b is not None]
        # A label-fitted Button holds a floor above its size hint; Qt draws
        # the box over a button squeezed below it (seen with macOS fonts).
        needed = box + sum(
            self._row.spacing() + max(b.sizeHint().width(), b.minimumWidth()) for b in buttons
        )
        stacked = self.width() < needed
        self.input.setMinimumWidth(min(box, self.width()) if stacked else box)
        direction = QBoxLayout.Direction.TopToBottom if stacked else QBoxLayout.Direction.LeftToRight
        if self._row.direction() != direction:
            self._row.setDirection(direction)
            for button in buttons:
                self._row.setAlignment(
                    button, Qt.AlignmentFlag.AlignLeft if stacked else Qt.AlignmentFlag(0))


class TransformsPanel(QWidget):
    """A library and editor for transforms, saved through ``mutate_settings``."""

    transforms_changed = pyqtSignal()
    capture_changed = pyqtSignal(bool)

    def __init__(self, parent=None, *, manager):
        super().__init__(parent)
        self.setObjectName("transformsPanel")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.manager = manager
        self._transform_id = ""
        self._saved = None
        self._loading = False
        self._hotkey_before_capture = ""
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        columns = self._columns = QHBoxLayout()
        columns.setSpacing(18)
        root.addLayout(columns, 1)
        library = self._library = QWidget()
        library.setObjectName("transformLibrary")
        library.setFixedWidth(LIBRARY_WIDTH)
        library_layout = QVBoxLayout(library)
        library_layout.setContentsMargins(0, 0, 0, 0)
        library_layout.setSpacing(8)
        columns.addWidget(library)
        editor = self._editor = QWidget()
        editor.setObjectName("transformEditor")
        editor.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        layout = QVBoxLayout(editor)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)
        columns.addWidget(editor, 1)

        self.transform_list = QListWidget()
        self.transform_list.setObjectName("transformsList")
        self.transform_list.setAccessibleName("Transforms")
        self.transform_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.transform_list.setMinimumHeight(120)
        self.transform_list.currentItemChanged.connect(self._selection_changed)
        library_layout.addWidget(self.transform_list, 1)
        actions = QGridLayout()
        actions.setHorizontalSpacing(8)
        actions.setVerticalSpacing(8)
        self.new_button = compact_primary_button(Button("New transform"))
        self.duplicate_button = neutral_button(Button("Duplicate"))
        self.delete_button = neutral_button(Button("Delete"))
        for button, callback, row, column, span in (
            (self.new_button, self.new_transform, 0, 0, 2),
            (self.duplicate_button, self.duplicate_transform, 1, 0, 1),
            (self.delete_button, self.delete_transform, 1, 1, 1),
        ):
            button.set_base_minimum_size(0, 34)
            button.clicked.connect(callback)
            actions.addWidget(button, row, column, 1, span)
        library_layout.addLayout(actions)

        label = QLabel("Name")
        label.setObjectName("textModelFieldLabel")
        self.name_edit = QLineEdit()
        self.name_edit.setMaxLength(NAME_MAX_CHARS)
        self.name_edit.setPlaceholderText("e.g. Make it friendlier")
        label.setBuddy(self.name_edit)
        layout.addWidget(label)
        layout.addWidget(self.name_edit)
        label = QLabel("Instruction")
        label.setObjectName("textModelFieldLabel")
        self.instruction_edit = QTextEdit()
        self.instruction_edit.setAcceptRichText(False)
        self.instruction_edit.setMinimumHeight(110)
        self.instruction_edit.setPlaceholderText(
            "Say how to change the selected text, e.g. "
            "“Make it warmer and shorter. Keep every date.”"
        )
        label.setBuddy(self.instruction_edit)
        layout.addWidget(label)
        layout.addWidget(self.instruction_edit, 1)
        start_from = QLabel("Start from")
        start_from.setObjectName("textModelFieldLabel")
        layout.addWidget(start_from)
        starters = []
        for transform in STARTER_TRANSFORMS:
            button = neutral_button(Button(transform.name))
            # The page lays out at its minimum height, so the floor is the label's.
            fit_compact_button(button, 0)
            button.setToolTip(transform.instruction)
            button.clicked.connect(lambda _checked=False, t=transform: self.use_starter(t))
            starters.append(button)
        self.starter_row = ButtonRow(starters)
        self.starter_row.setProperty("tileId", "transformStarters")
        layout.addWidget(self.starter_row)

        shortcut_label = QLabel("Shortcut (optional)")
        shortcut_label.setObjectName("textModelFieldLabel")
        self.hotkey_input = _shortcut_input("Transform shortcut")
        shortcut_label.setBuddy(self.hotkey_input)
        self.hotkey_input.capture_changed.connect(self._on_capture_changed)
        self.hotkey_input.captured.connect(self._on_hotkey_captured)
        layout.addWidget(shortcut_label)
        shortcut = QHBoxLayout()
        shortcut.addWidget(self.hotkey_input, 1)
        clear = neutral_button(Button("Clear"))
        clear.set_base_minimum_size(64, 34)
        clear.clicked.connect(lambda: self.hotkey_input.set_hotkey(""))
        shortcut.addWidget(clear)
        layout.addLayout(shortcut)
        hint = WrappedLabel("Select text in any app, then press the shortcut to rewrite it.")
        hint.setObjectName("infoLabel")
        layout.addWidget(hint)

        self.message = WrappedLabel("")
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setObjectName("infoLabel")
        root.addWidget(self.message)
        footer = QHBoxLayout()
        footer.addStretch()
        self.save_button = compact_primary_button(Button("Save transform"))
        fit_compact_button(self.save_button, 132)
        self.save_button.clicked.connect(self.save_transform)
        footer.addWidget(self.save_button)
        root.addLayout(footer)
        self.refresh()

    def _library_width(self) -> int:
        # Wider fonts (Omarchy's, or a large scale) need more than the
        # default for Duplicate and Delete to keep their gap.
        buttons = sum(
            max(button.minimumWidth(), button.sizeHint().width())
            for button in (self.duplicate_button, self.delete_button)
        )
        return max(LIBRARY_WIDTH, buttons + 8)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        library_width = self._library_width()
        narrow = (
            self.width()
            < library_width + self._columns.spacing() + self._editor.minimumSizeHint().width()
        )
        self._columns.setDirection(
            QBoxLayout.Direction.TopToBottom if narrow else QBoxLayout.Direction.LeftToRight
        )
        self._library.setMinimumWidth(0 if narrow else library_width)
        self._library.setMaximumWidth(16777215 if narrow else library_width)
        self.transform_list.setMaximumHeight(170 if narrow else 16777215)

    def _settings(self) -> dict:
        return self.manager.load_all_settings()

    def _draft(self) -> Transform:
        return Transform(
            self._transform_id,
            self.name_edit.text().strip(),
            self.instruction_edit.toPlainText().strip(),
            self.hotkey_input.hotkey,
        )

    def has_unsaved_changes(self) -> bool:
        return self._saved is not None and self._draft() != self._saved

    def _save_before_switch(self) -> bool:
        if not self.has_unsaved_changes():
            return True
        answer = QMessageBox.question(
            self,
            "Unsaved transform",
            "Save your changes to this transform?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Save:
            return self.save_transform()
        return answer == QMessageBox.StandardButton.Discard

    def save_draft(self, *, ask: bool = False) -> bool:
        """Save an unsaved edit when Settings leaves the page.

        A draft that can't be saved yet stays in the editor with the reason.
        With ``ask`` the user discards it or keeps editing instead.

        Returns:
            False when the user chose to keep editing.
        """
        if not self.has_unsaved_changes() or self.save_transform() or not ask:
            return True
        answer = QMessageBox.question(
            self,
            "Unsaved transform",
            f"{self.message.text()} Discard your changes to this transform?",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Discard:
            return False
        self.refresh(self._transform_id)
        return True

    def refresh(self, selected_id=None) -> None:
        # Coming back to Settings must not throw away a draft in progress.
        if selected_id is None and self.has_unsaved_changes():
            return
        selected_id = self._transform_id if selected_id is None else selected_id
        self._loading = True
        try:
            self.transform_list.clear()
            transforms = load_transforms(self._settings())
            selected = None
            for transform in transforms:
                label = transform.name
                if transform.hotkey:
                    label += f"\n{format_hotkey_display(transform.hotkey)}"
                item = QListWidgetItem(label)
                item.setToolTip(transform.instruction)
                item.setData(Qt.ItemDataRole.UserRole, transform)
                self.transform_list.addItem(item)
                if transform.id == selected_id:
                    selected = item
            if transforms:
                selected = selected or self.transform_list.item(0)
                self.transform_list.setCurrentItem(selected)
                self._show(selected.data(Qt.ItemDataRole.UserRole))
            else:
                self._show(Transform(uuid4().hex, "", ""))
        finally:
            self._loading = False

    def _show(self, transform: Transform) -> None:
        self.hotkey_input.cancel_capture()
        self._transform_id = transform.id
        self.name_edit.setText(transform.name)
        self.instruction_edit.setPlainText(transform.instruction)
        self.hotkey_input.set_hotkey(transform.hotkey)
        self._saved = transform
        exists = any(t.id == transform.id for t in load_transforms(self._settings()))
        self.delete_button.setEnabled(exists)
        self.duplicate_button.setEnabled(bool(transform.name))
        self.message.clear()

    def _selection_changed(self, current, previous) -> None:
        if self._loading or current is None:
            return
        transform = current.data(Qt.ItemDataRole.UserRole)
        if not self._save_before_switch():
            self.transform_list.blockSignals(True)
            self.transform_list.setCurrentItem(previous)
            self.transform_list.blockSignals(False)
            return
        self.refresh(transform.id)

    def _start_draft(self, transform: Transform) -> None:
        self.transform_list.blockSignals(True)
        self.transform_list.setCurrentRow(-1)
        self.transform_list.blockSignals(False)
        self._show(transform)

    def new_transform(self) -> None:
        if self._save_before_switch():
            self._start_draft(Transform(uuid4().hex, "", ""))
            self.name_edit.setFocus()

    def duplicate_transform(self) -> None:
        source = self._draft()
        if not self._save_before_switch():
            return
        names = {t.name.casefold() for t in load_transforms(self._settings())}
        stem = source.name[: NAME_MAX_CHARS - 8]
        name = f"{stem} copy"
        suffix = 2
        while name.casefold() in names:
            name = f"{stem} copy {suffix}"
            suffix += 1
        self._start_draft(Transform(uuid4().hex, name, source.instruction))
        # A duplicate is a draft until it is saved.
        self._saved = Transform(self._transform_id, "", "")

    def use_starter(self, transform: Transform) -> None:
        if self.instruction_edit.toPlainText().strip():
            answer = QMessageBox.question(
                self,
                "Use this starter?",
                "Replace the current instruction?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        if not self.name_edit.text().strip():
            self.name_edit.setText(transform.name)
        self.instruction_edit.setPlainText(transform.instruction)

    def cancel_capture(self) -> None:
        self.hotkey_input.cancel_capture()

    def _on_capture_changed(self, capturing: bool) -> None:
        if capturing:
            self._hotkey_before_capture = self.hotkey_input.hotkey
        self.capture_changed.emit(capturing)

    def _on_hotkey_captured(self, hotkey: str) -> None:
        conflict = transform_hotkey_conflict(
            hotkey, self._settings(), exclude_id=self._transform_id
        )
        if conflict:
            self.hotkey_input.set_hotkey(self._hotkey_before_capture)
            self.message.setText(f"That shortcut is already used by {conflict}. Choose another.")
        else:
            self.message.clear()

    def save_transform(self) -> bool:
        self.hotkey_input.cancel_capture()
        transform = self._draft()
        try:
            self.manager.mutate_settings(lambda settings: save_transform(settings, transform))
        except Exception as exc:
            self.message.setText(str(exc))
            return False
        self._saved = transform
        self.refresh(transform.id)
        shortcut = format_hotkey_display(transform.hotkey)
        self.message.setText(
            f"Saved {transform.name}. Select text anywhere and press {shortcut}."
            if shortcut else f"Saved {transform.name}."
        )
        self.transforms_changed.emit()
        return True

    def delete_transform(self) -> None:
        answer = QMessageBox.question(
            self,
            "Delete transform?",
            "Delete this transform and its shortcut?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        transform_id = self._transform_id
        try:
            self.manager.mutate_settings(lambda settings: delete_transform(settings, transform_id))
        except Exception as exc:
            self.message.setText(str(exc))
            return
        self._saved = None
        self.refresh("")
        self.transforms_changed.emit()

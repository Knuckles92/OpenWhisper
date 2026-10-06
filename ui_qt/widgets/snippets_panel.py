"""The snippet library and editor on Settings → Personalize → Snippets."""

from PyQt6.QtCore import QRect, QSignalBlocker, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QFontMetrics
from PyQt6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from config import config
from services.snippets import (
    MAX_TRIGGER_CHARS,
    Snippet,
    delete_snippet,
    find_duplicate,
    load_snippets,
    new_snippet_id,
    plain_text,
    save_snippet,
    trigger_note,
)
from ui_qt.utils.font_scale import current_ui_font_scale
from ui_qt.utils.palette import token_color
from ui_qt.utils.restyle import set_style_property
from ui_qt.widgets.button_row import ButtonRow
from ui_qt.widgets.buttons import Button, compact_primary_button, neutral_button
from ui_qt.widgets.settings_switch import SettingsSwitch
from ui_qt.widgets.wrapped_label import WrappedLabel

#: Library column width at 100%; Duplicate and Delete fit side by side.
LIBRARY_WIDTH = 220
#: Narrowest editor column at 100% before the library moves above it.
EDITOR_MIN_WIDTH = 300

#: Offered while the library is empty; each starts a draft with that trigger.
SUGGESTED_TRIGGERS = ("my email address", "my calendar link", "my sign-off")

_ID_ROLE = Qt.ItemDataRole.UserRole
_PREVIEW_ROLE = Qt.ItemDataRole.UserRole + 1


def _first_line(snippet: Snippet) -> str:
    return next((line.strip() for line in plain_text(snippet).splitlines() if line.strip()), "")


class _SnippetDelegate(QStyledItemDelegate):
    """Paints a snippet as its trigger over a dimmer first line of its text.

    The row background (hover, selection) still comes from the stylesheet.
    """

    PAD_X = 10
    PAD_Y = 8
    GAP = 2

    @staticmethod
    def _fonts(option) -> tuple[QFont, QFont]:
        title = QFont(option.font)
        title.setWeight(QFont.Weight.DemiBold)
        detail = QFont(option.font)
        if detail.pixelSize() > 0:
            detail.setPixelSize(max(1, detail.pixelSize() - 1))
        else:
            detail.setPointSizeF(max(1.0, detail.pointSizeF() - 0.75))
        return title, detail

    def sizeHint(self, option, index) -> QSize:
        title, detail = self._fonts(option)
        height = QFontMetrics(title).height() + self.GAP + QFontMetrics(detail).height()
        return QSize(0, height + 2 * self.PAD_Y)

    def paint(self, painter, option, index) -> None:
        item = QStyleOptionViewItem(option)
        self.initStyleOption(item, index)
        trigger, item.text = item.text, ""
        style = item.widget.style() if item.widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, item, painter, item.widget)
        selected = bool(item.state & QStyle.StateFlag.State_Selected)
        title_font, detail_font = self._fonts(item)
        area = item.rect.adjusted(self.PAD_X, self.PAD_Y, -self.PAD_X, -self.PAD_Y)
        painter.save()
        top = area.top()
        for text, font, color in (
            (trigger, title_font, "text-heading" if selected else "slate-text"),
            (index.data(_PREVIEW_ROLE) or "", detail_font, "accent-soft" if selected else "slate-text-3"),
        ):
            metrics = QFontMetrics(font)
            line = QRect(area.left(), top, area.width(), metrics.height())
            painter.setFont(font)
            painter.setPen(token_color(color))
            painter.drawText(
                line,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                metrics.elidedText(text, Qt.TextElideMode.ElideRight, area.width()),
            )
            top += metrics.height() + self.GAP
        painter.restore()


def _field_label(text: str, buddy: QWidget) -> QLabel:
    label = QLabel(text)
    label.setObjectName("textModelFieldLabel")
    label.setBuddy(buddy)
    return label


class SnippetsPanel(QWidget):
    """Pick a snippet on the left, edit it on the right; stacks when narrow.

    An unsaved edit is never dropped silently: switching snippets asks to
    save it first, and ``refresh`` leaves it in place.
    """

    snippets_changed = pyqtSignal()

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.setObjectName("snippetsPanel")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.manager = manager
        self._snippets: list[Snippet] = []
        self._saved = Snippet(new_snippet_id(), "", "")
        self._snippet_id = self._saved.id
        self._loading = False

        self.count_label = QLabel()
        self.count_label.setObjectName("snippetsCount")
        self.count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)
        self._columns = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self._columns.setSpacing(18)
        root.addLayout(self._columns)
        self._library = QWidget()
        self._library.setObjectName("snippetsLibrary")
        self._columns.addWidget(self._library)
        self._editor = QWidget()
        self._editor.setObjectName("snippetsEditor")
        self._columns.addWidget(self._editor, 1)
        self._build_library()
        self._build_editor()
        self.refresh()

    def _build_library(self) -> None:
        column = QVBoxLayout(self._library)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        self.snippet_list = QListWidget()
        self.snippet_list.setObjectName("snippetsList")
        self.snippet_list.setAccessibleName("Snippets")
        self.snippet_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.snippet_list.setUniformItemSizes(True)
        self.snippet_list.setItemDelegate(_SnippetDelegate(self.snippet_list))
        self.snippet_list.setMinimumHeight(120)
        self.snippet_list.currentItemChanged.connect(self._selection_changed)
        column.addWidget(self.snippet_list, 1)

        self.empty_state = QFrame()
        self.empty_state.setObjectName("snippetsEmpty")
        empty = QVBoxLayout(self.empty_state)
        empty.setContentsMargins(14, 14, 14, 14)
        empty.setSpacing(8)
        title = WrappedLabel("No snippets yet")
        title.setObjectName("snippetsEmptyTitle")
        empty.addWidget(title)
        hint = WrappedLabel("Start with one of these, or add your own.")
        hint.setObjectName("infoLabel")
        empty.addWidget(hint)
        self.suggestion_buttons = []
        for trigger in SUGGESTED_TRIGGERS:
            button = neutral_button(Button(trigger))
            button.setAccessibleName(f"Start a snippet for “{trigger}”")
            button.clicked.connect(lambda _checked=False, t=trigger: self.new_snippet(t))
            empty.addWidget(button)
            self.suggestion_buttons.append(button)
        empty.addStretch()
        column.addWidget(self.empty_state, 1)

        self.new_button = compact_primary_button(Button("New snippet"))
        self.new_button.clicked.connect(lambda: self.new_snippet())
        column.addWidget(self.new_button)
        self.duplicate_button = neutral_button(Button("Duplicate"))
        self.duplicate_button.clicked.connect(self.duplicate_snippet)
        self.delete_button = neutral_button(Button("Delete"))
        self.delete_button.clicked.connect(self.delete_snippet)
        # Wraps onto two rows rather than overlapping in a narrow column.
        column.addWidget(ButtonRow([self.duplicate_button, self.delete_button]))

    def _build_editor(self) -> None:
        column = QVBoxLayout(self._editor)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)

        self.trigger_edit = QLineEdit()
        self.trigger_edit.setObjectName("snippetTriggerInput")
        self.trigger_edit.setMaxLength(MAX_TRIGGER_CHARS)
        self.trigger_edit.setPlaceholderText("e.g. my calendar link")
        self.trigger_edit.setAccessibleName("Trigger phrase")
        self.trigger_edit.textChanged.connect(self._update_hints)
        column.addWidget(_field_label("Trigger phrase", self.trigger_edit))
        column.addWidget(self.trigger_edit)
        self.trigger_note = WrappedLabel("")
        self.trigger_note.setObjectName("snippetTriggerNote")
        self.trigger_note.setTextFormat(Qt.TextFormat.PlainText)
        self.trigger_note.hide()
        column.addWidget(self.trigger_note)
        column.addSpacing(4)

        self.text_edit = QPlainTextEdit()
        self.text_edit.setObjectName("snippetTextInput")
        self.text_edit.setPlaceholderText("The address, link, or sign-off to type for you…")
        self.text_edit.setAccessibleName("Text to insert")
        self.text_edit.setTabChangesFocus(True)
        self.text_edit.setMinimumHeight(round(120 * current_ui_font_scale()))
        self.text_edit.textChanged.connect(self._update_hints)
        column.addWidget(_field_label("Text to insert", self.text_edit))
        column.addWidget(self.text_edit, 1)
        column.addSpacing(4)

        format_row = QWidget()
        format_row.setObjectName("snippetsFormatRow")
        row = QHBoxLayout(format_row)
        row.setContentsMargins(0, 2, 0, 2)
        row.setSpacing(12)
        self.format_switch = SettingsSwitch()
        self.format_switch.setAccessibleName("Keep formatting")
        self.format_switch.toggled.connect(self._update_hints)
        row.addWidget(self.format_switch, alignment=Qt.AlignmentFlag.AlignTop)
        copy = QVBoxLayout()
        copy.setContentsMargins(0, 0, 0, 0)
        copy.setSpacing(2)
        format_title = WrappedLabel("Keep formatting")
        format_title.setObjectName("snippetsFormatTitle")
        copy.addWidget(format_title)
        format_hint = WrappedLabel(
            "Write **bold**, *italic*, [links](https://…) and - lists. Apps that "
            "take rich text paste them formatted; others get plain text."
        )
        format_hint.setObjectName("infoLabel")
        format_hint.setTextFormat(Qt.TextFormat.PlainText)
        copy.addWidget(format_hint)
        row.addLayout(copy, 1)
        column.addWidget(format_row)
        column.addSpacing(4)

        self._footer = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self._footer.setSpacing(12)
        self.preview = WrappedLabel("")
        self.preview.setObjectName("snippetPreview")
        self.preview.setTextFormat(Qt.TextFormat.PlainText)
        self._footer.addWidget(self.preview, 1)
        self.save_button = compact_primary_button(Button("Save snippet"))
        self.save_button.clicked.connect(self.save_snippet)
        self._footer.addWidget(self.save_button, 0, Qt.AlignmentFlag.AlignRight)
        column.addLayout(self._footer)
        self.message = WrappedLabel("")
        self.message.setObjectName("snippetMessage")
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.hide()
        column.addWidget(self.message)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow()

    def _reflow(self) -> None:
        scale = current_ui_font_scale()
        library_width = round(LIBRARY_WIDTH * scale)
        spacing = self._columns.spacing()
        narrow = self.width() < library_width + spacing + round(EDITOR_MIN_WIDTH * scale)
        self._columns.setDirection(
            QBoxLayout.Direction.TopToBottom if narrow else QBoxLayout.Direction.LeftToRight
        )
        self._library.setMinimumWidth(0 if narrow else library_width)
        self._library.setMaximumWidth(16777215 if narrow else library_width)
        editor_width = self.width() if narrow else self.width() - library_width - spacing
        self._footer.setDirection(
            QBoxLayout.Direction.TopToBottom
            if editor_width < round(340 * scale)
            else QBoxLayout.Direction.LeftToRight
        )
        if narrow and self.snippet_list.count():
            # Stacked above the editor, the list shows about three rows and
            # scrolls, instead of pushing the editor off the page.
            rows = min(self.snippet_list.count(), 3.5)
            chrome = self.snippet_list.height() - self.snippet_list.viewport().height()
            height = round(rows * self.snippet_list.sizeHintForRow(0) + max(chrome, 12))
            self.snippet_list.setMinimumHeight(height)
            self.snippet_list.setMaximumHeight(height)
        else:
            self.snippet_list.setMinimumHeight(round(120 * scale))
            self.snippet_list.setMaximumHeight(16777215)

    def _settings(self) -> dict:
        try:
            return self.manager.load_all_settings()
        except Exception:
            return {}

    def _draft(self) -> Snippet:
        return Snippet(
            self._snippet_id,
            self.trigger_edit.text().strip(),
            self.text_edit.toPlainText().strip("\r\n"),
            self.format_switch.isChecked(),
        )

    def has_unsaved_changes(self) -> bool:
        return self._draft() != self._saved

    def _save_before_switch(self) -> bool:
        if not self.has_unsaved_changes():
            return True
        answer = QMessageBox.question(
            self,
            "Unsaved snippet",
            "Save your changes to this snippet?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Save:
            return self.save_snippet()
        return answer == QMessageBox.StandardButton.Discard

    def refresh(self, selected_id: str | None = None) -> None:
        """Re-read the library; keeps an unsaved draft unless told what to select."""
        self._snippets = load_snippets(self._settings())
        self.count_label.setText(f"{len(self._snippets)} / {config.MAX_SNIPPETS}")
        self.empty_state.setVisible(not self._snippets)
        self.snippet_list.setVisible(bool(self._snippets))
        keep_draft = selected_id is None and self.has_unsaved_changes()
        selected_id = self._snippet_id if selected_id is None else selected_id
        self._loading = True
        try:
            self.snippet_list.clear()
            selected = None
            for snippet in self._snippets:
                preview = _first_line(snippet)
                item = QListWidgetItem(snippet.trigger)
                item.setToolTip(f"{snippet.trigger}\n{preview}")
                item.setData(_ID_ROLE, snippet.id)
                item.setData(_PREVIEW_ROLE, preview)
                self.snippet_list.addItem(item)
                if snippet.id == selected_id:
                    selected = item
            if keep_draft:
                if selected is not None:
                    self.snippet_list.setCurrentItem(selected)
                self._update_hints()
                return
            if selected is None and self._snippets:
                selected = self.snippet_list.item(0)
            if selected is not None:
                self.snippet_list.setCurrentItem(selected)
                self._show(self._snippet(selected.data(_ID_ROLE)))
            else:
                self.snippet_list.setCurrentRow(-1)
                self._show(Snippet(new_snippet_id(), "", ""))
        finally:
            self._loading = False
            self._reflow()

    def _snippet(self, snippet_id: str) -> Snippet:
        return next((s for s in self._snippets if s.id == snippet_id), Snippet(new_snippet_id(), "", ""))

    def _show(self, snippet: Snippet, *, saved: Snippet | None = None) -> None:
        for widget in (self.trigger_edit, self.text_edit, self.format_switch):
            widget.blockSignals(True)
        try:
            self.trigger_edit.setText(snippet.trigger)
            self.text_edit.setPlainText(snippet.text)
            self.format_switch.setChecked(snippet.formatted)
        finally:
            for widget in (self.trigger_edit, self.text_edit, self.format_switch):
                widget.blockSignals(False)
        self._snippet_id = snippet.id
        # Compared through the editor, so text it trims never reads as an edit.
        self._saved = self._draft() if saved is None else saved
        exists = any(s.id == snippet.id for s in self._snippets)
        self.delete_button.setEnabled(exists)
        self.duplicate_button.setEnabled(exists)
        self._set_message("")
        self._update_hints()

    def _update_hints(self, *_args) -> None:
        draft = self._draft()
        note = trigger_note(draft.trigger, self._snippets, exclude_id=draft.id)
        duplicate = find_duplicate(draft.trigger, self._snippets, exclude_id=draft.id) is not None
        self.trigger_note.setText(note)
        self.trigger_note.setVisible(bool(note))
        set_style_property(self.trigger_note, "tone", "error" if duplicate else "hint")
        if not draft.trigger:
            preview = "Say the trigger on its own or inside a sentence."
        elif not draft.text.strip():
            preview = f"Say “{draft.trigger}” → add the text it inserts."
        else:
            count = len(plain_text(draft))
            noun = "character" if count == 1 else "characters"
            styled = " with formatting" if draft.formatted else ""
            preview = f"Say “{draft.trigger}” → inserts {count:,} {noun}{styled}"
        self.preview.setText(preview)

    def _set_message(self, text: str, *, error: bool = False) -> None:
        self.message.setText(text)
        self.message.setVisible(bool(text))
        set_style_property(self.message, "tone", "error" if error else "ok")

    def _selection_changed(self, current, previous) -> None:
        if self._loading or current is None:
            return
        # Saving first rebuilds the list, which deletes ``current``.
        snippet_id = current.data(_ID_ROLE)
        if not self._save_before_switch():
            with QSignalBlocker(self.snippet_list):
                self.snippet_list.setCurrentItem(previous)
            return
        self.refresh(snippet_id)

    def new_snippet(self, trigger: str = "") -> bool:
        """Start a blank draft, or one with ``trigger`` filled in."""
        if not self._save_before_switch():
            return False
        with QSignalBlocker(self.snippet_list):
            self.snippet_list.setCurrentRow(-1)
        blank = Snippet(new_snippet_id(), "", "")
        self._show(Snippet(blank.id, trigger, ""), saved=blank)
        (self.text_edit if trigger else self.trigger_edit).setFocus()
        return True

    def duplicate_snippet(self) -> None:
        source = self._draft()
        if not self._save_before_switch():
            return
        self._snippets = load_snippets(self._settings())
        base = source.trigger[:MAX_TRIGGER_CHARS - 3].rstrip()
        number = 2
        while find_duplicate(f"{base} {number}", self._snippets) is not None:
            number += 1
        with QSignalBlocker(self.snippet_list):
            self.snippet_list.setCurrentRow(-1)
        blank = Snippet(new_snippet_id(), "", "")
        self._show(
            Snippet(blank.id, f"{base} {number}", source.text, source.formatted), saved=blank,
        )
        self.trigger_edit.setFocus()
        self.trigger_edit.selectAll()

    def save_snippet(self) -> bool:
        draft = self._draft()
        try:
            self.manager.mutate_settings(lambda settings: save_snippet(settings, draft))
        except ValueError as exc:
            self._set_message(str(exc), error=True)
            return False
        except Exception as exc:
            self._set_message(f"Couldn't save the snippet: {exc}", error=True)
            return False
        self._saved = draft
        self.refresh(draft.id)
        self._set_message(f"Saved. Say “{draft.trigger}” to insert it.")
        self.snippets_changed.emit()
        return True

    def delete_snippet(self) -> None:
        snippet = next((s for s in self._snippets if s.id == self._snippet_id), None)
        if snippet is None:
            return
        answer = QMessageBox.question(
            self,
            "Delete snippet?",
            f"Delete “{snippet.trigger}”? Saying it will no longer insert its text.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.manager.mutate_settings(lambda settings: delete_snippet(settings, snippet.id))
        except Exception as exc:
            self._set_message(f"Couldn't delete the snippet: {exc}", error=True)
            return
        self.refresh("")
        self.snippets_changed.emit()

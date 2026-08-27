"""
Editable combo box whose own dropdown filters as the user types.

A plain editable QComboBox cannot filter its dropdown: keys typed while
the dropdown is open are forwarded to the line edit (appending to the
current text), and a QCompleter popup cannot appear while the combo's
dropdown holds the input grab. SearchableComboBox instead hides
non-matching rows in the combo's own view, so typing narrows the visible
dropdown directly whether it was opened by clicking or by typing.
"""
from PyQt6.QtCore import QCoreApplication, QEvent, Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import (
    QComboBox,
    QLabel,
    QStyle,
    QStyleOptionComboBox,
)

from ui_qt.utils.fuzzy_match import fuzzy_score
from ui_qt.widgets.no_wheel import NoWheelComboBox


class SearchableComboBox(NoWheelComboBox):
    """Editable combo box with case-insensitive type-to-filter dropdown.

    Behaves like a plain editable QComboBox for the usual
    ``clear()``/``addItems()``/``setCurrentText()``/``currentText()``
    usage. Custom values not present in the item list remain allowed
    (InsertPolicy.NoInsert).

    Can also carry a badge — a short marker drawn inside the closed combo,
    left of the arrow — for state that belongs to the shown value itself.
    """

    #: Height of the badge, and the gap it keeps from the editor text.
    BADGE_HEIGHT = 18
    BADGE_GAP = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        line_edit = self.lineEdit()
        line_edit.textEdited.connect(self._on_text_edited)
        # Qt normally keeps keyboard focus on the combo frame, even for an
        # editable combo. Route those keys to the line edit ourselves so the
        # combo's built-in type-selection never commits a match mid-search.
        line_edit.installEventFilter(self)

        # True while the popup is being shown in response to typing, so
        # showPopup() keeps the active filter instead of resetting it.
        self._filtering = False
        # True once the user has started typing a search, so only the
        # first keystroke replaces the current text.
        self._search_started = False
        # Popup container and list get event filters the first time they appear.
        self._popup_filter_installed = False

        # Created after the line edit so it stacks above the editor. The
        # application stylesheet never reaches a QComboBox's own children, so
        # the badge carries its appearance itself.
        self._badge = QLabel("", self)
        self._badge.setObjectName("searchableComboBadge")
        self._badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._badge.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        self._badge.setStyleSheet("""
            QLabel#searchableComboBadge {
                color: #42df91;
                background-color: #0f2a21;
                border: 1px solid #217353;
                border-radius: 8px;
                padding: 0px 7px;
                font-size: 10px;
                font-weight: 700;
            }
        """)
        self._badge.hide()

    def set_badge(self, text: str) -> None:
        """Show a short marker inside the closed combo, or hide it.

        Args:
            text: Badge text. An empty string hides the badge.
        """
        text = text.strip()
        if text == self.badge_text():
            return
        self._badge.setText(text)
        self._badge.setVisible(bool(text))
        self._layout_badge()

    def badge_text(self) -> str:
        """Return the badge text, empty when no badge is shown."""
        return self._badge.text()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_badge()

    def _layout_badge(self):
        """Park the badge at the right edge of the editor area."""
        line_edit = self.lineEdit()
        if not self._badge.text():
            if line_edit is not None:
                line_edit.setTextMargins(0, 0, 0, 0)
            return
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        field = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox,
            option,
            QStyle.SubControl.SC_ComboBoxEditField,
            self,
        )
        width = self._badge.sizeHint().width()
        height = min(self.BADGE_HEIGHT, field.height())
        self._badge.setGeometry(
            field.right() - width,
            field.center().y() - height // 2 + 1,
            width,
            height,
        )
        if line_edit is not None:
            line_edit.setTextMargins(0, 0, width + self.BADGE_GAP, 0)

    def showPopup(self):
        """Show the dropdown, clearing hidden rows on fresh opens.

        A fresh open (mouse/keyboard, as opposed to a re-show triggered
        by typing) restores the full unfiltered list and arms the next
        keystroke to replace the current text with a new search.
        """
        if not self._filtering:
            self._apply_filter("")
            self._search_started = False
        super().showPopup()
        self._install_popup_key_filters()

    def eventFilter(self, obj, event):
        """Keep search keys in the editor instead of Qt's type selector."""
        if event.type() != QEvent.Type.KeyPress:
            return super().eventFilter(obj, event)

        popup_visible = self.view().window().isVisible()
        if obj is self.lineEdit():
            if popup_visible and self._handle_popup_navigation(event):
                return True
            if not self._search_started and self._is_printable_key(event):
                self.lineEdit().selectAll()
                self._search_started = True
        elif self._is_popup_widget(obj):
            if self._handle_popup_navigation(event):
                return True
            if self._is_text_editing_key(event):
                self._forward_to_line_edit(event)
                return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event: QKeyEvent):
        """Route typing on the combo frame to its line edit."""
        if self.view().window().isVisible():
            if self._handle_popup_navigation(event):
                return
        if self._is_text_editing_key(event):
            self._forward_to_line_edit(event)
            event.accept()
            return
        super().keyPressEvent(event)

    def _handle_popup_navigation(self, event: QKeyEvent) -> bool:
        """Handle Up/Down/Enter while the filtered dropdown is open.

        Returns:
            True if the event was fully handled (caller should return).
        """
        key = event.key()
        view = self.view()
        model = self.model()
        if key == Qt.Key.Key_Escape:
            self.hidePopup()
            event.accept()
            return True
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            current = view.currentIndex().row()
            step = 1 if key == Qt.Key.Key_Down else -1
            row = current if current >= 0 else -1
            for _ in range(model.rowCount()):
                row = (row + step) % model.rowCount()
                if not view.isRowHidden(row):
                    self._set_highlighted_row(row)
                    break
            event.accept()
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            index = view.currentIndex()
            if index.isValid() and not view.isRowHidden(index.row()):
                text = model.data(index) or ""
                self.setCurrentText(text)
                self.hidePopup()
                # Qt emits these itself for its own list activation; this
                # branch replaces that path, so listeners still see a pick.
                self.activated.emit(index.row())
                self.textActivated.emit(text)
                event.accept()
                return True
        return False

    def _on_text_edited(self, text: str):
        """Narrow the dropdown to rows containing the typed text."""
        self._search_started = True
        self._apply_filter(text)
        if not text or self.view().window().isVisible():
            return
        self._filtering = True
        try:
            self.showPopup()
        finally:
            self._filtering = False

    def _apply_filter(self, text: str):
        """Hide dropdown rows that do not match ``text``.

        The highlight moves to the best-matching visible row so Enter
        activates the most relevant candidate. The item model itself
        is left untouched.
        """
        view = self.view()
        model = self.model()
        needle = text.strip()
        best_row = -1
        best_score = -1
        for row in range(model.rowCount()):
            item_text = model.data(model.index(row, 0)) or ""
            if not needle:
                view.setRowHidden(row, False)
                if best_row < 0:
                    best_row = row
                continue
            score = fuzzy_score(needle, item_text)
            hidden = score is None
            view.setRowHidden(row, hidden)
            if not hidden and (score is not None and score > best_score):
                best_score = score
                best_row = row
        if best_row >= 0:
            self._set_highlighted_row(best_row)

    def _set_highlighted_row(self, row: int):
        """Highlight a row without changing the combo's chosen value."""
        view = self.view()
        selection_model = view.selectionModel()
        view_was_blocked = view.blockSignals(True)
        selection_was_blocked = selection_model.blockSignals(True)
        try:
            view.setCurrentIndex(self.model().index(row, 0))
        finally:
            selection_model.blockSignals(selection_was_blocked)
            view.blockSignals(view_was_blocked)

    def _install_popup_key_filters(self):
        """Capture keys that Qt sends to the popup instead of the combo."""
        view = self.view()
        popup = view.window()
        if popup is not None and popup is not self:
            if not self._popup_filter_installed:
                popup.installEventFilter(self)
                view.installEventFilter(self)
                self._popup_filter_installed = True

    def _is_popup_widget(self, obj) -> bool:
        """Return True if ``obj`` is the dropdown list or its container."""
        view = self.view()
        popup = view.window()
        return obj is view or obj is popup

    def _forward_to_line_edit(self, event: QKeyEvent):
        """Deliver a combo or popup key event to the text editor."""
        line_edit = self.lineEdit()
        if line_edit is None:
            return
        forwarded = QKeyEvent(
            event.type(),
            event.key(),
            event.modifiers(),
            event.text(),
            event.isAutoRepeat(),
            event.count(),
        )
        QCoreApplication.sendEvent(line_edit, forwarded)

    @classmethod
    def _is_text_editing_key(cls, event: QKeyEvent) -> bool:
        """Return True for text input, editing, and standard edit shortcuts."""
        if cls._is_printable_key(event):
            return True
        if event.key() in (
            Qt.Key.Key_Backspace,
            Qt.Key.Key_Delete,
            Qt.Key.Key_Left,
            Qt.Key.Key_Right,
            Qt.Key.Key_Home,
            Qt.Key.Key_End,
        ):
            return True
        return bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier) and (
            event.key()
            in (
                Qt.Key.Key_A,
                Qt.Key.Key_C,
                Qt.Key.Key_V,
                Qt.Key.Key_X,
                Qt.Key.Key_Y,
                Qt.Key.Key_Z,
            )
        )

    @staticmethod
    def _is_printable_key(event: QKeyEvent) -> bool:
        """Return True for plain text-producing keys (no Ctrl/Alt)."""
        if event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
        ):
            return False
        text = event.text()
        return bool(text) and text.isprintable()

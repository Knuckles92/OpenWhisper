"""The saved dictionary words as rows: star, badges, edit, and remove or undo."""
from typing import Optional, Sequence

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from services.dictionary import DictionaryTerm
from ui_qt.utils.font_scale import current_ui_font_scale
from ui_qt.utils.restyle import set_style_property
from ui_qt.widgets.buttons import Button, fit_compact_button, neutral_button
from ui_qt.widgets.eliding_label import ElidingLabel
from ui_qt.widgets.wrapped_label import WrappedLabel


class Reflow(QWidget):
    """A lead widget with actions beside it, or below it once the row is narrow.

    The width comes from the column the row sits in (its own minimum is
    ignored), so a narrow tiled window or a large font moves the actions to
    their own line instead of pushing them past the edge.

    Args:
        lead: Takes the spare width.
        actions: Kept at their natural width.
        lead_width: The narrowest the lead may get beside the actions, in
            pixels at 100% font scale.
    """

    def __init__(self, lead: QWidget, actions: QWidget, lead_width: int, parent=None):
        super().__init__(parent)
        self.setObjectName("dictionaryReflow")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._lead = lead
        self._actions = actions
        self._lead_width = lead_width
        self._stacked: Optional[bool] = None
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(8)
        self._grid.setVerticalSpacing(6)
        self._grid.addWidget(lead, 0, 0)
        self._grid.setColumnStretch(0, 1)
        self._place(False)

    @property
    def stacked(self) -> bool:
        return bool(self._stacked)

    def _place(self, stacked: bool) -> None:
        if stacked == self._stacked:
            return
        self._stacked = stacked
        self._grid.removeWidget(self._actions)
        if stacked:
            self._grid.addWidget(self._actions, 1, 0, alignment=Qt.AlignmentFlag.AlignRight)
        else:
            self._grid.addWidget(self._actions, 0, 1, alignment=Qt.AlignmentFlag.AlignVCenter)

    def _reflow(self) -> None:
        needed = (
            round(self._lead_width * current_ui_font_scale())
            + self._grid.horizontalSpacing()
            + self._actions.sizeHint().width()
        )
        self._place(self.width() < needed)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow()


def action_group(*buttons) -> QWidget:
    """Buttons side by side in a transparent holder, for ``Reflow``."""
    holder = QWidget()
    holder.setObjectName("dictionaryActions")
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(8)
    for button in buttons:
        row.addWidget(button)
    return holder


class DictionaryRow(QFrame):
    """One word. Learned words that are still new offer Undo instead of Remove."""

    star_toggled = pyqtSignal(str, bool)
    edit_requested = pyqtSignal(str)
    remove_requested = pyqtSignal(str)

    def __init__(self, term: DictionaryTerm, parent=None):
        super().__init__(parent)
        self.setObjectName("dictionaryRow")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.term = term
        frame = QVBoxLayout(self)
        frame.setContentsMargins(8, 7, 8, 7)
        frame.setSpacing(0)
        lead = QWidget()
        lead.setObjectName("dictionaryRowText")
        row = QHBoxLayout(lead)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self.star = QPushButton()
        self.star.setObjectName("dictionaryStar")
        self.star.setCheckable(True)
        self.star.setFlat(True)
        self.star.setCursor(Qt.CursorShape.PointingHandCursor)
        self.star.setIconSize(QSize(18, 18))
        self.star.setFixedSize(30, 30)
        self.star.clicked.connect(lambda checked: self.star_toggled.emit(self.term.id, checked))
        row.addWidget(self.star, alignment=Qt.AlignmentFlag.AlignTop)

        text = QWidget()
        text.setObjectName("dictionaryRowText")
        column = QVBoxLayout(text)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(3)
        self.name = ElidingLabel()
        self.name.setObjectName("dictionaryTerm")
        column.addWidget(self.name)
        # The word gets a line to itself; its badges sit with the detail.
        self.second_line = QHBoxLayout()
        self.second_line.setContentsMargins(0, 0, 0, 0)
        self.second_line.setSpacing(6)
        self.learned_badge = self._badge("Learned", "learned")
        self.new_badge = self._badge("New", "new")
        self.kept_badge = self._badge("Kept as said", "kept")
        self.kept_badge.setToolTip(
            "You put this word back after AI cleanup changed it, so cleanup leaves it alone"
        )
        self.second_line.addWidget(self.learned_badge)
        self.second_line.addWidget(self.new_badge)
        self.second_line.addWidget(self.kept_badge)
        self.detail = ElidingLabel()
        self.detail.setObjectName("dictionaryDetail")
        self.second_line.addWidget(self.detail, stretch=1)
        self.second_line.addStretch()
        column.addLayout(self.second_line)
        row.addWidget(text, stretch=1, alignment=Qt.AlignmentFlag.AlignVCenter)

        self.edit = neutral_button(Button("Edit"))
        self.edit.setObjectName("dictionaryEditButton")
        fit_compact_button(self.edit)
        self.edit.clicked.connect(lambda: self.edit_requested.emit(self.term.id))
        self.remove = neutral_button(Button("Remove"))
        self.remove.setObjectName("dictionaryRemoveButton")
        fit_compact_button(self.remove)
        self.remove.clicked.connect(lambda: self.remove_requested.emit(self.term.id))
        self.layout_row = Reflow(lead, action_group(self.edit, self.remove), lead_width=170)
        frame.addWidget(self.layout_row)
        self.set_term(term)

    @staticmethod
    def _badge(text: str, tone: str) -> QLabel:
        badge = QLabel(text)
        badge.setObjectName("dictionaryBadge")
        badge.setProperty("tone", tone)
        badge.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        return badge

    def set_term(self, term: DictionaryTerm) -> None:
        self.term = term
        self.name.setText(term.term)
        self.star.setChecked(term.starred)
        set_style_property(self.star, "starred", term.starred)
        self.star.setToolTip("Unstar" if term.starred else "Star: listened for first")
        self.star.setAccessibleName(f"{'Unstar' if term.starred else 'Star'} {term.term}")
        self.learned_badge.setVisible(term.learned)
        self.new_badge.setVisible(term.new)
        self.kept_badge.setVisible(term.protected)
        detail = "Sounds like " + ", ".join(term.heard) if term.heard else ""
        self.learned_badge.setToolTip("Learned from your corrections")
        self.detail.setText(detail)
        self.detail.setVisible(bool(detail))
        undo = term.learned and term.new
        self.remove.setText("Undo" if undo else "Remove")
        # As wide as "Remove" while it reads "Undo", so every row's buttons line up.
        self.remove.ensurePolished()
        self.remove.set_base_minimum_size(
            self.remove.fontMetrics().horizontalAdvance("Remove") + 40, 34
        )
        self.remove.setToolTip(
            "Forget this learned word" if undo else "Remove this word from your dictionary"
        )
        self.remove.setAccessibleName(f"{self.remove.text()} {term.term}")
        self.edit.setAccessibleName(f"Edit {term.term}")


class DictionaryLibrary(QWidget):
    """Every saved word, newest first, with a search box once the list grows.

    Builds at most ``MAX_ROWS`` rows, reusing them between updates, so a full
    dictionary stays quick to show; the search reaches the rest. Removing a
    word by keyboard leaves focus on the word that takes its place, or on
    ``focus_when_empty`` once no word is left to show.
    """

    MAX_ROWS = 100
    #: Words before the search box appears.
    SEARCH_FROM = 9

    star_toggled = pyqtSignal(str, bool)
    edit_requested = pyqtSignal(str)
    remove_requested = pyqtSignal(str)

    def __init__(self, parent=None, *, focus_when_empty: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("dictionaryLibrary")
        self._focus_when_empty = focus_when_empty
        self._terms: list[DictionaryTerm] = []
        self._rows: dict[str, DictionaryRow] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.search = QLineEdit()
        self.search.setObjectName("dictionarySearchInput")
        self.search.setPlaceholderText("Find a word")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Find a word")
        self.search.textChanged.connect(lambda _text: self._render())
        layout.addWidget(self.search)

        self._list = QWidget()
        self._list.setObjectName("dictionaryRows")
        self._list_layout = QVBoxLayout(self._list)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(6)
        layout.addWidget(self._list)

        self.empty = WrappedLabel("")
        self.empty.setObjectName("dictionaryEmpty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty)

        self.more = WrappedLabel("")
        self.more.setObjectName("infoLabel")
        layout.addWidget(self.more)
        self._render()

    @property
    def visible_ids(self) -> list[str]:
        """The rows on show, top to bottom."""
        return [
            self._list_layout.itemAt(index).widget().term.id
            for index in range(self._list_layout.count())
        ]

    def row(self, term_id: str) -> Optional[DictionaryRow]:
        return self._rows.get(term_id)

    def set_terms(self, terms: Sequence[DictionaryTerm]) -> None:
        self._terms = list(terms)
        self._render()

    def _matching(self) -> list[DictionaryTerm]:
        needle = self.search.text().strip().casefold()
        if not needle:
            return self._terms
        return [
            term for term in self._terms
            if needle in term.term.casefold()
            or any(needle in variant.casefold() for variant in term.heard)
        ]

    def _render(self) -> None:
        self.search.setVisible(len(self._terms) >= self.SEARCH_FROM or bool(self.search.text()))
        matching = self._matching()
        shown = matching[: self.MAX_ROWS]
        keep = {term.id for term in shown}
        focused = QApplication.focusWidget()
        held = next((
            index for index, term_id in enumerate(self.visible_ids)
            if term_id not in keep and focused is not None and self._rows[term_id].isAncestorOf(focused)
        ), None)
        stale = [self._rows.pop(term_id) for term_id in list(self._rows) if term_id not in keep]
        for row in stale:
            self._list_layout.removeWidget(row)
        for index, term in enumerate(shown):
            row = self._rows.get(term.id)
            if row is None:
                row = DictionaryRow(term)
                row.star_toggled.connect(self.star_toggled)
                row.edit_requested.connect(self.edit_requested)
                row.remove_requested.connect(self.remove_requested)
                self._rows[term.id] = row
            elif row.term != term:
                row.set_term(term)
            if self._list_layout.indexOf(row) != index:
                self._list_layout.removeWidget(row)
                self._list_layout.insertWidget(index, row)
            row.show()
        # New rows join the end of the window's Tab order, after the tiles
        # below the list; the search box sits where the list starts.
        previous = self.search
        for term in shown:
            row = self._rows[term.id]
            for widget in (row.star, row.edit, row.remove):
                QWidget.setTabOrder(previous, widget)
                previous = widget
        if held is not None:
            # Before the removed row hides, which would pass its focus to
            # whatever follows the list.
            if shown:
                target = self._rows[shown[min(held, len(shown) - 1)].id].remove
            else:
                target = self.search if self.search.isVisible() else self._focus_when_empty
            if target is not None:
                target.setFocus(Qt.FocusReason.OtherFocusReason)
        for row in stale:
            row.hide()
            row.deleteLater()
        self._list.setVisible(bool(shown))
        if not self._terms:
            self.empty.setText(
                "No words yet. Add names, product terms, or anything dictation tends to get wrong."
            )
        elif not matching:
            self.empty.setText(f"No words match “{self.search.text().strip()}”.")
        self.empty.setVisible(not shown)
        hidden = len(matching) - len(shown)
        self.more.setText(
            f"Showing {len(shown)} of {len(matching)}. Search to find the rest." if hidden else ""
        )
        self.more.setVisible(bool(hidden))

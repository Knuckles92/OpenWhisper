"""Qt tests for the SearchableComboBox type-to-filter dropdown."""
import pytest
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from ui_qt.widgets.searchable_combo import SearchableComboBox

MODELS = [
    "openai/gpt-4o",
    "openai/gpt-4o-mini",
    "openai/o4-mini",
    "anthropic/claude-3.5-sonnet",
    "anthropic/claude-3-haiku",
    "google/gemini-pro-1.5",
    "meta-llama/llama-3.1-70b",
]
CLAUDE_MODELS = [
    "anthropic/claude-3.5-sonnet",
    "anthropic/claude-3-haiku",
]


def _visible_rows(combo):
    """Item texts of the rows not hidden in the combo's dropdown view."""
    model = combo.model()
    return [
        model.data(model.index(row, 0))
        for row in range(model.rowCount())
        if not combo.view().isRowHidden(row)
    ]


class TestSearchableComboBox:
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.combo = SearchableComboBox()
        self.combo.addItems(MODELS)
        self.combo.setCurrentText("openai/gpt-4o")
        self.combo.show()
        self.app.processEvents()

    @pytest.fixture(autouse=True)
    def _teardown(self):
        yield
        self.combo.close()
        self.combo.deleteLater()
        self.app.processEvents()

    def _type_search(self, text: str):
        """Type into the line edit (real search path keeps focus there)."""
        line_edit = self.combo.lineEdit()
        line_edit.setFocus()
        self.app.processEvents()
        QTest.keyClicks(line_edit, text)
        QTest.qWait(50)
        self.app.processEvents()

    def test_typing_with_dropdown_open_filters_items(self):
        """Typing while the dropdown is open narrows it live."""
        self.combo.showPopup()
        self._type_search("claude")
        assert self.combo.currentText() == "claude"
        assert _visible_rows(self.combo) == CLAUDE_MODELS
        assert self.combo.view().window().isVisible()
        assert self.combo.lineEdit().hasFocus()
        self.combo.hidePopup()

    def test_typing_replaces_instead_of_appending(self):
        """The first keystroke replaces the current item text."""
        self.combo.showPopup()
        self._type_search("llama")
        assert self.combo.currentText() == "llama"
        assert _visible_rows(self.combo) == ["meta-llama/llama-3.1-70b"]
        assert self.combo.lineEdit().hasFocus()
        self.combo.hidePopup()

    def test_focus_stays_on_line_edit_while_typing(self):
        """Each keystroke keeps the caret in the line edit, not the list."""
        self.combo.showPopup()
        line_edit = self.combo.lineEdit()
        line_edit.setFocus()
        for ch in "claude":
            QTest.keyClick(line_edit, ch)
            self.app.processEvents()
            assert line_edit.hasFocus(), f"editor lost focus after typing {ch!r}"
        assert self.combo.currentText() == "claude"
        assert _visible_rows(self.combo) == CLAUDE_MODELS
        self.combo.hidePopup()

    def test_backspace_widens_filter(self):
        """Deleting characters broadens the visible rows again."""
        self.combo.showPopup()
        self._type_search("claude-3-h")
        assert _visible_rows(self.combo) == ["anthropic/claude-3-haiku"]
        line_edit = self.combo.lineEdit()
        QTest.keyClick(line_edit, Qt.Key.Key_Backspace)
        QTest.keyClick(line_edit, Qt.Key.Key_Backspace)
        QTest.qWait(50)
        assert _visible_rows(self.combo) == CLAUDE_MODELS
        assert line_edit.hasFocus()
        self.combo.hidePopup()

    def test_fresh_open_resets_filter(self):
        """Reopening the dropdown restores the full unfiltered list."""
        self.combo.showPopup()
        self._type_search("claude")
        self.combo.hidePopup()
        self.combo.showPopup()
        assert len(_visible_rows(self.combo)) == len(MODELS)
        self.combo.hidePopup()

    def test_typing_with_dropdown_closed_opens_filtered(self):
        """Typing follows Qt focus changes without interrupting the search."""
        self.combo.lineEdit().setFocus()
        self.combo.lineEdit().selectAll()

        prefix = ""
        for char in "gpt":
            prefix += char
            target = self.app.focusWidget() or self.combo
            QTest.keyClick(target, char)
            self.app.processEvents()
            assert self.combo.currentText() == prefix

        assert self.combo.view().window().isVisible()
        assert _visible_rows(self.combo) == ["openai/gpt-4o", "openai/gpt-4o-mini"]
        self.combo.hidePopup()

    def test_combo_frame_typing_never_activates_an_intermediate_match(self):
        """Real combo key events remain text edits until Enter is pressed."""
        activated = []
        self.combo.activated.connect(activated.append)
        self.combo.showPopup()
        self.combo.setFocus()

        prefix = ""
        for char in "claude":
            prefix += char
            QTest.keyClick(self.combo, char)
            self.app.processEvents()
            assert self.combo.currentText() == prefix
            assert activated == []
            assert self.combo.view().window().isVisible()

        assert _visible_rows(self.combo) == CLAUDE_MODELS
        self.combo.hidePopup()

    def test_typing_from_closed_combo_replaces_current_selection(self):
        """Starting a search does not append to the saved model id."""
        self.combo.setFocus()
        QTest.keyClicks(self.combo, "gpt")
        self.app.processEvents()

        assert self.combo.currentText() == "gpt"
        assert _visible_rows(self.combo) == ["openai/gpt-4o", "openai/gpt-4o-mini"]
        assert self.combo.view().window().isVisible()
        self.combo.hidePopup()

    def test_filter_is_case_insensitive(self):
        """Matching ignores letter case."""
        self.combo.showPopup()
        self._type_search("CLAUDE")
        assert _visible_rows(self.combo) == CLAUDE_MODELS
        self.combo.hidePopup()

    def test_enter_activates_first_visible_match(self):
        """Enter picks the highlighted (first matching) row."""
        self.combo.showPopup()
        self._type_search("claude")
        QTest.keyClick(self.combo.lineEdit(), Qt.Key.Key_Return)
        QTest.qWait(50)
        assert self.combo.currentText() == "anthropic/claude-3.5-sonnet"
        assert not self.combo.view().window().isVisible()

    def test_repopulate_keeps_plain_combo_semantics(self):
        """clear/addItems/setCurrentText behave like a plain QComboBox."""
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItems(MODELS)
        self.combo.setCurrentText("openai/o4-mini")
        self.combo.blockSignals(False)
        self.app.processEvents()
        assert self.combo.currentText() == "openai/o4-mini"
        assert len(_visible_rows(self.combo)) == len(MODELS)
        assert not self.combo.view().window().isVisible()



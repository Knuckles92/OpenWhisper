"""Dictionary words: Tab follows the list as drawn, and focus survives Remove.

Rows are built after the tiles below them, so they used to come last in Tab
order, newest word last of all, and removing the bottom word by keyboard sent
focus to the window header, where the next Space switched Settings to Basic.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication, QEvent, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from services import dictionary
from services.settings import SettingsKey, SettingsView
from tests import test_personalize_fix2_g2_enter_key as shared
from ui_qt.dialogs.settings_destinations import DICTIONARY

_qapp = shared._qapp
_restore_metadata = shared._restore_metadata
_windows_text = shared._windows_text
make_dialog = shared.make_dialog

KEY = SettingsKey.DICTATION_DICTIONARY


def _word(term_id, term, **flags):
    return {"id": term_id, "term": term, "starred": False, "heard": [], "learned": False,
            "new": False, **flags}


WORDS = [_word("a", "Ksenia", starred=True), _word("b", "Oluwaseun")]


def _flush():
    # Deferred deletes are what clear a destroyed button's focus; plain
    # processEvents() never runs them outside an event loop.
    for _ in range(3):
        QCoreApplication.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)


def _page(make_dialog, words):
    dialog, store = make_dialog({KEY: words})
    dialog.select_destination(DICTIONARY)
    _flush()
    return dialog, store, dialog._dictionary_page


def _focus(widget):
    widget.setFocus(Qt.FocusReason.TabFocusReason)
    _flush()


def _key(key, modifier=Qt.KeyboardModifier.NoModifier):
    QTest.keyClick(QApplication.focusWidget(), key, modifier)
    _flush()


def _focused_name():
    widget = QApplication.focusWidget()
    return widget.accessibleName() if widget is not None else None


def _tab_walk(count):
    names = []
    for _ in range(count):
        _key(Qt.Key.Key_Tab)
        names.append(_focused_name())
    return names


def _saved(store):
    return [term.term for term in dictionary.load_dictionary(store.load_all_settings())]


def test_tab_reaches_the_words_in_order_before_the_switches_below(make_dialog):
    dialog, _store, page = _page(make_dialog, WORDS)
    _focus(page.add_button)

    assert _tab_walk(7) == [
        "Unstar Ksenia", "Edit Ksenia", "Remove Ksenia",
        "Star Oluwaseun", "Edit Oluwaseun", "Remove Oluwaseun",
        dialog.dictionary_steer_switch.accessibleName(),
    ]

    _focus(page.library.row("a").star)
    _key(Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
    assert QApplication.focusWidget() is page.add_button


def test_a_new_word_is_reached_first(make_dialog):
    _dialog, _store, page = _page(make_dialog, WORDS)
    page.term_edit.setText("Zed")
    page.add_button.click()
    _flush()
    assert page.library.visible_ids[0] != "a"

    _focus(page.add_button)

    assert _tab_walk(4) == ["Star Zed", "Edit Zed", "Remove Zed", "Unstar Ksenia"]


def test_removing_the_bottom_word_focuses_the_one_above_then_the_word_field(make_dialog):
    dialog, store, page = _page(make_dialog, WORDS)
    _focus(page.library.row("b").remove)

    _key(Qt.Key.Key_Space)

    assert _saved(store) == ["Ksenia"]
    assert _focused_name() == "Remove Ksenia"

    _key(Qt.Key.Key_Space)

    assert _saved(store) == []
    assert QApplication.focusWidget() is page.term_edit
    assert dialog._settings_view == SettingsView.ADVANCED
    assert store.get(SettingsKey.SETTINGS_VIEW) == SettingsView.ADVANCED


def test_removing_a_word_focuses_the_one_that_takes_its_place(make_dialog):
    _dialog, store, page = _page(make_dialog, [*WORDS, _word("c", "Kubernetes")])
    _focus(page.library.row("b").remove)

    _key(Qt.Key.Key_Space)

    assert _saved(store) == ["Ksenia", "Kubernetes"]
    assert _focused_name() == "Remove Kubernetes"


def test_undoing_a_learned_word_moves_focus_like_remove(make_dialog):
    _dialog, store, page = _page(make_dialog, [*WORDS, _word("c", "Kubernetes", learned=True, new=True)])
    _focus(page.library.row("c").remove)
    assert _focused_name() == "Undo Kubernetes"

    _key(Qt.Key.Key_Space)

    assert _saved(store) == ["Ksenia", "Oluwaseun"]
    assert _focused_name() == "Remove Oluwaseun"


def test_removing_the_only_search_match_focuses_the_search(make_dialog):
    words = [_word(str(index), f"Word{index}") for index in range(10)] + [_word("k", "Ksenia")]
    _dialog, store, page = _page(make_dialog, words)
    page.library.search.setText("Ksen")
    _flush()
    _focus(page.library.row("k").remove)

    _key(Qt.Key.Key_Space)

    assert "Ksenia" not in _saved(store)
    assert QApplication.focusWidget() is page.library.search


def test_starring_by_keyboard_keeps_focus_on_the_star(make_dialog):
    _dialog, store, page = _page(make_dialog, WORDS)
    _focus(page.library.row("b").star)

    _key(Qt.Key.Key_Space)

    assert [term.starred for term in dictionary.load_dictionary(store.load_all_settings())] == [True, True]
    assert _focused_name() == "Unstar Oluwaseun"


@pytest.mark.parametrize("text", ["", "Kse"])
def test_typing_a_search_leaves_focus_in_the_search(make_dialog, text):
    words = [_word(str(index), f"Word{index}") for index in range(10)] + [_word("k", "Ksenia")]
    _dialog, _store, page = _page(make_dialog, words)
    page.library.search.setText(text)
    _focus(page.library.search)

    QTest.keyClicks(page.library.search, "n")
    _flush()

    assert QApplication.focusWidget() is page.library.search

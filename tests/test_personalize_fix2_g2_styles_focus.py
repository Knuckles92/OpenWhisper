"""Apps & styles app lists: keyboard focus survives Remove, and Tab follows the page.

Each list is rebuilt after a change. Removing an app with the keyboard used to
destroy the focused button and leave nothing focused, so the next Tab started
again at the window header.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication, QEvent, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QPushButton

from services import app_styles
from services.settings import SettingsKey, SettingsView
from tests import test_personalize_fix2_g2_enter_key as shared
from ui_qt.dialogs.settings_destinations import STYLES

_qapp = shared._qapp
_restore_metadata = shared._restore_metadata
_windows_text = shared._windows_text
make_dialog = shared.make_dialog

EXCLUDED = SettingsKey.APP_CONTEXT_EXCLUDED_APPS
OVERRIDES = SettingsKey.APP_STYLE_OVERRIDES
BOTH_LISTS = {
    **shared.CLEANUP_ON,
    EXCLUDED: ["Signal", "1Password"],
    OVERRIDES: [{"match": "Signal", "category": "work"}, {"match": "Foo Tool", "category": "email"}],
}


def _flush():
    # Deferred deletes are what clear a destroyed button's focus; plain
    # processEvents() never runs them outside an event loop.
    for _ in range(3):
        QCoreApplication.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)


def _page(make_dialog, values):
    dialog, store = make_dialog(values)
    dialog.select_destination(STYLES)
    _flush()
    return dialog, store, dialog.apps_styles_page


def _removes(rows):
    return [button for row in rows.rows for button in row.findChildren(QPushButton)]


def _remove_button(rows, name):
    return next(button for button in _removes(rows) if button.accessibleName() == f"Remove {name}")


def _focus(widget):
    widget.setFocus(Qt.FocusReason.TabFocusReason)
    _flush()


def _key(key, modifier=Qt.KeyboardModifier.NoModifier):
    QTest.keyClick(QApplication.focusWidget(), key, modifier)
    _flush()


def _focused_name():
    widget = QApplication.focusWidget()
    return widget.accessibleName() if widget is not None else None


def test_removing_an_excluded_app_focuses_the_next_then_the_picker(make_dialog):
    dialog, store, page = _page(make_dialog, {EXCLUDED: ["Signal", "1Password"]})
    _focus(_remove_button(page.excluded_rows, "Signal"))

    _key(Qt.Key.Key_Space)

    assert store.get(EXCLUDED) == ["1Password"]
    assert _focused_name() == "Remove 1Password"

    _key(Qt.Key.Key_Space)

    assert store.get(EXCLUDED) == []
    assert QApplication.focusWidget() is page.excluded_picker
    assert dialog._settings_view == SettingsView.ADVANCED


def test_removing_the_last_excluded_app_focuses_the_one_above(make_dialog):
    _dialog, store, page = _page(make_dialog, {EXCLUDED: ["Signal", "1Password", "Zoom"]})
    _focus(_remove_button(page.excluded_rows, "Zoom"))

    _key(Qt.Key.Key_Space)

    assert store.get(EXCLUDED) == ["Signal", "1Password"]
    assert _focused_name() == "Remove 1Password"


def test_removing_a_style_override_focuses_the_next_then_the_picker(make_dialog):
    _dialog, store, page = _page(make_dialog, BOTH_LISTS)
    _focus(_remove_button(page.override_rows, "Signal"))

    _key(Qt.Key.Key_Space)

    assert app_styles.resolve_overrides(store.load_all_settings()) == (("Foo Tool", "email"),)
    assert _focused_name() == "Remove Foo Tool"

    _key(Qt.Key.Key_Space)

    assert app_styles.resolve_overrides(store.load_all_settings()) == ()
    assert QApplication.focusWidget() is page.override_picker


def test_a_rebuild_keeps_focus_on_the_same_app(make_dialog):
    _dialog, _store, page = _page(make_dialog, {EXCLUDED: ["Signal", "1Password"]})
    _focus(_remove_button(page.excluded_rows, "1Password"))

    page.excluded_rows.set_rows([("Zoom", "Zoom"), ("Signal", "Signal"), ("1Password", "1Password")])
    _flush()

    assert _focused_name() == "Remove 1Password"


def test_adding_an_app_leaves_focus_on_add(make_dialog):
    _dialog, store, page = _page(make_dialog, {EXCLUDED: ["Signal"]})
    _focus(page.excluded_add)
    page.excluded_picker.setEditText("Zoom")

    _key(Qt.Key.Key_Space)

    assert store.get(EXCLUDED) == ["Signal", "Zoom"]
    assert QApplication.focusWidget() is page.excluded_add


@pytest.mark.parametrize("rows,picker,beside", [
    ("excluded_rows", "excluded_picker", "excluded_add"),
    ("override_rows", "override_picker", "override_category"),
])
def test_tab_walks_each_list_in_order_before_its_picker(make_dialog, rows, picker, beside):
    dialog, _store, page = _page(make_dialog, BOTH_LISTS)
    rows, picker, beside = getattr(page, rows), getattr(page, picker), getattr(page, beside)
    first, second = _removes(rows)
    _focus(first)

    _key(Qt.Key.Key_Tab)
    assert QApplication.focusWidget() is second
    _key(Qt.Key.Key_Tab)
    assert QApplication.focusWidget() is picker
    _key(Qt.Key.Key_Tab)
    assert QApplication.focusWidget() is beside

    _focus(first)
    _key(Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
    before = QApplication.focusWidget()
    assert before not in dialog.view_buttons.values()
    assert not rows.isAncestorOf(before)
    _key(Qt.Key.Key_Tab)
    assert QApplication.focusWidget() is first


def test_tab_order_holds_after_adding_an_app(make_dialog):
    _dialog, _store, page = _page(make_dialog, {EXCLUDED: ["Signal"]})
    page.excluded_picker.setEditText("Zoom")
    page.excluded_add.click()
    _flush()

    _focus(_remove_button(page.excluded_rows, "Signal"))
    _key(Qt.Key.Key_Tab)
    assert _focused_name() == "Remove Zoom"
    _key(Qt.Key.Key_Tab)
    assert QApplication.focusWidget() is page.excluded_picker

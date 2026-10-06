"""Settings → Recording backups: keyboard focus survives Move, Remove and rebuilds.

The list is rebuilt after every change. Focus has to follow the entry rather
than fall to the window header, where the next Space switched Settings to
Basic, and Tab has to reach the rows where they are drawn.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication, QEvent, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from services.audio_devices import InputDevice
from services.settings import SettingsKey, SettingsView
from tests import test_personalize_s7_settings as s7
from tests.test_personalize_s7_settings import DEVICES, LAPEL, MME, SNOWBALL, USB, _button, _rows
from ui_qt.dialogs.settings_microphones import entry_token

WEBCAM = {"name": "Microphone (Webcam)", "hostapi": MME}
_qapp = s7._qapp
make_dialog = s7.make_dialog


@pytest.fixture(params=["classic", "omarchy"])
def shown(request, monkeypatch, make_dialog):
    monkeypatch.setenv("OPENWHISPER_UI", request.param)
    dialogs = []

    def build(priority):
        dialog, store = make_dialog({SettingsKey.AUDIO_INPUT_PRIORITY: priority})
        dialogs.append(dialog)
        dialog.resize(1000, 800)
        dialog.show()
        dialog.activateWindow()
        assert QTest.qWaitForWindowActive(dialog)
        _flush()
        return dialog, store

    yield build
    for dialog in dialogs:
        dialog.hide()


def _flush():
    # Deferred deletes are what clear a destroyed button's focus; plain
    # processEvents() never runs them outside an event loop.
    for _ in range(3):
        QCoreApplication.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)


def _focus(widget):
    widget.setFocus(Qt.FocusReason.TabFocusReason)
    _flush()


def _key(key, modifier=Qt.KeyboardModifier.NoModifier):
    QTest.keyClick(QApplication.focusWidget(), key, modifier)
    _flush()


def _focused_name():
    widget = QApplication.focusWidget()
    return widget.accessibleName() if widget is not None else None


def _priority(store):
    return store.get(SettingsKey.AUDIO_INPUT_PRIORITY)


def test_moved_microphone_keeps_focus_and_a_second_space_moves_it_back(shown):
    dialog, store = shown([USB, SNOWBALL, LAPEL])
    _focus(_button(_rows(dialog)[0], "down"))
    in_list = []

    def record(_old, new):
        in_list.append(new is not None and dialog._microphones.rows.isAncestorOf(new))

    QApplication.instance().focusChanged.connect(record)
    try:
        _key(Qt.Key.Key_Space)
    finally:
        QApplication.instance().focusChanged.disconnect(record)

    assert _priority(store) == [USB, LAPEL, SNOWBALL]
    # Down is gone at the bottom, so the same microphone's Up takes focus.
    assert _focused_name() == "Move up: Microphone (Blue Snowball)"
    assert in_list == [True]

    _key(Qt.Key.Key_Space)

    assert _priority(store) == [USB, SNOWBALL, LAPEL]
    assert _focused_name() == "Move down: Microphone (Blue Snowball)"
    assert dialog._settings_view == SettingsView.ADVANCED
    assert store.get(SettingsKey.SETTINGS_VIEW) == SettingsView.ADVANCED


def test_moved_microphone_keeps_the_same_button_away_from_the_edges(shown):
    dialog, store = shown([USB, SNOWBALL, LAPEL, WEBCAM])
    _focus(_button(_rows(dialog)[0], "down"))

    _key(Qt.Key.Key_Space)

    assert _priority(store) == [USB, LAPEL, SNOWBALL, WEBCAM]
    assert _focused_name() == "Move down: Microphone (Blue Snowball)"


@pytest.mark.parametrize("row,left,focused", [
    (0, [USB, LAPEL, WEBCAM], "Remove: Microphone (Lapel)"),
    (1, [USB, SNOWBALL, WEBCAM], "Remove: Microphone (Webcam)"),
    (2, [USB, SNOWBALL, LAPEL], "Remove: Microphone (Lapel)"),
], ids=["first", "middle", "last"])
def test_removing_a_microphone_focuses_the_row_that_takes_its_place(shown, row, left, focused):
    dialog, store = shown([USB, SNOWBALL, LAPEL, WEBCAM])
    _focus(_button(_rows(dialog)[row], "remove"))

    _key(Qt.Key.Key_Space)

    assert _priority(store) == left
    assert _focused_name() == focused


def test_removing_the_only_backup_focuses_the_add_picker(shown):
    dialog, store = shown([USB, SNOWBALL])
    _focus(_button(_rows(dialog)[0], "remove"))

    _key(Qt.Key.Key_Space)

    assert _priority(store) == [USB]
    assert QApplication.focusWidget() is dialog._microphones.add_combo


def test_adding_the_last_free_microphone_focuses_its_row(shown, make_dialog):
    make_dialog.devices[:] = [device for device in DEVICES if device.index in (1, 2, 3)]
    dialog, store = shown([USB, SNOWBALL])
    add = dialog._microphones.add_combo
    _focus(add)

    add.setCurrentIndex(add.findData(entry_token(WEBCAM)))
    add.activated.emit(add.currentIndex())
    _flush()

    assert _priority(store) == [USB, SNOWBALL, WEBCAM]
    assert not add.isEnabled()
    assert _focused_name() == "Remove: Microphone (Webcam)"


def test_a_device_refresh_keeps_focus_on_the_same_button(shown):
    dialog, _store = shown([USB, SNOWBALL, LAPEL])
    _focus(_button(_rows(dialog)[1], "up"))
    old = QApplication.focusWidget()

    dialog._microphones.apply_devices(list(DEVICES), "")
    _flush()

    assert QApplication.focusWidget() is not old
    assert _focused_name() == "Move up: Microphone (Lapel)"


def test_a_rebuild_leaves_focus_outside_the_list_alone(shown):
    dialog, _store = shown([USB, SNOWBALL, LAPEL])
    _focus(dialog.audio_device_combo)

    dialog._microphones.apply_devices(list(DEVICES) + [InputDevice(9, "Microphone (Dock)", MME, default_api=True)], "")
    _flush()

    assert QApplication.focusWidget() is dialog.audio_device_combo


def test_tab_reaches_the_backups_between_refresh_and_add(shown):
    dialog, _store = shown([USB, SNOWBALL, LAPEL])
    section = dialog._microphones
    _focus(section.refresh_button)

    walk = []
    for _ in range(6):
        _key(Qt.Key.Key_Tab)
        walk.append(QApplication.focusWidget())

    assert [widget.accessibleName() for widget in walk[:4]] == [
        "Move down: Microphone (Blue Snowball)", "Remove: Microphone (Blue Snowball)",
        "Move up: Microphone (Lapel)", "Remove: Microphone (Lapel)",
    ]
    assert walk[4] is section.add_combo
    assert not section.rows.isAncestorOf(walk[5])

    _focus(_button(_rows(dialog)[0], "down"))
    _key(Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
    assert QApplication.focusWidget() is section.refresh_button


def test_tab_order_holds_after_a_move(shown):
    dialog, _store = shown([USB, SNOWBALL, LAPEL])
    section = dialog._microphones
    _focus(_button(_rows(dialog)[0], "down"))
    _key(Qt.Key.Key_Space)

    _key(Qt.Key.Key_Tab)
    assert _focused_name() == "Remove: Microphone (Blue Snowball)"
    _key(Qt.Key.Key_Tab)
    assert QApplication.focusWidget() is section.add_combo

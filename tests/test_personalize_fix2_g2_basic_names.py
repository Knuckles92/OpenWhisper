"""Basic settings: a button's accessible name says what its label says.

Basic rows name their control after the row title, which for a button with
text replaced "Add word" with "Dictionary": a screen reader announced the
wrong action and voice control could not find the button by its label.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QAbstractButton, QApplication, QPushButton

from services.settings import SettingsKey, SettingsView
from tests import test_personalize_fix2_g2_enter_key as shared
from ui_qt.dialogs.settings_destinations import BASIC_APP, BASIC_DICTATION, BASIC_MEETINGS

_qapp = shared._qapp
_restore_metadata = shared._restore_metadata
_windows_text = shared._windows_text
make_dialog = shared.make_dialog


def _label(button) -> str:
    return " ".join(button.text().replace("&&", "\0").replace("&", "").replace("\0", "&").split())


def test_add_word_is_named_after_its_label(make_dialog):
    dialog, _store = make_dialog({SettingsKey.SETTINGS_VIEW: SettingsView.BASIC})
    page = dialog._basic_pages[BASIC_DICTATION]

    button = page.findChild(QPushButton, "basicDictionaryAddButton")

    assert button.text() == "Add word"
    assert button.accessibleName().startswith("Add word")


@pytest.mark.parametrize("key", [BASIC_DICTATION, BASIC_MEETINGS, BASIC_APP])
def test_every_basic_button_with_a_label_keeps_it_in_its_name(make_dialog, key):
    dialog, _store = make_dialog({SettingsKey.SETTINGS_VIEW: SettingsView.BASIC})
    dialog.select_destination(key)
    QApplication.processEvents()
    page = dialog._basic_pages[key]

    labelled = [button for button in page.findChildren(QAbstractButton) if _label(button)]

    assert labelled
    for button in labelled:
        name = button.accessibleName()
        assert not name or _label(button).casefold() in name.casefold(), (_label(button), name)

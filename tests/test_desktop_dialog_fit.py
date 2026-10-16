"""fit_desktop_dialog must not collapse dialogs that already fit the screen.

On Omarchy every QDialog passes through fit_desktop_dialog on Show. Wrapping
a fitting dialog's layout in a scroll area hides the contents' size from the
window, so a QMessageBox such as Help -> About collapsed into a clipped
sliver. Only dialogs larger than the available screen get the treatment.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication, QDialog, QLabel, QMessageBox, QVBoxLayout

from ui_qt.utils.desktop import fit_desktop_dialog


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _room(dialog):
    available = dialog.screen().availableGeometry()
    return available.width() - 64, available.height() - 64


def test_fitting_message_box_keeps_its_layout_and_size():
    box = QMessageBox()
    box.setWindowTitle("About OpenWhisper")
    box.setText(
        "<p><b>OpenWhisper - Speech-to-Text Application</b></p>"
        "<p>Version 0.0.0<br>Install: test</p>"
        "<p>Record audio and turn it into text.</p>"
    )
    hint_before = box.sizeHint()
    room_w, room_h = _room(box)
    assert hint_before.width() <= room_w and hint_before.height() <= room_h, (
        "test precondition: offscreen screen must fit the message box"
    )
    minimum_before = box.minimumSize()

    fit_desktop_dialog(box)

    assert getattr(box, "_desktop_dialog_scroll", None) is None
    assert box.minimumSize() == minimum_before
    assert box.sizeHint() == hint_before


def test_oversized_dialog_gets_scroll_area_and_bounded_minimum():
    dialog = QDialog()
    layout = QVBoxLayout(dialog)
    tall = QLabel("tall form")
    tall.setFixedHeight(4000)
    layout.addWidget(tall)
    room_w, room_h = _room(dialog)
    assert dialog.sizeHint().height() > room_h, (
        "test precondition: form must exceed the offscreen screen"
    )

    fit_desktop_dialog(dialog)

    assert getattr(dialog, "_desktop_dialog_scroll", None) is not None
    assert dialog.minimumSize().width() <= room_w
    assert dialog.minimumSize().height() <= room_h

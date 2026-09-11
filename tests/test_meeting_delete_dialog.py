"""Exercise the rendered deletion options and safe dismissal behavior."""

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QPushButton

from ui_qt.dialogs.meeting_delete_dialog import MeetingDeleteDialog


@pytest.mark.parametrize("has_audio", [False, True])
def test_options_stay_in_layout_after_show_and_resize(has_audio):
    dialog = MeetingDeleteDialog(has_audio=has_audio)
    dialog.show()
    QTest.qWait(20)
    for width in (dialog.width(), dialog.width() + 100):
        dialog.resize(width, dialog.height() + 30)
        QTest.qWait(20)
        controls = dialog.findChildren(QLabel) + [
            dialog.delete_recordings, dialog.dont_ask_again, dialog.buttons
        ]
        visible = [control for control in controls if control.isVisible()]
        assert dialog.delete_recordings.isVisible() == has_audio
        assert not dialog.delete_recordings.isChecked()
        for index, control in enumerate(visible):
            assert dialog.layout().indexOf(control) >= 0
            assert dialog.rect().contains(control.geometry())
            assert control.width() >= control.minimumSizeHint().width()
            for other in visible[index + 1:]:
                assert not control.geometry().intersects(other.geometry())
        assert dialog.buttons.y() > dialog.dont_ask_again.geometry().bottom()
    assert {button.text() for button in dialog.findChildren(QPushButton)} == {
        "Delete", "Cancel"
    }
    dialog.close()


@pytest.mark.parametrize("action", ["delete", "cancel", "enter", "escape", "close"])
def test_confirmation_actions(action):
    dialog = MeetingDeleteDialog(has_audio=True)
    dialog.show()
    QTest.qWait(20)
    if action in ("delete", "cancel"):
        standard = (
            QDialogButtonBox.StandardButton.Ok if action == "delete"
            else QDialogButtonBox.StandardButton.Cancel
        )
        QTest.mouseClick(dialog.buttons.button(standard), Qt.MouseButton.LeftButton)
    elif action in ("enter", "escape"):
        key = Qt.Key.Key_Return if action == "enter" else Qt.Key.Key_Escape
        QTest.keyClick(dialog, key)
    else:
        dialog.close()
    assert not dialog.isVisible()
    expected = (
        QDialog.DialogCode.Accepted if action == "delete"
        else QDialog.DialogCode.Rejected
    )
    assert dialog.result() == expected

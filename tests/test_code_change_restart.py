"""The restart offer after OpenWhisper's code changed under the running app.

Features load on first use, so after a pull or a package upgrade the app runs
two versions of its code at once; a restart is the way back to one.
"""

import os
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox, QWidget

_QAPP = QApplication.instance() or QApplication([])

from services.backup_startup import RESTART_EXIT_CODE
from ui_qt.ui_controller import UIController


class _Window(QWidget):
    def __init__(self):
        super().__init__()
        self._force_quit = False
        self.geometry_saves = 0

    def _save_geometry(self):
        self.geometry_saves += 1


class _Controller:
    """Enough of a UIController for the unbound restart methods to run."""

    def __init__(self):
        self.main_window = _Window()
        self._code_change_timer = QTimer()
        self._code_change_timer.start(60_000)
        self.busy = None
        self.restarts = 0

    def _update_work_is_busy(self):
        return self.busy

    def _confirm_quit_while_busy(self):
        return UIController._confirm_quit_while_busy(self)

    def offer_restart_for_code_change(self, failure=""):
        UIController.offer_restart_for_code_change(self, failure)

    def restart_app(self):
        self.restarts += 1

    def set_meeting_status(self, text):
        self.status = text


@pytest.fixture
def answers():
    """Each offer shown, answered with the next queued button label."""
    shown, queued = [], []

    def exec_(box):
        shown.append(box)
        label = queued.pop(0)
        next(b for b in box.buttons() if b.text() == label).click()
        return 0

    with patch.object(QMessageBox, "exec", exec_):
        yield shown, queued


def test_the_poll_asks_once_the_code_changed_and_nothing_is_running(answers):
    shown, queued = answers
    controller = _Controller()

    with patch("ui_qt.ui_controller.changed_since_launch", return_value=False):
        UIController._on_code_change_poll(controller)
    assert not shown

    controller.busy = "a recording"
    with patch("ui_qt.ui_controller.changed_since_launch", return_value=True):
        UIController._on_code_change_poll(controller)
        assert not shown and controller._code_change_timer.isActive()

        controller.busy = None
        queued.append("Later")
        UIController._on_code_change_poll(controller)

    assert len(shown) == 1
    assert "updated while it was running" in shown[0].text()
    assert controller.restarts == 0
    assert not controller._code_change_timer.isActive()  # Later means not again.


def test_restart_now_confirms_running_work_before_restarting(answers):
    shown, queued = answers
    controller = _Controller()
    controller.busy = "a meeting"

    with patch("ui_qt.ui_controller.QMessageBox.question",
               return_value=QMessageBox.StandardButton.No) as question:
        queued.append("Restart now")
        UIController.offer_restart_for_code_change(controller)
    assert "a meeting" in question.call_args.args[2]
    assert controller.restarts == 0

    controller.busy = None
    queued.append("Restart now")
    UIController.offer_restart_for_code_change(controller)
    assert controller.restarts == 1


def test_a_restart_exits_with_the_code_main_relaunches_on():
    controller = _Controller()

    # Exiting the real application disables later nested Qt event loops.
    with patch("ui_qt.ui_controller.QApplication") as qapp:
        UIController.restart_app(controller)

    qapp.instance.return_value.exit.assert_called_once_with(RESTART_EXIT_CODE)
    assert controller.main_window._force_quit
    assert controller.main_window.geometry_saves == 1


def test_a_meeting_error_after_the_change_offers_a_restart_with_the_error(answers):
    shown, queued = answers
    controller = _Controller()
    error = "Could not start the meeting: cannot import name 'route_is_local'"

    with patch("ui_qt.ui_controller.changed_since_launch", return_value=True):
        queued.append("Restart now")
        UIController.on_meeting_error(controller, error)

    assert shown[0].detailedText() == error
    assert controller.restarts == 1

    with patch("ui_qt.ui_controller.changed_since_launch", return_value=False), \
            patch("ui_qt.ui_controller.QMessageBox.warning") as warning:
        UIController.on_meeting_error(controller, error)
    warning.assert_called_once_with(controller.main_window, "Meeting Error", error)
    assert len(shown) == 1

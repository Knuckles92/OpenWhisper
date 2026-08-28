"""Tests for the moment the app hands an update over to the updater helper.

The helper waits for this process to exit before it touches the install, so a
handoff that leaves the app running is a hang the user cannot escape.
"""
import os
import threading
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

_QAPP = QApplication.instance() or QApplication([])

from ui_qt.ui_controller import UIController, arm_handoff_watchdog


class _FakeWindow:
    def __init__(self):
        self._force_quit = False
        self.quit_calls = 0

    def quit_application(self):
        self.quit_calls += 1


class _FakeController:
    """Enough of a UIController for the unbound handoff methods to run."""

    def __init__(self, dialog=None):
        self.main_window = _FakeWindow()
        self._update_dialog = dialog
        self._update_canceled = False
        self._last_update_result = None
        self.status = None

    def exit_for_update(self):
        UIController.exit_for_update(self)

    def set_status(self, text):
        self.status = text


class TestNativeHandoff(unittest.TestCase):
    def setUp(self):
        # The real watchdog would take the test runner down with it.
        watchdog = patch("ui_qt.ui_controller.arm_handoff_watchdog")
        self.watchdog = watchdog.start()
        self.addCleanup(watchdog.stop)

    def test_handoff_exits_instead_of_asking_to_quit(self):
        dialog = MagicMock()
        controller = _FakeController(dialog)

        with patch("ui_qt.ui_controller.decode_native_result", return_value="a" * 32), \
                patch("ui_qt.ui_controller.load_journal"), \
                patch("ui_qt.ui_controller.helper_exe_for", return_value="helper.exe"), \
                patch("ui_qt.ui_controller.helper_argv", return_value=["--tx"]), \
                patch("ui_qt.ui_controller._start_detached", return_value=True), \
                patch("ui_qt.ui_controller.QApplication") as qapp:
            UIController.on_update_download_finished(
                controller, "native:" + "a" * 32, ""
            )

        app = qapp.instance.return_value
        app.exit.assert_called_once_with(0)
        app.quit.assert_not_called()
        self.assertTrue(controller.main_window._force_quit)
        dialog.mark_handed_off.assert_called_once_with()
        self.watchdog.assert_called_once_with()

    def test_failed_launch_keeps_the_app_running(self):
        dialog = MagicMock()
        controller = _FakeController(dialog)

        with patch("ui_qt.ui_controller.decode_native_result", return_value="a" * 32), \
                patch("ui_qt.ui_controller.load_journal"), \
                patch("ui_qt.ui_controller.helper_exe_for", return_value="helper.exe"), \
                patch("ui_qt.ui_controller.helper_argv", return_value=["--tx"]), \
                patch("ui_qt.ui_controller._start_detached", return_value=False), \
                patch("ui_qt.ui_controller.QApplication") as qapp:
            UIController.on_update_download_finished(
                controller, "native:" + "a" * 32, ""
            )

        qapp.instance.return_value.exit.assert_not_called()
        self.assertFalse(controller.main_window._force_quit)
        dialog.set_error.assert_called_once()

    def test_cancel_after_the_dialog_closed_does_not_pop_a_failure(self):
        controller = _FakeController()
        controller._update_canceled = True

        with patch("ui_qt.ui_controller.QMessageBox") as message_box:
            UIController.on_update_download_finished(
                controller, "", "The update was cancelled."
            )

        message_box.warning.assert_not_called()

    def test_tray_exit_goes_through_the_window(self):
        controller = _FakeController()

        UIController._on_tray_exit(controller)

        self.assertEqual(controller.main_window.quit_calls, 1)


class TestHandoffWatchdog(unittest.TestCase):
    """A stalled shutdown must not cost the user the update."""

    def test_a_stalled_exit_is_forced(self):
        fired = threading.Event()

        with patch("ui_qt.ui_controller._hard_exit", side_effect=fired.set):
            timer = arm_handoff_watchdog(0.05)
            self.assertTrue(fired.wait(5))
        timer.cancel()

    def test_a_normal_exit_leaves_nothing_holding_the_process(self):
        with patch("ui_qt.ui_controller._hard_exit") as hard_exit:
            timer = arm_handoff_watchdog(30.0)
            try:
                self.assertTrue(timer.daemon)
                self.assertTrue(timer.is_alive())
            finally:
                timer.cancel()
                timer.join(5)
            hard_exit.assert_not_called()


if __name__ == "__main__":
    unittest.main()

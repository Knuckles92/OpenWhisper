"""Tests for idle Start/Stop/Cancel visual inactive state."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from ui_qt.widgets.buttons import DangerButton, SuccessButton, WarningButton


class TestInactiveButtons(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_set_active_publishes_inactive_property(self):
        stop = DangerButton("Stop")
        cancel = WarningButton("Cancel")
        start = SuccessButton("Start Recording")

        stop.set_active(False)
        cancel.set_active(False)
        start.set_active(True)

        self.assertTrue(stop.property("inactive"))
        self.assertTrue(cancel.property("inactive"))
        self.assertFalse(bool(start.property("inactive")))

        start.set_active(False)
        self.assertTrue(start.property("inactive"))

    def test_idle_stop_and_cancel_drop_their_colour(self):
        """A 28% red/orange fill still read as live; idle is a neutral outline."""
        from PyQt6.QtWidgets import QHBoxLayout, QWidget

        from ui_qt.utils.theme_manager import ThemeManager

        previous = self.app.styleSheet()
        self.app.setStyleSheet(ThemeManager().stylesheet)
        try:
            host = QWidget()
            row = QHBoxLayout(host)
            stop, cancel = DangerButton("Stop"), WarningButton("Cancel")
            for button in (stop, cancel):
                row.addWidget(button)
            host.show()
            self.app.processEvents()

            def fill(button):
                return button.grab().toImage().pixelColor(button.width() // 2, 6)

            live = {button: fill(button) for button in (stop, cancel)}
            for button in (stop, cancel):
                button.set_active(False)
            self.app.processEvents()
            for button in (stop, cancel):
                idle = fill(button)
                self.assertGreater(live[button].red() - live[button].blue(), 100)
                self.assertLess(abs(idle.red() - idle.blue()), 24, button.text())
            host.close()
        finally:
            self.app.setStyleSheet(previous)

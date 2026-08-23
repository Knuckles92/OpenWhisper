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

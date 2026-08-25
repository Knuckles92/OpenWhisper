"""Layout tests for the redesigned Settings rail."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QFormLayout, QFrame, QScrollArea

from ui_qt.dialogs.settings_dialog import (
    ADVANCED,
    CLEANUP,
    CLEANUP_RULES,
    GENERAL,
    HOTKEYS,
    MEETING_AFTER,
    MEETING_DASHBOARD,
    MEETING_INTELLIGENCE,
    RECORDING,
    SettingsDialog,
)
from ui_qt.utils.theme_manager import ThemeManager
from ui_qt.widgets.nav_rail import NavRail
from ui_qt.widgets.wrapped_label import WrappedLabel


class TestSettingsGeneralLayout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_uses_rail_and_stack_without_scroll_areas(self):
        with patch.object(SettingsDialog, "_load_settings", lambda self: None):
            dialog = SettingsDialog()
        try:
            self.assertIsInstance(dialog.rail, NavRail)
            self.assertEqual(
                dialog.rail.keys(),
                (
                    GENERAL,
                    RECORDING,
                    CLEANUP,
                    CLEANUP_RULES,
                    MEETING_INTELLIGENCE,
                    MEETING_AFTER,
                    MEETING_DASHBOARD,
                    HOTKEYS,
                    ADVANCED,
                ),
            )
            self.assertIsNone(dialog.findChild(QScrollArea))
            recording = dialog._pages[RECORDING]
            forms = recording.findChildren(QFormLayout)
            self.assertGreaterEqual(len(forms), 2)
            self.assertGreaterEqual(dialog.max_recordings_spinbox.minimumWidth(), 110)
            self.assertGreaterEqual(
                dialog.streaming_font_size_spinbox.minimumWidth(), 110
            )
            helpers = recording.findChildren(WrappedLabel)
            self.assertGreaterEqual(len(helpers), 2)
            hotkeys = dialog._pages[HOTKEYS]
            shortcut_cards = [
                card
                for card in hotkeys.findChildren(QFrame)
                if card.objectName() == "hotkeyShortcutCard"
            ]
            self.assertEqual(len(shortcut_cards), 5)
            self.assertEqual(
                set(dialog.hotkey_inputs),
                {
                    "record_toggle",
                    "cancel",
                    "meeting_toggle",
                    "enable_disable",
                    "minimize_tray",
                },
            )
        finally:
            dialog.close()

    def test_every_destination_fits_the_default_height(self):
        previous_stylesheet = self.app.styleSheet()
        self.app.setStyleSheet(ThemeManager().stylesheet)
        with patch.object(SettingsDialog, "_load_settings", lambda self: None):
            dialog = SettingsDialog()
        try:
            dialog.show()
            self.app.processEvents()
            self.assertEqual((dialog.width(), dialog.height()), (980, 760))
            self.assertEqual(
                (dialog.minimumWidth(), dialog.minimumHeight()), (840, 700)
            )
            for key in dialog.rail.keys():
                dialog.rail.select(key)
                self.app.processEvents()
                page = dialog._pages[key]
                self.assertLessEqual(
                    page.sizeHint().height(),
                    page.height(),
                    msg=f"{key} overflows ({page.sizeHint().height()} > {page.height()})",
                )
        finally:
            dialog.close()
            self.app.setStyleSheet(previous_stylesheet)

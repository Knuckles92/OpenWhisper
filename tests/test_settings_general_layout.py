"""Layout tests for the redesigned Settings rail."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (
    QApplication,
    QFormLayout,
    QFrame,
    QLabel,
    QScrollArea,
)

from ui_qt.dialogs.settings_dialog import (
    ADVANCED,
    API_KEYS,
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
from ui_qt.widgets.setting_tile import FieldTile, SettingTile, TileBase
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
                    API_KEYS,
                    HOTKEYS,
                    ADVANCED,
                ),
            )
            self.assertIsNone(dialog.findChild(QScrollArea))
            general = dialog._pages[GENERAL]
            tiles = general.findChildren(SettingTile)
            self.assertEqual(len(tiles), 5)
            self.assertEqual(len(general.findChildren(FieldTile)), 1)
            self.assertIs(dialog.auto_paste_check, dialog.auto_paste_tile.checkbox)
            self.assertIs(
                dialog.update_notify_check, dialog.update_notify_tile.checkbox
            )
            self.assertIs(
                dialog.ui_font_scale_tile.control, dialog.ui_font_scale_combo
            )
            group_titles = [
                label.text()
                for label in general.findChildren(QLabel)
                if label.objectName() == "settingsTileGroupTitle"
            ]
            self.assertEqual(
                group_titles, ["OUTPUT", "WINDOW", "APPEARANCE", "UPDATES"]
            )
            recording = dialog._pages[RECORDING]
            forms = recording.findChildren(QFormLayout)
            self.assertGreaterEqual(len(forms), 2)
            self.assertEqual(len(recording.findChildren(FieldTile)), 2)
            self.assertEqual(len(recording.findChildren(SettingTile)), 1)
            self.assertIs(
                dialog.streaming_enabled_check,
                dialog.streaming_enabled_tile.checkbox,
            )
            self.assertIs(
                dialog.recording_retention_tile.control,
                dialog.recording_retention_combo,
            )
            intelligence = dialog._pages[MEETING_INTELLIGENCE]
            self.assertEqual(len(intelligence.findChildren(SettingTile)), 2)
            self.assertIs(
                dialog.meeting_context_folder_path.parentWidget(),
                dialog.meeting_context_folder_tile.body,
            )
            after = dialog._pages[MEETING_AFTER]
            self.assertEqual(len(after.findChildren(SettingTile)), 6)
            dashboard = dialog._pages[MEETING_DASHBOARD]
            self.assertEqual(len(dashboard.findChildren(FieldTile)), 2)
            self.assertEqual(len(dashboard.findChildren(TileBase)), 2)
            self.assertIs(
                dialog.meeting_bind_warning.parentWidget(),
                dialog.meeting_bind_tile.body,
            )
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

    def test_general_tiles_toggle_and_gate_update_notify(self):
        with patch.object(SettingsDialog, "_load_settings", lambda self: None):
            dialog = SettingsDialog()
        try:
            dialog.show()
            self.app.processEvents()
            dialog.rail.select(GENERAL)
            self.app.processEvents()
            tile = dialog.copy_clipboard_tile
            before = tile.checkbox.isChecked()
            QTest.mouseClick(tile, Qt.MouseButton.LeftButton, pos=QPoint(8, 8))
            self.assertEqual(tile.checkbox.isChecked(), not before)
            self.assertEqual(tile.property("checked"), not before)

            dialog.update_check_check.setChecked(True)
            self.assertTrue(dialog.update_notify_check.isEnabled())
            dialog.update_check_check.setChecked(False)
            self.assertFalse(dialog.update_notify_tile.isEnabled())
            self.assertFalse(dialog.update_notify_check.isEnabled())
        finally:
            dialog.close()

    def test_report_view_tiles_follow_the_final_report_toggle(self):
        with patch.object(SettingsDialog, "_load_settings", lambda self: None):
            dialog = SettingsDialog()
        try:
            dialog.meeting_end_report_check.setChecked(True)
            self.assertTrue(dialog.meeting_report_ribbon_tile.isEnabled())
            dialog.meeting_end_report_check.setChecked(False)
            for tile in (
                dialog.meeting_report_ribbon_tile,
                dialog.meeting_report_brief_tile,
                dialog.meeting_report_signal_tile,
            ):
                self.assertFalse(tile.isEnabled())
                self.assertFalse(tile.checkbox.isEnabled())
            self.assertFalse(dialog.meeting_report_views_title.isEnabled())
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
            self.assertEqual((dialog.width(), dialog.height()), (980, 810))
            self.assertEqual(
                (dialog.minimumWidth(), dialog.minimumHeight()), (840, 750)
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

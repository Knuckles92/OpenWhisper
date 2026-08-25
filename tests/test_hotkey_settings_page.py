"""Behavior tests for the integrated Hotkeys Settings destination."""

import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox

from config import config
from services.hotkey_manager import format_hotkey_display
from services.settings import SettingsKey, SettingsManager
from ui_qt.dialogs import settings_dialog as settings_dialog_module
from ui_qt.dialogs.settings_dialog import GENERAL, HOTKEYS, SettingsDialog
from ui_qt.ui_controller import UIController


class _FakeSignal:
    def __init__(self):
        self.disconnected = False

    def disconnect(self):
        self.disconnected = True


class _FakeCaptureThread:
    def __init__(self):
        self.captured = _FakeSignal()
        self.failed = _FakeSignal()
        self.stopped = False
        self.waited = False

    def isRunning(self):
        return True

    def stop(self):
        self.stopped = True

    def wait(self, _timeout):
        self.waited = True


class TestHotkeySettingsPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _patch_settings(self, manager):
        return patch.object(settings_dialog_module, "settings_manager", manager)

    def test_partial_settings_merge_with_platform_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = SettingsManager(os.path.join(temp_dir, "settings.json"))
            manager.save_all_settings(
                {SettingsKey.HOTKEYS: {"record_toggle": "f8"}}
            )
            with self._patch_settings(manager):
                dialog = SettingsDialog()
            try:
                self.assertEqual(dialog.current_hotkeys["record_toggle"], "f8")
                self.assertEqual(
                    dialog.current_hotkeys["cancel"],
                    config.DEFAULT_HOTKEYS["cancel"],
                )
                self.assertEqual(
                    dialog.hotkey_inputs["record_toggle"].text(),
                    format_hotkey_display("f8"),
                )
                self.assertEqual(
                    dialog.rail.value(HOTKEYS), format_hotkey_display("f8")
                )
            finally:
                dialog.close()

    def test_capture_applies_and_persists_immediately(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = SettingsManager(os.path.join(temp_dir, "settings.json"))
            with self._patch_settings(manager):
                dialog = SettingsDialog()
                applied = []

                def apply_hotkeys(hotkeys):
                    applied.append(hotkeys.copy())
                    manager.save_hotkey_settings(hotkeys)

                dialog.on_hotkeys_changed = apply_hotkeys
                field = dialog.hotkey_inputs["record_toggle"]
                thread = object()
                dialog.capturing = "record_toggle"
                dialog.current_hotkey_input = field
                dialog.capture_thread = thread

                dialog._on_hotkey_captured(thread, "ctrl+shift+r")

            try:
                self.assertEqual(applied[-1]["record_toggle"], "ctrl+shift+r")
                self.assertEqual(
                    manager.load_hotkey_settings()["record_toggle"],
                    "ctrl+shift+r",
                )
                self.assertEqual(
                    field.text(), format_hotkey_display("ctrl+shift+r")
                )
                self.assertIn("updated", dialog.message_label.text().lower())
            finally:
                dialog.close()

    def test_clear_and_confirmed_reset_apply_immediately(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = SettingsManager(os.path.join(temp_dir, "settings.json"))
            custom = config.DEFAULT_HOTKEYS.copy()
            custom["meeting_toggle"] = "ctrl+alt+m"
            custom["record_toggle"] = "f9"
            manager.save_hotkey_settings(custom)
            with self._patch_settings(manager):
                dialog = SettingsDialog()
                applied = []
                dialog.on_hotkeys_changed = lambda hotkeys: applied.append(
                    hotkeys.copy()
                )

                dialog._clear_meeting_hotkey()
                self.assertEqual(applied[-1]["meeting_toggle"], "")

                with patch.object(
                    QMessageBox,
                    "question",
                    return_value=QMessageBox.StandardButton.Cancel,
                ):
                    dialog._confirm_reset_hotkeys()
                self.assertEqual(len(applied), 1)

                with patch.object(
                    QMessageBox,
                    "question",
                    return_value=QMessageBox.StandardButton.Yes,
                ):
                    dialog._confirm_reset_hotkeys()
                self.assertEqual(applied[-1], config.DEFAULT_HOTKEYS)
            dialog.close()

    def test_leaving_hotkeys_stops_active_capture(self):
        with patch.object(SettingsDialog, "_load_settings", lambda self: None):
            dialog = SettingsDialog()
        try:
            field = dialog.hotkey_inputs["record_toggle"]
            field.set_capturing(True)
            thread = _FakeCaptureThread()
            dialog.capturing = "record_toggle"
            dialog.current_hotkey_input = field
            dialog.capture_thread = thread

            dialog._on_destination_changed(GENERAL)

            self.assertTrue(thread.captured.disconnected)
            self.assertTrue(thread.failed.disconnected)
            self.assertTrue(thread.stopped)
            self.assertTrue(thread.waited)
            self.assertIsNone(dialog.capture_thread)
            self.assertFalse(field.property("capturing"))
        finally:
            dialog.close()


class TestHotkeySettingsNavigation(unittest.TestCase):
    def test_file_hotkeys_deep_links_to_settings_destination(self):
        class FakeDialog:
            def __init__(self):
                self.refreshed = False
                self.destination = None

            def refresh(self):
                self.refreshed = True

            def select_destination(self, destination):
                self.destination = destination

        dialog = FakeDialog()

        class FakeController:
            def __init__(self):
                self.raised = None

            def _prepare_settings_dialog(self):
                return dialog

            def _raise_dialog(self, target):
                self.raised = target

        controller = FakeController()
        UIController.open_hotkey_settings(controller)

        self.assertTrue(dialog.refreshed)
        self.assertEqual(dialog.destination, HOTKEYS)
        self.assertIs(controller.raised, dialog)

    def test_regular_settings_still_selects_general(self):
        class FakeDialog:
            def __init__(self):
                self.destination = None

            def refresh(self):
                pass

            def select_destination(self, destination):
                self.destination = destination

        dialog = FakeDialog()

        class FakeController:
            def __init__(self):
                self.raised = None

            def _prepare_settings_dialog(self):
                return dialog

            def _raise_dialog(self, target):
                self.raised = target

        controller = FakeController()
        UIController.open_settings_dialog(controller)

        self.assertEqual(dialog.destination, GENERAL)
        self.assertIs(controller.raised, dialog)


if __name__ == "__main__":
    unittest.main()

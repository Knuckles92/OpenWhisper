"""Qt tests for deleting entries from the history sidebar."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QCheckBox, QMessageBox

from services.settings import SettingsKey
from ui_qt.widgets.history_sidebar import HistorySidebar


class TestHistorySidebarDelete(unittest.TestCase):
    """Confirmation behavior for context-menu deletion requests."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.sidebar = HistorySidebar()

    def tearDown(self):
        self.sidebar.deleteLater()

    def test_delete_is_canceled_when_confirmation_is_rejected(self):
        with (
            patch(
                "ui_qt.widgets.history_sidebar.settings_manager.get",
                return_value=True,
            ),
            patch.object(
                QMessageBox,
                "exec",
                return_value=QMessageBox.StandardButton.No,
            ),
            patch(
                "ui_qt.widgets.history_sidebar.history_manager.delete_entry"
            ) as delete_entry,
        ):
            self.sidebar._on_delete_requested("entry-test-id")

        delete_entry.assert_not_called()

    def test_delete_proceeds_after_confirmation(self):
        deleted = []
        self.sidebar.entry_deleted.connect(deleted.append)

        with (
            patch(
                "ui_qt.widgets.history_sidebar.settings_manager.get",
                return_value=True,
            ),
            patch.object(
                QMessageBox,
                "exec",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch.object(QCheckBox, "isChecked", return_value=False),
            patch(
                "ui_qt.widgets.history_sidebar.history_manager.delete_entry",
                return_value=True,
            ) as delete_entry,
        ):
            self.sidebar._on_delete_requested("entry-test-id")

        delete_entry.assert_called_once_with("entry-test-id")
        self.assertEqual(deleted, ["entry-test-id"])

    def test_dont_ask_again_preference_is_saved(self):
        with (
            patch(
                "ui_qt.widgets.history_sidebar.settings_manager.get",
                return_value=True,
            ),
            patch.object(
                QMessageBox,
                "exec",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch.object(QCheckBox, "isChecked", return_value=True),
            patch(
                "ui_qt.widgets.history_sidebar.settings_manager.save_setting"
            ) as save_setting,
            patch(
                "ui_qt.widgets.history_sidebar.history_manager.delete_entry",
                return_value=True,
            ),
        ):
            self.sidebar._on_delete_requested("entry-test-id")

        save_setting.assert_called_once_with(
            SettingsKey.CONFIRM_HISTORY_ENTRY_DELETE,
            False,
        )

    def test_saved_opt_out_skips_confirmation(self):
        with (
            patch(
                "ui_qt.widgets.history_sidebar.settings_manager.get",
                return_value=False,
            ),
            patch.object(QMessageBox, "exec") as show_confirmation,
            patch(
                "ui_qt.widgets.history_sidebar.history_manager.delete_entry",
                return_value=True,
            ) as delete_entry,
        ):
            self.sidebar._on_delete_requested("entry-test-id")

        show_confirmation.assert_not_called()
        delete_entry.assert_called_once_with("entry-test-id")


if __name__ == "__main__":
    unittest.main()

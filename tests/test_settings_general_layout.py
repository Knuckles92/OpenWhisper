"""Layout tests for Settings → General."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QFormLayout, QScrollArea

from ui_qt.dialogs.settings_dialog import SettingsDialog
from ui_qt.widgets.wrapped_label import WrappedLabel


class TestSettingsGeneralLayout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_general_tab_uses_scroll_area_and_form_rows(self):
        with patch.object(SettingsDialog, "_load_settings", lambda self: None):
            dialog = SettingsDialog()
        try:
            general = dialog.tabs.widget(0)
            if isinstance(general, QScrollArea):
                scroll = general
                content = general.widget()
            else:
                scroll = general.findChild(QScrollArea)
                content = general
            self.assertIsNotNone(scroll)
            self.assertTrue(scroll.widgetResizable())
            forms = content.findChildren(QFormLayout)
            self.assertGreaterEqual(len(forms), 2)
            self.assertGreaterEqual(dialog.max_recordings_spinbox.minimumWidth(), 110)
            self.assertGreaterEqual(
                dialog.streaming_font_size_spinbox.minimumWidth(), 110
            )
            helpers = content.findChildren(WrappedLabel)
            self.assertGreaterEqual(len(helpers), 2)
        finally:
            dialog.close()

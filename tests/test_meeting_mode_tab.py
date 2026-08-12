"""Tests for the Meeting Mode main-window tab."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from services.settings import SettingsKey, settings_manager
from ui_qt.main_window import MainWindow
from ui_qt.widgets.meeting_mode_tab import MeetingModeTab
from ui_qt.widgets.tabbed_content import TabbedContentWidget


class TestMeetingModeTabRegistration(unittest.TestCase):
    """Verify Meeting Mode is registered as its own content tab."""

    @classmethod
    def setUpClass(cls):
        """Create the shared Qt application."""
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        """Create a main window with isolated settings access."""
        self.load_settings = patch.object(
            settings_manager,
            "load_all_settings",
            return_value={},
        )
        self.get_setting = patch.object(
            settings_manager,
            "get",
            side_effect=lambda key, default=None: default,
        )
        self.save_setting = patch.object(settings_manager, "save_setting")
        self.load_settings.start()
        self.get_setting.start()
        self.save_setting.start()
        self.window = MainWindow()

    def tearDown(self):
        """Close the window and restore settings methods."""
        self.window._force_quit = True
        self.window.close()
        self.app.processEvents()
        self.save_setting.stop()
        self.get_setting.stop()
        self.load_settings.stop()

    def test_meeting_mode_is_third_tab(self):
        """Main window hosts Meeting Mode at tab index 2."""
        self.assertEqual(TabbedContentWidget.TAB_MEETING_MODE, 2)
        self.assertEqual(self.window.tabbed_content.tab_bar.count(), 3)
        self.assertEqual(
            self.window.tabbed_content.tab_bar.tabText(
                TabbedContentWidget.TAB_MEETING_MODE
            ),
            "Meeting Mode",
        )
        self.assertIs(
            self.window.tabbed_content.stack.widget(
                TabbedContentWidget.TAB_MEETING_MODE
            ),
            self.window.meeting_mode_tab,
        )
        self.assertFalse(hasattr(self.window, "meeting_panel"))

    def test_sidebar_switches_to_past_meetings_for_meeting_mode(self):
        """Meeting Mode replaces recorder history with Past Meetings."""
        self.assertFalse(self.window.history_sidebar.content_widget.isHidden())
        self.assertTrue(
            self.window.history_sidebar.meetings_content_widget.isHidden()
        )

        self.window.tabbed_content.set_current_index(
            TabbedContentWidget.TAB_MEETING_MODE
        )

        self.assertTrue(self.window.history_sidebar.content_widget.isHidden())
        self.assertFalse(
            self.window.history_sidebar.meetings_content_widget.isHidden()
        )
        self.assertEqual(self.window.sidebar_action.text(), "Past Meetings")
        self.assertIn("Past Meetings", self.window.history_edge_tab.toolTip())

    def test_compact_mode_does_not_require_footer_meeting_strip(self):
        """Compact mode still works without a footer meeting strip."""
        self.window.set_compact_mode(True)
        self.assertTrue(self.window._compact_mode)
        self.assertFalse(self.window.tabbed_content.isVisibleTo(self.window))
        self.window.set_compact_mode(False)
        self.assertTrue(self.window.tabbed_content.isVisibleTo(self.window))
        self.window.tabbed_content.set_current_index(
            TabbedContentWidget.TAB_MEETING_MODE
        )
        self.assertEqual(
            self.window.tabbed_content.current_index(),
            TabbedContentWidget.TAB_MEETING_MODE,
        )


class TestMeetingModeTabState(unittest.TestCase):
    """Exercise idle/active control visibility on the Meeting Mode tab."""

    @classmethod
    def setUpClass(cls):
        """Create the shared Qt application."""
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        """Build a standalone tab with patched settings."""
        self.get_setting = patch.object(
            settings_manager,
            "get",
            side_effect=lambda key, default=None: (
                False if key == SettingsKey.MEETING_CLOUD_LAST_ENABLED else default
            ),
        )
        self.get_setting.start()
        self.tab = MeetingModeTab()

    def tearDown(self):
        """Dispose the tab and restore settings."""
        self.tab.deleteLater()
        self.app.processEvents()
        self.get_setting.stop()

    def test_idle_shows_start_controls(self):
        """Idle layout exposes Start Meeting and hides session controls."""
        self.assertFalse(self.tab.idle_card.isHidden())
        self.assertTrue(self.tab.session_card.isHidden())
        self.assertFalse(self.tab.is_meeting_active)

    def test_active_payload_switches_layout(self):
        """Active state payload swaps to the in-meeting session card."""
        self.tab.set_meeting_state(
            {"active": True, "status": "active", "elapsed_s": 65}
        )
        self.app.processEvents()

        self.assertTrue(self.tab.is_meeting_active)
        self.assertTrue(self.tab.idle_card.isHidden())
        self.assertFalse(self.tab.session_card.isHidden())
        self.assertEqual(self.tab.status_pill.text(), "Active")
        self.assertEqual(self.tab.elapsed_label.text(), "01:05")

    def test_start_emits_cloud_choice(self):
        """Start Meeting emits the current cloud-intelligence choice."""
        received = []
        self.tab.start_requested.connect(received.append)
        self.tab.cloud_checkbox.setChecked(True)
        self.tab.start_button.click()
        self.assertEqual(received, [True])


if __name__ == "__main__":
    unittest.main()

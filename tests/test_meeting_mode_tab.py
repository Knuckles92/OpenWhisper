"""Tests for the Meeting Mode main-window tab."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from config import config
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


class TestMeetingModeWindowHeight(unittest.TestCase):
    """The window must grow rather than squeeze the finalization checklist."""

    FINALIZATION = {
        "status": "running",
        "message": "Preparing final report...",
        "current_step": 3,
        "total_steps": 4,
        "step_details": (
            "Synthesizing executive summary, key decisions, and action "
            "items over 6 segments..."
        ),
        "steps": [
            {"id": "redecode", "name": "Audio Re-transcription", "status": "completed"},
            {"id": "polish", "name": "Transcript Cleanup", "status": "completed"},
            {"id": "consolidation", "name": "Summary & Action Items", "status": "running"},
            {"id": "finalize", "name": "State Finalization", "status": "pending"},
        ],
    }

    @classmethod
    def setUpClass(cls):
        """Create the shared Qt application."""
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        """Show a main window on the Meeting Mode tab with isolated settings."""
        self.load_settings = patch.object(
            settings_manager, "load_all_settings", return_value={}
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
        self.window.show()
        self.window.resize(
            config.MAIN_WINDOW_DEFAULT_WIDTH, config.MAIN_WINDOW_DEFAULT_HEIGHT
        )
        self.window.tabbed_content.set_current_index(
            TabbedContentWidget.TAB_MEETING_MODE
        )
        # The offscreen screen is shorter than a real desktop, and the floor is
        # capped to the screen; pretend there is room for the full page.
        self.window._max_usable_height = lambda: 2000
        self._settle()

    def tearDown(self):
        """Close the window and restore settings methods."""
        self.window._force_quit = True
        self.window.close()
        self.app.processEvents()
        self.save_setting.stop()
        self.get_setting.stop()
        self.load_settings.stop()

    def _settle(self):
        """Let deferred layout and height-sync work complete."""
        for _ in range(10):
            self.app.processEvents()

    def _step_rows(self):
        """Return the rendered step row widgets."""
        layout = self.window.meeting_mode_tab.finalization_steps_layout
        return [layout.itemAt(i).widget() for i in range(layout.count())]

    def test_finalization_steps_are_not_squeezed(self):
        """Every step row keeps its full height once the pipeline reports steps."""
        start_height = self.window.height()
        self.window.meeting_mode_tab.set_meeting_state({
            "active": False,
            "status": "ended",
            "finalization": self.FINALIZATION,
            "dashboard_available": True,
        })
        self._settle()

        self.assertGreater(self.window.height(), start_height)
        steps_widget = self.window.meeting_mode_tab.finalization_steps_widget
        self.assertGreaterEqual(
            steps_widget.height(), steps_widget.minimumSizeHint().height()
        )
        for row in self._step_rows():
            self.assertGreaterEqual(row.height(), row.minimumSizeHint().height())

    def test_borrowed_height_is_returned_when_the_card_clears(self):
        """Ending finalization gives the extra window height back."""
        start_height = self.window.height()
        self.window.meeting_mode_tab.set_meeting_state({
            "active": False,
            "status": "ended",
            "finalization": self.FINALIZATION,
        })
        self._settle()
        self.assertGreater(self.window.height(), start_height)

        floor_with_card = self.window.minimumHeight()

        self.window.meeting_mode_tab.set_meeting_state({
            "status": "starting",
            "active": True,
        })
        self._settle()

        self.assertLess(self.window.minimumHeight(), floor_with_card)
        self.assertEqual(self.window._meeting_height_growth, 0)
        # The shrink itself is animated; check where it is headed.
        self.assertEqual(
            self.window._resize_animation.endValue().height(), start_height
        )

    def test_step_rows_are_inset_from_the_list_edges(self):
        """Step rows keep padding on both sides instead of touching the border."""
        self.window.meeting_mode_tab.set_meeting_state({
            "active": False,
            "status": "ended",
            "finalization": self.FINALIZATION,
        })
        self._settle()

        steps_widget = self.window.meeting_mode_tab.finalization_steps_widget
        for row in self._step_rows():
            self.assertGreater(row.x(), 0)
            self.assertLess(row.x() + row.width(), steps_widget.width())


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
        self.assertTrue(self.tab.demo_button.isHidden())
        self.assertTrue(self.tab.demo_hint.isHidden())

    def test_developer_mode_shows_demo_meeting_control(self):
        """Developer mode reveals the demo loader on the idle Meeting tab."""
        received = []
        self.tab.demo_requested.connect(received.append)
        self.tab.set_developer_mode(True)
        self.app.processEvents()

        self.assertFalse(self.tab.demo_button.isHidden())
        self.assertFalse(self.tab.demo_hint.isHidden())

        self.tab.cloud_checkbox.setChecked(True)
        self.tab.demo_button.click()
        self.assertEqual(received, [True])

        self.tab.set_meeting_state({"active": True, "status": "active"})
        self.app.processEvents()
        self.assertTrue(self.tab.demo_button.isHidden())

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

    def test_running_finalization_hides_start_and_shows_indeterminate_bar(self):
        """Running finalization keeps a result card with indeterminate progress."""
        self.tab.set_meeting_state({
            "active": False,
            "status": "ended",
            "finalization": {
                "status": "running",
                "message": "Preparing final cloud insights…",
            },
            "dashboard_available": True,
        })
        self.app.processEvents()

        self.assertTrue(self.tab.idle_card.isHidden())
        self.assertTrue(self.tab.session_card.isHidden())
        self.assertFalse(self.tab.finalization_card.isHidden())
        self.assertFalse(self.tab.finalization_progress.isHidden())
        self.assertEqual(self.tab.finalization_progress.minimum(), 0)
        self.assertEqual(self.tab.finalization_progress.maximum(), 0)
        self.assertTrue(self.tab.finalization_dashboard_button.isEnabled())

    def test_completed_and_disabled_restore_start(self):
        """Terminal info outcomes keep the card and restore Start Meeting."""
        for status, message in (
            ("completed", "Final cloud insights are ready."),
            ("disabled", "Cloud intelligence is off for this meeting."),
        ):
            with self.subTest(status=status):
                self.tab.set_meeting_state({
                    "active": False,
                    "status": "ended",
                    "finalization": {"status": status, "message": message},
                    "dashboard_available": True,
                })
                self.app.processEvents()
                self.assertFalse(self.tab.idle_card.isHidden())
                self.assertFalse(self.tab.start_button.isHidden())
                self.assertFalse(self.tab.finalization_card.isHidden())
                self.assertTrue(self.tab.finalization_progress.isHidden())
                self.assertIn(message, self.tab.finalization_message.text())

    def test_unavailable_and_failed_use_warning_tone(self):
        """Unavailable/failed stay persistent warnings without dialogs."""
        self.tab.set_meeting_state({
            "active": False,
            "status": "ended",
            "finalization": {
                "status": "failed",
                "message": "Final cloud insights failed: boom",
            },
        })
        self.app.processEvents()
        self.assertEqual(
            self.tab.finalization_card.property("finalizationTone"),
            "warning",
        )
        self.assertFalse(self.tab.finalization_card.isHidden())
        self.assertFalse(self.tab.start_button.isHidden())

    def test_starting_clears_previous_finalization(self):
        """A subsequent start payload clears the previous result card."""
        self.tab.set_meeting_state({
            "active": False,
            "status": "ended",
            "finalization": {
                "status": "completed",
                "message": "done",
            },
        })
        self.tab.set_meeting_state({"status": "starting", "active": True})
        self.app.processEvents()
        self.assertIsNone(self.tab.finalization_status)
        self.assertTrue(self.tab.finalization_card.isHidden())

    def test_dashboard_signal_available_after_inactive(self):
        """Open dashboard remains wired after active=False."""
        clicked = []
        self.tab.open_dashboard_requested.connect(lambda: clicked.append(True))
        self.tab.set_meeting_state({
            "active": False,
            "status": "ended",
            "finalization": {"status": "running", "message": "…"},
            "dashboard_available": True,
        })
        self.app.processEvents()
        self.tab.finalization_dashboard_button.click()
        self.assertEqual(clicked, [True])

    def test_failed_finalization_shows_retry_button_and_emits_signal(self):
        """Failed finalization shows Retry insights button and clicking emits signal."""
        clicked = []
        self.tab.retry_insights_requested.connect(lambda: clicked.append(True))
        self.tab.set_meeting_state({
            "active": False,
            "status": "ended",
            "finalization": {
                "status": "failed",
                "message": "Final cloud insights failed: RPC timeout",
            },
            "dashboard_available": True,
        })
        self.app.processEvents()
        self.assertFalse(self.tab.finalization_retry_button.isHidden())
        self.assertTrue(self.tab.finalization_retry_button.isEnabled())
        self.assertFalse(self.tab.finalization_retry_speakers_button.isHidden())
        self.tab.finalization_retry_button.click()
        self.assertEqual(clicked, [True])

        # Switching to running hides the retry button
        self.tab.set_meeting_state({
            "active": False,
            "status": "ended",
            "finalization": {
                "status": "running",
                "message": "Re-running final cloud insights…",
            },
        })
        self.app.processEvents()
        self.assertTrue(self.tab.finalization_retry_button.isHidden())

    def test_multi_step_progress_rendering(self):
        """Multi-step finalization renders step badge, determinate progress, details, and step rows."""
        steps = [
            {"id": "redecode", "name": "Audio Re-transcription", "status": "completed", "detail": "Re-decoded 8 windows"},
            {"id": "polish", "name": "Transcript Cleanup", "status": "running", "detail": "Polishing block 1 of 2"},
            {"id": "consolidation", "name": "Summary & Action Items", "status": "pending", "detail": "Queued"},
            {"id": "finalize", "name": "State Finalization", "status": "pending", "detail": "Queued"},
        ]
        self.tab.set_meeting_state({
            "active": False,
            "status": "ended",
            "finalization": {
                "status": "running",
                "message": "Cleaning transcript…",
                "stage": "polish",
                "current_step": 2,
                "total_steps": 4,
                "step_details": "Polishing block 1 of 2 (40 segments)...",
                "steps": steps,
            },
            "dashboard_available": True,
        })
        self.app.processEvents()

        self.assertFalse(self.tab.finalization_card.isHidden())
        self.assertFalse(self.tab.finalization_progress.isHidden())
        self.assertEqual(self.tab.finalization_progress.minimum(), 0)
        self.assertEqual(self.tab.finalization_progress.maximum(), 100)
        self.assertGreater(self.tab.finalization_progress.value(), 0)
        self.assertEqual(self.tab.finalization_step_badge.text(), "Step 2 of 4")
        self.assertFalse(self.tab.finalization_step_badge.isHidden())
        self.assertFalse(self.tab.finalization_detail.isHidden())
        self.assertEqual(self.tab.finalization_detail.text(), "Polishing block 1 of 2 (40 segments)...")
        self.assertFalse(self.tab.finalization_steps_widget.isHidden())
        self.assertEqual(self.tab.finalization_steps_layout.count(), 4)

    def test_completed_summary_stats_rendering(self):
        """Completed finalization displays step badge, summary stats grid, and completed checklist."""
        steps = [
            {"id": "redecode", "name": "Audio Re-transcription", "status": "completed", "detail": "Done"},
            {"id": "consolidation", "name": "Summary & Action Items", "status": "completed", "detail": "Done"},
            {"id": "finalize", "name": "State Finalization", "status": "completed", "detail": "Done"},
        ]
        stats = {
            "duration_s": 150.0,
            "segments": 32,
            "words": 520,
            "key_points": 4,
            "action_items": 2,
            "decisions": 1,
        }
        self.tab.set_meeting_state({
            "active": False,
            "status": "ended",
            "finalization": {
                "status": "completed",
                "message": "Final insights ready — 32 segments, 4 key points, 2 action items, 1 decisions.",
                "stage": "complete",
                "current_step": 3,
                "total_steps": 3,
                "step_details": "All finalization passes completed successfully.",
                "steps": steps,
                "summary_stats": stats,
            },
            "dashboard_available": True,
        })
        self.app.processEvents()

        self.assertFalse(self.tab.finalization_card.isHidden())
        self.assertTrue(self.tab.finalization_progress.isHidden())
        self.assertEqual(self.tab.finalization_step_badge.text(), "Complete")
        self.assertFalse(self.tab.finalization_stats_widget.isHidden())
        self.assertGreaterEqual(self.tab.finalization_stats_layout.count(), 5)
        self.assertFalse(self.tab.finalization_steps_widget.isHidden())
        self.assertEqual(self.tab.finalization_steps_layout.count(), 3)


if __name__ == "__main__":
    unittest.main()

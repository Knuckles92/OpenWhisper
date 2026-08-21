import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint
from PyQt6.QtWidgets import QApplication, QPushButton

from config import config
from services.settings import SettingsKey, settings_manager
from ui_qt.main_window import MainWindow
from ui_qt.widgets.meeting_mode_tab import (
    MeetingModeTab,
    meeting_audio_support_copy,
)
from ui_qt.widgets.tabbed_content import TabbedContentWidget


class TestMeetingModeTabRegistration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
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
        self.window._force_quit = True
        self.window.close()
        self.app.processEvents()
        self.save_setting.stop()
        self.get_setting.stop()
        self.load_settings.stop()

    def test_meeting_mode_is_third_tab(self):
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

    def test_expanded_past_meetings_keeps_primary_controls_visible(self):
        """The sidebar must not clip Start, cloud copy, or the tab label."""
        self.window.show()
        self.window.resize(985, 800)
        self.window.tabbed_content.set_current_index(
            TabbedContentWidget.TAB_MEETING_MODE
        )
        self.window.history_sidebar._set_sidebar_width(
            self.window.history_sidebar.EXPANDED_WIDTH
        )
        for _ in range(5):
            self.app.processEvents()

        tab = self.window.meeting_mode_tab
        viewport = tab.scroll_area.viewport()
        for control in (tab.cloud_checkbox, tab.start_button):
            top_left = control.mapTo(viewport, QPoint(0, 0))
            self.assertGreaterEqual(top_left.x(), 0)
            self.assertGreaterEqual(top_left.y(), 0)
            self.assertLessEqual(top_left.x() + control.width(), viewport.width())
            self.assertLessEqual(top_left.y() + control.height(), viewport.height())

        tab_bar = self.window.tabbed_content.tab_bar
        meeting_rect = tab_bar.tabRect(TabbedContentWidget.TAB_MEETING_MODE)
        label_width = tab_bar.fontMetrics().horizontalAdvance("Meeting Mode")
        self.assertGreaterEqual(meeting_rect.width(), label_width + 20)


class TestMeetingModeWindowHeight(unittest.TestCase):
    """Finalization remains readable in the responsive scroll viewport."""

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
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
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
        self.window._force_quit = True
        self.window.close()
        self.app.processEvents()
        self.save_setting.stop()
        self.get_setting.stop()
        self.load_settings.stop()

    def _settle(self):
        for _ in range(10):
            self.app.processEvents()

    def _step_rows(self):
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

        self.assertEqual(self.window.height(), start_height)
        self.assertGreater(
            self.window.meeting_mode_tab.scroll_area.verticalScrollBar().maximum(),
            0,
        )
        steps_widget = self.window.meeting_mode_tab.finalization_steps_widget
        self.assertGreaterEqual(
            steps_widget.height(), steps_widget.minimumSizeHint().height()
        )
        for row in self._step_rows():
            self.assertGreaterEqual(row.height(), row.minimumSizeHint().height())

    def test_scroll_space_is_released_when_the_card_clears(self):
        """Clearing finalization removes overflow without resizing the window."""
        start_height = self.window.height()
        self.window.meeting_mode_tab.set_meeting_state({
            "active": False,
            "status": "ended",
            "finalization": self.FINALIZATION,
        })
        self._settle()
        self.assertEqual(self.window.height(), start_height)
        self.assertGreater(
            self.window.meeting_mode_tab.scroll_area.verticalScrollBar().maximum(),
            0,
        )

        self.window.meeting_mode_tab.set_meeting_state({
            "status": "starting",
            "active": False,
        })
        self._settle()

        self.assertEqual(self.window.height(), start_height)
        self.assertEqual(self.window._meeting_height_growth, 0)
        self.assertEqual(
            self.window.meeting_mode_tab.scroll_area.verticalScrollBar().maximum(),
            0,
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
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
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

    def test_platform_copy_does_not_promise_unavailable_system_audio(self):
        subtitle, linux_hint = meeting_audio_support_copy("linux")

        self.assertIn("when supported", subtitle)
        self.assertIn("microphone audio only", linux_hint)
        self.assertIn("Windows", linux_hint)
        self.assertEqual(self.tab.platform_hint.text(), linux_hint)

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

    def test_starting_is_non_active_and_does_not_run_timer(self):
        """Startup shows progress without exposing live controls or a timer."""
        self.tab.set_meeting_state({"active": False, "status": "starting"})
        self.app.processEvents()

        self.assertFalse(self.tab.is_meeting_active)
        self.assertTrue(self.tab.idle_card.isHidden())
        self.assertFalse(self.tab.session_card.isHidden())
        self.assertEqual(self.tab.status_pill.text(), "Starting")
        self.assertFalse(self.tab._elapsed_timer.isActive())
        self.assertFalse(self.tab.pause_button.isEnabled())
        self.assertFalse(self.tab.end_button.isEnabled())
        self.assertEqual(self.tab.elapsed_label.text(), "00:00")

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
                self.assertEqual(
                    self.tab.finalization_active_box.isHidden(),
                    status == "completed",
                )

    def test_empty_meeting_stays_visible_and_disables_speaker_rerun(self):
        """A zero-content result cannot look successful or rerun speakers."""
        self.tab.set_meeting_state({
            "active": False,
            "status": "failed",
            "finalization": {
                "status": "completed",
                "message": "Final cloud insights are ready.",
                "content_summary": {
                    "meeting_status": "failed",
                    "is_empty": True,
                    "has_audio": False,
                    "has_transcript": False,
                    "can_rerun_speakers": False,
                },
            },
        })
        self.app.processEvents()

        self.assertEqual(self.tab.finalization_title.text(), "Meeting Failed")
        self.assertFalse(self.tab.finalization_active_box.isHidden())
        self.assertIn("No audio or transcript", self.tab.finalization_message.text())
        self.assertFalse(
            self.tab.finalization_retry_speakers_button.isEnabled()
        )
        self.assertIn(
            "No system-audio",
            self.tab.finalization_retry_speakers_button.toolTip(),
        )

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
        self.assertTrue(self.tab.start_button.isHidden())
        self.assertTrue(self.tab.idle_card.isHidden())
        self.assertFalse(self.tab.finalization_keep_later_button.isHidden())
        self.assertFalse(self.tab.finalization_start_new_button.isHidden())
        self.assertIn(
            "stay in Past Meetings",
            self.tab.finalization_keep_hint.text(),
        )

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
        self.tab.set_meeting_state({"status": "starting", "active": False})
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

    def test_finalization_dashboard_reports_through_signal_without_url(self):
        """An unavailable dashboard remains clickable so runtime can explain."""
        clicked = []
        self.tab.open_dashboard_requested.connect(lambda: clicked.append(True))
        self.tab.set_meeting_state({
            "active": False,
            "status": "ended",
            "finalization": {"status": "unavailable", "message": "No dashboard"},
            "dashboard_available": False,
        })
        self.app.processEvents()

        self.assertTrue(self.tab.finalization_dashboard_button.isEnabled())
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
        self.assertEqual(
            self.tab.finalization_retry_button.text(), "Retry failed steps"
        )
        self.assertFalse(self.tab.finalization_retry_speakers_button.isHidden())
        self.tab.finalization_retry_button.click()
        self.assertEqual(clicked, [True])

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

    def test_completed_hides_recap_and_keeps_checklist(self):
        """Completed finalization keeps the checklist and hides the recap boxes."""
        steps = [
            {"id": "redecode", "name": "Audio Re-transcription", "status": "completed", "detail": "Done"},
            {"id": "consolidation", "name": "Summary & Action Items", "status": "completed", "detail": "Done"},
            {"id": "finalize", "name": "State Finalization", "status": "completed", "detail": "Done"},
        ]
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
                "summary_stats": {
                    "duration_s": 150.0,
                    "segments": 32,
                    "words": 520,
                    "key_points": 4,
                    "action_items": 2,
                    "decisions": 1,
                },
            },
            "dashboard_available": True,
        })
        self.app.processEvents()

        self.assertFalse(self.tab.finalization_card.isHidden())
        self.assertTrue(self.tab.finalization_progress.isHidden())
        self.assertEqual(self.tab.finalization_step_badge.text(), "Complete")
        self.assertTrue(self.tab.finalization_active_box.isHidden())
        self.assertFalse(hasattr(self.tab, "finalization_stats_widget"))
        self.assertFalse(self.tab.finalization_steps_widget.isHidden())
        self.assertEqual(self.tab.finalization_steps_layout.count(), 3)

    def test_failed_redecode_under_completed_shows_retry_controls(self):
        """A failed checklist row stays retryable even when overall status is completed."""
        clicked = []
        self.tab.retry_step_requested.connect(clicked.append)
        steps = [
            {
                "id": "redecode",
                "name": "Audio Re-transcription",
                "status": "failed",
                "detail": "Re-decoding failed; kept live transcript",
            },
            {
                "id": "polish",
                "name": "Transcript Cleanup",
                "status": "completed",
            },
            {
                "id": "consolidation",
                "name": "Summary & Action Items",
                "status": "completed",
            },
            {
                "id": "finalize",
                "name": "State Finalization",
                "status": "completed",
            },
        ]
        self.tab.set_meeting_state({
            "active": False,
            "status": "ended",
            "finalization": {
                "status": "completed",
                "message": "Final insights ready — 12 segments.",
                "steps": steps,
            },
            "dashboard_available": True,
        })
        self.app.processEvents()

        self.assertEqual(self.tab.finalization_step_badge.text(), "Needs retry")
        self.assertEqual(
            self.tab.finalization_title.text(), "Meeting Finished With Issues"
        )
        self.assertFalse(self.tab.finalization_retry_button.isHidden())
        actions = self.tab.finalization_steps_widget.findChildren(QPushButton)
        labels = [button.text() for button in actions]
        self.assertIn("Retry", labels)
        self.assertIn("Run again", labels)
        retry = next(button for button in actions if button.text() == "Retry")
        retry.click()
        self.assertEqual(clicked, ["redecode"])

        self.tab.set_meeting_state({
            "active": False,
            "status": "ended",
            "finalization": {
                "status": "running",
                "message": "Re-transcribing meeting…",
                "steps": steps,
            },
        })
        self.app.processEvents()
        self.assertTrue(self.tab.finalization_retry_button.isHidden())
        running_actions = []
        for index in range(self.tab.finalization_steps_layout.count()):
            row = self.tab.finalization_steps_layout.itemAt(index).widget()
            if row is not None:
                running_actions.extend(row.findChildren(QPushButton))
        self.assertEqual(running_actions, [])

    def test_incomplete_card_emits_keep_later_and_start_new(self):
        """Incomplete cards expose defer and start-new instead of idle Start."""
        deferred = []
        started = []
        self.tab.defer_insights_requested.connect(lambda: deferred.append(True))
        self.tab.start_new_meeting_requested.connect(started.append)
        self.tab.cloud_checkbox.setChecked(True)
        self.tab.set_meeting_state({
            "active": False,
            "status": "ended",
            "finalization": {
                "status": "failed",
                "message": "Final cloud insights were interrupted.",
            },
        })
        self.app.processEvents()

        self.assertTrue(self.tab.start_button.isHidden())
        self.assertFalse(self.tab.finalization_keep_later_button.isHidden())
        self.assertFalse(self.tab.finalization_start_new_button.isHidden())
        self.tab.finalization_keep_later_button.click()
        self.tab.finalization_start_new_button.click()
        self.assertEqual(deferred, [True])
        self.assertEqual(started, [True])

    def test_completed_card_keeps_idle_start_without_defer_actions(self):
        """A clean completed card still uses the idle Start Meeting control."""
        self.tab.set_meeting_state({
            "active": False,
            "status": "ended",
            "finalization": {
                "status": "completed",
                "message": "Final cloud insights are ready.",
            },
        })
        self.app.processEvents()
        self.assertFalse(self.tab.start_button.isHidden())
        self.assertTrue(self.tab.finalization_keep_later_button.isHidden())
        self.assertTrue(self.tab.finalization_start_new_button.isHidden())


if __name__ == "__main__":
    unittest.main()

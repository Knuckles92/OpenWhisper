"""First-visit Meeting Mode overview: copy, dismiss, and when it appears."""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel, QPushButton

from services.settings import SettingsKey, settings_manager
from ui_qt.dialogs.meeting_intro_dialog import (
    MeetingModeIntroDialog,
    maybe_show_meeting_mode_intro,
)
from ui_qt.main_window import MainWindow
from ui_qt.widgets.tabbed_content import TabbedContentWidget


class TestMeetingIntroDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _dialog_text(self, dialog):
        return " ".join(
            label.text() for label in dialog.findChildren(QLabel) if label.text()
        )

    def test_copy_covers_mode_features_and_settings(self):
        dialog = MeetingModeIntroDialog()
        text = self._dialog_text(dialog)
        self.assertIn("Welcome to Meeting Mode", text)
        self.assertIn("live transcript", text.lower())
        self.assertIn("dashboard", text.lower())
        self.assertIn("cloud insights", text.lower())
        self.assertIn("Settings → Meeting", text)
        self.assertIn("Model Manager", text)
        self.assertIn("Skip anytime", text)

        skip = dialog.findChild(QPushButton, "meetingIntroSkipButton")
        got_it = dialog.findChild(QPushButton, "meetingIntroGotItButton")
        self.assertIsNotNone(skip)
        self.assertIsNotNone(got_it)
        self.assertTrue(skip.isEnabled())
        self.assertTrue(got_it.isEnabled())
        self.assertTrue(got_it.isDefault())

    def test_skip_keeps_skip_result(self):
        dialog = MeetingModeIntroDialog()
        dialog.findChild(QPushButton, "meetingIntroSkipButton").click()
        self.assertEqual(dialog.result_action, MeetingModeIntroDialog.RESULT_SKIP)

    def test_got_it_records_result(self):
        dialog = MeetingModeIntroDialog()
        dialog.findChild(QPushButton, "meetingIntroGotItButton").click()
        self.assertEqual(dialog.result_action, MeetingModeIntroDialog.RESULT_GOT_IT)


class TestMaybeShowMeetingIntro(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_skips_dialog_after_it_has_been_seen(self):
        with patch(
            "ui_qt.dialogs.meeting_intro_dialog.resolve_meeting_mode_intro_seen",
            return_value=True,
        ), patch(
            "ui_qt.dialogs.meeting_intro_dialog.MeetingModeIntroDialog"
        ) as dialog_cls:
            self.assertFalse(maybe_show_meeting_mode_intro())
            dialog_cls.assert_not_called()

    def test_skip_and_got_it_both_persist_seen(self):
        for action in (
            MeetingModeIntroDialog.RESULT_SKIP,
            MeetingModeIntroDialog.RESULT_GOT_IT,
        ):
            with self.subTest(action=action):
                class _Dismissed:
                    result_action = action

                    def exec(self):
                        return 1

                with patch(
                    "ui_qt.dialogs.meeting_intro_dialog"
                    ".resolve_meeting_mode_intro_seen",
                    return_value=False,
                ), patch(
                    "ui_qt.dialogs.meeting_intro_dialog.MeetingModeIntroDialog",
                    return_value=_Dismissed(),
                ), patch.object(settings_manager, "save_setting") as save:
                    self.assertTrue(maybe_show_meeting_mode_intro())
                    save.assert_called_once_with(
                        SettingsKey.MEETING_MODE_INTRO_SEEN, True
                    )


class TestMeetingIntroAppearsOnFirstVisit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _make_window(self, *, intro_seen=False, supported=True, last_tab=0):
        self._patches = [
            patch(
                "ui_qt.widgets.tabbed_content.meeting_mode_supported",
                return_value=supported,
            ),
            patch(
                "ui_qt.main_window.resolve_meeting_mode_intro_seen",
                return_value=intro_seen,
            ),
            patch.object(
                settings_manager,
                "load_all_settings",
                return_value={SettingsKey.LAST_TAB_INDEX: last_tab},
            ),
            patch.object(
                settings_manager,
                "get",
                side_effect=lambda key, default=None: default,
            ),
            patch.object(settings_manager, "save_setting"),
        ]
        for item in self._patches:
            item.start()
        window = MainWindow()
        window._force_quit = True
        self.addCleanup(window.close)
        self.addCleanup(window.deleteLater)
        return window

    def tearDown(self):
        for item in getattr(self, "_patches", []):
            item.stop()
        self.app.processEvents()

    def test_first_visit_shows_intro_after_tab_opens(self):
        window = self._make_window(intro_seen=False)
        window.show()
        with patch(
            "ui_qt.dialogs.meeting_intro_dialog.maybe_show_meeting_mode_intro"
        ) as show:
            window.tabbed_content.set_current_index(
                TabbedContentWidget.TAB_MEETING_MODE
            )
            self.app.processEvents()
            show.assert_called_once_with(window)

    def test_seen_intro_does_not_show_again(self):
        window = self._make_window(intro_seen=True)
        window.show()
        with patch(
            "ui_qt.dialogs.meeting_intro_dialog.maybe_show_meeting_mode_intro"
        ) as show:
            window.tabbed_content.set_current_index(
                TabbedContentWidget.TAB_MEETING_MODE
            )
            self.app.processEvents()
            show.assert_not_called()

    def test_active_meeting_does_not_block_on_intro(self):
        window = self._make_window(intro_seen=False)
        window.show()
        window.meeting_mode_tab._active = True
        with patch(
            "ui_qt.dialogs.meeting_intro_dialog.maybe_show_meeting_mode_intro"
        ) as show:
            window.tabbed_content.set_current_index(
                TabbedContentWidget.TAB_MEETING_MODE
            )
            self.app.processEvents()
            show.assert_not_called()

    def test_restored_meeting_tab_shows_intro_on_first_show(self):
        window = self._make_window(
            intro_seen=False,
            last_tab=TabbedContentWidget.TAB_MEETING_MODE,
        )
        with patch(
            "ui_qt.dialogs.meeting_intro_dialog.maybe_show_meeting_mode_intro"
        ) as show:
            window.show()
            self.app.processEvents()
            show.assert_called_once_with(window)

    def test_locked_tab_waits_for_platform_ack(self):
        window = self._make_window(intro_seen=False, supported=False)
        window.show()
        self.assertTrue(window.tabbed_content.meeting_tab_is_locked())
        with patch(
            "ui_qt.dialogs.meeting_intro_dialog.maybe_show_meeting_mode_intro"
        ) as show:
            window.tabbed_content.set_current_index(
                TabbedContentWidget.TAB_MEETING_MODE
            )
            self.app.processEvents()
            show.assert_not_called()

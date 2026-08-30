"""Unsupported-platform Meeting Mode gate: copy, dialog, tab, and ack."""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QCheckBox, QPushButton

from meeting.platform import meeting_mode_supported, meeting_unsupported_os_name
from services.settings import SettingsKey, settings_manager
from ui_qt.dialogs.meeting_unsupported_dialog import (
    MeetingUnsupportedPlatformDialog,
    acknowledge_unsupported_meeting_mode,
)
from ui_qt.widgets.tabbed_content import TabbedContentWidget


class TestMeetingPlatformPolicy(unittest.TestCase):
    def test_windows_and_modern_macos_are_supported(self):
        self.assertTrue(meeting_mode_supported("win32"))
        self.assertTrue(meeting_mode_supported("win64"))
        with patch("meeting.platform.platform_module.mac_ver",
                   return_value=("13.0", ("", "", ""), "arm64")):
            self.assertTrue(meeting_mode_supported("darwin"))
        # Linux implementation is ready, but public promotion stays gated.
        self.assertFalse(meeting_mode_supported("linux", machine="x86_64"))
        self.assertFalse(meeting_mode_supported("linux2", machine="aarch64"))
        self.assertFalse(meeting_mode_supported("linux", machine="i686"))
        self.assertFalse(meeting_mode_supported("freebsd14"))

    def test_macos_before_screencapturekit_audio_is_unsupported(self):
        with patch("meeting.platform.platform_module.mac_ver",
                   return_value=("12.7.1", ("", "", ""), "x86_64")):
            self.assertFalse(meeting_mode_supported("darwin"))
        with patch("meeting.platform.platform_module.mac_ver",
                   return_value=("13.0", ("", "", ""), "arm64")):
            self.assertTrue(meeting_mode_supported("darwin"))

    def test_undetectable_macos_version_fails_closed(self):
        # Unknown macOS versions must not be promoted as supported.
        with patch("meeting.platform.platform_module.mac_ver",
                   return_value=("", ("", "", ""), "")):
            self.assertFalse(meeting_mode_supported("darwin"))

    def test_linux_implementation_ready_is_separate_from_public_support(self):
        from meeting.platform import linux_meeting_implementation_ready
        self.assertTrue(linux_meeting_implementation_ready("x86_64"))
        self.assertTrue(linux_meeting_implementation_ready("aarch64"))
        self.assertFalse(linux_meeting_implementation_ready("i686"))
        self.assertFalse(meeting_mode_supported("linux", machine="x86_64"))

    def test_os_display_names(self):
        self.assertEqual(meeting_unsupported_os_name("darwin"), "macOS")
        self.assertEqual(meeting_unsupported_os_name("linux"), "Linux")
        self.assertEqual(meeting_unsupported_os_name("linux2"), "Linux")
        self.assertEqual(meeting_unsupported_os_name("freebsd14"), "this platform")


class TestUnsupportedMeetingDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_continue_stays_disabled_until_every_box_is_checked(self):
        dialog = MeetingUnsupportedPlatformDialog(platform="darwin")
        continue_btn = dialog.findChild(
            QPushButton, "meetingUnsupportedContinueButton"
        )
        go_back = dialog.findChild(QPushButton, "meetingUnsupportedGoBackButton")
        boxes = [
            dialog.findChild(QCheckBox, "meetingUnsupportedAckUnsupported"),
            dialog.findChild(QCheckBox, "meetingUnsupportedAckNoSystemAudio"),
            dialog.findChild(QCheckBox, "meetingUnsupportedAckTryAnyway"),
        ]
        self.assertIsNotNone(continue_btn)
        self.assertIsNotNone(go_back)
        self.assertTrue(all(box is not None for box in boxes))
        self.assertFalse(continue_btn.isEnabled())
        self.assertIn("macOS", dialog.windowTitle())
        self.assertIn("macOS", boxes[0].text())

        boxes[0].setChecked(True)
        boxes[1].setChecked(True)
        self.assertFalse(continue_btn.isEnabled())
        boxes[2].setChecked(True)
        self.assertTrue(continue_btn.isEnabled())

        continue_btn.click()
        self.assertEqual(
            dialog.result_action,
            MeetingUnsupportedPlatformDialog.RESULT_CONTINUE,
        )

    def test_go_back_keeps_cancel_result(self):
        dialog = MeetingUnsupportedPlatformDialog(
            platform="linux", implementation_ready=False
        )
        go_back = dialog.findChild(QPushButton, "meetingUnsupportedGoBackButton")
        go_back.click()
        self.assertEqual(
            dialog.result_action, MeetingUnsupportedPlatformDialog.RESULT_CANCEL
        )
        self.assertIn("Linux", dialog.windowTitle())

    def test_linux_preview_copy_does_not_claim_no_capture_path(self):
        dialog = MeetingUnsupportedPlatformDialog(
            platform="linux",
            machine="x86_64",
            implementation_ready=True,
        )
        body = dialog.body_label.text().lower()
        self.assertIn("preview", dialog.windowTitle().lower() + body)
        self.assertIn("not publicly supported", body)
        self.assertIn("implemented", body)
        self.assertNotIn("has no supported capture path", body)
        self.assertNotIn(
            "system audio will not be captured",
            dialog.ack_no_system_audio.text().lower(),
        )
        self.assertIn("microphone-only", dialog.ack_no_system_audio.text().lower())

    def test_unsupported_arch_linux_keeps_no_path_wording(self):
        dialog = MeetingUnsupportedPlatformDialog(
            platform="linux",
            machine="i686",
            implementation_ready=False,
        )
        body = dialog.body_label.text().lower()
        self.assertIn("no supported capture path", body)
        self.assertIn(
            "system audio will not be captured",
            dialog.ack_no_system_audio.text().lower(),
        )

    def test_acknowledge_helper_skips_dialog_on_windows(self):
        with patch(
            "ui_qt.dialogs.meeting_unsupported_dialog"
            ".MeetingUnsupportedPlatformDialog"
        ) as dialog_cls:
            self.assertTrue(acknowledge_unsupported_meeting_mode(platform="win32"))
            dialog_cls.assert_not_called()

    def test_acknowledge_helper_skips_dialog_after_saved_ack(self):
        with patch(
            "ui_qt.dialogs.meeting_unsupported_dialog"
            ".resolve_meeting_unsupported_platform_ack",
            return_value=True,
        ), patch(
            "ui_qt.dialogs.meeting_unsupported_dialog"
            ".MeetingUnsupportedPlatformDialog"
        ) as dialog_cls:
            self.assertTrue(acknowledge_unsupported_meeting_mode(platform="darwin"))
            dialog_cls.assert_not_called()

    def test_acknowledge_helper_persists_continue(self):
        class _Accepted:
            result_action = MeetingUnsupportedPlatformDialog.RESULT_CONTINUE

            def exec(self):
                return 1

        with patch(
            "ui_qt.dialogs.meeting_unsupported_dialog.meeting_mode_supported",
            return_value=False,
        ), patch(
            "ui_qt.dialogs.meeting_unsupported_dialog"
            ".resolve_meeting_unsupported_platform_ack",
            return_value=False,
        ), patch(
            "ui_qt.dialogs.meeting_unsupported_dialog"
            ".MeetingUnsupportedPlatformDialog",
            return_value=_Accepted(),
        ), patch.object(settings_manager, "save_setting") as save:
            self.assertTrue(acknowledge_unsupported_meeting_mode())
            save.assert_called_once_with(
                SettingsKey.MEETING_UNSUPPORTED_PLATFORM_ACK, True
            )

    def test_acknowledge_helper_does_not_persist_cancel(self):
        class _Cancelled:
            result_action = MeetingUnsupportedPlatformDialog.RESULT_CANCEL

            def exec(self):
                return 0

        with patch(
            "ui_qt.dialogs.meeting_unsupported_dialog.meeting_mode_supported",
            return_value=False,
        ), patch(
            "ui_qt.dialogs.meeting_unsupported_dialog"
            ".resolve_meeting_unsupported_platform_ack",
            return_value=False,
        ), patch(
            "ui_qt.dialogs.meeting_unsupported_dialog"
            ".MeetingUnsupportedPlatformDialog",
            return_value=_Cancelled(),
        ), patch.object(settings_manager, "save_setting") as save:
            self.assertFalse(acknowledge_unsupported_meeting_mode())
            save.assert_not_called()


class TestMeetingTabUnsupportedLock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _make_tabs(self, *, supported, acked=False, last_tab=0):
        self._patches = [
            patch(
                "ui_qt.widgets.tabbed_content.meeting_mode_supported",
                return_value=supported,
            ),
            patch(
                "ui_qt.widgets.tabbed_content"
                ".resolve_meeting_unsupported_platform_ack",
                return_value=acked,
            ),
            patch.object(
                settings_manager,
                "load_all_settings",
                return_value={SettingsKey.LAST_TAB_INDEX: last_tab},
            ),
            patch.object(settings_manager, "save_setting"),
        ]
        for item in self._patches:
            item.start()
        tabs = TabbedContentWidget()
        tabs.resize(720, 240)
        tabs.show()
        self.app.processEvents()
        self.addCleanup(tabs.deleteLater)
        return tabs

    def tearDown(self):
        for item in getattr(self, "_patches", []):
            item.stop()
        self.app.processEvents()

    def test_windows_tab_is_not_muted_or_locked(self):
        tabs = self._make_tabs(supported=True)
        self.assertFalse(tabs.meeting_tab_is_locked())
        self.assertNotEqual(tabs.tab_bar.property("unsupportedMeeting"), True)
        self.assertEqual(tabs.tab_bar.tabToolTip(tabs.TAB_MEETING_MODE), "")

    def test_unsupported_tab_is_muted_and_locked_until_ack(self):
        with patch(
            "meeting.platform.linux_meeting_implementation_ready",
            return_value=False,
        ):
            tabs = self._make_tabs(supported=False)
            self.assertTrue(tabs.meeting_tab_is_locked())
            self.assertEqual(tabs.tab_bar.property("unsupportedMeeting"), True)
            tip = tabs.tab_bar.tabToolTip(tabs.TAB_MEETING_MODE).lower()
            self.assertTrue(
                "not supported" in tip or "unsupported" in tip or "preview" in tip,
                tip,
            )

    def test_saved_ack_unlocks_but_keeps_tab_muted(self):
        with patch(
            "meeting.platform.linux_meeting_implementation_ready",
            return_value=False,
        ):
            tabs = self._make_tabs(supported=False, acked=True)
            self.assertFalse(tabs.meeting_tab_is_locked())
            self.assertEqual(tabs.tab_bar.property("unsupportedMeeting"), True)
            tip = tabs.tab_bar.tabToolTip(tabs.TAB_MEETING_MODE).lower()
            self.assertTrue(
                "unsupported" in tip or "preview" in tip or "not supported" in tip,
                tip,
            )

    def test_linux_preview_tab_tooltip(self):
        with patch(
            "meeting.platform.linux_meeting_implementation_ready",
            return_value=True,
        ), patch("sys.platform", "linux"):
            tabs = self._make_tabs(supported=False)
            tip = tabs.tab_bar.tabToolTip(tabs.TAB_MEETING_MODE).lower()
            self.assertIn("preview", tip)
            self.assertNotIn("system audio is unavailable", tip)

    def test_last_meeting_tab_is_not_restored_while_locked(self):
        tabs = self._make_tabs(
            supported=False, last_tab=TabbedContentWidget.TAB_MEETING_MODE
        )
        self.assertEqual(tabs.current_index(), TabbedContentWidget.TAB_QUICK_RECORD)

    def test_last_meeting_tab_is_restored_after_ack(self):
        tabs = self._make_tabs(
            supported=False,
            acked=True,
            last_tab=TabbedContentWidget.TAB_MEETING_MODE,
        )
        self.assertEqual(tabs.current_index(), TabbedContentWidget.TAB_MEETING_MODE)

    def test_clicking_locked_tab_without_ack_stays_put(self):
        tabs = self._make_tabs(supported=False)
        with patch(
            "ui_qt.dialogs.meeting_unsupported_dialog"
            ".acknowledge_unsupported_meeting_mode",
            return_value=False,
        ) as ack:
            rect = tabs.tab_bar.tabRect(TabbedContentWidget.TAB_MEETING_MODE)
            QTest.mouseClick(
                tabs.tab_bar,
                Qt.MouseButton.LeftButton,
                pos=rect.center(),
            )
            self.app.processEvents()
            ack.assert_called_once()
        self.assertEqual(tabs.current_index(), TabbedContentWidget.TAB_QUICK_RECORD)
        self.assertTrue(tabs.meeting_tab_is_locked())

    def test_clicking_locked_tab_with_ack_opens_meeting_mode(self):
        tabs = self._make_tabs(supported=False)
        with patch(
            "ui_qt.dialogs.meeting_unsupported_dialog"
            ".acknowledge_unsupported_meeting_mode",
            return_value=True,
        ):
            rect = tabs.tab_bar.tabRect(TabbedContentWidget.TAB_MEETING_MODE)
            QTest.mouseClick(
                tabs.tab_bar,
                Qt.MouseButton.LeftButton,
                pos=rect.center(),
            )
            self.app.processEvents()
        self.assertEqual(tabs.current_index(), TabbedContentWidget.TAB_MEETING_MODE)
        self.assertFalse(tabs.meeting_tab_is_locked())
        self.assertEqual(tabs.tab_bar.property("unsupportedMeeting"), True)

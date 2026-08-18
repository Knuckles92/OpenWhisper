"""Qt tests for the meeting audio-upload consent dialog."""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel, QPushButton

from ui_qt.dialogs.meeting_audio_consent_dialog import MeetingAudioConsentDialog
from ui_qt.dialogs.meeting_consent_dialog import MeetingConsentDialog


class _QtTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])


class TestAudioConsentDialog(_QtTestCase):
    def _button(self, dialog, name):
        return dialog.findChild(QPushButton, name)

    def test_buttons_and_default_result(self):
        dialog = MeetingAudioConsentDialog()
        self.assertEqual(dialog.result_action, MeetingAudioConsentDialog.RESULT_CANCEL)
        self.assertIsNotNone(self._button(dialog, "meetingAudioConsentNotNowButton"))
        self.assertIsNotNone(self._button(dialog, "meetingAudioConsentEnableButton"))

    def test_enable_records_result(self):
        dialog = MeetingAudioConsentDialog()
        self._button(dialog, "meetingAudioConsentEnableButton").click()
        self.assertEqual(dialog.result_action, MeetingAudioConsentDialog.RESULT_ENABLE)

    def test_copy_mentions_system_audio_and_openai(self):
        dialog = MeetingAudioConsentDialog()
        body = dialog.findChild(QLabel, "consentBodyLabel")
        text = body.text() if body is not None else ""
        self.assertIn("system-audio", text)
        self.assertIn("OpenAI", text)
        self.assertIn("Others", text)


class TestCloudConsentCopy(_QtTestCase):
    def test_cloud_consent_does_not_claim_audio_never_leaves(self):
        dialog = MeetingConsentDialog()
        body = dialog.findChild(QLabel, "consentBodyLabel")
        text = body.text() if body is not None else ""
        self.assertNotIn("Your audio never leaves this computer", text)
        self.assertIn("does not upload audio", text)

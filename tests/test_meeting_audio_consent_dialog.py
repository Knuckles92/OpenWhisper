"""Qt tests for the meeting audio-upload consent dialog."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel, QPushButton

from ui_qt.dialogs.meeting_audio_consent_dialog import MeetingAudioConsentDialog
from ui_qt.dialogs.meeting_consent_dialog import MeetingConsentDialog


class _QtTestCase:
    @classmethod
    def setup_class(cls):
        cls.app = QApplication.instance() or QApplication([])


class TestAudioConsentDialog(_QtTestCase):
    def _button(self, dialog, name):
        return dialog.findChild(QPushButton, name)

    def test_buttons_and_default_result(self):
        dialog = MeetingAudioConsentDialog()
        assert dialog.result_action == MeetingAudioConsentDialog.RESULT_CANCEL
        assert self._button(dialog, "meetingAudioConsentNotNowButton") is not None
        assert self._button(dialog, "meetingAudioConsentEnableButton") is not None

    def test_enable_records_result(self):
        dialog = MeetingAudioConsentDialog()
        self._button(dialog, "meetingAudioConsentEnableButton").click()
        assert dialog.result_action == MeetingAudioConsentDialog.RESULT_ENABLE

    def test_copy_mentions_system_audio_and_openai(self):
        dialog = MeetingAudioConsentDialog()
        body = dialog.findChild(QLabel, "consentBodyLabel")
        text = body.text() if body is not None else ""
        assert "system-audio" in text
        assert "OpenAI" in text
        assert "Others" in text


class TestCloudConsentCopy(_QtTestCase):
    def test_cloud_consent_does_not_claim_audio_never_leaves(self):
        dialog = MeetingConsentDialog()
        body = dialog.findChild(QLabel, "consentBodyLabel")
        text = body.text() if body is not None else ""
        assert "Your audio never leaves this computer" not in text
        assert "does not upload audio" in text
        assert "Past-meeting recall" in text

    def test_cloud_consent_names_local_endpoint(self):
        dialog = MeetingConsentDialog(
            destination="your local server at 127.0.0.1:1234",
            remote=False,
        )
        body = dialog.findChild(QLabel, "consentBodyLabel")
        text = body.text() if body is not None else ""
        assert "127.0.0.1:1234" in text
        assert "does not leave this machine" in text
        assert "OpenRouter" not in text

    def test_cloud_consent_names_remote_endpoint(self):
        dialog = MeetingConsentDialog(
            destination="Work gateway (llm.example.com)",
            remote=True,
        )
        body = dialog.findChild(QLabel, "consentBodyLabel")
        text = body.text() if body is not None else ""
        assert "Work gateway (llm.example.com)" in text
        assert "leaves this computer" in text
        assert "OpenRouter" not in text

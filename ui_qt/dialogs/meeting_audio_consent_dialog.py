"""One-time consent dialog for uploading meeting system audio to OpenAI.

Shown when the user switches Speaker identification to OpenAI in Settings.
Explains that the loopback (Others) recording leaves the machine after the
meeting; microphone audio and live transcription stay local.
"""
import logging
from typing import Final

from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QVBoxLayout

from ui_qt.widgets import Button, PrimaryButton

logger = logging.getLogger(__name__)


class MeetingAudioConsentDialog(QDialog):
    RESULT_CANCEL: Final[str] = "cancel"
    RESULT_ENABLE: Final[str] = "enable"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("meetingAudioConsentDialog")
        self.result_action = self.RESULT_CANCEL

        self.setWindowTitle("Upload system audio for speaker identification")
        self.setMinimumWidth(480)
        self.setModal(True)

        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        title = QLabel("Send system audio to OpenAI after each meeting?")
        title.setObjectName("headerLabel")
        layout.addWidget(title)

        body = QLabel(
            "OpenAI speaker identification uploads the system-audio "
            "recording — the \"Others\" channel, other participants' "
            "voices — to OpenAI after the meeting ends. OpenAI labels "
            "who spoke when. The local transcript text is kept; only "
            "speaker labels change.\n\n"
            "Your microphone recording and live transcription stay on "
            "this computer. This is separate from cloud intelligence, "
            "which sends transcript text and dashboard state but not "
            "audio.\n\n"
            "An OpenAI API key is required. You can switch back to "
            "on-device speaker labels in Settings at any time."
        )
        body.setObjectName("consentBodyLabel")
        body.setWordWrap(True)
        layout.addWidget(body)

        layout.addSpacing(8)
        button_layout = QHBoxLayout()
        button_layout.setSpacing(8)
        button_layout.addStretch()

        not_now_btn = Button("Not now")
        not_now_btn.setObjectName("meetingAudioConsentNotNowButton")
        not_now_btn.clicked.connect(self.reject)
        button_layout.addWidget(not_now_btn)

        enable_btn = PrimaryButton("Allow audio upload")
        enable_btn.setObjectName("meetingAudioConsentEnableButton")
        enable_btn.clicked.connect(lambda: self._finish(self.RESULT_ENABLE))
        button_layout.addWidget(enable_btn)
        enable_btn.setDefault(True)

        layout.addLayout(button_layout)

    def _finish(self, action: str):
        self.result_action = action
        self.accept()

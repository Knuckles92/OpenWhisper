"""One-time consent dialog for Meeting Mode cloud intelligence.

Shown before the first cloud-enabled meeting (and again from the cloud
toggle while consent has not been given). Explains exactly what leaves the
machine — transcript text and dashboard state sent to OpenRouter — and what
never does: audio.
"""
import logging
from typing import Final

from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QVBoxLayout

from ui_qt.widgets import Button, PrimaryButton

logger = logging.getLogger(__name__)


class MeetingConsentDialog(QDialog):
    """Modal dialog asking the user to approve cloud meeting intelligence."""

    RESULT_CANCEL: Final[str] = "cancel"
    RESULT_ENABLE: Final[str] = "enable"

    def __init__(self, parent=None):
        """Initialize the consent dialog.

        Args:
            parent: Parent widget (normally the main window).
        """
        super().__init__(parent)
        self.setObjectName("meetingConsentDialog")
        self.result_action = self.RESULT_CANCEL

        self.setWindowTitle("Enable Cloud Intelligence")
        self.setMinimumWidth(480)
        self.setModal(True)

        self._setup_ui()

    def _setup_ui(self):
        """Build the dialog copy and action buttons."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        title = QLabel("Enable cloud intelligence for meetings?")
        title.setObjectName("headerLabel")
        layout.addWidget(title)

        body = QLabel(
            "Cloud intelligence keeps live meeting insights — topic, key "
            "points, decisions, action items, and questions — updated on the "
            "dashboard while you talk.\n\n"
            "To do this, the meeting transcript text and the dashboard state "
            "are sent to OpenRouter (openrouter.ai), using the model chosen "
            "in Settings.\n\n"
            "Your audio never leaves this computer. Recording and "
            "transcription always run locally, with or without cloud "
            "intelligence."
        )
        body.setObjectName("consentBodyLabel")
        body.setWordWrap(True)
        layout.addWidget(body)

        toggle_note = QLabel(
            "You can turn this on or off for each meeting with the "
            '"Cloud intelligence" toggle. Without it, meetings are '
            "transcript-only."
        )
        toggle_note.setObjectName("infoLabel")
        toggle_note.setWordWrap(True)
        layout.addWidget(toggle_note)

        layout.addSpacing(8)
        button_layout = QHBoxLayout()
        button_layout.setSpacing(8)
        button_layout.addStretch()

        not_now_btn = Button("Not now")
        not_now_btn.setObjectName("meetingConsentNotNowButton")
        not_now_btn.clicked.connect(self.reject)
        button_layout.addWidget(not_now_btn)

        enable_btn = PrimaryButton("Enable cloud intelligence")
        enable_btn.setObjectName("meetingConsentEnableButton")
        enable_btn.clicked.connect(lambda: self._finish(self.RESULT_ENABLE))
        button_layout.addWidget(enable_btn)
        enable_btn.setDefault(True)

        layout.addLayout(button_layout)

    def _finish(self, action: str):
        """Record the chosen action and accept the dialog."""
        self.result_action = action
        self.accept()

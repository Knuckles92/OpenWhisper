"""One-time consent dialog for Meeting Mode AI insights.

Shown before the first meeting with AI insights on (and again from the AI
insights switch while consent has not been given). Explains exactly where
transcript text and dashboard state go — the selected text endpoint, which may
be remote or on this computer — and what never leaves: audio.
"""
import logging
from typing import Final, Optional

from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QVBoxLayout

from ui_qt.widgets import Button, PrimaryButton

logger = logging.getLogger(__name__)


class MeetingConsentDialog(QDialog):
    RESULT_CANCEL: Final[str] = "cancel"
    RESULT_ENABLE: Final[str] = "enable"

    def __init__(
        self,
        parent=None,
        destination: Optional[str] = None,
        remote: Optional[bool] = None,
    ):
        """Initialize the consent dialog.

        Args:
            parent: Parent widget (normally the main window).
            destination: Human-readable endpoint name/host. Resolved from
                the current meeting profile when omitted.
            remote: Whether transcript text would leave this machine.
                Resolved from the current meeting profile when omitted.
        """
        super().__init__(parent)
        self.setObjectName("meetingConsentDialog")
        self.result_action = self.RESULT_CANCEL
        self.destination, self.remote = self._resolve_destination(
            destination, remote
        )

        self.setWindowTitle("Turn On AI Insights")
        self.setAccessibleName("Turn on AI insights for meetings")
        self.setAccessibleDescription(
            "Consent choice for sending transcript text and dashboard state "
            f"to {self.destination}. Meeting audio is not uploaded."
        )
        self.setMinimumWidth(480)
        self.setModal(True)

        self._setup_ui()

    @staticmethod
    def _resolve_destination(
        destination: Optional[str],
        remote: Optional[bool],
    ) -> tuple:
        """Fill destination copy from the selected meeting endpoint."""
        if destination and remote is not None:
            return destination, remote
        try:
            from services.settings import (
                resolve_meeting_llm_profile,
                settings_manager,
            )
            from services.text_llm import (
                consent_destination,
                destination_is_remote,
            )

            profile = resolve_meeting_llm_profile(
                settings_manager.load_all_settings()
            )
            if not destination:
                destination = consent_destination(profile)
            if remote is None:
                remote = destination_is_remote(profile)
        except Exception:
            destination = destination or "the selected text endpoint"
            remote = True if remote is None else remote
        return destination, bool(remote)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        title = QLabel("Turn on AI insights for meetings?")
        title.setObjectName("headerLabel")
        layout.addWidget(title)

        if self.remote:
            location = (
                f"To do this, the meeting transcript text and the dashboard "
                f"state are sent to {self.destination}, using the model "
                f"chosen in Settings → Meeting Mode → Intelligence.\n\n"
                "That destination is remote, so transcript text leaves this "
                "computer."
            )
        else:
            location = (
                f"To do this, the meeting transcript text and the dashboard "
                f"state are sent to {self.destination}, using the model "
                f"chosen in Settings → Meeting Mode → Intelligence.\n\n"
                "That destination is on this computer, so transcript text "
                "does not leave this machine."
            )

        body = QLabel(
            "AI insights keep the meeting's topic, key points, decisions, "
            "action items, and questions updated on the dashboard while you "
            "talk.\n\n"
            f"{location}\n\n"
            "AI insights do not upload audio. Recording and "
            "transcription stay local. Speaker identification is a "
            "separate setting and, if enabled, uploads the system-audio "
            "recording after the meeting.\n\n"
            "Past-meeting recall is also off by default. If you later "
            "enable it in Settings, excerpts from earlier meetings may "
            "be sent so the agent can recall prior names and decisions."
        )
        body.setObjectName("consentBodyLabel")
        body.setWordWrap(True)
        layout.addWidget(body)

        toggle_note = QLabel(
            "You can turn this on or off for each meeting with the "
            '"AI insights" switch. Without it, meetings are '
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

        enable_btn = PrimaryButton("Turn on AI insights")
        enable_btn.setObjectName("meetingConsentEnableButton")
        enable_btn.clicked.connect(lambda: self._finish(self.RESULT_ENABLE))
        button_layout.addWidget(enable_btn)
        enable_btn.setDefault(True)

        layout.addLayout(button_layout)

    def _finish(self, action: str):
        """Record the chosen action and accept the dialog."""
        self.result_action = action
        self.accept()

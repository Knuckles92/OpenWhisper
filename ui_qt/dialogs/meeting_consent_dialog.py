"""One-time consent dialog for Meeting Mode cloud intelligence.

Shown before the first cloud-enabled meeting (and again from the cloud
toggle while consent has not been given). Explains exactly what leaves the
machine — transcript text and dashboard state sent to the selected text
endpoint — and what never does: audio.
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

        self.setWindowTitle("Enable Cloud Intelligence")
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

        title = QLabel("Enable cloud intelligence for meetings?")
        title.setObjectName("headerLabel")
        layout.addWidget(title)

        if self.remote:
            location = (
                f"To do this, the meeting transcript text and the dashboard "
                f"state are sent to {self.destination}, using the model "
                f"chosen in Model Manager.\n\n"
                "That destination is remote, so transcript text leaves this "
                "computer."
            )
        else:
            location = (
                f"To do this, the meeting transcript text and the dashboard "
                f"state are sent to {self.destination}, using the model "
                f"chosen in Model Manager.\n\n"
                "That destination is on this computer, so transcript text "
                "does not leave this machine."
            )

        body = QLabel(
            "Cloud intelligence keeps live meeting insights — topic, key "
            "points, decisions, action items, and questions — updated on the "
            "dashboard while you talk.\n\n"
            f"{location}\n\n"
            "Cloud intelligence does not upload audio. Recording and "
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

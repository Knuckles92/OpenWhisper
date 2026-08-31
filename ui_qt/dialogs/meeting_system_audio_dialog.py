"""Prompt for the macOS grant Meeting Mode needs to capture system audio.

Shown before a meeting starts when the Screen Recording grant is missing.
Without it ScreenCaptureKit yields nothing and the meeting quietly records the
microphone only -- a failure the user would not discover until they read the
transcript, so it is worth a modal.

macOS raises its own dialog only the first time a given binary asks. After a
denial the request returns immediately, which is why this dialog always offers
the System Settings deep link as well.
"""
from __future__ import annotations

import logging
from typing import Final, Optional

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QVBoxLayout

from meeting.platform import (
    SCREEN_RECORDING_SETTINGS_URL,
    request_system_audio_permission,
    system_audio_permission_granted,
    system_audio_permission_required,
)
from ui_qt.widgets import Button, PrimaryButton

logger = logging.getLogger(__name__)


class MeetingSystemAudioPermissionDialog(QDialog):
    RESULT_CANCEL: Final[str] = "cancel"
    RESULT_CONTINUE: Final[str] = "continue"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("meetingSystemAudioDialog")
        self.result_action = self.RESULT_CANCEL

        self.setWindowTitle("Allow OpenWhisper to record system audio")
        self.setAccessibleName("Allow system-audio recording")
        self.setAccessibleDescription(
            "macOS Screen Recording permission is needed to capture other speakers. "
            "Choose System Settings, microphone only, or go back."
        )
        self.setMinimumWidth(520)
        self.setModal(True)

        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        title = QLabel("Meeting Mode needs permission to record system audio")
        title.setObjectName("headerLabel")
        title.setWordWrap(True)
        layout.addWidget(title)

        body = QLabel(
            "macOS captures the other side of a call through Screen "
            "Recording. Without it, this meeting records your microphone "
            "only \u2014 you would be the only speaker in the transcript.\n\n"
            "Grant it under Privacy & Security > Screen & System Audio "
            "Recording. macOS attaches the grant to the app you launch, so a "
            "source checkout is listed as Python rather than OpenWhisper, and "
            "you may need to quit and relaunch before it takes effect."
        )
        body.setObjectName("consentBodyLabel")
        body.setWordWrap(True)
        layout.addWidget(body)

        layout.addSpacing(8)
        button_layout = QHBoxLayout()
        button_layout.setSpacing(8)

        settings_btn = Button("Open System Settings")
        settings_btn.setObjectName("meetingSystemAudioSettingsButton")
        settings_btn.setAutoDefault(False)
        settings_btn.clicked.connect(self._open_settings)
        button_layout.addWidget(settings_btn)

        button_layout.addStretch()

        continue_btn = Button("Record microphone only")
        continue_btn.setObjectName("meetingSystemAudioContinueButton")
        continue_btn.setAutoDefault(False)
        continue_btn.clicked.connect(lambda: self._finish(self.RESULT_CONTINUE))
        button_layout.addWidget(continue_btn)

        cancel_btn = PrimaryButton("Go back")
        cancel_btn.setObjectName("meetingSystemAudioGoBackButton")
        cancel_btn.setDefault(True)
        cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(cancel_btn)

        layout.addLayout(button_layout)

    def _open_settings(self) -> None:
        QDesktopServices.openUrl(QUrl(SCREEN_RECORDING_SETTINGS_URL))

    def _finish(self, action: str):
        self.result_action = action
        self.accept()


# Bound at import time so tests can mock the dialog class without breaking
# the continue-vs-cancel comparison in ensure_meeting_system_audio_permission.
_CONTINUE = MeetingSystemAudioPermissionDialog.RESULT_CONTINUE


def ensure_meeting_system_audio_permission(parent=None) -> bool:
    """Return True when a meeting may start on this machine.

    Requests the grant first, since macOS shows its own dialog on the first
    ask and that is a better experience than being sent to System Settings.
    Only an outright refusal reaches our modal, where the user can still
    choose a deliberate microphone-only meeting.

    Args:
        parent: Widget to parent the modal dialog to.

    Returns:
        True when the grant is held, or the user accepted a mic-only meeting.
    """
    if not system_audio_permission_required():
        return True
    if system_audio_permission_granted():
        return True
    if request_system_audio_permission():
        return True

    logger.warning(
        "Screen Recording permission denied; offering a mic-only meeting"
    )
    dialog = MeetingSystemAudioPermissionDialog(parent=parent)
    dialog.exec()
    return getattr(dialog, "result_action", None) == _CONTINUE

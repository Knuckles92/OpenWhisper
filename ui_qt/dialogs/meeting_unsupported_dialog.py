"""First-time acknowledgement that Meeting Mode is unsupported or unattested.

Shown when a user on a platform without public Meeting Mode promotion — a Mac
too old for ScreenCaptureKit, an unsupported Linux architecture, or Linux
x86_64/aarch64 before hardware attestation — opens Meeting Mode or starts a
meeting from the tray or a hotkey, before they have accepted the warning.
Continue stays disabled until every explicit checkbox is ticked.
"""
from __future__ import annotations

import logging
from typing import Final, Optional

from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
)

from meeting.platform import (
    linux_meeting_implementation_ready,
    meeting_mode_supported,
    meeting_unsupported_os_name,
)
from services.settings import (
    MEETING_LINUX_PREVIEW_ACK_VERSION,
    SettingsKey,
    resolve_meeting_linux_preview_ack,
    resolve_meeting_unsupported_platform_ack,
    settings_manager,
)
from ui_qt.widgets import Button, PrimaryButton

logger = logging.getLogger(__name__)


class MeetingUnsupportedPlatformDialog(QDialog):
    RESULT_CANCEL: Final[str] = "cancel"
    RESULT_CONTINUE: Final[str] = "continue"

    def __init__(
        self,
        parent=None,
        platform: Optional[str] = None,
        *,
        machine: Optional[str] = None,
        implementation_ready: Optional[bool] = None,
    ):
        super().__init__(parent)
        self.setObjectName("meetingUnsupportedDialog")
        self.result_action = self.RESULT_CANCEL
        self.os_name = meeting_unsupported_os_name(platform)
        self._platform = platform
        if implementation_ready is None:
            import sys

            host = platform or sys.platform
            if str(host).startswith("linux"):
                self._implementation_ready = linux_meeting_implementation_ready(
                    machine
                )
            else:
                self._implementation_ready = False
        else:
            self._implementation_ready = bool(implementation_ready)

        if self._implementation_ready:
            self.setWindowTitle(
                f"Meeting Mode on {self.os_name} is a preview"
            )
        else:
            self.setWindowTitle(
                f"Meeting Mode is not supported on {self.os_name}"
            )
        self.setMinimumWidth(500)
        self.setModal(True)

        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        if self._implementation_ready:
            title = QLabel(
                f"Meeting Mode on {self.os_name} is implemented but not "
                "publicly supported yet"
            )
        else:
            title = QLabel(
                f"Meeting Mode is not supported on {self.os_name}"
            )
        title.setObjectName("headerLabel")
        title.setWordWrap(True)
        layout.addWidget(title)

        self.body_label = QLabel(self._body_text())
        self.body_label.setObjectName("consentBodyLabel")
        self.body_label.setWordWrap(True)
        layout.addWidget(self.body_label)

        if self._implementation_ready:
            self.ack_unsupported = QCheckBox(
                f"I understand Meeting Mode on {self.os_name} is a preview "
                "and not publicly supported yet"
            )
            self.ack_no_system_audio = QCheckBox(
                "I understand capture may fail diagnostics and fall back to "
                "microphone-only"
            )
            self.ack_try_anyway = QCheckBox(
                "I want to try the preview path anyway"
            )
        else:
            self.ack_unsupported = QCheckBox(
                f"I understand Meeting Mode is not supported on {self.os_name}"
            )
            self.ack_no_system_audio = QCheckBox(
                "I understand system audio will not be captured"
            )
            self.ack_try_anyway = QCheckBox(
                "I want to try it anyway, knowing it is unsupported"
            )

        self.ack_unsupported.setObjectName("meetingUnsupportedAckUnsupported")
        layout.addWidget(self.ack_unsupported)

        self.ack_no_system_audio.setObjectName(
            "meetingUnsupportedAckNoSystemAudio"
        )
        layout.addWidget(self.ack_no_system_audio)

        self.ack_try_anyway.setObjectName("meetingUnsupportedAckTryAnyway")
        layout.addWidget(self.ack_try_anyway)

        for box in (
            self.ack_unsupported,
            self.ack_no_system_audio,
            self.ack_try_anyway,
        ):
            box.toggled.connect(self._update_continue_enabled)

        layout.addSpacing(8)
        button_layout = QHBoxLayout()
        button_layout.setSpacing(8)
        button_layout.addStretch()

        self.continue_btn = Button("Continue anyway")
        self.continue_btn.setObjectName("meetingUnsupportedContinueButton")
        self.continue_btn.setEnabled(False)
        self.continue_btn.setAutoDefault(False)
        self.continue_btn.setDefault(False)
        self.continue_btn.clicked.connect(
            lambda: self._finish(self.RESULT_CONTINUE)
        )
        button_layout.addWidget(self.continue_btn)

        go_back_btn = PrimaryButton("Go back")
        go_back_btn.setObjectName("meetingUnsupportedGoBackButton")
        go_back_btn.setDefault(True)
        go_back_btn.clicked.connect(self.reject)
        button_layout.addWidget(go_back_btn)

        layout.addLayout(button_layout)

    def _body_text(self) -> str:
        if self._implementation_ready:
            return (
                "Meeting Mode can record microphone and system audio on "
                f"{self.os_name} x86_64/aarch64 through PulseAudio or "
                "PipeWire-Pulse, but this path is not publicly supported until "
                "the hardware release matrix is attested.\n\n"
                "Capture is implemented and may work after diagnostics. It can "
                "still fail open/monitor checks and fall back to microphone-only "
                "for a single meeting. Check every box below if you still want "
                "to try the preview path."
            )
        return (
            "Meeting Mode records microphone and system audio on Windows, "
            f"macOS 13+, and Linux x86_64/aarch64. {self.os_name} has no "
            "supported capture path. System audio will not be captured, and a "
            "meeting here may fail or run microphone-only.\n\n"
            "This is unsupported. Check every box below if you still want "
            "to try it."
        )

    def _update_continue_enabled(self) -> None:
        self.continue_btn.setEnabled(
            self.ack_unsupported.isChecked()
            and self.ack_no_system_audio.isChecked()
            and self.ack_try_anyway.isChecked()
        )

    def _finish(self, action: str):
        self.result_action = action
        self.accept()


# Bound at import time so tests can mock the dialog class without breaking
# the continue-vs-cancel comparison in acknowledge_unsupported_meeting_mode.
_ACK_CONTINUE = MeetingUnsupportedPlatformDialog.RESULT_CONTINUE


def acknowledge_unsupported_meeting_mode(
    parent=None,
    platform: Optional[str] = None,
    machine: Optional[str] = None,
) -> bool:
    """Return True when Meeting Mode may proceed on this platform.

    Windows is always allowed. On other platforms the first call shows the
    acknowledgement dialog; a granted answer is persisted so later calls
    skip it.

    Args:
        parent: Widget to parent the modal dialog to.
        platform: Optional ``sys.platform`` override for tests.

    Returns:
        True when the platform is supported or the user accepted the warning.
    """
    import sys

    host = platform or sys.platform
    if meeting_mode_supported(host, machine=machine):
        return True
    linux_preview = (
        str(host).startswith("linux")
        and linux_meeting_implementation_ready(machine)
    )
    if linux_preview:
        acknowledged = resolve_meeting_linux_preview_ack()
    else:
        acknowledged = resolve_meeting_unsupported_platform_ack()
    if acknowledged:
        return True

    dialog = MeetingUnsupportedPlatformDialog(
        parent=parent,
        platform=host,
        machine=machine,
        implementation_ready=linux_preview,
    )
    dialog.exec()
    if getattr(dialog, "result_action", None) != _ACK_CONTINUE:
        return False

    try:
        if linux_preview:
            settings_manager.save_setting(
                SettingsKey.MEETING_LINUX_PREVIEW_ACK_VERSION,
                MEETING_LINUX_PREVIEW_ACK_VERSION,
            )
        else:
            settings_manager.save_setting(
                SettingsKey.MEETING_UNSUPPORTED_PLATFORM_ACK, True
            )
    except Exception as exc:
        logger.warning(
            "Could not persist unsupported-platform Meeting Mode ack: %s",
            exc,
        )
    return True

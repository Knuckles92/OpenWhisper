"""First-visit overview of Meeting Mode.

Shown the first time the Meeting Mode tab is opened. Explains what the mode
does, the main features, and where settings live. Skip and Got it both
dismiss it permanently.
"""
from __future__ import annotations

import logging
from typing import Final, Iterable, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from services.settings import (
    SettingsKey,
    resolve_meeting_mode_intro_seen,
    settings_manager,
)
from ui_qt.widgets import Button, PrimaryButton

logger = logging.getLogger(__name__)

_FEATURES: Final[tuple[str, ...]] = (
    "Live transcript and a shareable browser dashboard",
    "Optional cloud insights: topics, decisions, action items",
    "Speaker labels, playback, and export",
    "Past meetings stay in the sidebar",
)

_SETTINGS: Final[tuple[str, ...]] = (
    "Settings → Meeting — dashboard, after-meeting cleanup, recall",
    "Model Manager — voice model, language, speakers, intelligence",
)


class MeetingModeIntroDialog(QDialog):
    RESULT_SKIP: Final[str] = "skip"
    RESULT_GOT_IT: Final[str] = "got_it"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("meetingIntroDialog")
        self.result_action = self.RESULT_SKIP

        self.setWindowTitle("Meeting Mode (Beta)")
        self.setAccessibleName("Welcome to Meeting Mode")
        self.setAccessibleDescription(
            "Overview of meeting capture, the browser dashboard, and Meeting settings."
        )
        self.setMinimumWidth(460)
        self.setMaximumWidth(520)
        self.setModal(True)

        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)

        title = QLabel("Welcome to Meeting Mode")
        title.setObjectName("headerLabel")
        title.setWordWrap(True)
        layout.addWidget(title)

        lead = QLabel(
            "Record a meeting — your mic and the other side — and follow "
            "a live transcript in the browser.\n\n"
            "Meeting Mode is in beta. Transcripts and insights may be inaccurate."
        )
        lead.setObjectName("meetingIntroLead")
        lead.setWordWrap(True)
        layout.addWidget(lead)

        layout.addWidget(self._section("Features", _FEATURES))
        layout.addWidget(self._section("Settings", _SETTINGS))

        skip_hint = QLabel("Skip anytime. This only appears once.")
        skip_hint.setObjectName("infoLabel")
        skip_hint.setWordWrap(True)
        layout.addWidget(skip_hint)

        layout.addSpacing(4)
        button_layout = QHBoxLayout()
        button_layout.setSpacing(8)
        button_layout.addStretch()

        skip_btn = Button("Skip")
        skip_btn.setObjectName("meetingIntroSkipButton")
        skip_btn.setAutoDefault(False)
        skip_btn.setDefault(False)
        skip_btn.clicked.connect(self.reject)
        button_layout.addWidget(skip_btn)

        got_it_btn = PrimaryButton("Got it")
        got_it_btn.setObjectName("meetingIntroGotItButton")
        got_it_btn.setDefault(True)
        got_it_btn.clicked.connect(lambda: self._finish(self.RESULT_GOT_IT))
        button_layout.addWidget(got_it_btn)

        layout.addLayout(button_layout)

    def _section(self, heading: str, lines: Iterable[str]) -> QWidget:
        frame = QFrame()
        frame.setObjectName("meetingIntroSection")
        frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        section = QVBoxLayout(frame)
        section.setContentsMargins(14, 12, 14, 12)
        section.setSpacing(6)

        header = QLabel(heading)
        header.setObjectName("sectionLabel")
        section.addWidget(header)

        for line in lines:
            row = QHBoxLayout()
            row.setSpacing(8)
            row.setContentsMargins(0, 0, 0, 0)
            mark = QLabel("•")
            mark.setObjectName("meetingIntroBullet")
            mark.setAlignment(Qt.AlignmentFlag.AlignTop)
            body = QLabel(line)
            body.setObjectName("meetingIntroItem")
            body.setWordWrap(True)
            row.addWidget(mark, 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(body, 1)
            section.addLayout(row)

        return frame

    def _finish(self, action: str) -> None:
        self.result_action = action
        self.accept()


def maybe_show_meeting_mode_intro(parent: Optional[QWidget] = None) -> bool:
    """Show the first-visit intro when it has not been dismissed yet.

    Skip, Got it, Escape, and the window close button all count as seen.

    Args:
        parent: Widget to parent the modal dialog to.

    Returns:
        True when the dialog was shown.
    """
    if resolve_meeting_mode_intro_seen():
        return False

    dialog = MeetingModeIntroDialog(parent=parent)
    dialog.exec()
    _persist_meeting_mode_intro_seen()
    return True


def _persist_meeting_mode_intro_seen() -> None:
    try:
        settings_manager.save_setting(SettingsKey.MEETING_MODE_INTRO_SEEN, True)
    except Exception as exc:
        logger.warning("Could not persist Meeting Mode intro: %s", exc)

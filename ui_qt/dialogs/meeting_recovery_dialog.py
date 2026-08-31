"""Startup dialog listing meetings interrupted by a crash.

Each interrupted meeting can be finalized (transcribed audio is kept and the
session is closed out into history) or discarded (session and audio spool
deleted). Actions are dispatched through callbacks assigned by the UI
controller; rows disable themselves once acted on.
"""
import logging
from typing import Any, Callable, Dict, List, Optional

from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
)

from ui_qt.widgets import Button, DangerButton, PrimaryButton

logger = logging.getLogger(__name__)


class MeetingRecoveryDialog(QDialog):
    def __init__(self, meetings: List[Dict[str, Any]], parent=None):
        """Initialize the recovery dialog.

        Args:
            meetings: Interrupted meeting dicts (``id``, ``title``,
                ``started_at`` are used for display).
            parent: Parent widget (normally the main window).
        """
        super().__init__(parent)
        self.setObjectName("meetingRecoveryDialog")
        self.setWindowTitle("Recover Interrupted Meetings")
        self.setAccessibleName("Recover interrupted meetings")
        self.setAccessibleDescription(
            "Finalize captured audio into meeting history or permanently discard it."
        )
        self.setMinimumWidth(520)
        self.setModal(True)

        # Assigned by the UI controller; each receives a meeting id.
        self.on_finalize: Optional[Callable[[str], None]] = None
        self.on_discard: Optional[Callable[[str], None]] = None

        self._setup_ui(meetings)

    def _setup_ui(self, meetings: List[Dict[str, Any]]):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        title = QLabel("Some meetings did not finish")
        title.setObjectName("headerLabel")
        layout.addWidget(title)

        body = QLabel(
            "OpenWhisper closed while these meetings were running. Their "
            "audio is safe on disk. Finalize a meeting to transcribe what "
            "was captured and keep it in history, or discard it to delete "
            "the session and its audio."
        )
        body.setObjectName("infoLabel")
        body.setWordWrap(True)
        layout.addWidget(body)

        for meeting in meetings:
            layout.addWidget(self._build_row(meeting))

        layout.addSpacing(8)
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        close_btn = Button("Close")
        close_btn.setObjectName("meetingRecoveryCloseButton")
        close_btn.clicked.connect(self.accept)
        close_btn.setDefault(True)
        button_layout.addWidget(close_btn)
        layout.addLayout(button_layout)

    def _build_row(self, meeting: Dict[str, Any]) -> QFrame:
        meeting_id = str(meeting.get("id", ""))
        row = QFrame()
        row.setObjectName("meetingRecoveryRow")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(12, 8, 12, 8)
        row_layout.setSpacing(8)

        title = meeting.get("title") or "Untitled meeting"
        started_at = str(meeting.get("started_at") or "")
        # ISO timestamps read fine truncated to minutes.
        started_display = started_at.replace("T", " ")[:16]

        label = QLabel(
            f"{title} — {started_display}" if started_display else str(title)
        )
        label.setObjectName("meetingRecoveryRowLabel")
        row_layout.addWidget(label, stretch=1)

        finalize_btn = PrimaryButton("Finalize")
        finalize_btn.setObjectName("meetingRecoveryFinalizeButton")
        finalize_btn.setAccessibleName(f"Finalize {title}")
        finalize_btn.setAccessibleDescription(
            "Transcribe the captured audio and keep this meeting in history."
        )
        discard_btn = DangerButton("Discard")
        discard_btn.setObjectName("meetingRecoveryDiscardButton")
        discard_btn.setAccessibleName(f"Discard {title}")
        discard_btn.setAccessibleDescription(
            "Permanently delete this interrupted meeting and its captured audio."
        )

        def settle(action_text: str) -> None:
            finalize_btn.setEnabled(False)
            discard_btn.setEnabled(False)
            label.setText(f"{label.text()}  ({action_text})")

        def do_finalize() -> None:
            logger.info(f"Recovery: finalize meeting '{meeting_id}'")
            if self.on_finalize:
                self.on_finalize(meeting_id)
            settle("finalizing")

        def do_discard() -> None:
            logger.info(f"Recovery: discard meeting '{meeting_id}'")
            if self.on_discard:
                self.on_discard(meeting_id)
            settle("discarded")

        finalize_btn.clicked.connect(do_finalize)
        discard_btn.clicked.connect(do_discard)
        row_layout.addWidget(finalize_btn)
        row_layout.addWidget(discard_btn)
        return row

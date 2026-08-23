"""Meeting-specific content for the main window's collapsible sidebar."""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Iterable, Optional

from meeting.content import (
    fallback_meeting_title,
    meeting_insights_pill,
    summarize_meeting_content,
)
from meeting.time_utils import format_meeting_duration, format_meeting_started_at

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)

_NON_HISTORICAL_STATUSES = {"active", "paused", "ending"}

# Test-facing aliases for the shared formatters.
_format_started_at = format_meeting_started_at
_format_duration = format_meeting_duration


class PastMeetingItem(QFrame):
    """Compact card for one persisted meeting session."""

    meeting_selected = pyqtSignal(str)

    def __init__(self, meeting: Dict[str, Any], parent=None):
        super().__init__(parent)
        self.meeting = dict(meeting)
        self.meeting_id = str(meeting.get("id") or "")
        self.setObjectName("pastMeetingItem")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("selected", False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(7)

        title = fallback_meeting_title(meeting)
        self.title_label = QLabel(title, self)
        self.title_label.setObjectName("pastMeetingTitle")
        self.title_label.setWordWrap(True)
        self.title_label.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
        layout.addWidget(self.title_label)

        self.date_label = QLabel(
            format_meeting_started_at(meeting.get("started_at")), self
        )
        self.date_label.setObjectName("pastMeetingMeta")
        layout.addWidget(self.date_label)

        status = str(meeting.get("status") or "").lower()
        content = dict(meeting.get("content_summary") or {})
        content_note = ""
        if status == "failed":
            content_note = "Meeting failed to start"
        elif content.get("is_empty") is True:
            content_note = "No audio or transcript captured"
        elif content.get("has_transcript") is False:
            content_note = "No transcript captured"
        elif content.get("has_audio") is False:
            content_note = "No audio captured"
        self.content_label = QLabel(content_note, self)
        self.content_label.setObjectName("pastMeetingContentWarning")
        self.content_label.setWordWrap(True)
        layout.addWidget(self.content_label)
        self.content_label.setVisible(bool(content_note))

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 2, 0, 0)
        footer.setSpacing(8)

        duration = format_meeting_duration(meeting)
        if status not in {"", "ended"}:
            lifecycle = (
                "Failed" if status == "failed"
                else status.replace("_", " ").title()
            )
            duration = f"{duration} · {lifecycle}" if duration else lifecycle
        self.detail_label = QLabel(duration, self)
        self.detail_label.setObjectName("pastMeetingMeta")
        footer.addWidget(self.detail_label)

        pill = meeting_insights_pill(meeting)
        self.insights_pill = QLabel("", self)
        self.insights_pill.setObjectName("pastMeetingInsightsPill")
        self.insights_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.insights_pill.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
        footer.addWidget(self.insights_pill)
        if pill:
            label, tone = pill
            self.insights_pill.setText(label)
            self.insights_pill.setProperty("pillTone", tone)
            style = self.insights_pill.style()
            if style is not None:
                style.unpolish(self.insights_pill)
                style.polish(self.insights_pill)
            self.insights_pill.show()
        else:
            self.insights_pill.hide()
        footer.addStretch()
        layout.addLayout(footer)

    def set_selected(self, selected: bool) -> None:
        """Mark this tile as the meeting shown on the Meeting Mode tab."""
        self.setProperty("selected", bool(selected))
        style = self.style()
        if style is not None:
            style.unpolish(self)
            style.polish(self)
        self.update()

    def _emit_selected(self) -> None:
        if self.meeting_id:
            self.meeting_selected.emit(self.meeting_id)

    def mousePressEvent(self, event) -> None:
        """Load the meeting in Qt when the card itself is clicked."""
        if event.button() == Qt.MouseButton.LeftButton:
            self._emit_selected()
        super().mousePressEvent(event)


class PastMeetingsPanel(QWidget):
    """Sidebar page listing completed meetings from the meeting repository."""

    meeting_selected = pyqtSignal(str)
    MAX_MEETINGS = 100

    def __init__(
        self,
        parent=None,
        meeting_provider: Optional[Callable[[], Iterable[Dict[str, Any]]]] = None,
    ):
        super().__init__(parent)
        self._meeting_provider = meeting_provider
        self._repository = None
        self._selected_id: Optional[str] = None
        self.setObjectName("pastMeetingsContent")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._setup_ui()
        self._apply_style()

    def set_selected_meeting_id(self, meeting_id: Optional[str]) -> None:
        """Highlight the tile that matches the leftover card, if listed."""
        self._selected_id = str(meeting_id) if meeting_id else None
        self._apply_selection()

    def _apply_selection(self) -> None:
        for card in self.findChildren(PastMeetingItem):
            card.set_selected(card.meeting_id == self._selected_id)

    def _on_card_selected(self, meeting_id: str) -> None:
        self._selected_id = meeting_id
        self._apply_selection()
        self.meeting_selected.emit(meeting_id)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("Past Meetings")
        title.setObjectName("pastMeetingsHeader")
        title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        header.addWidget(title)
        header.addStretch()

        self.refresh_button = QPushButton("↻")
        self.refresh_button.setObjectName("pastMeetingsRefreshButton")
        self.refresh_button.setFixedSize(28, 28)
        self.refresh_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_button.setToolTip("Refresh past meetings")
        self.refresh_button.clicked.connect(self.refresh)
        header.addWidget(self.refresh_button)
        layout.addLayout(header)

        hint = QLabel("Select a meeting to review it here.")
        hint.setObjectName("pastMeetingsHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("pastMeetingsScrollArea")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        scroll_content = QWidget()
        scroll_content.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.meetings_layout = QVBoxLayout(scroll_content)
        self.meetings_layout.setContentsMargins(0, 0, 6, 0)
        self.meetings_layout.setSpacing(12)
        self.meetings_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll_area.setWidget(scroll_content)
        layout.addWidget(self.scroll_area, stretch=1)

    def _load_meetings(self) -> list[Dict[str, Any]]:
        if self._meeting_provider is not None:
            rows = self._meeting_provider()
        else:
            if self._repository is None:
                from meeting.persist.repository import SqlMeetingRepository

                self._repository = SqlMeetingRepository()
            rows = self._repository.list_meetings()
        meetings = []
        for row in rows:
            meeting = dict(row)
            if (
                str(meeting.get("status") or "").lower()
                in _NON_HISTORICAL_STATUSES
            ):
                continue
            if "content_summary" not in meeting and self._repository is not None:
                meeting["content_summary"] = summarize_meeting_content(
                    self._repository, str(meeting.get("id") or "")
                )
            meetings.append(meeting)
        return meetings

    def refresh(self) -> None:
        """Reload persisted meetings and rebuild the card list."""
        while self.meetings_layout.count():
            item = self.meetings_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        try:
            meetings = self._load_meetings()
        except Exception as exc:
            logger.error("Failed to load past meetings: %s", exc)
            self.meetings_layout.addWidget(
                self._placeholder("Past meetings could not be loaded")
            )
            return

        if not meetings:
            self.meetings_layout.addWidget(
                self._placeholder("No past meetings yet")
            )
            return

        for meeting in meetings[: self.MAX_MEETINGS]:
            card = PastMeetingItem(meeting, self.scroll_area.widget())
            card.meeting_selected.connect(self._on_card_selected)
            self.meetings_layout.addWidget(card)

        if len(meetings) > self.MAX_MEETINGS:
            self.meetings_layout.addWidget(
                self._placeholder(f"Showing the newest {self.MAX_MEETINGS} meetings")
            )
        self._apply_selection()

    @staticmethod
    def _placeholder(message: str) -> QLabel:
        label = QLabel(message)
        label.setObjectName("pastMeetingsEmpty")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        return label

    def _apply_style(self) -> None:
        self.setStyleSheet("""
            QWidget#pastMeetingsContent {
                background-color: #1c1c1e;
                border-left: 1px solid rgba(255, 255, 255, 0.08);
            }
            QLabel#pastMeetingsHeader { color: #ffffff; font-weight: 700; }
            QLabel#pastMeetingsHint {
                color: #8e8e93;
                font-size: 12px;
            }
            QLabel#pastMeetingsEmpty {
                color: #636366;
                font-size: 12px;
                padding: 18px 6px;
            }
            QScrollArea#pastMeetingsScrollArea {
                background-color: transparent;
                border: none;
            }
            QScrollArea#pastMeetingsScrollArea > QWidget > QWidget {
                background-color: transparent;
            }
            QFrame#pastMeetingItem {
                background-color: rgba(44, 44, 46, 0.5);
                border-radius: 12px;
                border: 1px solid rgba(255, 255, 255, 0.05);
            }
            QFrame#pastMeetingItem:hover {
                background-color: rgba(58, 58, 60, 0.6);
                border: 1px solid rgba(10, 132, 255, 0.35);
            }
            QFrame#pastMeetingItem[selected="true"] {
                background-color: rgba(10, 132, 255, 0.16);
                border: 1px solid rgba(10, 132, 255, 0.55);
            }
            QLabel#pastMeetingTitle { color: #e5e5e7; }
            QLabel#pastMeetingMeta { color: #98989d; font-size: 11px; }
            QLabel#pastMeetingContentWarning {
                color: #ff9f0a;
                font-size: 11px;
            }
            QLabel#pastMeetingInsightsPill {
                color: #98989d;
                background-color: rgba(255, 255, 255, 0.08);
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 10px;
                padding: 2px 8px;
                font-size: 10px;
                font-weight: 600;
            }
            QLabel#pastMeetingInsightsPill[pillTone="warning"] {
                color: #ff9f0a;
                background-color: rgba(255, 159, 10, 0.15);
                border: 1px solid rgba(255, 159, 10, 0.35);
            }
            QLabel#pastMeetingInsightsPill[pillTone="success"] {
                color: #30d158;
                background-color: rgba(48, 209, 88, 0.15);
                border: 1px solid rgba(48, 209, 88, 0.35);
            }
            QLabel#pastMeetingInsightsPill[pillTone="neutral"] {
                color: #98989d;
                background-color: rgba(255, 255, 255, 0.08);
                border: 1px solid rgba(255, 255, 255, 0.12);
            }
            QPushButton#pastMeetingsRefreshButton {
                background-color: transparent;
                color: #8e8e93;
                border: none;
                border-radius: 14px;
                font-size: 18px;
            }
            QPushButton#pastMeetingsRefreshButton:hover {
                background-color: rgba(255, 255, 255, 0.1);
                color: #ffffff;
            }
        """)

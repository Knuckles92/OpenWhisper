"""Meeting-specific content for the main window's collapsible sidebar."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, Optional

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


def _parse_datetime(value: Any) -> Optional[datetime]:
    """Parse one repository timestamp without assuming timezone information."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _format_started_at(value: Any) -> str:
    started = _parse_datetime(value)
    return started.strftime("%b %d, %Y · %I:%M %p") if started else "Unknown date"


def _format_duration(meeting: Dict[str, Any]) -> str:
    started = _parse_datetime(meeting.get("started_at"))
    ended = _parse_datetime(meeting.get("ended_at"))
    if started is None or ended is None:
        return ""
    try:
        seconds = max(
            0,
            int((ended - started).total_seconds())
            - int(float(meeting.get("paused_total_s") or 0)),
        )
    except (TypeError, ValueError):
        return ""
    hours, remainder = divmod(seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes} min"


class PastMeetingItem(QFrame):
    """Compact card for one persisted meeting session."""

    open_requested = pyqtSignal(str)

    def __init__(self, meeting: Dict[str, Any], parent=None):
        super().__init__(parent)
        self.meeting = dict(meeting)
        self.meeting_id = str(meeting.get("id") or "")
        self.setObjectName("pastMeetingItem")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(7)

        title = str(meeting.get("title") or "").strip() or "Untitled meeting"
        self.title_label = QLabel(title)
        self.title_label.setObjectName("pastMeetingTitle")
        self.title_label.setWordWrap(True)
        self.title_label.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
        layout.addWidget(self.title_label)

        self.date_label = QLabel(_format_started_at(meeting.get("started_at")))
        self.date_label.setObjectName("pastMeetingMeta")
        layout.addWidget(self.date_label)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 2, 0, 0)
        footer.setSpacing(8)

        status = str(meeting.get("status") or "ended").replace("_", " ").title()
        duration = _format_duration(meeting)
        detail = " · ".join(value for value in (duration, status) if value)
        self.detail_label = QLabel(detail)
        self.detail_label.setObjectName("pastMeetingMeta")
        footer.addWidget(self.detail_label)
        footer.addStretch()

        self.open_button = QPushButton("Open")
        self.open_button.setObjectName("pastMeetingOpenButton")
        self.open_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_button.setToolTip("Load this meeting in the web dashboard")
        self.open_button.clicked.connect(self._emit_open)
        footer.addWidget(self.open_button)
        layout.addLayout(footer)

    def _emit_open(self) -> None:
        if self.meeting_id:
            self.open_requested.emit(self.meeting_id)

    def mousePressEvent(self, event) -> None:
        """Open the meeting when the card itself is clicked."""
        if event.button() == Qt.MouseButton.LeftButton:
            child = self.childAt(event.position().toPoint())
            if not isinstance(child, QPushButton):
                self._emit_open()
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
        self.setObjectName("pastMeetingsContent")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._setup_ui()
        self._apply_style()

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

        hint = QLabel("Open a previous meeting in the web dashboard.")
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
        return [
            dict(row)
            for row in rows
            if str(row.get("status") or "").lower() not in _NON_HISTORICAL_STATUSES
        ]

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
            card = PastMeetingItem(meeting)
            card.open_requested.connect(self.meeting_selected.emit)
            self.meetings_layout.addWidget(card)

        if len(meetings) > self.MAX_MEETINGS:
            self.meetings_layout.addWidget(
                self._placeholder(f"Showing the newest {self.MAX_MEETINGS} meetings")
            )

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
            QLabel#pastMeetingTitle { color: #e5e5e7; }
            QLabel#pastMeetingMeta { color: #98989d; font-size: 11px; }
            QPushButton#pastMeetingOpenButton {
                background-color: rgba(10, 132, 255, 0.14);
                color: #6fb1ff;
                border: 1px solid rgba(10, 132, 255, 0.3);
                border-radius: 7px;
                padding: 4px 12px;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton#pastMeetingOpenButton:hover {
                background-color: rgba(10, 132, 255, 0.26);
                color: #ffffff;
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

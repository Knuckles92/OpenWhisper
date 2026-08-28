"""Meeting-specific content for the main window's collapsible sidebar."""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, Iterable, Optional

from config import config
from meeting.content import (
    fallback_meeting_title,
    meeting_insights_pill,
    meeting_preview_text,
    summarize_meeting_content,
)
from meeting.time_utils import format_meeting_duration, format_meeting_started_at
from services.settings import SettingsKey, settings_manager

from PyQt6.QtCore import Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)

_NON_HISTORICAL_STATUSES = {"active", "paused", "ending"}
_MENU_STYLESHEET = """
    QMenu {
        background-color: rgba(44, 44, 46, 0.95);
        color: #f5f5f7;
        border: 1px solid rgba(255, 255, 255, 0.1);
        border-radius: 10px;
        padding: 6px;
    }
    QMenu::item {
        padding: 8px 28px 8px 14px;
        border-radius: 6px;
        font-size: 13px;
    }
    QMenu::item:selected {
        background-color: #0a84ff;
        color: #ffffff;
    }
    QMenu::separator {
        background-color: rgba(255, 255, 255, 0.08);
        height: 1px;
        margin: 4px 8px;
    }
    QMenu::item:disabled {
        color: #8e8e93;
    }
"""

# Test-facing aliases for the shared formatters.
_format_started_at = format_meeting_started_at
_format_duration = format_meeting_duration


class PastMeetingItem(QFrame):
    """Compact card for one persisted meeting session."""

    meeting_selected = pyqtSignal(str)
    copy_transcript_requested = pyqtSignal(str)
    delete_requested = pyqtSignal(str)

    def __init__(self, meeting: Dict[str, Any], parent=None):
        super().__init__(parent)
        self.meeting = dict(meeting)
        self.meeting_id = str(meeting.get("id") or "")
        self.setObjectName("pastMeetingItem")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("selected", False)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        title = fallback_meeting_title(meeting)
        self.title_label = QLabel(title, self)
        self.title_label.setObjectName("pastMeetingTitle")
        self.title_label.setWordWrap(True)
        self.title_label.setFont(QFont("Segoe UI", 12, QFont.Weight.DemiBold))
        layout.addWidget(self.title_label)

        self.date_label = QLabel(
            format_meeting_started_at(meeting.get("started_at")), self
        )
        self.date_label.setObjectName("pastMeetingMeta")
        self.date_label.setFont(QFont("Segoe UI", 10))
        layout.addWidget(self.date_label)

        preview = meeting_preview_text(meeting)
        self.preview_label = QLabel(preview, self)
        self.preview_label.setObjectName("pastMeetingPreview")
        self.preview_label.setWordWrap(True)
        self.preview_label.setFont(QFont("Segoe UI", 11))
        layout.addWidget(self.preview_label)
        self.preview_label.setVisible(bool(preview))

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
        self.insights_pill.setFixedHeight(20)
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

    def _has_transcript(self) -> bool:
        summary = dict(self.meeting.get("content_summary") or {})
        if "has_transcript" in summary:
            return bool(summary.get("has_transcript"))
        return bool(meeting_preview_text(self.meeting))

    def _show_context_menu(self, pos) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(_MENU_STYLESHEET)
        copy_action = menu.addAction("Copy transcript")
        copy_action.setEnabled(self._has_transcript())
        copy_action.triggered.connect(
            lambda: self.copy_transcript_requested.emit(self.meeting_id)
        )
        menu.addSeparator()
        delete_action = menu.addAction("Delete")
        delete_action.triggered.connect(
            lambda: self.delete_requested.emit(self.meeting_id)
        )
        menu.exec(self.mapToGlobal(pos))

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
    copy_transcript_requested = pyqtSignal(str)
    delete_meeting_requested = pyqtSignal(str, bool)
    clear_meetings_requested = pyqtSignal(bool)
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
        self._meetings: list[Dict[str, Any]] = []
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
        header.setSpacing(8)

        self.menu_btn = QPushButton("☰")
        self.menu_btn.setObjectName("pastMeetingsMenuBtn")
        self.menu_btn.setFixedSize(28, 28)
        self.menu_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.menu_btn.clicked.connect(self._show_header_menu)
        header.addWidget(self.menu_btn)

        title = QLabel("Past Meetings")
        title.setObjectName("pastMeetingsHeader")
        title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        header.addWidget(title)
        header.addStretch()
        layout.addLayout(header)

        self.search_input = QLineEdit()
        self.search_input.setObjectName("historySearchInput")
        self.search_input.setPlaceholderText("Search meetings...")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setFixedHeight(32)
        self.search_input.textChanged.connect(self._on_search_text_changed)
        layout.addWidget(self.search_input)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._rebuild_list)

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

        self.section_header = QLabel("PAST MEETINGS")
        self.section_header.setObjectName("sectionHeader")
        self.section_header.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
        self.meetings_layout.addWidget(self.section_header)

        self.meetings_list_layout = QVBoxLayout()
        self.meetings_list_layout.setSpacing(12)
        self.meetings_layout.addLayout(self.meetings_list_layout)

        self.scroll_area.setWidget(scroll_content)
        layout.addWidget(self.scroll_area, stretch=1)

    def _build_header_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.setStyleSheet(_MENU_STYLESHEET)
        refresh = menu.addAction("Refresh")
        refresh.triggered.connect(self.refresh)
        has_meetings = bool(self._meetings)
        export_action = menu.addAction("Export past meetings…")
        export_action.setEnabled(has_meetings)
        export_action.triggered.connect(self._on_export_meetings)
        open_folder = menu.addAction("Open meetings folder")
        open_folder.triggered.connect(self._on_open_meetings_folder)
        menu.addSeparator()
        clear_action = menu.addAction("Clear history")
        clear_action.setEnabled(has_meetings)
        clear_action.triggered.connect(self._on_clear_history)
        clear_all_action = menu.addAction("Clear history + recordings")
        clear_all_action.setEnabled(has_meetings)
        clear_all_action.triggered.connect(self._on_clear_history_and_recordings)
        return menu

    def _show_header_menu(self) -> None:
        menu = self._build_header_menu()
        menu.exec(self.menu_btn.mapToGlobal(self.menu_btn.rect().bottomLeft()))

    def _on_search_text_changed(self, _text: str) -> None:
        self._search_timer.start()

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

    def _filter_meetings(
        self, meetings: list[Dict[str, Any]]
    ) -> list[Dict[str, Any]]:
        query = self.search_input.text().strip().lower()
        if not query:
            return meetings
        matches = []
        for meeting in meetings:
            haystack = " ".join(
                part for part in (
                    fallback_meeting_title(meeting),
                    format_meeting_started_at(meeting.get("started_at")),
                    format_meeting_duration(meeting),
                    meeting_preview_text(meeting),
                    (meeting_insights_pill(meeting) or ("", ""))[0],
                    str(meeting.get("status") or ""),
                ) if part
            ).lower()
            if query in haystack:
                matches.append(meeting)
        return matches

    def refresh(self) -> None:
        """Reload persisted meetings and rebuild the card list."""
        try:
            self._meetings = self._load_meetings()
        except Exception as exc:
            logger.error("Failed to load past meetings: %s", exc)
            self._meetings = []
            self._clear_list()
            self.section_header.setText("PAST MEETINGS")
            self.meetings_list_layout.addWidget(
                self._placeholder("Past meetings could not be loaded")
            )
            return
        self._rebuild_list()

    def _rebuild_list(self) -> None:
        self._clear_list()
        meetings = self._filter_meetings(self._meetings)
        query = self.search_input.text().strip()
        total = len(self._meetings)
        shown = min(len(meetings), self.MAX_MEETINGS)
        self.section_header.setText(f"PAST MEETINGS ({shown})")

        if not self._meetings:
            self.meetings_list_layout.addWidget(
                self._placeholder("No past meetings yet")
            )
            return
        if query and not meetings:
            self.meetings_list_layout.addWidget(
                self._placeholder("No matching meetings")
            )
            return

        for meeting in meetings[: self.MAX_MEETINGS]:
            card = PastMeetingItem(meeting, self.scroll_area.widget())
            card.meeting_selected.connect(self._on_card_selected)
            card.copy_transcript_requested.connect(
                self.copy_transcript_requested.emit
            )
            card.delete_requested.connect(self._confirm_delete)
            self.meetings_list_layout.addWidget(card)

        if len(meetings) > self.MAX_MEETINGS:
            self.meetings_list_layout.addWidget(
                self._placeholder(
                    f"Showing 100 of {len(meetings)} — search to find older meetings"
                )
            )
        elif not query and total > self.MAX_MEETINGS:
            self.meetings_list_layout.addWidget(
                self._placeholder(
                    f"Showing 100 of {total} — search to find older meetings"
                )
            )
        self._apply_selection()

    def _meeting_by_id(self, meeting_id: str) -> Optional[Dict[str, Any]]:
        for meeting in self._meetings:
            if str(meeting.get("id") or "") == meeting_id:
                return meeting
        return None

    @staticmethod
    def _meeting_has_audio(meeting: Optional[Dict[str, Any]]) -> bool:
        if not meeting:
            return False
        summary = dict(meeting.get("content_summary") or {})
        if "has_audio" in summary:
            return bool(summary.get("has_audio"))
        return bool(str(meeting.get("spool_dir") or ""))

    def _confirm_delete(self, meeting_id: str) -> None:
        delete_recordings = False
        try:
            should_confirm = settings_manager.get(
                SettingsKey.CONFIRM_MEETING_DELETE,
                True,
            )
        except Exception as exc:
            logger.warning("Failed to load meeting deletion preference: %s", exc)
            should_confirm = True

        if should_confirm is False:
            self.delete_meeting_requested.emit(meeting_id, False)
            return

        confirmation = QMessageBox(self)
        confirmation.setIcon(QMessageBox.Icon.Warning)
        confirmation.setWindowTitle("Delete Meeting")
        confirmation.setText("Delete this meeting from Past Meetings?")
        confirmation.setInformativeText("This cannot be undone.")
        confirmation.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        confirmation.setDefaultButton(QMessageBox.StandardButton.No)

        audio_choice = None
        meeting = self._meeting_by_id(meeting_id)
        if self._meeting_has_audio(meeting):
            audio_label = QLabel("Delete saved recordings too?", confirmation)
            audio_choice = QComboBox(confirmation)
            audio_choice.addItem("No — keep the recordings", False)
            audio_choice.addItem("Yes — permanently delete them", True)
            audio_choice.setToolTip(
                "Choose whether this meeting's audio spool should also "
                "be deleted"
            )
            message_layout = confirmation.layout()
            if isinstance(message_layout, QGridLayout):
                row = message_layout.rowCount()
                columns = max(1, message_layout.columnCount())
                message_layout.addWidget(audio_label, row, 0, 1, columns)
                message_layout.addWidget(
                    audio_choice, row + 1, 0, 1, columns
                )

        dont_ask_again = QCheckBox("Don't ask me again", confirmation)
        confirmation.setCheckBox(dont_ask_again)

        if confirmation.exec() != QMessageBox.StandardButton.Yes:
            return

        delete_recordings = bool(
            audio_choice is not None and audio_choice.currentData()
        )
        if dont_ask_again.isChecked():
            try:
                settings_manager.save_setting(
                    SettingsKey.CONFIRM_MEETING_DELETE,
                    False,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to save meeting deletion preference: %s",
                    exc,
                )

        self.delete_meeting_requested.emit(meeting_id, delete_recordings)

    def _on_clear_history(self) -> None:
        reply = QMessageBox.question(
            self,
            "Clear History",
            "Delete all past meetings?\n\nSaved recordings will be kept.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.clear_meetings_requested.emit(False)

    def _on_clear_history_and_recordings(self) -> None:
        reply = QMessageBox.question(
            self,
            "Clear History and Recordings",
            "Delete all past meetings AND permanently delete their "
            "recordings from disk?\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.clear_meetings_requested.emit(True)

    def _on_open_meetings_folder(self) -> None:
        folder = os.path.abspath(config.MEETINGS_FOLDER)
        os.makedirs(folder, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def _on_export_meetings(self) -> None:
        from ui_qt.dialogs.meeting_export_dialog import MeetingExportDialog

        dialog = MeetingExportDialog(
            self,
            meeting_provider=lambda: list(self._meetings),
        )
        dialog.exec()

    def _clear_list(self) -> None:
        while self.meetings_list_layout.count():
            item = self.meetings_list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()

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
            QLabel#pastMeetingsHeader {
                color: #ffffff;
                font-weight: 700;
                background-color: transparent;
            }
            QLabel#sectionHeader {
                color: #98989d;
                padding-top: 4px;
                letter-spacing: 0.5px;
                text-transform: uppercase;
                font-size: 10px;
                font-weight: 600;
                background-color: transparent;
            }
            QPushButton#pastMeetingsMenuBtn {
                background-color: transparent;
                color: #8e8e93;
                border: none;
                border-radius: 14px;
                padding: 0px;
                font-size: 15px;
            }
            QPushButton#pastMeetingsMenuBtn:hover {
                background-color: rgba(255, 255, 255, 0.1);
                color: #ffffff;
            }
            QLineEdit#historySearchInput {
                background-color: rgba(44, 44, 46, 0.8);
                color: #f5f5f7;
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 8px;
                padding: 4px 10px;
                font-size: 12px;
            }
            QLineEdit#historySearchInput:focus {
                border: 1px solid #0a84ff;
                background-color: rgba(44, 44, 46, 1.0);
            }
            QLineEdit#historySearchInput::placeholder {
                color: #636366;
            }
            QLabel#pastMeetingsEmpty {
                color: #636366;
                font-size: 12px;
                padding: 8px 0px;
                background-color: transparent;
            }
            QScrollArea#pastMeetingsScrollArea {
                background-color: transparent;
                border: none;
            }
            QScrollArea#pastMeetingsScrollArea > QWidget > QWidget {
                background-color: transparent;
            }
            QScrollArea#pastMeetingsScrollArea QScrollBar:vertical {
                background: transparent;
                width: 8px;
                margin: 0px;
            }
            QScrollArea#pastMeetingsScrollArea QScrollBar::handle:vertical {
                background: rgba(255, 255, 255, 0.15);
                border-radius: 4px;
                min-height: 30px;
            }
            QScrollArea#pastMeetingsScrollArea QScrollBar::handle:vertical:hover {
                background: rgba(255, 255, 255, 0.3);
            }
            QScrollArea#pastMeetingsScrollArea QScrollBar::add-line:vertical,
            QScrollArea#pastMeetingsScrollArea QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QScrollArea#pastMeetingsScrollArea QScrollBar::add-page:vertical,
            QScrollArea#pastMeetingsScrollArea QScrollBar::sub-page:vertical {
                background: transparent;
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
            QLabel#pastMeetingTitle {
                color: #f5f5f7;
                background-color: transparent;
            }
            QLabel#pastMeetingMeta {
                color: #98989d;
                font-size: 10px;
                background-color: transparent;
            }
            QLabel#pastMeetingPreview {
                color: #e5e5e7;
                background-color: transparent;
            }
            QLabel#pastMeetingContentWarning {
                color: #ff9f0a;
                font-size: 11px;
                background-color: transparent;
            }
            QLabel#pastMeetingInsightsPill {
                color: #98989d;
                background-color: rgba(255, 255, 255, 0.08);
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 6px;
                padding: 0px 8px;
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
        """)

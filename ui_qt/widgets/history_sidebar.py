"""
History sidebar widget for displaying transcription history and saved recordings.
Collapsible sidebar panel that slides in/out from the right side of the main window.
Supports context-aware mode switching between Quick Record history and Past Meetings.
"""
import logging
from typing import Optional, Callable, List
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QMenu, QSizePolicy, QApplication,
    QLineEdit, QInputDialog, QMessageBox
)
from PyQt6.QtCore import Qt, pyqtSignal, QPropertyAnimation, QEasingCurve, QSize, pyqtProperty
from PyQt6.QtGui import QFont, QCursor

from services.history_manager import HistoryEntry, RecordingInfo, history_manager


class HistoryItemWidget(QFrame):
    """Widget displaying a single history entry."""
    
    clicked = pyqtSignal(str)  # Emits entry_id
    copy_requested = pyqtSignal(str)  # Emits entry_id
    delete_requested = pyqtSignal(str)  # Emits entry_id
    retranscribe_requested = pyqtSignal(str)  # Emits audio file path
    
    def __init__(self, entry: HistoryEntry, parent=None):
        super().__init__(parent)
        self.entry = entry
        self.setObjectName("historyItem")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        
        self._setup_ui()
        self._apply_style()
    
    def _setup_ui(self):
        """Setup the widget UI."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        # Top row: timestamp and audio indicator
        top_row = QHBoxLayout()
        top_row.setSpacing(8)

        # Timestamp
        self.timestamp_label = QLabel(self.entry.formatted_timestamp)
        self.timestamp_label.setObjectName("historyTimestamp")
        self.timestamp_label.setFont(QFont("Segoe UI", 10))
        top_row.addWidget(self.timestamp_label)

        # Audio indicator if recording exists
        if self.entry.audio_file:
            audio_indicator = QLabel("🎤")
            audio_indicator.setToolTip("Audio recording available")
            top_row.addWidget(audio_indicator)

        top_row.addStretch()
        layout.addLayout(top_row)

        # Preview text
        self.preview_label = QLabel(self.entry.preview_text)
        self.preview_label.setObjectName("historyPreview")
        self.preview_label.setWordWrap(True)
        self.preview_label.setFont(QFont("Segoe UI", 11))
        self.preview_label.setMaximumHeight(60)
        layout.addWidget(self.preview_label)
    
    def _format_model_name(self, model: str) -> str:
        """Format model name for display."""
        model_display = {
            'local_whisper': 'Local',
            'api_whisper': 'API',
            'api_gpt4o': 'GPT-4o',
            'api_gpt4o_mini': 'GPT-4o Mini'
        }
        return model_display.get(model, model)
    
    def _apply_style(self):
        """Apply custom styling."""
        self.setStyleSheet("""
            QFrame#historyItem {
                background-color: rgba(44, 44, 46, 0.5);
                border-radius: 12px;
                border: 1px solid rgba(255, 255, 255, 0.05);
            }
            QFrame#historyItem:hover {
                background-color: rgba(58, 58, 60, 0.6);
                border: 1px solid rgba(255, 255, 255, 0.1);
            }
            QLabel#historyTimestamp {
                color: #98989d;
                background-color: transparent;
            }
            QLabel#historyPreview {
                color: #e5e5e7;
                background-color: transparent;
                line-height: 1.4;
            }
        """)
    
    def _show_context_menu(self, pos):
        """Show context menu."""
        menu = QMenu(self)
        menu.setStyleSheet("""
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
        """)

        # Model info (non-clickable)
        model_name = self._format_model_name(self.entry.model)
        model_action = menu.addAction(f"Model: {model_name}")
        model_action.setEnabled(False)

        menu.addSeparator()

        # Copy action
        copy_action = menu.addAction("Copy Text")
        copy_action.triggered.connect(lambda: self.copy_requested.emit(self.entry.id))
        
        # Re-transcribe action (only if audio exists)
        if self.entry.audio_file:
            audio_path = history_manager.get_recording_path(self.entry.audio_file)
            if audio_path:
                retranscribe_action = menu.addAction("Re-transcribe")
                retranscribe_action.triggered.connect(
                    lambda: self.retranscribe_requested.emit(audio_path)
                )
        
        menu.addSeparator()
        
        # Delete action
        delete_action = menu.addAction("Delete")
        delete_action.triggered.connect(lambda: self.delete_requested.emit(self.entry.id))
        
        menu.exec(self.mapToGlobal(pos))
    
    def mousePressEvent(self, event):
        """Handle click to view full transcription."""
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.entry.id)
        super().mousePressEvent(event)


class RecordingItemWidget(QFrame):
    """Widget displaying a saved recording."""
    
    retranscribe_requested = pyqtSignal(str)  # Emits file path
    
    def __init__(self, recording: RecordingInfo, parent=None):
        super().__init__(parent)
        self.recording = recording
        self.setObjectName("recordingItem")
        
        self._setup_ui()
        self._apply_style()
    
    def _setup_ui(self):
        """Setup the widget UI."""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(10)
        
        # Left side: info
        info_layout = QVBoxLayout()
        info_layout.setSpacing(2)
        
        # Timestamp
        self.timestamp_label = QLabel(self.recording.formatted_timestamp)
        self.timestamp_label.setObjectName("recordingTimestamp")
        self.timestamp_label.setFont(QFont("Segoe UI", 11))
        info_layout.addWidget(self.timestamp_label)
        
        # File size
        self.size_label = QLabel(self.recording.formatted_size)
        self.size_label.setObjectName("recordingSize")
        self.size_label.setFont(QFont("Segoe UI", 9))
        info_layout.addWidget(self.size_label)
        
        layout.addLayout(info_layout)
        layout.addStretch()
        
        # Re-transcribe button
        self.retranscribe_btn = QPushButton("Transcribe")
        self.retranscribe_btn.setObjectName("retranscribeBtn")
        self.retranscribe_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.retranscribe_btn.setFixedHeight(32)
        self.retranscribe_btn.clicked.connect(
            lambda: self.retranscribe_requested.emit(self.recording.file_path)
        )
        layout.addWidget(self.retranscribe_btn)
    
    def _apply_style(self):
        """Apply custom styling."""
        self.setStyleSheet("""
            QFrame#recordingItem {
                background-color: rgba(44, 44, 46, 0.5);
                border-radius: 12px;
                border: 1px solid rgba(255, 255, 255, 0.05);
            }
            QLabel#recordingTimestamp {
                color: #e5e5e7;
                background-color: transparent;
            }
            QLabel#recordingSize {
                color: #98989d;
                background-color: transparent;
            }
            QPushButton#retranscribeBtn {
                background-color: rgba(48, 209, 88, 0.15);
                color: #32d74b;
                border: 1px solid rgba(48, 209, 88, 0.3);
                border-radius: 8px;
                padding: 6px 16px;
                font-size: 12px;
                font-weight: 600;
            }
            QPushButton#retranscribeBtn:hover {
                background-color: rgba(48, 209, 88, 0.25);
                border: 1px solid rgba(48, 209, 88, 0.5);
            }
            QPushButton#retranscribeBtn:pressed {
                background-color: rgba(48, 209, 88, 0.35);
            }
        """)


class MeetingListItemWidget(QFrame):
    """Widget displaying a single meeting entry in the sidebar."""

    clicked = pyqtSignal(str)  # meeting_id
    delete_requested = pyqtSignal(str)  # meeting_id
    rename_requested = pyqtSignal(str, str)  # meeting_id, new_title
    copy_transcript_requested = pyqtSignal(str)  # meeting_id

    def __init__(self, meeting_id: str, title: str, date: str,
                 duration: str, preview: str, status: str = "completed", parent=None):
        super().__init__(parent)
        self.meeting_id = meeting_id
        self._title = title
        self._preview = preview
        self.setObjectName("meetingListItem")
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._apply_style()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        # Header row: title + status
        header_layout = QHBoxLayout()

        # Status indicator for interrupted meetings
        if status == "interrupted":
            status_label = QLabel("\u26a0")  # Warning symbol
            status_label.setToolTip("This meeting was interrupted")
            status_label.setStyleSheet("color: #ff9f0a; background: transparent; font-size: 12px;")
            header_layout.addWidget(status_label)

        title_label = QLabel(title or "Untitled Meeting")
        title_label.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        title_label.setStyleSheet("color: #f5f5f7; background: transparent;")

        header_layout.addWidget(title_label)
        header_layout.addStretch()

        # Delete button (small, subtle)
        self.delete_btn = QPushButton("\u00d7")  # X symbol
        self.delete_btn.setFixedSize(20, 20)
        self.delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #636366;
                border: none;
                font-size: 16px;
                font-weight: bold;
            }
            QPushButton:hover {
                color: #ff453a;
                background-color: rgba(255, 69, 58, 0.2);
                border-radius: 10px;
            }
        """)
        self.delete_btn.clicked.connect(self._on_delete_clicked)
        header_layout.addWidget(self.delete_btn)

        # Info row: date + duration
        info_layout = QHBoxLayout()

        date_label = QLabel(date)
        date_label.setFont(QFont("Segoe UI", 10))
        date_label.setStyleSheet("color: #8e8e93; background: transparent;")

        duration_label = QLabel(duration)
        duration_label.setFont(QFont("Segoe UI", 10))
        duration_label.setStyleSheet("color: #8e8e93; background: transparent;")

        info_layout.addWidget(date_label)
        info_layout.addStretch()
        info_layout.addWidget(duration_label)

        # Preview text
        preview_text = preview[:80] + "..." if len(preview) > 80 else preview
        preview_label = QLabel(preview_text if preview_text else "No transcript")
        preview_label.setFont(QFont("Segoe UI", 11))
        preview_label.setStyleSheet("color: #aeaeb2; background: transparent;")
        preview_label.setWordWrap(True)

        layout.addLayout(header_layout)
        layout.addLayout(info_layout)
        layout.addWidget(preview_label)

    def _apply_style(self):
        """Apply custom styling."""
        self.setStyleSheet("""
            QFrame#meetingListItem {
                background-color: rgba(44, 44, 46, 0.5);
                border-radius: 12px;
                border: 1px solid rgba(255, 255, 255, 0.05);
            }
            QFrame#meetingListItem:hover {
                background-color: rgba(58, 58, 60, 0.6);
                border: 1px solid rgba(255, 255, 255, 0.1);
            }
        """)

    def _on_delete_clicked(self):
        """Handle delete button click."""
        self.delete_requested.emit(self.meeting_id)

    def _show_context_menu(self, position):
        """Show context menu with meeting options."""
        menu = QMenu(self)
        menu.setStyleSheet("""
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
        """)

        # Open action
        open_action = menu.addAction("Open Meeting")
        open_action.triggered.connect(lambda: self.clicked.emit(self.meeting_id))

        menu.addSeparator()

        # Copy transcript action
        copy_action = menu.addAction("Copy Transcript")
        copy_action.triggered.connect(lambda: self.copy_transcript_requested.emit(self.meeting_id))

        menu.addSeparator()

        # Rename action
        rename_action = menu.addAction("Rename")
        rename_action.triggered.connect(self._on_rename_clicked)

        menu.addSeparator()

        # Delete action
        delete_action = menu.addAction("Delete Meeting")
        delete_action.triggered.connect(lambda: self.delete_requested.emit(self.meeting_id))

        menu.exec(self.mapToGlobal(position))

    def _on_rename_clicked(self):
        """Handle rename action."""
        new_title, ok = QInputDialog.getText(
            self,
            "Rename Meeting",
            "Enter new meeting title:",
            QLineEdit.EchoMode.Normal,
            self._title
        )
        if ok and new_title.strip():
            self.rename_requested.emit(self.meeting_id, new_title.strip())

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            # Don't emit click if clicking on delete button
            child = self.childAt(event.position().toPoint())
            if child != self.delete_btn:
                self.clicked.emit(self.meeting_id)
        super().mousePressEvent(event)


class HistorySidebar(QWidget):
    """Collapsible sidebar showing transcription history and saved recordings.

    Supports context-aware mode switching between:
    - MODE_QUICK_RECORD: Shows transcription history and recordings
    - MODE_MEETING: Shows past meetings list
    """

    # Signals for Quick Record mode
    entry_selected = pyqtSignal(str)  # Emits entry_id when clicked
    entry_copied = pyqtSignal(str)  # Emits entry_id when copy requested
    entry_deleted = pyqtSignal(str)  # Emits entry_id when delete requested
    retranscribe_requested = pyqtSignal(str)  # Emits audio file path

    # Signals for Meeting mode
    meeting_selected = pyqtSignal(str)  # Emits meeting_id when clicked
    meeting_delete_requested = pyqtSignal(str)  # Emits meeting_id
    meeting_rename_requested = pyqtSignal(str, str)  # meeting_id, new_title
    meeting_copy_requested = pyqtSignal(str)  # Emits meeting_id

    # Mode constants
    MODE_QUICK_RECORD = 0
    MODE_MEETING = 1

    COLLAPSED_WIDTH = 0
    EXPANDED_WIDTH = 380
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.logger = logging.getLogger(__name__)
        self._is_expanded = False
        self._current_width = self.COLLAPSED_WIDTH
        self._current_mode = self.MODE_QUICK_RECORD
        self._meetings_data: List[dict] = []  # Cached meetings data

        self._setup_ui()
        self._apply_style()

        # Start collapsed - use minimumWidth and maximumWidth instead of fixedWidth for smooth animation
        self.setMinimumWidth(self.COLLAPSED_WIDTH)
        self.setMaximumWidth(self.COLLAPSED_WIDTH)
    
    def _setup_ui(self):
        """Setup the sidebar UI with support for both Quick Record and Meeting modes."""
        self.setObjectName("historySidebar")

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Content container (will be animated)
        self.content_widget = QWidget()
        self.content_widget.setObjectName("sidebarContent")
        content_layout = QVBoxLayout(self.content_widget)
        content_layout.setContentsMargins(16, 16, 16, 16)
        content_layout.setSpacing(16)

        # Header with close button
        header_layout = QHBoxLayout()
        header_layout.setSpacing(8)

        self.header_label = QLabel("History")
        self.header_label.setObjectName("sidebarHeader")
        self.header_label.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        header_layout.addWidget(self.header_label)

        header_layout.addStretch()

        self.close_btn = QPushButton("\u00d7")  # X symbol
        self.close_btn.setObjectName("sidebarCloseBtn")
        self.close_btn.setFixedSize(28, 28)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.collapse)
        header_layout.addWidget(self.close_btn)

        content_layout.addLayout(header_layout)

        # ========== Quick Record Mode Content ==========
        self.quick_record_content = QWidget()
        quick_record_layout = QVBoxLayout(self.quick_record_content)
        quick_record_layout.setContentsMargins(0, 0, 0, 0)
        quick_record_layout.setSpacing(12)

        # Recordings section header
        recordings_header = QLabel("RECENT RECORDINGS")
        recordings_header.setObjectName("sectionHeader")
        recordings_header.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
        quick_record_layout.addWidget(recordings_header)

        # Recordings container
        self.recordings_container = QVBoxLayout()
        self.recordings_container.setSpacing(12)
        quick_record_layout.addLayout(self.recordings_container)

        # Divider
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setStyleSheet("background-color: rgba(255, 255, 255, 0.06); max-height: 1px; margin: 8px 0px;")
        quick_record_layout.addWidget(divider)

        # History section header
        history_header = QLabel("TRANSCRIPTION HISTORY")
        history_header.setObjectName("sectionHeader")
        history_header.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
        quick_record_layout.addWidget(history_header)

        # Scrollable history list
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setObjectName("historyScrollArea")

        self.history_list_widget = QWidget()
        self.history_list_layout = QVBoxLayout(self.history_list_widget)
        self.history_list_layout.setContentsMargins(0, 0, 0, 0)
        self.history_list_layout.setSpacing(12)
        self.history_list_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.scroll_area.setWidget(self.history_list_widget)
        quick_record_layout.addWidget(self.scroll_area, stretch=1)

        content_layout.addWidget(self.quick_record_content)

        # ========== Meeting Mode Content ==========
        self.meeting_content = QWidget()
        meeting_layout = QVBoxLayout(self.meeting_content)
        meeting_layout.setContentsMargins(0, 0, 0, 0)
        meeting_layout.setSpacing(12)

        # Meetings section header
        meetings_header = QLabel("PAST MEETINGS")
        meetings_header.setObjectName("sectionHeader")
        meetings_header.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
        meeting_layout.addWidget(meetings_header)

        # Scrollable meetings list
        self.meetings_scroll_area = QScrollArea()
        self.meetings_scroll_area.setWidgetResizable(True)
        self.meetings_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.meetings_scroll_area.setObjectName("historyScrollArea")

        self.meetings_list_widget = QWidget()
        self.meetings_list_layout = QVBoxLayout(self.meetings_list_widget)
        self.meetings_list_layout.setContentsMargins(0, 0, 0, 0)
        self.meetings_list_layout.setSpacing(12)
        self.meetings_list_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.meetings_scroll_area.setWidget(self.meetings_list_widget)
        meeting_layout.addWidget(self.meetings_scroll_area, stretch=1)

        content_layout.addWidget(self.meeting_content)

        # Hide meeting content initially (Quick Record mode is default)
        self.meeting_content.hide()

        main_layout.addWidget(self.content_widget)

        # Animation for expand/collapse - animate both min and max width together
        self.animation = QPropertyAnimation(self, b"sidebarWidth")
        self.animation.setDuration(250)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.finished.connect(self._on_animation_finished)
    
    def _get_sidebar_width(self):
        """Get the current sidebar width."""
        return self._current_width
    
    def _set_sidebar_width(self, width):
        """Set the sidebar width (used by animation)."""
        self._current_width = int(width)
        self.setMinimumWidth(self._current_width)
        self.setMaximumWidth(self._current_width)
    
    sidebarWidth = pyqtProperty(int, _get_sidebar_width, _set_sidebar_width)
    
    def _on_animation_finished(self):
        """Called when animation finishes."""
        # Ensure final state is correct
        if self._is_expanded:
            self.setMinimumWidth(self.EXPANDED_WIDTH)
            self.setMaximumWidth(self.EXPANDED_WIDTH)
            # Refresh content after expansion is complete to avoid glitches during animation
            self.refresh()
        else:
            self.setMinimumWidth(self.COLLAPSED_WIDTH)
            self.setMaximumWidth(self.COLLAPSED_WIDTH)
    
    def _apply_style(self):
        """Apply custom styling."""
        self.setStyleSheet("""
            QWidget#historySidebar {
                background-color: #1c1c1e;
                border-left: 1px solid rgba(255, 255, 255, 0.08);
            }
            QWidget#sidebarContent {
                background-color: #1c1c1e;
            }
            QLabel#sidebarHeader {
                color: #ffffff;
                font-weight: 700;
            }
            QLabel#sectionHeader {
                color: #98989d;
                padding-top: 4px;
                letter-spacing: 0.5px;
                text-transform: uppercase;
                font-size: 10px;
                font-weight: 600;
            }
            QPushButton#sidebarCloseBtn {
                background-color: transparent;
                color: #8e8e93;
                border: none;
                border-radius: 14px;
                font-size: 20px;
                font-weight: bold;
            }
            QPushButton#sidebarCloseBtn:hover {
                background-color: rgba(255, 255, 255, 0.1);
                color: #ffffff;
            }
            QScrollArea#historyScrollArea {
                background-color: transparent;
                border: none;
            }
            QScrollArea#historyScrollArea > QWidget > QWidget {
                background-color: transparent;
            }
        """)
    
    def expand(self):
        """Expand the sidebar."""
        if self._is_expanded:
            return
        
        self._is_expanded = True
        
        # Start animation immediately - no delay
        self.animation.stop()
        current_width = self.width() if self.width() > 0 else self.COLLAPSED_WIDTH
        self.animation.setStartValue(current_width)
        self.animation.setEndValue(self.EXPANDED_WIDTH)
        self.animation.start()
        
        # Refresh will happen automatically when animation finishes (in _on_animation_finished)
        
        self.logger.debug("Sidebar expanded")
    
    def collapse(self):
        """Collapse the sidebar."""
        if not self._is_expanded:
            return
        
        self._is_expanded = False
        
        # Start animation immediately - smooth collapse
        self.animation.stop()
        current_width = self.width() if self.width() > 0 else self.EXPANDED_WIDTH
        self.animation.setStartValue(current_width)
        self.animation.setEndValue(self.COLLAPSED_WIDTH)
        self.animation.start()
        
        self.logger.debug("Sidebar collapsed")
    
    def toggle(self):
        """Toggle sidebar visibility."""
        if self._is_expanded:
            self.collapse()
        else:
            self.expand()
    
    @property
    def is_expanded(self) -> bool:
        """Return whether sidebar is expanded."""
        return self._is_expanded

    def set_mode(self, mode: int):
        """Switch sidebar content based on active tab.

        Args:
            mode: MODE_QUICK_RECORD or MODE_MEETING
        """
        if mode == self._current_mode:
            return

        self._current_mode = mode

        if mode == self.MODE_QUICK_RECORD:
            self.header_label.setText("History")
            self.meeting_content.hide()
            self.quick_record_content.show()
            if self._is_expanded:
                self._load_recordings()
                self._load_history()
        else:
            self.header_label.setText("Meetings")
            self.quick_record_content.hide()
            self.meeting_content.show()
            if self._is_expanded:
                self._load_meetings()

        self.logger.debug(f"Sidebar mode changed to: {mode}")

    def get_mode(self) -> int:
        """Get the current sidebar mode."""
        return self._current_mode

    def refresh(self):
        """Refresh the sidebar content based on current mode."""
        if self._current_mode == self.MODE_QUICK_RECORD:
            self._load_recordings()
            self._load_history()
        else:
            self._load_meetings()

    def refresh_meetings(self, meetings: List[dict]):
        """Update the meetings data and refresh the list.

        Args:
            meetings: List of meeting dictionaries with id, title, date, duration, preview, status
        """
        self._meetings_data = meetings
        if self._current_mode == self.MODE_MEETING and self._is_expanded:
            self._load_meetings()
    
    def _load_recordings(self):
        """Load and display saved recordings."""
        # Clear existing items
        while self.recordings_container.count():
            item = self.recordings_container.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        
        recordings = history_manager.get_recordings()
        
        if not recordings:
            no_recordings_label = QLabel("No saved recordings")
            no_recordings_label.setStyleSheet("color: #636366; font-size: 12px;")
            no_recordings_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.recordings_container.addWidget(no_recordings_label)
            return
        
        for recording in recordings:
            item = RecordingItemWidget(recording)
            item.retranscribe_requested.connect(self.retranscribe_requested.emit)
            self.recordings_container.addWidget(item)
    
    def _load_history(self):
        """Load and display transcription history."""
        # Clear existing items
        while self.history_list_layout.count():
            item = self.history_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        entries = history_manager.get_history()

        if not entries:
            no_history_label = QLabel("No transcription history")
            no_history_label.setStyleSheet("color: #636366; font-size: 12px;")
            no_history_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.history_list_layout.addWidget(no_history_label)
            return

        for entry in entries:
            item = HistoryItemWidget(entry)
            item.clicked.connect(self._on_entry_clicked)
            item.copy_requested.connect(self._on_copy_requested)
            item.delete_requested.connect(self._on_delete_requested)
            item.retranscribe_requested.connect(self.retranscribe_requested.emit)
            self.history_list_layout.addWidget(item)

    def _load_meetings(self):
        """Load and display past meetings."""
        # Clear existing items
        while self.meetings_list_layout.count():
            item = self.meetings_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self._meetings_data:
            no_meetings_label = QLabel("No meetings yet.\nStart your first meeting!")
            no_meetings_label.setStyleSheet("color: #636366; font-size: 12px;")
            no_meetings_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.meetings_list_layout.addWidget(no_meetings_label)
            return

        for meeting in self._meetings_data:
            item = MeetingListItemWidget(
                meeting_id=meeting['id'],
                title=meeting.get('title', 'Untitled'),
                date=meeting.get('date', ''),
                duration=meeting.get('duration', ''),
                preview=meeting.get('preview', ''),
                status=meeting.get('status', 'completed')
            )
            item.clicked.connect(self._on_meeting_clicked)
            item.delete_requested.connect(self._on_meeting_delete_requested)
            item.rename_requested.connect(self._on_meeting_rename_requested)
            item.copy_transcript_requested.connect(self._on_meeting_copy_requested)
            self.meetings_list_layout.addWidget(item)
    
    def _on_entry_clicked(self, entry_id: str):
        """Handle history entry click."""
        entry = history_manager.get_entry_by_id(entry_id)
        if entry:
            self.entry_selected.emit(entry_id)
            self.logger.debug(f"Entry selected: {entry_id[:8]}...")
    
    def _on_copy_requested(self, entry_id: str):
        """Handle copy request."""
        entry = history_manager.get_entry_by_id(entry_id)
        if entry:
            try:
                clipboard = QApplication.clipboard()
                clipboard.setText(entry.text)
                self.entry_copied.emit(entry_id)
                self.logger.info(f"Copied entry to clipboard: {entry_id[:8]}...")
            except Exception as e:
                self.logger.error(f"Failed to copy to clipboard: {e}")
    
    def _on_delete_requested(self, entry_id: str):
        """Handle delete request."""
        if history_manager.delete_entry(entry_id):
            self.entry_deleted.emit(entry_id)
            self.refresh()  # Refresh the list
            self.logger.info(f"Deleted entry: {entry_id[:8]}...")

    # Meeting mode event handlers
    def _on_meeting_clicked(self, meeting_id: str):
        """Handle meeting item click."""
        self.meeting_selected.emit(meeting_id)
        self.logger.debug(f"Meeting selected: {meeting_id[:8]}...")

    def _on_meeting_delete_requested(self, meeting_id: str):
        """Handle meeting delete request."""
        self.meeting_delete_requested.emit(meeting_id)

    def _on_meeting_rename_requested(self, meeting_id: str, new_title: str):
        """Handle meeting rename request."""
        self.meeting_rename_requested.emit(meeting_id, new_title)

    def _on_meeting_copy_requested(self, meeting_id: str):
        """Handle meeting copy transcript request."""
        self.meeting_copy_requested.emit(meeting_id)


class HistoryToggleButton(QPushButton):
    """Toggle button to show/hide the history sidebar."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setText("History")
        self.setObjectName("historyToggleBtn")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(36)
        
        self._apply_style()
    
    def _apply_style(self):
        """Apply custom styling."""
        self.setStyleSheet("""
            QPushButton#historyToggleBtn {
                background-color: #2c2c2e;
                color: #f5f5f7;
                border: 1px solid #3a3a3c;
                border-radius: 8px;
                padding: 8px 16px;
                font-size: 13px;
                font-weight: 500;
            }
            QPushButton#historyToggleBtn:hover {
                background-color: #3a3a3c;
                border-color: #48484a;
            }
            QPushButton#historyToggleBtn:pressed {
                background-color: #1c1c1e;
            }
        """)


class HistoryEdgeTab(QPushButton):
    """Vertical edge tab button to toggle history sidebar - always visible."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("historyEdgeTab")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedWidth(24)
        self.setMinimumHeight(80)
        self._is_expanded = False
        self._update_icon()
        self._apply_style()
    
    def set_expanded(self, expanded: bool):
        """Update the tab state."""
        self._is_expanded = expanded
        self._update_icon()
    
    def _update_icon(self):
        """Update the icon based on expanded state."""
        # Use arrow characters to indicate direction
        if self._is_expanded:
            self.setText("›")  # Arrow pointing right (to collapse)
            self.setToolTip("Close History")
        else:
            self.setText("‹")  # Arrow pointing left (to expand)
            self.setToolTip("Open History")
    
    def _apply_style(self):
        """Apply custom styling."""
        self.setStyleSheet("""
            QPushButton#historyEdgeTab {
                background-color: #2c2c2e;
                color: #8e8e93;
                border: 1px solid #3a3a3c;
                border-right: none;
                border-top-left-radius: 8px;
                border-bottom-left-radius: 8px;
                border-top-right-radius: 0px;
                border-bottom-right-radius: 0px;
                font-size: 16px;
                font-weight: bold;
                padding: 0px;
            }
            QPushButton#historyEdgeTab:hover {
                background-color: #3a3a3c;
                color: #f5f5f7;
            }
            QPushButton#historyEdgeTab:pressed {
                background-color: #1c1c1e;
            }
        """)


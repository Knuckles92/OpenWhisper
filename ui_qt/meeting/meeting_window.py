"""
Meeting Mode Window for long-form transcription sessions.
Provides a dedicated window for recording and transcribing meetings,
lectures, and other extended audio sessions.
"""
import logging
from typing import Optional, Callable, List
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QTextEdit, QFrame, QPushButton, QLineEdit,
    QSplitter, QListWidget, QListWidgetItem, QScrollArea,
    QMenu, QInputDialog, QFileDialog, QApplication, QMessageBox
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QSize
from PyQt6.QtGui import QFont, QColor, QAction, QCursor

from ui_qt.widgets import (
    Card, HeaderCard, ControlPanel,
    SuccessButton, DangerButton, PrimaryButton, WarningButton
)


class MeetingTimerWidget(QWidget):
    """Widget displaying elapsed meeting time."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self._elapsed_seconds = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update_time)
        
        self._setup_ui()
    
    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # Recording indicator dot
        self.indicator = QLabel("●")
        self.indicator.setStyleSheet("color: #48484a; font-size: 18px;")
        self.indicator.setFixedWidth(24)
        
        # Time display
        self.time_label = QLabel("00:00:00")
        self.time_label.setFont(QFont("Segoe UI", 28, QFont.Weight.Bold))
        self.time_label.setStyleSheet("color: #f5f5f7;")
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        layout.addStretch()
        layout.addWidget(self.indicator)
        layout.addWidget(self.time_label)
        layout.addStretch()
    
    def start(self):
        """Start the timer."""
        self._elapsed_seconds = 0
        self._update_display()
        self._timer.start(1000)
        self.indicator.setStyleSheet("color: #ff453a; font-size: 18px;")
    
    def stop(self):
        """Stop the timer."""
        self._timer.stop()
        self.indicator.setStyleSheet("color: #48484a; font-size: 18px;")
    
    def reset(self):
        """Reset the timer."""
        self._timer.stop()
        self._elapsed_seconds = 0
        self._update_display()
        self.indicator.setStyleSheet("color: #48484a; font-size: 18px;")
    
    def _update_time(self):
        """Update the elapsed time."""
        self._elapsed_seconds += 1
        self._update_display()
    
    def _update_display(self):
        """Update the time display."""
        hours = self._elapsed_seconds // 3600
        minutes = (self._elapsed_seconds % 3600) // 60
        seconds = self._elapsed_seconds % 60
        self.time_label.setText(f"{hours:02d}:{minutes:02d}:{seconds:02d}")
    
    @property
    def elapsed_seconds(self) -> int:
        """Get the elapsed time in seconds."""
        return self._elapsed_seconds


class MeetingListItem(QFrame):
    """A single meeting item in the past meetings list."""
    
    clicked = pyqtSignal(str)  # meeting_id
    delete_requested = pyqtSignal(str)  # meeting_id
    rename_requested = pyqtSignal(str, str)  # meeting_id, new_title
    copy_transcript_requested = pyqtSignal(str)  # meeting_id
    export_requested = pyqtSignal(str)  # meeting_id
    
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
        self.setStyleSheet("""
            #meetingListItem {
                background-color: #2c2c2e;
                border-radius: 8px;
                border: 1px solid #3a3a3c;
                padding: 8px;
            }
            #meetingListItem:hover {
                background-color: #3a3a3c;
                border: 1px solid #48484a;
            }
        """)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)
        
        # Header row: title + delete button
        header_layout = QHBoxLayout()
        
        title_label = QLabel(title or "Untitled Meeting")
        title_label.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        title_label.setStyleSheet("color: #f5f5f7; background: transparent;")
        
        # Status indicator for interrupted meetings
        if status == "interrupted":
            status_label = QLabel("⚠")
            status_label.setToolTip("This meeting was interrupted")
            status_label.setStyleSheet("color: #ff9f0a; background: transparent; font-size: 12px;")
            header_layout.addWidget(status_label)
        
        header_layout.addWidget(title_label)
        header_layout.addStretch()
        
        # Delete button (small, subtle)
        self.delete_btn = QPushButton("×")
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
        preview_label = QLabel(preview[:80] + "..." if len(preview) > 80 else preview)
        preview_label.setFont(QFont("Segoe UI", 11))
        preview_label.setStyleSheet("color: #aeaeb2; background: transparent;")
        preview_label.setWordWrap(True)
        
        layout.addLayout(header_layout)
        layout.addLayout(info_layout)
        layout.addWidget(preview_label)
    
    def _on_delete_clicked(self):
        """Handle delete button click."""
        self.delete_requested.emit(self.meeting_id)
    
    def _show_context_menu(self, position):
        """Show context menu with meeting options."""
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #2c2c2e;
                color: #f5f5f7;
                border: 1px solid #3a3a3c;
                border-radius: 8px;
                padding: 4px;
            }
            QMenu::item {
                background-color: transparent;
                padding: 8px 24px 8px 12px;
                border-radius: 4px;
                margin: 2px 4px;
            }
            QMenu::item:selected {
                background-color: #0a84ff;
            }
            QMenu::separator {
                height: 1px;
                background-color: #3a3a3c;
                margin: 4px 8px;
            }
        """)
        
        # Open/View action
        open_action = QAction("📄 Open Meeting", self)
        open_action.triggered.connect(lambda: self.clicked.emit(self.meeting_id))
        menu.addAction(open_action)
        
        menu.addSeparator()
        
        # Copy transcript action
        copy_action = QAction("📋 Copy Transcript", self)
        copy_action.triggered.connect(lambda: self.copy_transcript_requested.emit(self.meeting_id))
        menu.addAction(copy_action)
        
        # Export action
        export_action = QAction("💾 Export as Text File", self)
        export_action.triggered.connect(lambda: self.export_requested.emit(self.meeting_id))
        menu.addAction(export_action)
        
        menu.addSeparator()
        
        # Rename action
        rename_action = QAction("✏️ Rename", self)
        rename_action.triggered.connect(self._on_rename_clicked)
        menu.addAction(rename_action)
        
        menu.addSeparator()
        
        # Delete action (styled red)
        delete_action = QAction("🗑️ Delete Meeting", self)
        delete_action.triggered.connect(lambda: self.delete_requested.emit(self.meeting_id))
        menu.addAction(delete_action)
        
        # Show the menu at cursor position
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


class PastMeetingsSidebar(QWidget):
    """Sidebar showing list of past meetings."""
    
    meeting_selected = pyqtSignal(str)  # meeting_id
    meeting_delete_requested = pyqtSignal(str)  # meeting_id
    meeting_rename_requested = pyqtSignal(str, str)  # meeting_id, new_title
    meeting_copy_requested = pyqtSignal(str)  # meeting_id
    meeting_export_requested = pyqtSignal(str)  # meeting_id
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(280)
        self.setMaximumWidth(350)
        
        self._setup_ui()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        
        # Header
        header = QLabel("Past Meetings")
        header.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        header.setStyleSheet("color: #f5f5f7; padding: 8px 12px;")
        layout.addWidget(header)
        
        # Scroll area for meetings list
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("""
            QScrollArea {
                background-color: transparent;
                border: none;
            }
            QScrollArea > QWidget > QWidget {
                background-color: transparent;
            }
        """)
        
        self.list_container = QWidget()
        self.list_layout = QVBoxLayout(self.list_container)
        self.list_layout.setContentsMargins(8, 0, 8, 8)
        self.list_layout.setSpacing(8)
        self.list_layout.addStretch()
        
        scroll.setWidget(self.list_container)
        layout.addWidget(scroll)
        
        # Empty state label
        self.empty_label = QLabel("No meetings yet.\nStart your first meeting!")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setStyleSheet("color: #636366; font-size: 12px;")
        self.list_layout.insertWidget(0, self.empty_label)
    
    def refresh(self, meetings: List[dict]):
        """Refresh the meetings list.
        
        Args:
            meetings: List of meeting dictionaries with id, title, date, duration, preview, status
        """
        # Clear existing items (except stretch)
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        
        if not meetings:
            self.empty_label = QLabel("No meetings yet.\nStart your first meeting!")
            self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.empty_label.setStyleSheet("color: #636366; font-size: 12px;")
            self.list_layout.insertWidget(0, self.empty_label)
        else:
            for meeting in meetings:
                item = MeetingListItem(
                    meeting_id=meeting['id'],
                    title=meeting.get('title', 'Untitled'),
                    date=meeting.get('date', ''),
                    duration=meeting.get('duration', ''),
                    preview=meeting.get('preview', ''),
                    status=meeting.get('status', 'completed')
                )
                item.clicked.connect(self._on_meeting_clicked)
                item.delete_requested.connect(self._on_meeting_delete)
                item.rename_requested.connect(self._on_meeting_rename)
                item.copy_transcript_requested.connect(self._on_meeting_copy)
                item.export_requested.connect(self._on_meeting_export)
                self.list_layout.insertWidget(self.list_layout.count() - 1, item)
    
    def _on_meeting_clicked(self, meeting_id: str):
        self.meeting_selected.emit(meeting_id)
    
    def _on_meeting_delete(self, meeting_id: str):
        self.meeting_delete_requested.emit(meeting_id)
    
    def _on_meeting_rename(self, meeting_id: str, new_title: str):
        self.meeting_rename_requested.emit(meeting_id, new_title)
    
    def _on_meeting_copy(self, meeting_id: str):
        self.meeting_copy_requested.emit(meeting_id)
    
    def _on_meeting_export(self, meeting_id: str):
        self.meeting_export_requested.emit(meeting_id)


class MeetingModeWindow(QMainWindow):
    """Main window for Meeting Mode - long-form transcription sessions."""
    
    # Signals
    meeting_started = pyqtSignal()
    meeting_stopped = pyqtSignal()
    meeting_selected = pyqtSignal(str)  # meeting_id
    meeting_delete_requested = pyqtSignal(str)  # meeting_id
    meeting_rename_requested = pyqtSignal(str, str)  # meeting_id, new_title
    meeting_copy_requested = pyqtSignal(str)  # meeting_id
    meeting_export_requested = pyqtSignal(str)  # meeting_id
    
    # States
    STATE_IDLE = "idle"
    STATE_RECORDING = "recording"
    STATE_PROCESSING = "processing"
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.logger = logging.getLogger(__name__)
        
        self.setWindowTitle("Meeting Mode - OpenWhisper")
        self.setMinimumSize(900, 650)
        self.resize(1100, 750)
        
        # State
        self._state = self.STATE_IDLE
        self._current_meeting_id: Optional[str] = None
        
        # Callbacks
        self.on_start_meeting: Optional[Callable] = None
        self.on_stop_meeting: Optional[Callable] = None
        self.on_load_meeting: Optional[Callable[[str], None]] = None
        self.on_delete_meeting: Optional[Callable[[str], None]] = None
        self.on_rename_meeting: Optional[Callable[[str, str], None]] = None
        self.on_get_meeting: Optional[Callable[[str], Optional[dict]]] = None
        
        self._setup_ui()
        self._connect_signals()
    
    def _setup_ui(self):
        """Setup the user interface."""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        central_widget.setStyleSheet("background-color: #1c1c1e;")
        
        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        # Left: Main content area
        content_area = QWidget()
        content_layout = QVBoxLayout(content_area)
        content_layout.setContentsMargins(24, 24, 24, 24)
        content_layout.setSpacing(16)
        
        # Title input row
        title_row = QHBoxLayout()
        title_label = QLabel("Meeting Title:")
        title_label.setFont(QFont("Segoe UI", 12))
        title_label.setStyleSheet("color: #8e8e93;")
        
        self.title_input = QLineEdit()
        self.title_input.setPlaceholderText("Enter meeting name (optional)")
        self.title_input.setMaximumWidth(400)
        self.title_input.setStyleSheet("""
            QLineEdit {
                background-color: #2c2c2e;
                color: #f5f5f7;
                border: 1px solid #3a3a3c;
                border-radius: 8px;
                padding: 10px 16px;
                font-size: 14px;
            }
            QLineEdit:focus {
                border: 1px solid #0a84ff;
            }
        """)
        
        title_row.addWidget(title_label)
        title_row.addWidget(self.title_input)
        title_row.addStretch()
        content_layout.addLayout(title_row)
        
        # Timer display
        self.timer_widget = MeetingTimerWidget()
        content_layout.addWidget(self.timer_widget)
        
        # Status label
        self.status_label = QLabel("Ready to start meeting")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setFont(QFont("Segoe UI", 13))
        self.status_label.setStyleSheet("color: #8e8e93;")
        content_layout.addWidget(self.status_label)
        
        # Control buttons - made extra prominent for meeting mode
        control_panel = QWidget()
        control_layout = QHBoxLayout(control_panel)
        control_layout.setSpacing(16)
        
        self.start_button = SuccessButton("Start Meeting")
        self.start_button.setMinimumWidth(200)
        self.start_button.setMinimumHeight(56)
        self.start_button.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        self.start_button.setStyleSheet("""
            QPushButton {
                background-color: #30d158;
                color: #ffffff;
                border: none;
                border-radius: 12px;
                padding: 14px 28px;
                font-size: 15px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #34d860;
                border: 2px solid rgba(48, 209, 88, 0.5);
            }
            QPushButton:pressed {
                background-color: #28b84c;
            }
            QPushButton:disabled {
                background-color: #2a3d2f;
                color: #5a7a5f;
            }
        """)
        
        self.stop_button = DangerButton("End Meeting")
        self.stop_button.setMinimumWidth(200)
        self.stop_button.setMinimumHeight(56)
        self.stop_button.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        self.stop_button.setStyleSheet("""
            QPushButton {
                background-color: #ff453a;
                color: #ffffff;
                border: none;
                border-radius: 12px;
                padding: 14px 28px;
                font-size: 15px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #ff5c52;
                border: 2px solid rgba(255, 69, 58, 0.5);
            }
            QPushButton:pressed {
                background-color: #e03e34;
            }
            QPushButton:disabled {
                background-color: #3d2a2a;
                color: #7a5a5a;
            }
        """)
        self.stop_button.setEnabled(False)
        
        control_layout.addStretch()
        control_layout.addWidget(self.start_button)
        control_layout.addWidget(self.stop_button)
        control_layout.addStretch()
        
        content_layout.addWidget(control_panel)
        
        # Transcription display card
        transcription_card = HeaderCard("Live Transcription")
        transcription_card.setMinimumHeight(350)
        
        self.transcription_text = QTextEdit()
        self.transcription_text.setReadOnly(True)
        self.transcription_text.setFont(QFont("Segoe UI", 13))
        self.transcription_text.setPlaceholderText(
            "Live transcription will appear here as the meeting progresses...\n\n"
            "Click 'Start Meeting' to begin recording and transcribing."
        )
        self.transcription_text.setStyleSheet("""
            QTextEdit {
                background-color: #2c2c2e;
                color: #f5f5f7;
                border: none;
                border-radius: 8px;
                padding: 16px;
                line-height: 1.6;
            }
        """)
        
        transcription_card.layout.addWidget(self.transcription_text)
        content_layout.addWidget(transcription_card, stretch=1)
        
        # Copy button row
        copy_row = QHBoxLayout()
        copy_row.addStretch()
        
        self.copy_button = PrimaryButton("Copy Transcript")
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self._copy_transcript)
        copy_row.addWidget(self.copy_button)
        
        content_layout.addLayout(copy_row)
        
        main_layout.addWidget(content_area, stretch=1)
        
        # Right: Past meetings sidebar
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setStyleSheet("background-color: #3a3a3c;")
        separator.setFixedWidth(1)
        main_layout.addWidget(separator)
        
        self.past_meetings_sidebar = PastMeetingsSidebar()
        self.past_meetings_sidebar.setStyleSheet("background-color: #1c1c1e;")
        main_layout.addWidget(self.past_meetings_sidebar)
    
    def _connect_signals(self):
        """Connect button signals."""
        self.start_button.clicked.connect(self._on_start_clicked)
        self.stop_button.clicked.connect(self._on_stop_clicked)
        self.past_meetings_sidebar.meeting_selected.connect(self._on_meeting_selected)
        self.past_meetings_sidebar.meeting_delete_requested.connect(self._on_meeting_delete_requested)
        self.past_meetings_sidebar.meeting_rename_requested.connect(self._on_meeting_rename_requested)
        self.past_meetings_sidebar.meeting_copy_requested.connect(self._on_meeting_copy_requested)
        self.past_meetings_sidebar.meeting_export_requested.connect(self._on_meeting_export_requested)
    
    def _on_start_clicked(self):
        """Handle start meeting button click."""
        self.logger.info("Start meeting clicked")
        self._set_state(self.STATE_RECORDING)
        
        if self.on_start_meeting:
            self.on_start_meeting()
        
        self.meeting_started.emit()
    
    def _on_stop_clicked(self):
        """Handle stop meeting button click."""
        self.logger.info("Stop meeting clicked")
        self._set_state(self.STATE_PROCESSING)
        
        if self.on_stop_meeting:
            self.on_stop_meeting()
        
        self.meeting_stopped.emit()
    
    def _on_meeting_selected(self, meeting_id: str):
        """Handle past meeting selection."""
        self.logger.info(f"Meeting selected: {meeting_id}")
        
        if self.on_load_meeting:
            self.on_load_meeting(meeting_id)
        
        self.meeting_selected.emit(meeting_id)
    
    def _on_meeting_delete_requested(self, meeting_id: str):
        """Handle meeting deletion request."""
        self.logger.info(f"Meeting delete requested: {meeting_id}")
        
        # Show confirmation dialog
        reply = QMessageBox.question(
            self,
            "Delete Meeting",
            "Are you sure you want to delete this meeting?\n\nThis action cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            if self.on_delete_meeting:
                self.on_delete_meeting(meeting_id)
            
            self.meeting_delete_requested.emit(meeting_id)
    
    def _on_meeting_rename_requested(self, meeting_id: str, new_title: str):
        """Handle meeting rename request."""
        self.logger.info(f"Meeting rename requested: {meeting_id} -> {new_title}")
        
        if self.on_rename_meeting:
            self.on_rename_meeting(meeting_id, new_title)
        
        self.meeting_rename_requested.emit(meeting_id, new_title)
    
    def _on_meeting_copy_requested(self, meeting_id: str):
        """Handle copy transcript request."""
        self.logger.info(f"Meeting copy requested: {meeting_id}")
        
        # Get meeting data and copy transcript
        if self.on_get_meeting:
            meeting = self.on_get_meeting(meeting_id)
            if meeting and meeting.transcript:
                clipboard = QApplication.clipboard()
                clipboard.setText(meeting.transcript)
                self.set_status("Transcript copied to clipboard!")
                QTimer.singleShot(2000, lambda: self.set_status(
                    "Ready to start meeting" if self._state == self.STATE_IDLE 
                    else "Recording in progress..."
                ))
            else:
                self.set_status("No transcript to copy")
                QTimer.singleShot(2000, lambda: self.set_status("Ready to start meeting"))
        
        self.meeting_copy_requested.emit(meeting_id)
    
    def _on_meeting_export_requested(self, meeting_id: str):
        """Handle export meeting request."""
        self.logger.info(f"Meeting export requested: {meeting_id}")
        
        if self.on_get_meeting:
            meeting = self.on_get_meeting(meeting_id)
            if meeting:
                # Open file save dialog
                default_filename = f"{meeting.title.replace(' ', '_')}.txt"
                file_path, _ = QFileDialog.getSaveFileName(
                    self,
                    "Export Meeting Transcript",
                    default_filename,
                    "Text Files (*.txt);;Markdown Files (*.md);;All Files (*.*)"
                )
                
                if file_path:
                    try:
                        # Build export content
                        content = f"Meeting: {meeting.title}\n"
                        content += f"Date: {meeting.formatted_start_time}\n"
                        content += f"Duration: {meeting.formatted_duration}\n"
                        content += f"Status: {meeting.status}\n"
                        content += "-" * 50 + "\n\n"
                        content += meeting.transcript or "(No transcript available)"
                        
                        # Write to file
                        with open(file_path, 'w', encoding='utf-8') as f:
                            f.write(content)
                        
                        self.set_status(f"Exported to {file_path}")
                        QTimer.singleShot(3000, lambda: self.set_status(
                            "Ready to start meeting" if self._state == self.STATE_IDLE 
                            else "Recording in progress..."
                        ))
                    except Exception as e:
                        self.logger.error(f"Failed to export meeting: {e}")
                        QMessageBox.warning(
                            self,
                            "Export Failed",
                            f"Failed to export meeting:\n{str(e)}"
                        )
        
        self.meeting_export_requested.emit(meeting_id)
    
    def _set_state(self, state: str):
        """Update the UI state."""
        self._state = state
        
        if state == self.STATE_IDLE:
            self.start_button.setEnabled(True)
            self.start_button.setText("Start Meeting")
            self.stop_button.setEnabled(False)
            self.title_input.setEnabled(True)
            self.status_label.setText("Ready to start meeting")
            self.status_label.setStyleSheet("color: #8e8e93;")
            self.timer_widget.reset()
            
        elif state == self.STATE_RECORDING:
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(True)
            self.title_input.setEnabled(False)
            self.status_label.setText("Recording in progress...")
            self.status_label.setStyleSheet("color: #30d158;")
            self.timer_widget.start()
            self.transcription_text.clear()
            
        elif state == self.STATE_PROCESSING:
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(False)
            self.status_label.setText("Processing final transcription...")
            self.status_label.setStyleSheet("color: #ff9f0a;")
            self.timer_widget.stop()
    
    def set_idle(self):
        """Set the window to idle state."""
        self._set_state(self.STATE_IDLE)
        self.copy_button.setEnabled(bool(self.transcription_text.toPlainText()))
    
    def set_recording(self):
        """Set the window to recording state."""
        self._set_state(self.STATE_RECORDING)
    
    def set_processing(self):
        """Set the window to processing state."""
        self._set_state(self.STATE_PROCESSING)
    
    def set_status(self, status: str):
        """Update the status label."""
        self.status_label.setText(status)
    
    def append_transcription(self, text: str):
        """Append text to the transcription display.
        
        Args:
            text: Text to append
        """
        cursor = self.transcription_text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        
        if self.transcription_text.toPlainText():
            cursor.insertText(" ")
        cursor.insertText(text)
        
        self.transcription_text.setTextCursor(cursor)
        self.transcription_text.ensureCursorVisible()
        
        # Enable copy button when we have text
        self.copy_button.setEnabled(True)
    
    def set_transcription(self, text: str):
        """Set the full transcription text.
        
        Args:
            text: Full transcription text
        """
        self.transcription_text.setPlainText(text)
        self.copy_button.setEnabled(bool(text))
    
    def clear_transcription(self):
        """Clear the transcription display."""
        self.transcription_text.clear()
        self.copy_button.setEnabled(False)
    
    def get_meeting_title(self) -> str:
        """Get the meeting title from the input field."""
        return self.title_input.text().strip()
    
    def set_meeting_title(self, title: str):
        """Set the meeting title in the input field."""
        self.title_input.setText(title)
    
    def get_elapsed_time(self) -> int:
        """Get the elapsed recording time in seconds."""
        return self.timer_widget.elapsed_seconds
    
    def refresh_meetings_list(self, meetings: List[dict]):
        """Refresh the past meetings sidebar.
        
        Args:
            meetings: List of meeting dictionaries
        """
        self.past_meetings_sidebar.refresh(meetings)
    
    def _copy_transcript(self):
        """Copy the transcript to clipboard."""
        text = self.transcription_text.toPlainText()
        if text:
            from PyQt6.QtWidgets import QApplication
            clipboard = QApplication.clipboard()
            clipboard.setText(text)
            
            self.set_status("Transcript copied to clipboard!")
            QTimer.singleShot(2000, lambda: self.set_status(
                "Ready to start meeting" if self._state == self.STATE_IDLE 
                else "Recording in progress..."
            ))
    
    def closeEvent(self, event):
        """Handle window close."""
        if self._state == self.STATE_RECORDING:
            # If recording, stop first
            self._on_stop_clicked()
        
        event.accept()

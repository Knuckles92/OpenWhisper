"""
Meeting Tab widget for long-form transcription sessions.
Provides a tab-based interface for recording and transcribing meetings,
lectures, and other extended audio sessions.
"""
import logging
from typing import Optional, Callable
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTextEdit, QLineEdit,
    QApplication
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont

from ui_qt.widgets.cards import HeaderCard
from ui_qt.widgets.buttons import SuccessButton, DangerButton, PrimaryButton


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
        self.indicator = QLabel("\u25cf")  # Bullet character
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


class MeetingTab(QWidget):
    """Tab widget for Meeting Mode - long-form transcription sessions."""

    # Signals
    meeting_started = pyqtSignal()
    meeting_stopped = pyqtSignal()

    # States
    STATE_IDLE = "idle"
    STATE_RECORDING = "recording"
    STATE_PROCESSING = "processing"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.logger = logging.getLogger(__name__)

        # State
        self._state = self.STATE_IDLE
        self._current_meeting_id: Optional[str] = None

        # Callbacks (will be set by controller)
        self.on_start_meeting: Optional[Callable] = None
        self.on_stop_meeting: Optional[Callable] = None
        self.on_load_meeting: Optional[Callable[[str], None]] = None
        self.on_delete_meeting: Optional[Callable[[str], None]] = None
        self.on_rename_meeting: Optional[Callable[[str, str], None]] = None
        self.on_get_meeting: Optional[Callable[[str], Optional[dict]]] = None

        self._setup_ui()
        self._connect_signals()

    def _setup_ui(self):
        """Setup the widget UI."""
        # Main layout
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Content area (centered)
        content_container = QWidget()
        content_container.setObjectName("meetingContent")
        content_layout = QVBoxLayout(content_container)
        content_layout.setContentsMargins(24, 16, 24, 24)
        content_layout.setSpacing(16)

        # Wrapper to center content horizontally
        center_wrapper = QHBoxLayout()
        center_wrapper.addStretch()
        center_wrapper.addWidget(content_container, stretch=1)
        center_wrapper.addStretch()

        # Limit max width of content
        content_container.setMaximumWidth(800)
        content_container.setMinimumWidth(500)

        main_layout.addLayout(center_wrapper)

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

        title_row.addStretch()
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

        # Control buttons
        control_panel = QWidget()
        control_layout = QHBoxLayout(control_panel)
        control_layout.setSpacing(16)

        self.start_button = SuccessButton("Start Meeting")
        self.start_button.setMinimumWidth(180)
        self.start_button.setMinimumHeight(52)
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
        self.stop_button.setMinimumWidth(180)
        self.stop_button.setMinimumHeight(52)
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

        self.transcription_text = QTextEdit()
        self.transcription_text.setReadOnly(True)
        self.transcription_text.setMinimumHeight(300)
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

    def _connect_signals(self):
        """Connect button signals."""
        self.start_button.clicked.connect(self._on_start_clicked)
        self.stop_button.clicked.connect(self._on_stop_clicked)

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

    # Public API methods

    def set_idle(self):
        """Set the tab to idle state."""
        self._set_state(self.STATE_IDLE)
        self.copy_button.setEnabled(bool(self.transcription_text.toPlainText()))

    def set_recording(self):
        """Set the tab to recording state."""
        self._set_state(self.STATE_RECORDING)

    def set_processing(self):
        """Set the tab to processing state."""
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

    def is_recording(self) -> bool:
        """Check if meeting is currently recording."""
        return self._state == self.STATE_RECORDING

    def _copy_transcript(self):
        """Copy the transcript to clipboard."""
        text = self.transcription_text.toPlainText()
        if text:
            clipboard = QApplication.clipboard()
            clipboard.setText(text)

            self.set_status("Transcript copied to clipboard!")
            QTimer.singleShot(2000, lambda: self.set_status(
                "Ready to start meeting" if self._state == self.STATE_IDLE
                else "Recording in progress..."
            ))

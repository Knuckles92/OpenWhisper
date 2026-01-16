"""
Quick Record Tab widget.
Contains the model selection, recording controls, and transcription display
for quick recording and transcription sessions.
"""
import logging
from typing import Optional, Callable
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QTextEdit
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont

from config import config
from ui_qt.widgets.cards import Card, HeaderCard, ControlPanel
from ui_qt.widgets.buttons import SuccessButton, DangerButton, WarningButton
from ui_qt.widgets.stats_display import TranscriptionStatsWidget


class QuickRecordTab(QWidget):
    """Tab widget for quick recording and transcription."""

    # Signals
    record_toggled = pyqtSignal(bool)  # True = start, False = stop
    model_changed = pyqtSignal(str)  # Model display name
    retranscribe_requested = pyqtSignal(str)  # Audio file path

    def __init__(self, parent=None):
        super().__init__(parent)
        self.logger = logging.getLogger(__name__)

        # State
        self.is_recording = False
        self.current_model = config.MODEL_CHOICES[0]

        # Streaming transcription state
        self._partial_buffer = []  # Store finalized chunks

        # Callbacks (will be set by controller)
        self.on_record_start: Optional[Callable] = None
        self.on_record_stop: Optional[Callable] = None
        self.on_record_cancel: Optional[Callable] = None
        self.on_model_changed: Optional[Callable] = None

        self._setup_ui()
        self._connect_signals()

    def _setup_ui(self):
        """Setup the widget UI."""
        # Main layout with centering
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Content Container (Centered)
        content_container = QWidget()
        content_container.setObjectName("quickRecordContent")
        content_layout = QVBoxLayout(content_container)
        content_layout.setContentsMargins(24, 16, 24, 24)
        content_layout.setSpacing(16)
        content_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

        # Wrapper to center the content container horizontally
        center_wrapper = QHBoxLayout()
        center_wrapper.addStretch()
        center_wrapper.addWidget(content_container, stretch=1)
        center_wrapper.addStretch()

        # Limit max width of content
        content_container.setMaximumWidth(700)
        content_container.setMinimumWidth(500)

        main_layout.addLayout(center_wrapper)

        # Model selection card
        model_card = Card()

        model_label = QLabel("Transcription Model")
        model_label.setObjectName("headerLabel")
        model_label.setFont(QFont("Segoe UI", 13))
        model_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.model_combo = QComboBox()
        self.model_combo.addItems(config.MODEL_CHOICES)
        self.model_combo.setMinimumHeight(40)
        self.model_combo.setFont(QFont("Segoe UI", 12))

        # Device info label (shows CUDA/CPU status)
        self.device_info_label = QLabel("")
        self.device_info_label.setObjectName("deviceInfoLabel")
        self.device_info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.device_info_label.setFont(QFont("Segoe UI", 10))
        self.device_info_label.setStyleSheet("color: #8888aa; margin-top: 4px;")
        self.device_info_label.hide()

        model_card.layout.addWidget(model_label)
        model_card.layout.addWidget(self.model_combo)
        model_card.layout.addWidget(self.device_info_label)

        content_layout.addWidget(model_card)

        # Status label
        self.status_label = QLabel("Ready to record")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setFont(QFont("Segoe UI", 13))
        content_layout.addWidget(self.status_label)

        # Control buttons
        control_panel = ControlPanel()
        control_panel.layout.setSpacing(12)

        self.record_button = SuccessButton("Start Recording")
        self.cancel_button = WarningButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.stop_button = DangerButton("Stop")
        self.stop_button.setEnabled(False)

        control_panel.layout.addStretch()
        control_panel.layout.addWidget(self.record_button)
        control_panel.layout.addWidget(self.stop_button)
        control_panel.layout.addWidget(self.cancel_button)
        control_panel.layout.addStretch()

        content_layout.addWidget(control_panel)

        # Transcription display card
        transcription_card = HeaderCard("Transcription")

        self.transcription_text = QTextEdit()
        self.transcription_text.setReadOnly(True)
        self.transcription_text.setMinimumHeight(250)
        self.transcription_text.setFont(QFont("Segoe UI", 13))
        self.transcription_text.setPlaceholderText(
            "Transcription will appear here...\n"
            "Start recording to begin."
        )

        transcription_card.layout.addWidget(self.transcription_text)

        content_layout.addWidget(transcription_card)

        # Transcription statistics display (hidden by default)
        self.stats_widget = TranscriptionStatsWidget()
        content_layout.addWidget(self.stats_widget)

        content_layout.addStretch()  # Push everything up

    def _connect_signals(self):
        """Connect button signals to slots."""
        self.record_button.clicked.connect(self._on_record_clicked)
        self.stop_button.clicked.connect(self._on_stop_clicked)
        self.cancel_button.clicked.connect(self._on_cancel_clicked)
        self.model_combo.currentTextChanged.connect(self._on_model_changed)

    def _on_record_clicked(self):
        """Handle record button click."""
        self.is_recording = True
        self._update_recording_state()

        if self.on_record_start:
            self.on_record_start()

        self.record_toggled.emit(True)

    def _on_stop_clicked(self):
        """Handle stop button click."""
        self.is_recording = False
        self._update_recording_state()

        if self.on_record_stop:
            self.on_record_stop()

        self.record_toggled.emit(False)

    def _on_cancel_clicked(self):
        """Handle cancel button click."""
        self.is_recording = False
        self._update_recording_state()

        if self.on_record_cancel:
            self.on_record_cancel()

    def _on_model_changed(self, model_name: str):
        """Handle model selection change."""
        self.current_model = model_name
        if self.on_model_changed:
            self.on_model_changed(model_name)
        self.model_changed.emit(model_name)

    def _update_recording_state(self):
        """Update button states based on recording status."""
        if self.is_recording:
            self.record_button.setEnabled(False)
            self.record_button.setText("Recording...")
            self.stop_button.setEnabled(True)
            self.cancel_button.setEnabled(True)
            self.model_combo.setEnabled(False)
            self.status_label.setText("Recording in progress...")
        else:
            self.record_button.setEnabled(True)
            self.record_button.setText("Start Recording")
            self.stop_button.setEnabled(False)
            self.cancel_button.setEnabled(False)
            self.model_combo.setEnabled(True)
            self.status_label.setText("Ready to record")

    # Public API methods

    def set_status(self, status_text: str):
        """Update the status label."""
        self.status_label.setText(status_text)

    def set_device_info(self, device_info: str):
        """Set the device info label (e.g., 'cuda (float16)').

        Args:
            device_info: Device information string to display.
        """
        if device_info:
            self.device_info_label.setText(device_info)
            self.device_info_label.show()
        else:
            self.device_info_label.hide()

    def set_transcription(self, text: str):
        """Set the transcription text."""
        self.transcription_text.setText(text)

    def append_transcription(self, text: str):
        """Append text to the transcription."""
        cursor = self.transcription_text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.transcription_text.setTextCursor(cursor)
        self.transcription_text.insertPlainText(text)

    def clear_transcription(self):
        """Clear the transcription text."""
        self.transcription_text.clear()

    def set_partial_transcription(self, text: str, is_final: bool):
        """Display partial transcription with visual indicator.

        Args:
            text: Partial transcription text
            is_final: Whether this chunk is finalized
        """
        if is_final:
            # This chunk is finalized, add to buffer
            self._partial_buffer.append(text)

        # Combine finalized chunks + current partial
        combined = " ".join(self._partial_buffer)
        if not is_final:
            # Still processing - add ellipsis indicator
            if combined:
                combined += " "
            combined += text + " ..."

        # Update display
        self.transcription_text.setPlainText(combined)

        # Auto-scroll to bottom
        cursor = self.transcription_text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.transcription_text.setTextCursor(cursor)

    def clear_partial_transcription(self):
        """Clear partial transcription buffer."""
        self._partial_buffer.clear()

    def set_transcription_stats(
        self,
        transcription_time: float,
        audio_duration: float,
        file_size: int
    ):
        """Set the transcription statistics display.

        Args:
            transcription_time: Time taken to transcribe in seconds.
            audio_duration: Duration of the audio in seconds.
            file_size: Size of the audio file in bytes.
        """
        self.stats_widget.set_stats(transcription_time, audio_duration, file_size)

    def clear_transcription_stats(self):
        """Clear and hide the transcription statistics display."""
        self.stats_widget.clear()

    def get_model_value(self) -> str:
        """Get the model value key."""
        return config.MODEL_VALUE_MAP.get(self.current_model, "local_whisper")

    def set_model_selection(self, model_value: str):
        """Set the model selection by internal value.

        Args:
            model_value: Internal model value (e.g., 'local_whisper')
        """
        for display_name, internal_value in config.MODEL_VALUE_MAP.items():
            if internal_value == model_value:
                index = self.model_combo.findText(display_name)
                if index >= 0:
                    self.model_combo.blockSignals(True)
                    self.model_combo.setCurrentIndex(index)
                    self.current_model = display_name
                    self.model_combo.blockSignals(False)
                break

    def update_hotkeys(self, record_key: str, cancel_key: str, enable_disable_key: str = "Ctrl+Alt+*"):
        """Update the hotkey display on buttons.

        Args:
            record_key: The key for recording
            cancel_key: The key for canceling
            enable_disable_key: The key for enabling/disabling STT
        """
        self.record_button.set_hotkey(record_key)
        self.cancel_button.set_hotkey(cancel_key)
        self.stop_button.set_hotkey(record_key)  # Stop uses same key as record

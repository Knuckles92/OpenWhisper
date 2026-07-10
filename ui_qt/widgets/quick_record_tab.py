"""
Quick Record Tab widget.
Adds recording controls and streaming-transcription display on top of the
shared transcription tab scaffolding (model selection, status, transcript).
"""
import logging
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout
from PyQt6.QtCore import pyqtSignal

from ui_qt.widgets.cards import ControlPanel
from ui_qt.widgets.buttons import SuccessButton, DangerButton, WarningButton
from ui_qt.widgets.transcription_tab_base import TranscriptionTabBase

logger = logging.getLogger(__name__)


class QuickRecordTab(TranscriptionTabBase):
    """Tab widget for quick recording and transcription."""

    record_toggled = pyqtSignal(bool)  # True = start, False = stop
    record_canceled = pyqtSignal()

    CONTENT_OBJECT_NAME = "quickRecordContent"
    INITIAL_STATUS = "Ready to record"
    TRANSCRIPT_PLACEHOLDER = (
        "Transcription will appear here...\n"
        "Start recording to begin."
    )

    def __init__(self, parent=None):
        super().__init__(parent)

        # State (safe to set after the base constructor: _setup_ui never
        # reads it, and no signals can fire during init)
        self.is_recording = False

        # Streaming transcription state
        self._partial_buffer = []  # Store finalized chunks

    def _build_content_after_status(self, layout: QVBoxLayout):
        """Build the record/stop/cancel control panel below the status label."""
        control_panel = ControlPanel()
        control_panel.layout.setSpacing(12)

        self.record_button = SuccessButton("Start Recording")
        self.cancel_button = WarningButton("Cancel")
        self.cancel_button.set_active(False)
        self.stop_button = DangerButton("Stop")
        self.stop_button.set_active(False)

        buttons_widget = QWidget()
        buttons_layout = QVBoxLayout(buttons_widget)
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        buttons_layout.setSpacing(12)

        top_row = QHBoxLayout()
        top_row.setSpacing(12)
        top_row.addWidget(self.record_button, stretch=1)

        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(12)
        bottom_row.addWidget(self.stop_button, stretch=1)
        bottom_row.addWidget(self.cancel_button, stretch=1)

        buttons_layout.addLayout(top_row)
        buttons_layout.addLayout(bottom_row)
        buttons_widget.setMaximumWidth(420)

        control_panel.layout.addStretch()
        control_panel.layout.addWidget(buttons_widget)
        control_panel.layout.addStretch()

        layout.addWidget(control_panel)

    def _connect_signals(self):
        """Connect button signals in addition to the shared ones."""
        super()._connect_signals()
        self.record_button.clicked.connect(self._on_record_clicked)
        self.stop_button.clicked.connect(self._on_stop_clicked)
        self.cancel_button.clicked.connect(self._on_cancel_clicked)

    def _on_record_clicked(self):
        """Handle record button click."""
        self.is_recording = True
        self._update_recording_state()

        self.record_toggled.emit(True)

    def _on_stop_clicked(self):
        """Handle stop button click."""
        self.is_recording = False
        self._update_recording_state()

        self.record_toggled.emit(False)

    def _on_cancel_clicked(self):
        """Handle cancel button click."""
        self.is_recording = False
        self._update_recording_state()

        self.record_canceled.emit()

    def _update_recording_state(self):
        """Update button states based on recording status."""
        if self.is_recording:
            self.record_button.set_active(False)
            self.record_button.setText("Recording...")
            self.stop_button.set_active(True)
            self.cancel_button.set_active(True)
            self.model_combo.setEnabled(False)
            self.local_engine.set_busy(True)
            self.status_label.setText("Recording in progress...")
        else:
            self.record_button.set_active(True)
            self.record_button.setText("Start Recording")
            self.stop_button.set_active(False)
            self.cancel_button.set_active(False)
            self.model_combo.setEnabled(True)
            self.local_engine.set_busy(False)
            self.status_label.setText("Ready to record")

    # Public API methods

    def append_transcription(self, text: str):
        """Append text to the transcription."""
        cursor = self.transcript_text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.transcript_text.setTextCursor(cursor)
        self.transcript_text.insertPlainText(text)

    def set_partial_transcription(self, text: str, is_final: bool):
        """Display partial transcription with visual indicator.

        Args:
            text: Partial transcription text
            is_final: Whether this chunk is finalized
        """
        if is_final:
            # Incremental preview emits the full accumulated preview each cycle,
            # so we REPLACE (not append) the buffer contents.
            self._partial_buffer = [text] if text else []

        # Combine finalized chunks + current partial
        combined = " ".join(self._partial_buffer)
        if not is_final:
            # Still processing - add ellipsis indicator
            if combined:
                combined += " "
            combined += text + " ..."

        # Update display
        self.transcript_text.setPlainText(combined)

        # Auto-scroll to bottom
        cursor = self.transcript_text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.transcript_text.setTextCursor(cursor)

    def clear_partial_transcription(self):
        """Clear partial transcription buffer."""
        self._partial_buffer.clear()

    def update_hotkeys(self, record_key: str, cancel_key: str, enable_disable_key: str = ""):
        """Update the hotkey display on buttons.

        Args:
            record_key: The key for recording
            cancel_key: The key for canceling
            enable_disable_key: The key for enabling/disabling STT
        """
        self.record_button.set_hotkey(record_key)
        self.cancel_button.set_hotkey(cancel_key)
        self.stop_button.set_hotkey(record_key)  # Stop uses same key as record

import logging
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout
from PyQt6.QtCore import pyqtSignal

from ui_qt.widgets.cards import ControlPanel
from ui_qt.widgets.buttons import SuccessButton, DangerButton, WarningButton
from ui_qt.widgets.transcription_tab_base import TranscriptionTabBase

logger = logging.getLogger(__name__)


class QuickRecordTab(TranscriptionTabBase):
    record_toggled = pyqtSignal(bool)
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

        self._partial_buffer = []

    def _build_content_after_status(self, layout: QVBoxLayout):
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
        super()._connect_signals()
        self.record_button.clicked.connect(self._on_record_clicked)
        self.stop_button.clicked.connect(self._on_stop_clicked)
        self.cancel_button.clicked.connect(self._on_cancel_clicked)

    def _on_record_clicked(self):
        """Request a start; Recording chrome waits for a successful stream."""
        self.record_button.set_active(False)
        self.record_toggled.emit(True)

    def _on_stop_clicked(self):
        """Request a stop; button state waits for recording_state_changed."""
        self.record_toggled.emit(False)

    def _on_cancel_clicked(self):
        """Request a cancel; chrome is reset by the cancel path."""
        self.record_canceled.emit()

    def _update_recording_state(self):
        if self.is_recording:
            self.record_button.set_active(False)
            self.record_button.setText("Recording...")
            self.stop_button.set_active(True)
            self.cancel_button.set_active(True)
            self.set_backend_enabled(False)
            self.local_engine.set_busy(True)
            self.status_label.setText("Recording in progress...")
        else:
            self.record_button.set_active(True)
            self.record_button.setText("Start Recording")
            self.stop_button.set_active(False)
            self.cancel_button.set_active(False)
            self.set_backend_enabled(True)
            self.local_engine.set_busy(False)
            self.status_label.setText("Ready to record")

    def append_transcription(self, text: str):
        cursor = self.transcript_text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.transcript_text.setTextCursor(cursor)
        self.transcript_text.insertPlainText(text)

    def set_partial_transcription(self, text: str, is_final: bool):
        if is_final:
            # Incremental preview emits the full accumulated preview each cycle,
            # so we REPLACE (not append) the buffer contents.
            self._partial_buffer = [text] if text else []

        combined = " ".join(self._partial_buffer)
        if not is_final:
            if combined:
                combined += " "
            combined += text + " ..."

        self.transcript_text.setPlainText(combined)

        cursor = self.transcript_text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.transcript_text.setTextCursor(cursor)

    def clear_partial_transcription(self):
        self._partial_buffer.clear()

    def update_hotkeys(self, record_key: str, cancel_key: str, enable_disable_key: str = ""):
        self.record_button.set_hotkey(record_key)
        self.cancel_button.set_hotkey(cancel_key)
        self.stop_button.set_hotkey(record_key)

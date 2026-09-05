import logging
from pathlib import Path

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton
from PyQt6.QtCore import Qt, QSize, QTimer, pyqtSignal
from PyQt6.QtGui import QIcon

from config import bundle_root

from ui_qt.widgets.cards import ControlPanel
from ui_qt.widgets.buttons import SuccessButton, DangerButton, WarningButton
from ui_qt.widgets.transcription_tab_base import TranscriptionTabBase

logger = logging.getLogger(__name__)


class QuickRecordTab(TranscriptionTabBase):
    record_toggled = pyqtSignal(bool)
    record_canceled = pyqtSignal()
    copy_requested = pyqtSignal(str)

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

        icons = Path(bundle_root()) / "ui_qt" / "assets" / "tabler"
        self._copy_icon = QIcon(str(icons / "copy-gray.svg"))
        self._copied_icon = QIcon(str(icons / "check-green.svg"))
        self.copy_button = QPushButton()
        self.collapsed_copy_button = QPushButton("Copy")
        for button in (self.copy_button, self.collapsed_copy_button):
            button.setObjectName("transcriptCopyButton")
            button.setIconSize(QSize(16, 16))
            button.setFixedHeight(26)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setAccessibleName("Copy transcript")
            button.clicked.connect(self._request_copy)
        self.copy_button.setFixedWidth(26)
        self.transcript_pane.set_corner_widget(self.copy_button)
        self.transcription_card.header_layout.insertWidget(
            2, self.collapsed_copy_button
        )
        self._copy_feedback_timer = QTimer(self)
        self._copy_feedback_timer.setSingleShot(True)
        self._copy_feedback_timer.setInterval(1500)
        self._copy_feedback_timer.timeout.connect(self._reset_copy_feedback)
        self.transcript_text.textChanged.connect(self._sync_copy_actions)
        self.transcription_card.toggled.connect(self._sync_copy_actions)
        self._sync_copy_actions()

    def set_transcription_collapsed(self, collapsed: bool):
        super().set_transcription_collapsed(collapsed)
        if hasattr(self, "copy_button"):
            self._sync_copy_actions()

    def _sync_copy_actions(self):
        self._copy_feedback_timer.stop()
        self._reset_copy_feedback()
        has_text = bool(self.shown_transcript().strip())
        self.copy_button.setEnabled(has_text)
        self.collapsed_copy_button.setVisible(
            has_text and self.is_transcription_collapsed()
        )
        if self.is_transcription_collapsed():
            self.transcription_card.setMaximumHeight(
                self.transcription_card.sizeHint().height()
            )

    def _request_copy(self):
        text = self.shown_transcript()
        if text.strip():
            self.copy_requested.emit(text)

    def show_copy_result(self, succeeded: bool):
        for button in (self.copy_button, self.collapsed_copy_button):
            button.setIcon(self._copied_icon if succeeded else self._copy_icon)
            button.setToolTip(
                "Copied to clipboard" if succeeded else "Copy failed. Try again."
            )
        self.collapsed_copy_button.setText("Copied" if succeeded else "Retry copy")
        self._copy_feedback_timer.start()

    def _reset_copy_feedback(self):
        for button in (self.copy_button, self.collapsed_copy_button):
            button.setIcon(self._copy_icon)
            button.setToolTip("Copy transcript")
        self.collapsed_copy_button.setText("Copy")

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
            self.set_status("Recording in progress...")
        else:
            self.record_button.set_active(True)
            self.record_button.setText("Start Recording")
            self.stop_button.set_active(False)
            self.cancel_button.set_active(False)
            self.set_backend_enabled(True)
            self.local_engine.set_busy(False)
            self.set_status("Ready to record")

    def append_transcription(self, text: str):
        self.set_transcript(self.shown_transcript() + text)
        cursor = self.transcript_text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.transcript_text.setTextCursor(cursor)

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

        self.set_transcript(combined)

        cursor = self.transcript_text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.transcript_text.setTextCursor(cursor)

    def clear_partial_transcription(self):
        self._partial_buffer.clear()

    def update_hotkeys(self, record_key: str, cancel_key: str, enable_disable_key: str = ""):
        self.record_button.set_hotkey(record_key)
        self.cancel_button.set_hotkey(cancel_key)
        self.stop_button.set_hotkey(record_key)

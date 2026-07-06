"""
Quick Record Tab widget.
Contains the model selection, recording controls, and transcription display
for quick recording and transcription sessions.
"""
import logging
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QTextEdit
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtGui import QFont

from config import config
from ui_qt.utils.collapse_animation import SECTION_COLLAPSE_DURATION_MS
from ui_qt.widgets.cards import Card, HeaderCard, ControlPanel
from ui_qt.widgets.buttons import SuccessButton, DangerButton, WarningButton
from ui_qt.widgets.stats_display import TranscriptionStatsWidget
from ui_qt.widgets.local_engine_controls import LocalEngineControls

logger = logging.getLogger(__name__)


class QuickRecordTab(QWidget):
    """Tab widget for quick recording and transcription."""

    # Signals
    record_toggled = pyqtSignal(bool)  # True = start, False = stop
    record_canceled = pyqtSignal()
    model_changed = pyqtSignal(str)  # Model display name
    retranscribe_requested = pyqtSignal(str)  # Audio file path
    engine_settings_changed = pyqtSignal()  # Local engine combo changed
    engine_settings_collapsed = pyqtSignal(bool, int)  # collapsed, freed-height delta
    transcription_collapsed = pyqtSignal(bool, int)  # collapsed, freed-height delta

    def __init__(self, parent=None):
        super().__init__(parent)

        # State
        self.is_recording = False
        self.current_model = config.MODEL_CHOICES[0]

        # Streaming transcription state
        self._partial_buffer = []  # Store finalized chunks

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
        content_layout.setContentsMargins(24, 14, 24, 16)
        content_layout.setSpacing(12)
        # No vertical alignment: the transcription card is the elastic element
        # (stretch=1) so it grows/shrinks with the window. Horizontal centering
        # is handled by the center_wrapper below.
        self.content_layout = content_layout

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
        self.model_combo.setMaximumWidth(420)
        self.model_combo.setFont(QFont("Segoe UI", 12))

        # Local engine controls (model / device / quant). Only meaningful for
        # the Local Whisper backend; visibility is toggled by the main window
        # via set_local_engine_visible(). The panel's resolved-info label shows
        # the actual device/compute that "auto" resolved to after a model load.
        self.local_engine = LocalEngineControls()

        model_card.layout.addWidget(model_label)
        combo_row = QHBoxLayout()
        combo_row.addStretch()
        combo_row.addWidget(self.model_combo)
        combo_row.addStretch()
        model_card.layout.addLayout(combo_row)
        model_card.layout.addWidget(self.local_engine)

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

        content_layout.addWidget(control_panel)

        # Transcription display card (collapsible to reclaim vertical space)
        self.transcription_card = HeaderCard("Transcription", collapsible=True)

        self.transcription_text = QTextEdit()
        self.transcription_text.setReadOnly(True)
        self.transcription_text.setMinimumHeight(130)
        self.transcription_text.setFont(QFont("Segoe UI", 13))
        self.transcription_text.setPlaceholderText(
            "Transcription will appear here...\n"
            "Start recording to begin."
        )

        self.transcription_card.add_content_widget(self.transcription_text)
        self.transcription_card.toggled.connect(self._on_transcription_toggled)

        # The transcription card is the elastic element: it expands to fill spare
        # height and shrinks first when the window gets smaller.
        content_layout.addWidget(self.transcription_card, stretch=1)

        # Transcription statistics display (hidden by default)
        self.stats_widget = TranscriptionStatsWidget()
        content_layout.addWidget(self.stats_widget)

        # Managed bottom stretch: 0 while expanded (card fills), 1 while collapsed
        # (pushes the compact content to the top).
        content_layout.addStretch()
        self._bottom_stretch_index = content_layout.count() - 1

        # Always start collapsed to keep the main window compact on launch.
        self.set_transcription_collapsed(True)

    def _connect_signals(self):
        """Connect button signals to slots."""
        self.record_button.clicked.connect(self._on_record_clicked)
        self.stop_button.clicked.connect(self._on_stop_clicked)
        self.cancel_button.clicked.connect(self._on_cancel_clicked)
        self.model_combo.currentTextChanged.connect(self._on_model_changed)
        self.local_engine.engine_settings_changed.connect(self.engine_settings_changed)
        self.local_engine.toggled.connect(self._on_engine_settings_toggled)

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

    def _on_model_changed(self, model_name: str):
        """Handle model selection change."""
        self.current_model = model_name
        self.model_changed.emit(model_name)

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

    def set_status(self, status_text: str):
        """Update the status label."""
        self.status_label.setText(status_text)

    def set_device_info(self, device_info: str):
        """Set the resolved-engine readout (e.g., 'base | cuda (float16)').

        The text is shown inside the Local engine panel; the panel as a whole is
        shown/hidden by set_local_engine_visible() based on the active backend.

        Args:
            device_info: Device information string to display.
        """
        self.local_engine.set_resolved_info(device_info)

    def set_local_engine_visible(self, visible: bool):
        """Show/hide the local-engine sub-panel (only for Local Whisper)."""
        self.local_engine.setVisible(visible)

    # ── Engine settings collapse ───────────────────────────────────

    def _on_engine_settings_toggled(self, collapsed: bool, delta: int):
        """User toggled engine settings: request a window resize."""
        self.engine_settings_collapsed.emit(collapsed, delta)

    def set_engine_settings_collapsed(self, collapsed: bool):
        """Apply collapsed state without emitting (sync/restore)."""
        self.local_engine.set_collapsed(collapsed, emit=False)

    # ── Transcription collapse ─────────────────────────────────────

    def _apply_transcription_stretch(self, collapsed: bool):
        """Hand the spare vertical space to the card or the bottom spacer."""
        if collapsed:
            self.content_layout.setStretchFactor(self.transcription_card, 0)
            self.content_layout.setStretch(self._bottom_stretch_index, 1)
        else:
            self.content_layout.setStretchFactor(self.transcription_card, 1)
            self.content_layout.setStretch(self._bottom_stretch_index, 0)

    def _on_transcription_toggled(self, collapsed: bool):
        """User toggled the card: rebalance and request a resize."""
        self.transcription_collapsed.emit(collapsed, self.transcription_card.content_height)
        QTimer.singleShot(
            SECTION_COLLAPSE_DURATION_MS,
            lambda c=collapsed: self._apply_transcription_stretch(c),
        )

    def set_transcription_collapsed(self, collapsed: bool):
        """Apply collapsed state without persisting or emitting (sync/restore)."""
        self.transcription_card.set_collapsed(collapsed, emit=False)
        self._apply_transcription_stretch(collapsed)

    def is_transcription_collapsed(self) -> bool:
        """Whether the transcription card is currently collapsed."""
        return self.transcription_card.is_collapsed

    def set_transcript(self, text: str):
        """Set the transcript text."""
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
            # Rolling re-transcription: each final result contains the COMPLETE
            # transcription of all audio so far, so we REPLACE (not append)
            self._partial_buffer = [text] if text else []

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

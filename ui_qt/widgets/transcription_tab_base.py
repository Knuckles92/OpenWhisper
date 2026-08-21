"""Shared model-selection and transcript tab scaffolding."""
import logging
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QTextEdit,
    QButtonGroup, QPushButton,
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtGui import QFont

from config import config
from services.settings import SettingsKey, settings_manager
from ui_qt.utils.collapse_animation import SECTION_COLLAPSE_DURATION_MS
from ui_qt.widgets.cards import Card, HeaderCard
from ui_qt.widgets.stats_display import TranscriptionStatsWidget
from ui_qt.widgets.local_engine_controls import LocalEngineControls

logger = logging.getLogger(__name__)


class TranscriptionTabBase(QWidget):
    """Base widget for tabs that select a model and display a transcript."""

    model_changed = pyqtSignal(str)  # Model display name
    engine_settings_changed = pyqtSignal()  # Local engine combo changed
    manage_models_requested = pyqtSignal()  # "Manage models…" clicked
    engine_settings_collapsed = pyqtSignal(bool, int)  # collapsed, freed-height delta
    transcription_collapsed = pyqtSignal(bool, int)  # collapsed, freed-height delta

    CONTENT_OBJECT_NAME = "transcriptionTabContent"
    INITIAL_STATUS = ""
    TRANSCRIPT_PLACEHOLDER = "Transcription will appear here..."

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_model = config.MODEL_CHOICES[0]
        self._fixed_text = ""
        self._raw_text: Optional[str] = None
        self._showing_raw = False
        self._setup_ui()
        self._connect_signals()
        self.load_cleanup_setting()

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        content_container = QWidget()
        content_container.setObjectName(self.CONTENT_OBJECT_NAME)
        content_layout = QVBoxLayout(content_container)
        content_layout.setContentsMargins(24, 14, 24, 16)
        content_layout.setSpacing(12)
        # No vertical alignment: the transcription card is the elastic element
        # (stretch=1). Horizontal centering is handled by the center_wrapper.
        self.content_layout = content_layout

        center_wrapper = QHBoxLayout()
        center_wrapper.addStretch()
        center_wrapper.addWidget(content_container, stretch=1)
        center_wrapper.addStretch()

        content_container.setMaximumWidth(700)
        content_container.setMinimumWidth(500)

        main_layout.addLayout(center_wrapper)

        model_card = Card()

        model_label = QLabel("Transcription Engine")
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

        # The AI cleanup checkbox is built inside the engine settings panel;
        # alias it here so persistence/sync methods keep a stable home.
        self.cleanup_check = self.local_engine.cleanup_check

        model_card.layout.addWidget(model_label)
        combo_row = QHBoxLayout()
        combo_row.addStretch()
        combo_row.addWidget(self.model_combo)
        combo_row.addStretch()
        model_card.layout.addLayout(combo_row)

        model_card.layout.addWidget(self.local_engine)
        content_layout.addWidget(model_card)

        self._build_content_before_status(content_layout)

        self.status_label = QLabel(self.INITIAL_STATUS)
        self.status_label.setObjectName("statusLabel")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setFont(QFont("Segoe UI", 13))
        content_layout.addWidget(self.status_label)

        self._build_content_after_status(content_layout)

        self.transcription_card = HeaderCard("Transcription", collapsible=True)

        self.version_toggle = QWidget()
        version_row = QHBoxLayout(self.version_toggle)
        version_row.setContentsMargins(0, 0, 0, 8)
        version_row.setSpacing(6)
        version_row.addStretch()

        self._version_group = QButtonGroup(self)
        self.fixed_btn = QPushButton("Fixed")
        self.raw_btn = QPushButton("Raw")
        for btn in (self.fixed_btn, self.raw_btn):
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setObjectName("transcriptVersionBtn")
            btn.setMinimumHeight(28)
            self._version_group.addButton(btn)
            version_row.addWidget(btn)
        version_row.addStretch()
        self.fixed_btn.setChecked(True)
        self.version_toggle.hide()

        self.transcript_text = QTextEdit()
        self.transcript_text.setReadOnly(True)
        self.transcript_text.setMinimumHeight(130)
        self.transcript_text.setFont(QFont("Segoe UI", 13))
        self.transcript_text.setPlaceholderText(self.TRANSCRIPT_PLACEHOLDER)

        self.transcription_card.add_content_widget(self.version_toggle)
        self.transcription_card.add_content_widget(self.transcript_text)
        self.transcription_card.toggled.connect(self._on_transcription_toggled)

        # The transcription card is the elastic element: it expands to fill
        # spare height and shrinks first when the window gets smaller.
        content_layout.addWidget(self.transcription_card, stretch=1)

        self.stats_widget = TranscriptionStatsWidget()
        content_layout.addWidget(self.stats_widget)

        # Managed bottom stretch: 0 while expanded (card fills), 1 while
        # collapsed (pushes the compact content to the top).
        content_layout.addStretch()
        self._bottom_stretch_index = content_layout.count() - 1

        # Always start collapsed to keep the main window compact on launch.
        self.set_transcription_collapsed(True)

    def _build_content_before_status(self, layout: QVBoxLayout):
        """Insert tab-specific widgets between the model card and status label."""

    def _build_content_after_status(self, layout: QVBoxLayout):
        """Insert tab-specific widgets between the status label and transcription card."""

    def _connect_signals(self):
        self.model_combo.currentTextChanged.connect(self._on_model_changed)
        self.local_engine.engine_settings_changed.connect(self.engine_settings_changed)
        self.local_engine.manage_models_requested.connect(self.manage_models_requested)
        self.local_engine.toggled.connect(self._on_engine_settings_toggled)
        self.cleanup_check.toggled.connect(self._on_cleanup_toggled)
        self.fixed_btn.toggled.connect(self._on_version_toggled)
        self.raw_btn.toggled.connect(self._on_version_toggled)

    def _on_cleanup_toggled(self, checked: bool):
        settings_manager.save_setting(
            SettingsKey.TRANSCRIPT_CLEANUP_ENABLED, checked
        )

    def load_cleanup_setting(self):
        enabled = settings_manager.get(
            SettingsKey.TRANSCRIPT_CLEANUP_ENABLED,
            config.TRANSCRIPT_CLEANUP_ENABLED,
        )
        self.cleanup_check.blockSignals(True)
        self.cleanup_check.setChecked(bool(enabled))
        self.cleanup_check.blockSignals(False)

    def _on_version_toggled(self, checked: bool):
        if not checked:
            return
        show_raw = self.raw_btn.isChecked()
        self._showing_raw = show_raw
        if show_raw and self._raw_text is not None:
            self.transcript_text.setText(self._raw_text)
        else:
            self.transcript_text.setText(self._fixed_text)

    def _on_model_changed(self, model_name: str):
        self.current_model = model_name
        self.model_changed.emit(model_name)

    def set_model_selection(self, model_value: str):
        """Select a model by its internal backend value."""
        for display_name, internal_value in config.MODEL_VALUE_MAP.items():
            if internal_value == model_value:
                index = self.model_combo.findText(display_name)
                if index >= 0:
                    self.model_combo.blockSignals(True)
                    self.model_combo.setCurrentIndex(index)
                    self.current_model = display_name
                    self.model_combo.blockSignals(False)
                break

    def set_status(self, status_text: str):
        self.status_label.setText(status_text)

    def set_device_info(self, device_info: str):
        """Set the resolved-engine readout (e.g., 'base | cuda (float16)').

        The text is shown inside the Local engine panel; the panel as a whole
        is shown/hidden by set_local_engine_visible() based on the active
        backend.

        Args:
            device_info: Device information string to display.
        """
        self.local_engine.set_resolved_info(device_info)

    def set_local_engine_visible(self, visible: bool):
        self.local_engine.setVisible(visible)

    def _on_engine_settings_toggled(self, collapsed: bool, delta: int):
        self.engine_settings_collapsed.emit(collapsed, delta)

    def set_engine_settings_collapsed(self, collapsed: bool):
        """Apply collapsed state without emitting (sync/restore)."""
        self.local_engine.set_collapsed(collapsed, emit=False)

    def _apply_transcription_stretch(self, collapsed: bool):
        if collapsed:
            self.content_layout.setStretchFactor(self.transcription_card, 0)
            self.content_layout.setStretch(self._bottom_stretch_index, 1)
        else:
            self.content_layout.setStretchFactor(self.transcription_card, 1)
            self.content_layout.setStretch(self._bottom_stretch_index, 0)

    def _on_transcription_toggled(self, collapsed: bool):
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

    def set_transcript(self, text: str, raw: Optional[str] = None):
        """Display fixed text and an optional distinct raw ASR version."""
        self._fixed_text = text or ""
        self._raw_text = raw if raw and raw != text else None
        self._showing_raw = False

        has_raw = self._raw_text is not None
        self.version_toggle.setVisible(has_raw)
        self.fixed_btn.blockSignals(True)
        self.raw_btn.blockSignals(True)
        self.fixed_btn.setChecked(True)
        self.raw_btn.setChecked(False)
        self.fixed_btn.blockSignals(False)
        self.raw_btn.blockSignals(False)

        self.transcript_text.setText(self._fixed_text)

    def clear_transcription(self):
        self._fixed_text = ""
        self._raw_text = None
        self._showing_raw = False
        self.version_toggle.hide()
        self.transcript_text.clear()

    def set_transcription_stats(
        self,
        transcription_time: float,
        audio_duration: float,
        file_size: int,
    ):
        self.stats_widget.set_stats(transcription_time, audio_duration, file_size)

    def clear_transcription_stats(self):
        self.stats_widget.clear()

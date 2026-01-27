"""
Insights Dialog for generating meeting insights via LLM.
Allows users to generate summaries, action items, or custom insights
from meeting transcriptions with customizable output options.
"""
import logging
from datetime import datetime
from typing import Optional, Callable, Dict, Any
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QTextEdit, QFrame, QApplication, QPlainTextEdit, QSlider,
    QCheckBox, QLineEdit, QScrollArea, QWidget, QSizePolicy,
    QToolButton, QGridLayout
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont

from ui_qt.widgets import PrimaryButton, ModernButton
from services.insights_service import (
    InsightsService, InsightType, InsightEntry,
    InsightGenerationOptions, InsightPreset
)
from services.settings import settings_manager
from config import config


class InsightGenerationWorker(QThread):
    """Worker thread for generating insights without blocking the UI."""

    finished = pyqtSignal(str)  # Emits the generated insight
    error = pyqtSignal(str)     # Emits error message
    progress = pyqtSignal(str)  # Emits progress updates

    def __init__(
        self,
        insights_service: InsightsService,
        insight_type: InsightType,
        transcript: str,
        custom_prompt: str = "",
        options: Optional[InsightGenerationOptions] = None
    ):
        super().__init__()
        self.insights_service = insights_service
        self.insight_type = insight_type
        self.transcript = transcript
        self.custom_prompt = custom_prompt
        self.options = options

    def run(self):
        """Execute insight generation in background thread."""
        try:
            # Set up progress callback
            self.insights_service.on_progress = self._on_progress

            if self.insight_type == InsightType.SUMMARY:
                result = self.insights_service.generate_summary(
                    self.transcript,
                    options=self.options
                )
            elif self.insight_type == InsightType.ACTION_ITEMS:
                result = self.insights_service.generate_action_items(
                    self.transcript,
                    options=self.options
                )
            else:  # CUSTOM
                result = self.insights_service.generate_custom(
                    self.transcript,
                    self.custom_prompt,
                    options=self.options
                )

            self.finished.emit(result)

        except Exception as e:
            self.error.emit(str(e))
        finally:
            self.insights_service.on_progress = None

    def _on_progress(self, message: str):
        """Handle progress updates from the service."""
        self.progress.emit(message)


class InsightsDialog(QDialog):
    """Dialog for generating and displaying meeting insights with customization options."""

    def __init__(
        self,
        transcript: str,
        meeting_title: str = "",
        meeting_id: Optional[str] = None,
        parent=None
    ):
        """Initialize the insights dialog.

        Args:
            transcript: The meeting transcript text.
            meeting_title: Title of the meeting (for display).
            meeting_id: Meeting ID for persistence (optional).
            parent: Parent widget.
        """
        super().__init__(parent)
        self.logger = logging.getLogger(__name__)
        self.transcript = transcript
        self.meeting_title = meeting_title
        self.meeting_id = meeting_id

        # Track current insight state
        self._current_saved_insight: Optional[InsightEntry] = None
        self._last_custom_prompt: Optional[str] = None

        # Track advanced options visibility
        self._advanced_visible = False

        # Service and worker
        self.insights_service = InsightsService()
        self.worker: Optional[InsightGenerationWorker] = None

        self.setWindowTitle("Generate Meeting Insights")
        self.setMinimumSize(650, 650)
        self.setMaximumSize(950, 900)

        self._setup_ui()
        self._connect_signals()
        self._load_last_used_options()

        # Load saved insight for default type (Summary)
        self._load_saved_insight_for_current_type()
    
    def _setup_ui(self):
        """Setup the user interface with Quick Settings and Advanced Options."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        # Header
        header_layout = QVBoxLayout()
        header_layout.setSpacing(4)

        title = QLabel("Generate Meeting Insights")
        title_font = QFont("Segoe UI", 16)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setStyleSheet("color: #e0e0ff;")
        header_layout.addWidget(title)

        if self.meeting_title:
            meeting_label = QLabel(f"Meeting: {self.meeting_title}")
            meeting_label.setStyleSheet("color: #00d4ff; font-size: 12px;")
            header_layout.addWidget(meeting_label)

        layout.addLayout(header_layout)

        # Insight type selection
        type_frame = self._create_frame()
        type_layout = QVBoxLayout(type_frame)
        type_layout.setContentsMargins(16, 12, 16, 12)
        type_layout.setSpacing(8)

        type_label = QLabel("Insight Type:")
        type_label.setStyleSheet("color: #e0e0ff; font-weight: bold;")
        type_layout.addWidget(type_label)

        self.type_combo = self._create_combo_box()
        self.type_combo.addItems([
            "Summary - Key points and decisions",
            "Action Items - Tasks and follow-ups",
            "Custom - Your own prompt"
        ])
        type_layout.addWidget(self.type_combo)

        layout.addWidget(type_frame)

        # Quick Settings frame
        quick_settings_frame = self._create_frame()
        quick_layout = QVBoxLayout(quick_settings_frame)
        quick_layout.setContentsMargins(16, 12, 16, 12)
        quick_layout.setSpacing(10)

        quick_label = QLabel("Quick Settings:")
        quick_label.setStyleSheet("color: #e0e0ff; font-weight: bold;")
        quick_layout.addWidget(quick_label)

        # Quick settings row
        quick_row = QHBoxLayout()
        quick_row.setSpacing(16)

        # Output Length
        length_layout = QVBoxLayout()
        length_layout.setSpacing(4)
        length_lbl = QLabel("Length")
        length_lbl.setStyleSheet("color: #a0a0c0; font-size: 11px;")
        length_layout.addWidget(length_lbl)
        self.length_combo = self._create_combo_box()
        for key, val in config.INSIGHT_LENGTH_OPTIONS.items():
            self.length_combo.addItem(val["label"], key)
        self.length_combo.setCurrentIndex(1)  # Standard
        length_layout.addWidget(self.length_combo)
        quick_row.addLayout(length_layout)

        # Format
        format_layout = QVBoxLayout()
        format_layout.setSpacing(4)
        format_lbl = QLabel("Format")
        format_lbl.setStyleSheet("color: #a0a0c0; font-size: 11px;")
        format_layout.addWidget(format_lbl)
        self.format_combo = self._create_combo_box()
        for key, val in config.INSIGHT_FORMAT_OPTIONS.items():
            self.format_combo.addItem(val["label"], key)
        format_layout.addWidget(self.format_combo)
        quick_row.addLayout(format_layout)

        # Tone
        tone_layout = QVBoxLayout()
        tone_layout.setSpacing(4)
        tone_lbl = QLabel("Tone")
        tone_lbl.setStyleSheet("color: #a0a0c0; font-size: 11px;")
        tone_layout.addWidget(tone_lbl)
        self.tone_combo = self._create_combo_box()
        for key, val in config.INSIGHT_TONE_OPTIONS.items():
            self.tone_combo.addItem(val["label"], key)
        tone_layout.addWidget(self.tone_combo)
        quick_row.addLayout(tone_layout)

        quick_layout.addLayout(quick_row)
        layout.addWidget(quick_settings_frame)

        # Advanced Options toggle
        self.advanced_toggle = QToolButton()
        self.advanced_toggle.setText("▶ Show Advanced Options")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setStyleSheet("""
            QToolButton {
                color: #a0a0c0;
                background: transparent;
                border: none;
                font-size: 12px;
                padding: 4px 8px;
            }
            QToolButton:hover {
                color: #e0e0ff;
            }
            QToolButton:checked {
                color: #00d4ff;
            }
        """)
        layout.addWidget(self.advanced_toggle)

        # Advanced Options frame (initially hidden)
        self.advanced_frame = self._create_frame()
        advanced_layout = QVBoxLayout(self.advanced_frame)
        advanced_layout.setContentsMargins(16, 12, 16, 12)
        advanced_layout.setSpacing(12)

        # Focus Areas
        focus_label = QLabel("Focus Areas:")
        focus_label.setStyleSheet("color: #a0a0c0; font-size: 11px;")
        advanced_layout.addWidget(focus_label)

        focus_row = QHBoxLayout()
        focus_row.setSpacing(12)
        self.focus_checkboxes = {}
        for key, val in config.INSIGHT_FOCUS_AREAS.items():
            cb = QCheckBox(val["label"])
            cb.setStyleSheet("""
                QCheckBox {
                    color: #e0e0ff;
                    font-size: 12px;
                }
                QCheckBox::indicator {
                    width: 16px;
                    height: 16px;
                }
                QCheckBox::indicator:unchecked {
                    background-color: #2d2d44;
                    border: 1px solid #404060;
                    border-radius: 3px;
                }
                QCheckBox::indicator:checked {
                    background-color: #6366f1;
                    border: 1px solid #6366f1;
                    border-radius: 3px;
                }
            """)
            self.focus_checkboxes[key] = cb
            focus_row.addWidget(cb)
        focus_row.addStretch()
        advanced_layout.addLayout(focus_row)

        # Filters row
        filters_row = QHBoxLayout()
        filters_row.setSpacing(16)

        # Participant filter
        participant_layout = QVBoxLayout()
        participant_layout.setSpacing(4)
        participant_lbl = QLabel("Focus on Participants (comma-separated)")
        participant_lbl.setStyleSheet("color: #a0a0c0; font-size: 11px;")
        participant_layout.addWidget(participant_lbl)
        self.participant_input = QLineEdit()
        self.participant_input.setPlaceholderText("e.g., John, Sarah")
        self.participant_input.setStyleSheet(self._get_input_style())
        participant_layout.addWidget(self.participant_input)
        filters_row.addLayout(participant_layout)

        # Topic filter
        topic_layout = QVBoxLayout()
        topic_layout.setSpacing(4)
        topic_lbl = QLabel("Focus on Topics (comma-separated)")
        topic_lbl.setStyleSheet("color: #a0a0c0; font-size: 11px;")
        topic_layout.addWidget(topic_lbl)
        self.topic_input = QLineEdit()
        self.topic_input.setPlaceholderText("e.g., budget, timeline")
        self.topic_input.setStyleSheet(self._get_input_style())
        topic_layout.addWidget(self.topic_input)
        filters_row.addLayout(topic_layout)

        advanced_layout.addLayout(filters_row)

        # Creativity and Language row
        creative_row = QHBoxLayout()
        creative_row.setSpacing(16)

        # Creativity slider
        creativity_layout = QVBoxLayout()
        creativity_layout.setSpacing(4)
        creativity_header = QHBoxLayout()
        creativity_lbl = QLabel("Creativity")
        creativity_lbl.setStyleSheet("color: #a0a0c0; font-size: 11px;")
        creativity_header.addWidget(creativity_lbl)
        self.creativity_value_label = QLabel("0.5")
        self.creativity_value_label.setStyleSheet("color: #00d4ff; font-size: 11px;")
        creativity_header.addWidget(self.creativity_value_label)
        creativity_header.addStretch()
        creativity_layout.addLayout(creativity_header)

        self.creativity_slider = QSlider(Qt.Orientation.Horizontal)
        self.creativity_slider.setMinimum(0)
        self.creativity_slider.setMaximum(100)
        self.creativity_slider.setValue(50)
        self.creativity_slider.setStyleSheet("""
            QSlider::groove:horizontal {
                background: #2d2d44;
                height: 6px;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #6366f1;
                width: 16px;
                height: 16px;
                margin: -5px 0;
                border-radius: 8px;
            }
            QSlider::sub-page:horizontal {
                background: #6366f1;
                border-radius: 3px;
            }
        """)
        creativity_layout.addWidget(self.creativity_slider)
        creative_row.addLayout(creativity_layout, stretch=2)

        # Language dropdown
        language_layout = QVBoxLayout()
        language_layout.setSpacing(4)
        language_lbl = QLabel("Language")
        language_lbl.setStyleSheet("color: #a0a0c0; font-size: 11px;")
        language_layout.addWidget(language_lbl)
        self.language_combo = self._create_combo_box()
        for lang in config.INSIGHT_LANGUAGE_OPTIONS:
            self.language_combo.addItem(lang.title(), lang)
        language_layout.addWidget(self.language_combo)
        creative_row.addLayout(language_layout, stretch=1)

        advanced_layout.addLayout(creative_row)

        # Checkboxes row
        checkbox_row = QHBoxLayout()
        checkbox_row.setSpacing(20)

        self.timestamps_checkbox = QCheckBox("Include Timestamps")
        self.timestamps_checkbox.setStyleSheet(self._get_checkbox_style())
        checkbox_row.addWidget(self.timestamps_checkbox)

        self.attribution_checkbox = QCheckBox("Include Speaker Attribution")
        self.attribution_checkbox.setStyleSheet(self._get_checkbox_style())
        self.attribution_checkbox.setChecked(True)
        checkbox_row.addWidget(self.attribution_checkbox)

        checkbox_row.addStretch()
        advanced_layout.addLayout(checkbox_row)

        self.advanced_frame.setVisible(False)
        layout.addWidget(self.advanced_frame)

        # Presets row
        presets_frame = self._create_frame()
        presets_layout = QHBoxLayout(presets_frame)
        presets_layout.setContentsMargins(12, 8, 12, 8)
        presets_layout.setSpacing(8)

        presets_label = QLabel("Presets:")
        presets_label.setStyleSheet("color: #a0a0c0; font-size: 11px;")
        presets_layout.addWidget(presets_label)

        self.preset_buttons = []
        for preset in config.INSIGHT_BUILTIN_PRESETS:
            btn = ModernButton(preset["name"])
            btn.setMaximumHeight(28)
            btn.setProperty("preset_id", preset["id"])
            btn.clicked.connect(lambda checked, p=preset: self._apply_preset(p))
            self.preset_buttons.append(btn)
            presets_layout.addWidget(btn)

        presets_layout.addStretch()
        layout.addWidget(presets_frame)

        # Custom prompt input (initially hidden)
        self.custom_frame = self._create_frame()
        custom_layout = QVBoxLayout(self.custom_frame)
        custom_layout.setContentsMargins(16, 12, 16, 12)
        custom_layout.setSpacing(8)

        custom_label = QLabel("Your Prompt:")
        custom_label.setStyleSheet("color: #e0e0ff; font-weight: bold;")
        custom_layout.addWidget(custom_label)

        self.custom_prompt_input = QPlainTextEdit()
        self.custom_prompt_input.setPlaceholderText(
            "Enter your custom prompt here...\n"
            "Examples:\n"
            "- What were the main concerns raised?\n"
            "- Summarize the technical discussions\n"
            "- Who made the most contributions?"
        )
        self.custom_prompt_input.setMaximumHeight(80)
        self.custom_prompt_input.setStyleSheet("""
            QPlainTextEdit {
                background-color: #2d2d44;
                color: #e0e0ff;
                border: 1px solid #404060;
                border-radius: 6px;
                padding: 10px;
                font-size: 12px;
            }
            QPlainTextEdit:focus {
                border: 1px solid #6366f1;
            }
        """)
        custom_layout.addWidget(self.custom_prompt_input)

        self.custom_frame.setVisible(False)
        layout.addWidget(self.custom_frame)

        # Generate button
        generate_layout = QHBoxLayout()
        generate_layout.addStretch()

        self.generate_btn = PrimaryButton("Generate Insights")
        self.generate_btn.setMinimumWidth(180)
        generate_layout.addWidget(self.generate_btn)

        generate_layout.addStretch()
        layout.addLayout(generate_layout)

        # Status label
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #a0a0c0; font-size: 12px;")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.status_label)

        # Results area
        results_frame = self._create_frame()
        results_layout = QVBoxLayout(results_frame)
        results_layout.setContentsMargins(16, 12, 16, 12)
        results_layout.setSpacing(8)

        results_header = QHBoxLayout()
        results_label = QLabel("Results:")
        results_label.setStyleSheet("color: #e0e0ff; font-weight: bold;")
        results_header.addWidget(results_label)

        results_header.addStretch()

        self.copy_btn = ModernButton("Copy")
        self.copy_btn.setMaximumWidth(80)
        self.copy_btn.setEnabled(False)
        results_header.addWidget(self.copy_btn)

        results_layout.addLayout(results_header)

        self.results_text = QTextEdit()
        self.results_text.setReadOnly(True)
        self.results_text.setMinimumHeight(150)
        self.results_text.setPlaceholderText(
            "Generated insights will appear here...\n\n"
            "Select an insight type above and click 'Generate Insights' to begin."
        )
        self.results_text.setStyleSheet("""
            QTextEdit {
                background-color: #2d2d44;
                color: #e0e0ff;
                border: 1px solid #404060;
                border-radius: 6px;
                padding: 12px;
                font-size: 13px;
                line-height: 1.5;
            }
        """)
        results_layout.addWidget(self.results_text)

        layout.addWidget(results_frame, stretch=1)

        # Bottom button row
        button_layout = QHBoxLayout()
        button_layout.setSpacing(12)

        close_btn = ModernButton("Close")
        close_btn.clicked.connect(self.close)
        button_layout.addWidget(close_btn)

        button_layout.addStretch()

        layout.addLayout(button_layout)

        # Apply dialog styling
        self.setStyleSheet("""
            InsightsDialog {
                background-color: #1e1e2e;
            }
        """)

    def _create_frame(self) -> QFrame:
        """Create a styled frame for sections."""
        frame = QFrame()
        frame.setStyleSheet("""
            QFrame {
                background-color: #252538;
                border: 1px solid #404060;
                border-radius: 8px;
            }
        """)
        return frame

    def _create_combo_box(self) -> QComboBox:
        """Create a styled combo box."""
        combo = QComboBox()
        combo.setMinimumHeight(32)
        combo.setStyleSheet("""
            QComboBox {
                background-color: #2d2d44;
                color: #e0e0ff;
                border: 1px solid #404060;
                border-radius: 6px;
                padding: 6px 10px;
                font-size: 12px;
            }
            QComboBox:hover {
                border: 1px solid #6366f1;
            }
            QComboBox::drop-down {
                border: none;
                padding-right: 10px;
            }
            QComboBox QAbstractItemView {
                background-color: #2d2d44;
                color: #e0e0ff;
                selection-background-color: #6366f1;
                border: 1px solid #404060;
            }
        """)
        return combo

    def _get_input_style(self) -> str:
        """Get stylesheet for line edit inputs."""
        return """
            QLineEdit {
                background-color: #2d2d44;
                color: #e0e0ff;
                border: 1px solid #404060;
                border-radius: 6px;
                padding: 8px 10px;
                font-size: 12px;
            }
            QLineEdit:focus {
                border: 1px solid #6366f1;
            }
            QLineEdit::placeholder {
                color: #606080;
            }
        """

    def _get_checkbox_style(self) -> str:
        """Get stylesheet for checkboxes."""
        return """
            QCheckBox {
                color: #e0e0ff;
                font-size: 12px;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
            }
            QCheckBox::indicator:unchecked {
                background-color: #2d2d44;
                border: 1px solid #404060;
                border-radius: 3px;
            }
            QCheckBox::indicator:checked {
                background-color: #6366f1;
                border: 1px solid #6366f1;
                border-radius: 3px;
            }
        """
    
    def _connect_signals(self):
        """Connect UI signals."""
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        self.generate_btn.clicked.connect(self._on_generate_clicked)
        self.copy_btn.clicked.connect(self._copy_results)
        self.advanced_toggle.clicked.connect(self._toggle_advanced_options)
        self.creativity_slider.valueChanged.connect(self._on_creativity_changed)

    def _toggle_advanced_options(self):
        """Toggle visibility of advanced options section."""
        self._advanced_visible = not self._advanced_visible
        self.advanced_frame.setVisible(self._advanced_visible)
        if self._advanced_visible:
            self.advanced_toggle.setText("▼ Hide Advanced Options")
        else:
            self.advanced_toggle.setText("▶ Show Advanced Options")

    def _on_creativity_changed(self, value: int):
        """Update creativity value label when slider changes."""
        creativity = value / 100.0
        self.creativity_value_label.setText(f"{creativity:.2f}")

    def _build_generation_options(self) -> InsightGenerationOptions:
        """Build InsightGenerationOptions from current UI state."""
        # Collect focus areas
        focus_areas = [
            key for key, cb in self.focus_checkboxes.items()
            if cb.isChecked()
        ]

        # Get filter values (None if empty)
        participant_filter = self.participant_input.text().strip() or None
        topic_filter = self.topic_input.text().strip() or None

        return InsightGenerationOptions(
            output_length=self.length_combo.currentData() or "standard",
            formatting_style=self.format_combo.currentData() or "bullet_points",
            tone=self.tone_combo.currentData() or "professional",
            focus_areas=focus_areas,
            participant_filter=participant_filter,
            topic_filter=topic_filter,
            creativity=self.creativity_slider.value() / 100.0,
            language=self.language_combo.currentData() or "english",
            include_timestamps=self.timestamps_checkbox.isChecked(),
            include_speaker_attribution=self.attribution_checkbox.isChecked()
        )

    def _apply_options_to_ui(self, options: InsightGenerationOptions):
        """Apply InsightGenerationOptions to UI controls."""
        # Set combo boxes by data value
        for i in range(self.length_combo.count()):
            if self.length_combo.itemData(i) == options.output_length:
                self.length_combo.setCurrentIndex(i)
                break

        for i in range(self.format_combo.count()):
            if self.format_combo.itemData(i) == options.formatting_style:
                self.format_combo.setCurrentIndex(i)
                break

        for i in range(self.tone_combo.count()):
            if self.tone_combo.itemData(i) == options.tone:
                self.tone_combo.setCurrentIndex(i)
                break

        for i in range(self.language_combo.count()):
            if self.language_combo.itemData(i) == options.language:
                self.language_combo.setCurrentIndex(i)
                break

        # Set focus areas
        for key, cb in self.focus_checkboxes.items():
            cb.setChecked(key in options.focus_areas)

        # Set filters
        self.participant_input.setText(options.participant_filter or "")
        self.topic_input.setText(options.topic_filter or "")

        # Set slider
        self.creativity_slider.setValue(int(options.creativity * 100))

        # Set checkboxes
        self.timestamps_checkbox.setChecked(options.include_timestamps)
        self.attribution_checkbox.setChecked(options.include_speaker_attribution)

    def _apply_preset(self, preset: Dict[str, Any]):
        """Apply a preset to the UI."""
        options_data = preset.get("options", {})
        options = InsightGenerationOptions.from_dict(options_data)
        self._apply_options_to_ui(options)

        # Show a brief status message
        self.status_label.setText(f"Applied preset: {preset['name']}")
        self.status_label.setStyleSheet("color: #00d4ff; font-size: 12px;")

        self.logger.info(f"Applied preset: {preset['name']}")

    def _load_last_used_options(self):
        """Load and apply last used options for the current insight type."""
        insight_type = self._get_selected_insight_type()
        type_key = insight_type.value  # "summary", "action_items", "custom"

        last_used = settings_manager.load_insights_last_used(type_key)
        if last_used:
            options = InsightGenerationOptions.from_dict(last_used)
            self._apply_options_to_ui(options)
        else:
            # Load defaults
            defaults = settings_manager.load_insights_generation_defaults(type_key)
            options = InsightGenerationOptions.from_dict(defaults)
            self._apply_options_to_ui(options)

    def _save_last_used_options(self):
        """Save the current UI state as last used options."""
        insight_type = self._get_selected_insight_type()
        type_key = insight_type.value
        options = self._build_generation_options()

        try:
            settings_manager.save_insights_last_used(type_key, options.to_dict())
        except Exception as e:
            self.logger.warning(f"Failed to save last used options: {e}")
    
    def _on_type_changed(self, index: int):
        """Handle insight type selection change."""
        # Show custom prompt input only for Custom type
        self.custom_frame.setVisible(index == 2)

        # Load last used options for the new type
        self._load_last_used_options()

        # Load saved insight for the new type
        self._load_saved_insight_for_current_type()
    
    def _get_selected_insight_type(self) -> InsightType:
        """Get the currently selected insight type."""
        index = self.type_combo.currentIndex()
        if index == 0:
            return InsightType.SUMMARY
        elif index == 1:
            return InsightType.ACTION_ITEMS
        else:
            return InsightType.CUSTOM

    def _load_saved_insight_for_current_type(self):
        """Load and display saved insight for the current type."""
        if not self.meeting_id:
            return

        insight_type = self._get_selected_insight_type()
        custom_prompt = None

        # For custom type, we need to check if there's a saved insight
        # with an empty prompt (we can't know what prompt the user will enter)
        if insight_type == InsightType.CUSTOM:
            # Don't auto-load for custom type since we don't know the prompt yet
            self._current_saved_insight = None
            self._update_ui_for_saved_state()
            return

        # Try to load saved insight
        saved = self.insights_service.get_saved_insight(
            meeting_id=self.meeting_id,
            insight_type=insight_type,
            custom_prompt=custom_prompt
        )

        self._current_saved_insight = saved
        self._update_ui_for_saved_state()

    def _update_ui_for_saved_state(self):
        """Update UI based on whether we have a saved insight."""
        if self._current_saved_insight:
            # Display saved insight
            self.results_text.setPlainText(self._current_saved_insight.content)
            self.copy_btn.setEnabled(True)

            # Update button text
            self.generate_btn.setText("Regenerate")

            # Show saved timestamp
            try:
                saved_time = self._current_saved_insight.generated_at_datetime
                time_str = saved_time.strftime("%b %d, %Y at %I:%M %p")
                self.status_label.setText(f"Saved on {time_str}")
                self.status_label.setStyleSheet("color: #34d399; font-size: 12px;")
            except Exception:
                self.status_label.setText("Previously saved")
                self.status_label.setStyleSheet("color: #34d399; font-size: 12px;")
        else:
            # No saved insight - clear and reset
            self.results_text.clear()
            self.copy_btn.setEnabled(False)
            self.generate_btn.setText("Generate Insights")
            self.status_label.setText("")

    def _on_generate_clicked(self):
        """Handle generate button click."""
        insight_type = self._get_selected_insight_type()
        custom_prompt = ""

        if insight_type == InsightType.CUSTOM:
            custom_prompt = self.custom_prompt_input.toPlainText().strip()
            if not custom_prompt:
                self.status_label.setText("Please enter a custom prompt.")
                self.status_label.setStyleSheet("color: #ff6b6b; font-size: 12px;")
                return

        # Track the custom prompt for saving later
        self._last_custom_prompt = custom_prompt if custom_prompt else None

        # Build generation options from UI
        options = self._build_generation_options()

        # Save as last used options
        self._save_last_used_options()

        # Disable UI during generation
        self.generate_btn.setEnabled(False)
        self.type_combo.setEnabled(False)
        self.custom_prompt_input.setEnabled(False)
        self._set_options_enabled(False)
        self.results_text.clear()
        self.copy_btn.setEnabled(False)

        self.status_label.setText("Initializing...")
        self.status_label.setStyleSheet("color: #fbbf24; font-size: 12px;")

        # Start generation in background thread with options
        self.worker = InsightGenerationWorker(
            self.insights_service,
            insight_type,
            self.transcript,
            custom_prompt,
            options=options
        )
        self.worker.finished.connect(self._on_generation_finished)
        self.worker.error.connect(self._on_generation_error)
        self.worker.progress.connect(self._on_progress)
        self.worker.start()

        self.logger.info(f"Started {insight_type.value} generation with custom options")

    def _set_options_enabled(self, enabled: bool):
        """Enable or disable all option controls."""
        self.length_combo.setEnabled(enabled)
        self.format_combo.setEnabled(enabled)
        self.tone_combo.setEnabled(enabled)
        self.advanced_toggle.setEnabled(enabled)
        self.language_combo.setEnabled(enabled)
        self.creativity_slider.setEnabled(enabled)
        self.participant_input.setEnabled(enabled)
        self.topic_input.setEnabled(enabled)
        self.timestamps_checkbox.setEnabled(enabled)
        self.attribution_checkbox.setEnabled(enabled)
        for cb in self.focus_checkboxes.values():
            cb.setEnabled(enabled)
        for btn in self.preset_buttons:
            btn.setEnabled(enabled)
    
    def _on_progress(self, message: str):
        """Handle progress updates."""
        self.status_label.setText(message)
    
    def _on_generation_finished(self, result: str):
        """Handle successful generation."""
        self.results_text.setPlainText(result)

        # Auto-save if we have a meeting_id
        insight_type = self._get_selected_insight_type()
        if self.meeting_id:
            try:
                self.insights_service.save_insight(
                    meeting_id=self.meeting_id,
                    insight_type=insight_type,
                    content=result,
                    custom_prompt=self._last_custom_prompt
                )
                self.status_label.setText("Saved!")
                self.logger.info(f"Auto-saved {insight_type.value} insight")
            except Exception as e:
                self.logger.error(f"Failed to save insight: {e}")
                self.status_label.setText("Generated (save failed)")
        else:
            self.status_label.setText("Generation complete!")

        self.status_label.setStyleSheet("color: #34d399; font-size: 12px;")

        # Re-enable UI
        self.generate_btn.setEnabled(True)
        self.type_combo.setEnabled(True)
        self.custom_prompt_input.setEnabled(True)
        self._set_options_enabled(True)
        self.copy_btn.setEnabled(True)

        # Update button text to Regenerate since we now have a saved insight
        self.generate_btn.setText("Regenerate")

        self.worker = None
        self.logger.info("Insight generation completed successfully")

    def _on_generation_error(self, error_message: str):
        """Handle generation error."""
        self.status_label.setText(f"Error: {error_message}")
        self.status_label.setStyleSheet("color: #ff6b6b; font-size: 12px;")

        # Re-enable UI
        self.generate_btn.setEnabled(True)
        self.type_combo.setEnabled(True)
        self.custom_prompt_input.setEnabled(True)
        self._set_options_enabled(True)

        self.worker = None
        self.logger.error(f"Insight generation failed: {error_message}")
    
    def _copy_results(self):
        """Copy results to clipboard."""
        text = self.results_text.toPlainText()
        if text:
            clipboard = QApplication.clipboard()
            clipboard.setText(text)
            self.status_label.setText("Copied to clipboard!")
            self.status_label.setStyleSheet("color: #34d399; font-size: 12px;")
    
    def closeEvent(self, event):
        """Handle dialog close."""
        # Cancel any running generation
        if self.worker and self.worker.isRunning():
            self.insights_service.cancel()
            self.worker.wait(2000)  # Wait up to 2 seconds
        
        # Cleanup
        self.insights_service.cleanup()
        super().closeEvent(event)

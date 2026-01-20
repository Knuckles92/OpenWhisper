"""
Modern Settings Dialog for PyQt6 UI.
Tabbed interface for managing application settings.
"""
import logging
from typing import Optional, Callable
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTabWidget,
    QWidget, QLabel, QComboBox, QCheckBox, QSpinBox,
    QSlider, QFrame, QLineEdit
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont

from config import config
from services.settings import settings_manager
from services.recorder import AudioRecorder
from ui_qt.widgets import PrimaryButton, ModernButton


class SettingsDialog(QDialog):
    """Modern settings dialog with tabbed interface."""

    settings_changed = pyqtSignal(dict)

    def __init__(self, parent=None):
        """Initialize settings dialog."""
        super().__init__(parent)
        self.logger = logging.getLogger(__name__)
        self.setWindowTitle("Settings")
        self.setMinimumSize(600, 500)
        self.setMaximumWidth(800)

        # Callbacks
        self.on_settings_save: Optional[Callable] = None

        self._setup_ui()
        self._load_settings()

    def _setup_ui(self):
        """Setup the user interface."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Tab widget
        self.tabs = QTabWidget()
        self.tabs.setStyleSheet("""
            QTabWidget::pane {
                border: 1px solid #404060;
                background-color: #1e1e2e;
            }
            QTabBar::tab {
                background-color: #2d2d44;
                color: #a0a0c0;
                border: none;
                padding: 10px 20px;
                margin-right: 2px;
            }
            QTabBar::tab:selected {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                            stop:0 #6366f1, stop:1 #8b5cf6);
                color: #ffffff;
            }
        """)

        # Create tabs
        self._create_general_tab()
        self._create_audio_tab()
        self._create_hotkeys_tab()
        self._create_insights_tab()
        self._create_advanced_tab()

        layout.addWidget(self.tabs)

        # Button layout
        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(16, 16, 16, 16)
        button_layout.setSpacing(8)

        button_layout.addStretch()

        cancel_btn = ModernButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(cancel_btn)

        save_btn = PrimaryButton("Save Settings")
        save_btn.clicked.connect(self._save_settings)
        button_layout.addWidget(save_btn)

        layout.addLayout(button_layout)

        # Apply background
        self.setStyleSheet("""
            SettingsDialog {
                background-color: #1e1e2e;
                border-radius: 8px;
            }
        """)

    def _create_general_tab(self):
        """Create general settings tab."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        # Title
        title = QLabel("General Settings")
        title_font = QFont("Segoe UI", 12)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setStyleSheet("color: #e0e0ff;")
        layout.addWidget(title)

        # Model selection
        layout.addSpacing(12)
        model_label = QLabel("Default Model:")
        model_label.setStyleSheet("color: #e0e0ff;")
        layout.addWidget(model_label)

        self.model_combo = QComboBox()
        self.model_combo.addItems(config.MODEL_CHOICES)
        self.model_combo.setMinimumHeight(36)
        layout.addWidget(self.model_combo)

        # Auto-paste checkbox
        layout.addSpacing(12)
        self.auto_paste_check = QCheckBox("Auto-paste transcription to active window")
        self.auto_paste_check.setStyleSheet("color: #e0e0ff;")
        layout.addWidget(self.auto_paste_check)

        # Copy to clipboard checkbox
        self.copy_clipboard_check = QCheckBox("Copy transcription to clipboard")
        self.copy_clipboard_check.setStyleSheet("color: #e0e0ff;")
        layout.addWidget(self.copy_clipboard_check)

        # Minimize to tray checkbox
        layout.addSpacing(12)
        self.minimize_tray_check = QCheckBox("Minimize to system tray on close")
        self.minimize_tray_check.setStyleSheet("color: #e0e0ff;")
        layout.addWidget(self.minimize_tray_check)

        # Streaming transcription checkbox
        layout.addSpacing(24)
        streaming_label = QLabel("Real-Time Transcription (Experimental)")
        streaming_label_font = QFont("Segoe UI", 10)
        streaming_label_font.setBold(True)
        streaming_label.setFont(streaming_label_font)
        streaming_label.setStyleSheet("color: #c0c0ff;")
        layout.addWidget(streaming_label)

        layout.addSpacing(8)
        self.streaming_enabled_check = QCheckBox("Enable real-time transcription preview (while recording)")
        self.streaming_enabled_check.setStyleSheet("color: #e0e0ff;")
        self.streaming_enabled_check.stateChanged.connect(self._on_streaming_enabled_changed)
        layout.addWidget(self.streaming_enabled_check)

        # Info label for streaming
        streaming_info = QLabel("Shows transcribed text as you speak. Requires Local Whisper backend.\nMay impact performance on slower systems (CPU: ~40-60%, GPU: ~15-20%).")
        streaming_info.setStyleSheet("color: #808090; font-size: 10px;")
        streaming_info.setWordWrap(True)
        layout.addWidget(streaming_info)

        # Streaming overlay with auto-paste checkbox
        layout.addSpacing(8)
        self.streaming_paste_check = QCheckBox("Show streaming overlay and auto-paste result")
        self.streaming_paste_check.setStyleSheet("color: #e0e0ff;")
        self.streaming_paste_check.stateChanged.connect(self._on_streaming_paste_changed)
        layout.addWidget(self.streaming_paste_check)

        # Info label for streaming overlay
        streaming_paste_info = QLabel("Displays streaming text in a popup overlay near cursor.\nFinal transcription is pasted when recording stops.")
        streaming_paste_info.setStyleSheet("color: #808090; font-size: 10px;")
        streaming_paste_info.setWordWrap(True)
        layout.addWidget(streaming_paste_info)

        layout.addStretch()
        self.tabs.addTab(tab, "General")

    def _create_audio_tab(self):
        """Create audio settings tab."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        # Title
        title = QLabel("Audio Settings")
        title_font = QFont("Segoe UI", 12)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setStyleSheet("color: #e0e0ff;")
        layout.addWidget(title)

        # Sample rate
        layout.addSpacing(12)
        sample_rate_label = QLabel("Sample Rate (Hz):")
        sample_rate_label.setStyleSheet("color: #e0e0ff;")
        layout.addWidget(sample_rate_label)

        self.sample_rate_combo = QComboBox()
        self.sample_rate_combo.addItems(["16000", "22050", "44100", "48000"])
        self.sample_rate_combo.setMinimumHeight(36)
        layout.addWidget(self.sample_rate_combo)

        # Channels
        layout.addSpacing(12)
        channels_label = QLabel("Channels:")
        channels_label.setStyleSheet("color: #e0e0ff;")
        layout.addWidget(channels_label)

        self.channels_combo = QComboBox()
        self.channels_combo.addItems(["Mono (1)", "Stereo (2)"])
        self.channels_combo.setMinimumHeight(36)
        layout.addWidget(self.channels_combo)

        # Silence threshold
        layout.addSpacing(12)
        threshold_label = QLabel("Silence Threshold:")
        threshold_label.setStyleSheet("color: #e0e0ff;")
        layout.addWidget(threshold_label)

        threshold_layout = QHBoxLayout()
        self.threshold_slider = QSlider(Qt.Orientation.Horizontal)
        self.threshold_slider.setMinimum(0)
        self.threshold_slider.setMaximum(100)
        self.threshold_slider.setValue(10)

        self.threshold_value_label = QLabel("0.01")
        self.threshold_value_label.setStyleSheet("color: #00d4ff; font-weight: bold;")
        self.threshold_value_label.setMaximumWidth(50)

        self.threshold_slider.valueChanged.connect(self._update_threshold_display)

        threshold_layout.addWidget(self.threshold_slider)
        threshold_layout.addWidget(self.threshold_value_label)
        layout.addLayout(threshold_layout)

        # Input device selection
        layout.addSpacing(16)
        device_label = QLabel("Input Device:")
        device_label.setStyleSheet("color: #e0e0ff;")
        layout.addWidget(device_label)

        self.audio_device_combo = QComboBox()
        self.audio_device_combo.setMinimumHeight(36)
        self._populate_audio_devices()
        layout.addWidget(self.audio_device_combo)

        device_info = QLabel("Select microphone for recording")
        device_info.setStyleSheet("color: #808090; font-size: 10px; font-style: italic;")
        layout.addWidget(device_info)

        layout.addStretch()
        self.tabs.addTab(tab, "Audio")

    def _create_hotkeys_tab(self):
        """Create hotkeys settings tab."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        # Title
        title = QLabel("Hotkeys")
        title_font = QFont("Segoe UI", 12)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setStyleSheet("color: #e0e0ff;")
        layout.addWidget(title)

        layout.addSpacing(12)
        info_label = QLabel("Configure global hotkeys for quick access")
        info_label.setStyleSheet("color: #a0a0c0; font-style: italic;")
        layout.addWidget(info_label)

        layout.addSpacing(16)
        hotkey_button = PrimaryButton("Configure Hotkeys...")
        hotkey_button.setMinimumHeight(40)
        hotkey_button.clicked.connect(self._open_hotkey_dialog)
        layout.addWidget(hotkey_button)

        layout.addStretch()
        self.tabs.addTab(tab, "Hotkeys")

    def _create_insights_tab(self):
        """Create insights/AI settings tab."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        # Title
        title = QLabel("Meeting Insights")
        title_font = QFont("Segoe UI", 12)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setStyleSheet("color: #e0e0ff;")
        layout.addWidget(title)

        # Description
        description = QLabel(
            "Configure AI providers for generating meeting insights.\n"
            "Right-click on any saved meeting to generate summaries, action items, or custom analysis."
        )
        description.setStyleSheet("color: #a0a0c0; font-size: 11px;")
        description.setWordWrap(True)
        layout.addWidget(description)

        # Provider selection
        layout.addSpacing(12)
        provider_label = QLabel("LLM Provider:")
        provider_label.setStyleSheet("color: #e0e0ff;")
        layout.addWidget(provider_label)

        self.insights_provider_combo = QComboBox()
        self.insights_provider_combo.addItems(["OpenAI", "OpenRouter"])
        self.insights_provider_combo.setMinimumHeight(36)
        self.insights_provider_combo.currentIndexChanged.connect(self._on_insights_provider_changed)
        layout.addWidget(self.insights_provider_combo)

        provider_info = QLabel(
            "OpenRouter provides access to 100+ models (Claude, Llama, Mistral, etc.)"
        )
        provider_info.setStyleSheet("color: #808090; font-size: 10px; font-style: italic;")
        provider_info.setWordWrap(True)
        layout.addWidget(provider_info)

        # Model selection
        layout.addSpacing(12)
        model_label = QLabel("Model:")
        model_label.setStyleSheet("color: #e0e0ff;")
        layout.addWidget(model_label)

        self.insights_model_combo = QComboBox()
        self.insights_model_combo.setEditable(True)  # Allow custom model names
        self.insights_model_combo.setMinimumHeight(36)
        self._populate_insights_models()
        layout.addWidget(self.insights_model_combo)

        model_info = QLabel("You can type a custom model name if not listed")
        model_info.setStyleSheet("color: #808090; font-size: 10px; font-style: italic;")
        layout.addWidget(model_info)

        # Separator
        layout.addSpacing(16)
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setStyleSheet("background-color: #404060;")
        layout.addWidget(separator)

        # API Keys section
        layout.addSpacing(8)
        api_keys_title = QLabel("API Keys")
        api_keys_title.setStyleSheet("color: #a0a0c0; font-weight: bold;")
        layout.addWidget(api_keys_title)

        # OpenAI API Key
        layout.addSpacing(8)
        openai_key_label = QLabel("OpenAI API Key:")
        openai_key_label.setStyleSheet("color: #e0e0ff;")
        layout.addWidget(openai_key_label)

        self.openai_key_input = QLineEdit()
        self.openai_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.openai_key_input.setPlaceholderText("sk-...")
        self.openai_key_input.setMinimumHeight(36)
        self.openai_key_input.setStyleSheet("""
            QLineEdit {
                background-color: #2d2d44;
                color: #e0e0ff;
                border: 1px solid #404060;
                border-radius: 6px;
                padding: 8px 12px;
            }
            QLineEdit:focus {
                border: 1px solid #6366f1;
            }
        """)
        layout.addWidget(self.openai_key_input)

        openai_info = QLabel("Used for OpenAI provider. Also used for transcription if configured.")
        openai_info.setStyleSheet("color: #808090; font-size: 10px;")
        layout.addWidget(openai_info)

        # OpenRouter API Key
        layout.addSpacing(12)
        openrouter_key_label = QLabel("OpenRouter API Key:")
        openrouter_key_label.setStyleSheet("color: #e0e0ff;")
        layout.addWidget(openrouter_key_label)

        self.openrouter_key_input = QLineEdit()
        self.openrouter_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.openrouter_key_input.setPlaceholderText("sk-or-...")
        self.openrouter_key_input.setMinimumHeight(36)
        self.openrouter_key_input.setStyleSheet("""
            QLineEdit {
                background-color: #2d2d44;
                color: #e0e0ff;
                border: 1px solid #404060;
                border-radius: 6px;
                padding: 8px 12px;
            }
            QLineEdit:focus {
                border: 1px solid #6366f1;
            }
        """)
        layout.addWidget(self.openrouter_key_input)

        openrouter_info = QLabel("Get your API key at openrouter.ai")
        openrouter_info.setStyleSheet("color: #808090; font-size: 10px;")
        layout.addWidget(openrouter_info)

        layout.addStretch()
        self.tabs.addTab(tab, "Insights")

    def _on_insights_provider_changed(self, index: int):
        """Handle insights provider selection change."""
        self._populate_insights_models()

    def _populate_insights_models(self):
        """Populate the insights model dropdown based on selected provider."""
        provider_index = self.insights_provider_combo.currentIndex()
        current_model = self.insights_model_combo.currentText()
        
        self.insights_model_combo.clear()
        
        if provider_index == 0:  # OpenAI
            models = [
                "gpt-4o",
                "gpt-4o-mini", 
                "gpt-4-turbo",
                "gpt-4",
                "gpt-3.5-turbo"
            ]
        else:  # OpenRouter
            models = [
                "anthropic/claude-3.5-sonnet",
                "anthropic/claude-3-opus",
                "openai/gpt-4o",
                "openai/gpt-4-turbo",
                "google/gemini-pro-1.5",
                "meta-llama/llama-3.1-70b-instruct",
                "mistralai/mistral-large"
            ]
        
        self.insights_model_combo.addItems(models)
        
        # Try to restore previous selection if valid for new provider
        if current_model:
            index = self.insights_model_combo.findText(current_model)
            if index >= 0:
                self.insights_model_combo.setCurrentIndex(index)
            else:
                # Set to first item
                self.insights_model_combo.setCurrentIndex(0)

    def _create_advanced_tab(self):
        """Create advanced settings tab."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        # Title
        title = QLabel("Advanced Settings")
        title_font = QFont("Segoe UI", 12)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setStyleSheet("color: #e0e0ff;")
        layout.addWidget(title)

        # Whisper Engine Settings section
        layout.addSpacing(12)
        whisper_title = QLabel("Whisper Engine")
        whisper_title.setStyleSheet("color: #a0a0c0; font-weight: bold;")
        layout.addWidget(whisper_title)

        # Whisper Model selection
        model_label = QLabel("Model:")
        model_label.setStyleSheet("color: #e0e0ff;")
        layout.addWidget(model_label)

        self.whisper_model_combo = QComboBox()
        self.whisper_model_combo.addItems(config.WHISPER_MODEL_CHOICES)
        self.whisper_model_combo.setMinimumHeight(36)
        layout.addWidget(self.whisper_model_combo)

        # Device selection
        layout.addSpacing(8)
        device_label = QLabel("Device:")
        device_label.setStyleSheet("color: #e0e0ff;")
        layout.addWidget(device_label)

        self.whisper_device_combo = QComboBox()
        self.whisper_device_combo.addItems(["auto", "cuda", "cpu"])
        self.whisper_device_combo.setMinimumHeight(36)
        layout.addWidget(self.whisper_device_combo)

        # Compute type selection
        layout.addSpacing(8)
        compute_label = QLabel("Compute Type:")
        compute_label.setStyleSheet("color: #e0e0ff;")
        layout.addWidget(compute_label)

        self.whisper_compute_combo = QComboBox()
        self.whisper_compute_combo.addItems(["auto", "float16", "float32", "int8"])
        self.whisper_compute_combo.setMinimumHeight(36)
        layout.addWidget(self.whisper_compute_combo)

        # Info label
        compute_info = QLabel("Changes require restarting the whisper engine")
        compute_info.setStyleSheet("color: #808090; font-size: 10px; font-style: italic;")
        layout.addWidget(compute_info)

        # Separator
        layout.addSpacing(16)
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setStyleSheet("background-color: #404060;")
        layout.addWidget(separator)

        # Meeting Recording Settings section
        layout.addSpacing(12)
        meeting_rec_title = QLabel("Meeting Recordings")
        meeting_rec_title.setStyleSheet("color: #a0a0c0; font-weight: bold;")
        layout.addWidget(meeting_rec_title)

        # Enable meeting recording checkbox
        self.meeting_recording_check = QCheckBox("Save complete audio recording of meetings")
        self.meeting_recording_check.setStyleSheet("color: #e0e0ff;")
        self.meeting_recording_check.stateChanged.connect(self._on_meeting_recording_changed)
        layout.addWidget(self.meeting_recording_check)

        # Info label for meeting recording
        meeting_rec_info = QLabel(
            "Saves uncut WAV file (~300MB/hour). Stored in 'meeting_recordings' folder.\n"
            "Useful for archiving meetings that cannot be replicated if lost."
        )
        meeting_rec_info.setStyleSheet("color: #808090; font-size: 10px;")
        meeting_rec_info.setWordWrap(True)
        layout.addWidget(meeting_rec_info)

        # Max recordings spinbox
        layout.addSpacing(8)
        max_rec_layout = QHBoxLayout()
        max_rec_label = QLabel("Maximum recordings to keep:")
        max_rec_label.setStyleSheet("color: #e0e0ff;")
        max_rec_layout.addWidget(max_rec_label)

        self.max_recordings_spinbox = QSpinBox()
        self.max_recordings_spinbox.setMinimum(1)
        self.max_recordings_spinbox.setMaximum(50)
        self.max_recordings_spinbox.setValue(config.MAX_MEETING_RECORDINGS)
        self.max_recordings_spinbox.setMinimumHeight(36)
        self.max_recordings_spinbox.setMaximumWidth(80)
        max_rec_layout.addWidget(self.max_recordings_spinbox)
        max_rec_layout.addStretch()
        layout.addLayout(max_rec_layout)

        max_rec_info = QLabel("Oldest recordings are automatically deleted when limit is reached")
        max_rec_info.setStyleSheet("color: #808090; font-size: 10px; font-style: italic;")
        layout.addWidget(max_rec_info)

        # Separator
        layout.addSpacing(16)
        separator2 = QFrame()
        separator2.setFrameShape(QFrame.Shape.HLine)
        separator2.setStyleSheet("background-color: #404060;")
        layout.addWidget(separator2)

        # Max file size
        layout.addSpacing(12)
        max_size_label = QLabel("Maximum File Size (MB):")
        max_size_label.setStyleSheet("color: #e0e0ff;")
        layout.addWidget(max_size_label)

        self.max_size_spinbox = QSpinBox()
        self.max_size_spinbox.setMinimum(1)
        self.max_size_spinbox.setMaximum(500)
        self.max_size_spinbox.setValue(23)
        self.max_size_spinbox.setMinimumHeight(36)
        layout.addWidget(self.max_size_spinbox)

        # Enable logging checkbox
        layout.addSpacing(12)
        self.logging_check = QCheckBox("Enable detailed logging")
        self.logging_check.setStyleSheet("color: #e0e0ff;")
        layout.addWidget(self.logging_check)

        layout.addStretch()
        self.tabs.addTab(tab, "Advanced")

    def _on_meeting_recording_changed(self, state):
        """Handle meeting recording checkbox state change."""
        enabled = state == Qt.CheckState.Checked.value
        self.max_recordings_spinbox.setEnabled(enabled)

    def _update_threshold_display(self, value):
        """Update threshold value display."""
        threshold = value / 1000.0
        self.threshold_value_label.setText(f"{threshold:.3f}")

    def _on_streaming_enabled_changed(self, state):
        """Handle streaming enabled checkbox state change."""
        # Enable/disable live typing checkbox based on streaming enabled state
        streaming_enabled = state == Qt.CheckState.Checked.value
        self.streaming_paste_check.setEnabled(streaming_enabled)
        if not streaming_enabled:
            self.streaming_paste_check.setChecked(False)

    def _on_streaming_paste_changed(self, state):
        """Handle streaming paste checkbox state change."""
        pass  # No additional action needed

    def _populate_audio_devices(self):
        """Populate the audio device dropdown with available input devices."""
        self.audio_device_combo.clear()
        # Add system default option
        self.audio_device_combo.addItem("System Default", None)

        # Add available input devices
        devices = AudioRecorder.get_input_devices()
        for device_id, device_name in devices:
            self.audio_device_combo.addItem(device_name, device_id)

    def _open_hotkey_dialog(self):
        """Open hotkey configuration dialog."""
        self.logger.info("Opening hotkey configuration dialog")
        from ui_qt.dialogs.hotkey_dialog import HotkeyDialog
        
        dialog = HotkeyDialog(self)
        dialog.exec()

    def _load_settings(self):
        """Load settings from configuration."""
        try:
            settings = settings_manager.load_all_settings()

            # Load model selection
            saved_model = settings.get('selected_model', 'local_whisper')
            # Find display name for saved model
            for display_name, internal_value in config.MODEL_VALUE_MAP.items():
                if internal_value == saved_model:
                    index = self.model_combo.findText(display_name)
                    if index >= 0:
                        self.model_combo.setCurrentIndex(index)
                    break

            # Load checkboxes
            self.auto_paste_check.setChecked(settings.get('auto_paste', True))
            self.copy_clipboard_check.setChecked(settings.get('copy_clipboard', True))
            self.minimize_tray_check.setChecked(settings.get('minimize_tray', True))
            
            # Load streaming settings
            streaming_enabled = settings.get('streaming_enabled', config.STREAMING_ENABLED)
            self.streaming_enabled_check.setChecked(streaming_enabled)
            self.streaming_paste_check.setChecked(settings.get('streaming_paste_enabled', False))
            self.streaming_paste_check.setEnabled(streaming_enabled)

            # Load whisper engine settings
            whisper_model = settings.get('whisper_model', config.DEFAULT_WHISPER_MODEL)
            whisper_device = settings.get('whisper_device', 'auto')
            whisper_compute = settings.get('whisper_compute_type', 'auto')

            model_index = self.whisper_model_combo.findText(whisper_model)
            if model_index >= 0:
                self.whisper_model_combo.setCurrentIndex(model_index)

            device_index = self.whisper_device_combo.findText(whisper_device)
            if device_index >= 0:
                self.whisper_device_combo.setCurrentIndex(device_index)

            compute_index = self.whisper_compute_combo.findText(whisper_compute)
            if compute_index >= 0:
                self.whisper_compute_combo.setCurrentIndex(compute_index)

            # Load audio input device
            saved_device_id = settings.get('audio_input_device')
            if saved_device_id is not None:
                # Find the device in the combo box by its data (device ID)
                for i in range(self.audio_device_combo.count()):
                    if self.audio_device_combo.itemData(i) == saved_device_id:
                        self.audio_device_combo.setCurrentIndex(i)
                        break

            # Load insights settings
            insights_provider = settings.get('insights_provider', 'openai')
            if insights_provider == 'openrouter':
                self.insights_provider_combo.setCurrentIndex(1)
            else:
                self.insights_provider_combo.setCurrentIndex(0)
            
            # Populate models first, then set saved model
            self._populate_insights_models()
            insights_model = settings.get('insights_model', '')
            if insights_model:
                index = self.insights_model_combo.findText(insights_model)
                if index >= 0:
                    self.insights_model_combo.setCurrentIndex(index)
                else:
                    # Custom model - set it directly
                    self.insights_model_combo.setCurrentText(insights_model)
            
            # Load API keys (if saved)
            openai_key = settings.get('insights_openai_key', '')
            openrouter_key = settings.get('insights_openrouter_key', '')
            self.openai_key_input.setText(openai_key)
            self.openrouter_key_input.setText(openrouter_key)

            # Load meeting recording settings
            meeting_rec_enabled = settings.get('meeting_recording_enabled', True)
            max_recordings = settings.get('meeting_max_recordings', config.MAX_MEETING_RECORDINGS)
            self.meeting_recording_check.setChecked(meeting_rec_enabled)
            self.max_recordings_spinbox.setValue(max_recordings)
            self.max_recordings_spinbox.setEnabled(meeting_rec_enabled)

            self.logger.info("Settings loaded successfully")
        except Exception as e:
            self.logger.error(f"Failed to load settings: {e}")
            # Use defaults on error
            self.auto_paste_check.setChecked(True)
            self.copy_clipboard_check.setChecked(True)
            self.minimize_tray_check.setChecked(True)
            self.streaming_enabled_check.setChecked(config.STREAMING_ENABLED)
            self.streaming_paste_check.setChecked(False)
            self.streaming_paste_check.setEnabled(config.STREAMING_ENABLED)
            self.meeting_recording_check.setChecked(True)
            self.max_recordings_spinbox.setValue(config.MAX_MEETING_RECORDINGS)
            self.max_recordings_spinbox.setEnabled(True)

    def _save_settings(self):
        """Save settings and close dialog."""
        try:
            # Get current display name and convert to internal value
            model_display = self.model_combo.currentText()
            model_internal = config.MODEL_VALUE_MAP.get(model_display, 'local_whisper')

            # Load existing settings
            settings = settings_manager.load_all_settings()

            # Check if whisper engine settings changed
            old_whisper_model = settings.get('whisper_model', config.DEFAULT_WHISPER_MODEL)
            old_device = settings.get('whisper_device', 'auto')
            old_compute = settings.get('whisper_compute_type', 'auto')
            new_whisper_model = self.whisper_model_combo.currentText()
            new_device = self.whisper_device_combo.currentText()
            new_compute = self.whisper_compute_combo.currentText()
            whisper_settings_changed = (
                old_whisper_model != new_whisper_model or
                old_device != new_device or
                old_compute != new_compute
            )

            # Check if audio input device changed
            old_audio_device = settings.get('audio_input_device')
            new_audio_device = self.audio_device_combo.currentData()
            audio_device_changed = old_audio_device != new_audio_device

            # Check if streaming settings changed
            old_streaming_enabled = settings.get('streaming_enabled', False)
            old_streaming_paste = settings.get('streaming_paste_enabled', False)
            streaming_settings_changed = (
                old_streaming_enabled != self.streaming_enabled_check.isChecked() or
                old_streaming_paste != self.streaming_paste_check.isChecked()
            )

            # Update with new values
            settings['selected_model'] = model_internal
            settings['auto_paste'] = self.auto_paste_check.isChecked()
            settings['copy_clipboard'] = self.copy_clipboard_check.isChecked()
            settings['minimize_tray'] = self.minimize_tray_check.isChecked()
            settings['streaming_enabled'] = self.streaming_enabled_check.isChecked()
            settings['streaming_paste_enabled'] = self.streaming_paste_check.isChecked()
            settings['whisper_model'] = new_whisper_model
            settings['whisper_device'] = new_device
            settings['whisper_compute_type'] = new_compute

            # Save audio input device (None for system default)
            if new_audio_device is None:
                settings.pop('audio_input_device', None)
            else:
                settings['audio_input_device'] = new_audio_device

            # Save insights settings
            insights_provider = 'openrouter' if self.insights_provider_combo.currentIndex() == 1 else 'openai'
            settings['insights_provider'] = insights_provider
            settings['insights_model'] = self.insights_model_combo.currentText()
            
            # Save API keys (only if provided)
            openai_key = self.openai_key_input.text().strip()
            openrouter_key = self.openrouter_key_input.text().strip()
            if openai_key:
                settings['insights_openai_key'] = openai_key
            else:
                settings.pop('insights_openai_key', None)
            if openrouter_key:
                settings['insights_openrouter_key'] = openrouter_key
            else:
                settings.pop('insights_openrouter_key', None)

            # Save meeting recording settings
            settings['meeting_recording_enabled'] = self.meeting_recording_check.isChecked()
            settings['meeting_max_recordings'] = self.max_recordings_spinbox.value()

            # Save to file
            settings_manager.save_all_settings(settings)

            self.logger.info("Settings saved successfully")

            # Call callback if set
            if self.on_settings_save:
                self.on_settings_save(settings)

            # Emit signal with change flags
            settings['_whisper_settings_changed'] = whisper_settings_changed
            settings['_audio_device_changed'] = audio_device_changed
            settings['_streaming_settings_changed'] = streaming_settings_changed
            self.settings_changed.emit(settings)

            self.accept()
        except Exception as e:
            self.logger.error(f"Failed to save settings: {e}")
            self.reject()

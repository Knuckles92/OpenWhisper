"""
Modern Settings Dialog for PyQt6 UI.
Tabbed interface for managing application settings.
"""
import logging
from typing import Optional, Callable
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTabWidget,
    QWidget, QLabel, QComboBox, QCheckBox, QSpinBox,
    QSlider, QFrame
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

        # Streaming paste (live typing) checkbox
        layout.addSpacing(8)
        self.streaming_paste_check = QCheckBox("Type live to cursor while recording")
        self.streaming_paste_check.setStyleSheet("color: #e0e0ff;")
        self.streaming_paste_check.stateChanged.connect(self._on_streaming_paste_changed)
        layout.addWidget(self.streaming_paste_check)

        # Info label for live typing
        streaming_paste_info = QLabel("Types transcribed text character-by-character wherever your cursor is.\nFinal transcription will replace the streamed text.")
        streaming_paste_info.setStyleSheet("color: #808090; font-size: 10px;")
        streaming_paste_info.setWordWrap(True)
        layout.addWidget(streaming_paste_info)

        # Typing speed slider
        layout.addSpacing(8)
        typing_speed_label = QLabel("Typing Speed:")
        typing_speed_label.setStyleSheet("color: #e0e0ff;")
        layout.addWidget(typing_speed_label)

        typing_speed_layout = QHBoxLayout()
        self.typing_speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.typing_speed_slider.setMinimum(0)  # 0ms = instant
        self.typing_speed_slider.setMaximum(50)  # 50ms = slow typing
        self.typing_speed_slider.setValue(20)  # Default 20ms
        self.typing_speed_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.typing_speed_slider.setTickInterval(10)

        self.typing_speed_value_label = QLabel("20ms")
        self.typing_speed_value_label.setStyleSheet("color: #00d4ff; font-weight: bold;")
        self.typing_speed_value_label.setMinimumWidth(50)

        self.typing_speed_slider.valueChanged.connect(self._update_typing_speed_display)

        typing_speed_layout.addWidget(self.typing_speed_slider)
        typing_speed_layout.addWidget(self.typing_speed_value_label)
        layout.addLayout(typing_speed_layout)

        typing_speed_info = QLabel("Lower = faster typing. 0 = instant, 50 = slow typewriter effect.")
        typing_speed_info.setStyleSheet("color: #808090; font-size: 10px;")
        layout.addWidget(typing_speed_info)

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
        # Also update typing speed slider state
        self._update_typing_speed_slider_state()

    def _on_streaming_paste_changed(self, state):
        """Handle streaming paste checkbox state change."""
        self._update_typing_speed_slider_state()

    def _update_typing_speed_slider_state(self):
        """Update typing speed slider enabled state based on checkbox states."""
        paste_enabled = self.streaming_paste_check.isChecked() and self.streaming_paste_check.isEnabled()
        self.typing_speed_slider.setEnabled(paste_enabled)

    def _update_typing_speed_display(self, value):
        """Update typing speed value display."""
        if value == 0:
            self.typing_speed_value_label.setText("Instant")
        else:
            self.typing_speed_value_label.setText(f"{value}ms")

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
            
            # Load typing speed (stored as seconds, convert to ms for slider)
            typing_delay = settings.get('streaming_typing_delay', 0.02)
            typing_delay_ms = int(typing_delay * 1000)
            self.typing_speed_slider.setValue(typing_delay_ms)
            self._update_typing_speed_display(typing_delay_ms)
            self._update_typing_speed_slider_state()

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
            self.typing_speed_slider.setValue(20)
            self._update_typing_speed_slider_state()

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

            # Update with new values
            settings['selected_model'] = model_internal
            settings['auto_paste'] = self.auto_paste_check.isChecked()
            settings['copy_clipboard'] = self.copy_clipboard_check.isChecked()
            settings['minimize_tray'] = self.minimize_tray_check.isChecked()
            settings['streaming_enabled'] = self.streaming_enabled_check.isChecked()
            settings['streaming_paste_enabled'] = self.streaming_paste_check.isChecked()
            settings['streaming_typing_delay'] = self.typing_speed_slider.value() / 1000.0  # Convert ms to seconds
            settings['whisper_model'] = new_whisper_model
            settings['whisper_device'] = new_device
            settings['whisper_compute_type'] = new_compute

            # Save audio input device (None for system default)
            if new_audio_device is None:
                settings.pop('audio_input_device', None)
            else:
                settings['audio_input_device'] = new_audio_device

            # Save to file
            settings_manager.save_all_settings(settings)

            self.logger.info("Settings saved successfully")

            # Call callback if set
            if self.on_settings_save:
                self.on_settings_save(settings)

            # Emit signal with change flags
            settings['_whisper_settings_changed'] = whisper_settings_changed
            settings['_audio_device_changed'] = audio_device_changed
            self.settings_changed.emit(settings)

            self.accept()
        except Exception as e:
            self.logger.error(f"Failed to save settings: {e}")
            self.reject()

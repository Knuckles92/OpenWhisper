"""
Settings dialog for PyQt6 UI.
Tabbed interface for managing application settings.
"""
import logging
import sys
from typing import Optional, Callable
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTabWidget,
    QWidget, QLabel, QComboBox, QCheckBox, QSpinBox,
    QSlider, QFrame, QScrollArea
)
from PyQt6.QtCore import Qt, pyqtSignal

from config import config
from services.settings import (
    HuggingFaceAccessPolicy,
    RecordingRetentionMode,
    SettingsKey,
    resolve_max_saved_recordings,
    resolve_streaming_overlay_font_size,
    settings_manager,
)
from services.history_manager import history_manager
from services.recorder import AudioRecorder
from ui_qt.widgets import PrimaryButton, Button

logger = logging.getLogger(__name__)


class SettingsDialog(QDialog):
    """Settings dialog with tabbed interface."""

    settings_changed = pyqtSignal(dict)

    def __init__(self, parent=None):
        """Initialize settings dialog."""
        super().__init__(parent)
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

        # Tab widget: segmented-button tab styling lives in theme.qss
        # under the #settingsTabs rules.
        self.tabs = QTabWidget()
        self.tabs.setObjectName("settingsTabs")
        self.tabs.tabBar().setCursor(Qt.CursorShape.PointingHandCursor)

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

        cancel_btn = Button("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(cancel_btn)

        save_btn = PrimaryButton("Save Settings")
        save_btn.clicked.connect(self._save_settings)
        button_layout.addWidget(save_btn)

        layout.addLayout(button_layout)

    def _create_general_tab(self):
        """Create general settings tab."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        # Title
        title = QLabel("General Settings")
        title.setObjectName("headerLabel")
        layout.addWidget(title)

        # Model selection
        layout.addSpacing(12)
        model_label = QLabel("Default Model:")
        layout.addWidget(model_label)

        self.model_combo = QComboBox()
        self.model_combo.addItems(config.MODEL_CHOICES)
        self.model_combo.setMinimumHeight(36)
        layout.addWidget(self.model_combo)

        # Auto-paste checkbox
        layout.addSpacing(12)
        self.auto_paste_check = QCheckBox("Auto-paste transcription to active window")
        layout.addWidget(self.auto_paste_check)

        # Copy to clipboard checkbox
        self.copy_clipboard_check = QCheckBox("Copy transcription to clipboard")
        layout.addWidget(self.copy_clipboard_check)

        # Minimize to tray checkbox
        layout.addSpacing(12)
        self.minimize_tray_check = QCheckBox("Minimize to system tray on close")
        layout.addWidget(self.minimize_tray_check)

        # Saved recordings retention
        layout.addSpacing(24)
        recordings_label = QLabel("Saved Recordings")
        recordings_label.setObjectName("sectionLabel")
        layout.addWidget(recordings_label)

        layout.addSpacing(8)
        retention_label = QLabel("Keep recordings:")
        layout.addWidget(retention_label)

        self.recording_retention_combo = QComboBox()
        self.recording_retention_combo.addItem("Keep all", RecordingRetentionMode.KEEP_ALL)
        self.recording_retention_combo.addItem("Custom", RecordingRetentionMode.CUSTOM)
        self.recording_retention_combo.setMinimumHeight(36)
        self.recording_retention_combo.currentIndexChanged.connect(
            self._update_recording_retention_ui
        )
        layout.addWidget(self.recording_retention_combo)

        custom_count_layout = QHBoxLayout()
        custom_count_layout.setSpacing(8)
        self.max_recordings_label = QLabel("Number to keep:")
        custom_count_layout.addWidget(self.max_recordings_label)

        self.max_recordings_spinbox = QSpinBox()
        self.max_recordings_spinbox.setMinimum(1)
        self.max_recordings_spinbox.setMaximum(1000)
        self.max_recordings_spinbox.setValue(config.MAX_SAVED_RECORDINGS)
        self.max_recordings_spinbox.setMinimumHeight(36)
        custom_count_layout.addWidget(self.max_recordings_spinbox)
        custom_count_layout.addStretch()
        layout.addLayout(custom_count_layout)

        retention_info = QLabel(
            "Older audio files are deleted automatically when the limit is exceeded. "
            "Transcription history text is kept separately."
        )
        retention_info.setObjectName("infoLabel")
        retention_info.setWordWrap(True)
        layout.addWidget(retention_info)

        # Streaming transcription checkbox
        layout.addSpacing(24)
        streaming_label = QLabel("Real-Time Transcription (Experimental)")
        streaming_label.setObjectName("sectionLabel")
        layout.addWidget(streaming_label)

        layout.addSpacing(8)
        self.streaming_enabled_check = QCheckBox("Enable real-time transcription preview (while recording)")
        self.streaming_enabled_check.toggled.connect(self._update_streaming_font_ui)
        layout.addWidget(self.streaming_enabled_check)

        font_size_layout = QHBoxLayout()
        font_size_layout.setSpacing(8)
        self.streaming_font_size_label = QLabel("Preview font size:")
        font_size_layout.addWidget(self.streaming_font_size_label)

        self.streaming_font_size_spinbox = QSpinBox()
        self.streaming_font_size_spinbox.setMinimum(10)
        self.streaming_font_size_spinbox.setMaximum(48)
        self.streaming_font_size_spinbox.setSuffix(" pt")
        self.streaming_font_size_spinbox.setValue(config.STREAMING_OVERLAY_FONT_SIZE)
        self.streaming_font_size_spinbox.setMinimumHeight(36)
        font_size_layout.addWidget(self.streaming_font_size_spinbox)
        font_size_layout.addStretch()
        layout.addLayout(font_size_layout)

        # Info label for streaming
        streaming_info = QLabel(
            "Shows transcribed text as you speak on the near-cursor overlay using a dedicated "
            "tiny.en preview model. Requires Local Whisper backend. Final transcription still "
            "uses your selected model and normal auto-paste / clipboard settings."
        )
        streaming_info.setObjectName("infoLabel")
        streaming_info.setWordWrap(True)
        layout.addWidget(streaming_info)

        self._update_streaming_font_ui()

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
        title.setObjectName("headerLabel")
        layout.addWidget(title)

        # Sample rate
        layout.addSpacing(12)
        sample_rate_label = QLabel("Sample Rate (Hz):")
        layout.addWidget(sample_rate_label)

        self.sample_rate_combo = QComboBox()
        self.sample_rate_combo.addItems(["16000", "22050", "44100", "48000"])
        self.sample_rate_combo.setMinimumHeight(36)
        layout.addWidget(self.sample_rate_combo)

        # Channels
        layout.addSpacing(12)
        channels_label = QLabel("Channels:")
        layout.addWidget(channels_label)

        self.channels_combo = QComboBox()
        self.channels_combo.addItems(["Mono (1)", "Stereo (2)"])
        self.channels_combo.setMinimumHeight(36)
        layout.addWidget(self.channels_combo)

        # Silence threshold
        layout.addSpacing(12)
        threshold_label = QLabel("Silence Threshold:")
        layout.addWidget(threshold_label)

        threshold_layout = QHBoxLayout()
        self.threshold_slider = QSlider(Qt.Orientation.Horizontal)
        self.threshold_slider.setMinimum(0)
        self.threshold_slider.setMaximum(100)
        self.threshold_slider.setValue(10)

        self.threshold_value_label = QLabel("0.01")
        self.threshold_value_label.setObjectName("accentLabel")
        self.threshold_value_label.setMaximumWidth(50)

        self.threshold_slider.valueChanged.connect(self._update_threshold_display)

        threshold_layout.addWidget(self.threshold_slider)
        threshold_layout.addWidget(self.threshold_value_label)
        layout.addLayout(threshold_layout)

        # Input device selection
        layout.addSpacing(16)
        device_label = QLabel("Input Device:")
        layout.addWidget(device_label)

        self.audio_device_combo = QComboBox()
        self.audio_device_combo.setMinimumHeight(36)
        self._populate_audio_devices()
        layout.addWidget(self.audio_device_combo)

        device_info = QLabel("Select microphone for recording")
        device_info.setObjectName("infoLabel")
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
        title.setObjectName("headerLabel")
        layout.addWidget(title)

        layout.addSpacing(12)
        info_label = QLabel("Configure global hotkeys for quick access")
        info_label.setObjectName("infoLabel")
        layout.addWidget(info_label)

        layout.addSpacing(16)
        hotkey_button = PrimaryButton("Configure Hotkeys...")
        hotkey_button.setMinimumHeight(40)
        hotkey_button.clicked.connect(self._open_hotkey_dialog)
        layout.addWidget(hotkey_button)

        layout.addStretch()
        self.tabs.addTab(tab, "Hotkeys")

    def _create_advanced_tab(self):
        """Create advanced settings tab with scrollable content."""
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(0, 0, 0, 0)
        tab_layout.setSpacing(0)

        # Create scroll area
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)

        # Content widget for scrollable area
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        # Title
        title = QLabel("Advanced Settings")
        title.setObjectName("headerLabel")
        layout.addWidget(title)

        # Whisper Engine Settings section
        layout.addSpacing(12)
        whisper_title = QLabel("Whisper Engine")
        whisper_title.setObjectName("sectionLabel")
        layout.addWidget(whisper_title)

        # Whisper Model selection
        model_label = QLabel("Model:")
        layout.addWidget(model_label)

        self.whisper_model_combo = QComboBox()
        self.whisper_model_combo.addItems(config.WHISPER_MODEL_CHOICES)
        self.whisper_model_combo.setMinimumHeight(36)
        layout.addWidget(self.whisper_model_combo)

        # Device selection
        layout.addSpacing(8)
        device_label = QLabel("Device:")
        layout.addWidget(device_label)

        self.whisper_device_combo = QComboBox()
        # CUDA is unavailable on macOS (no Metal backend in faster-whisper).
        if sys.platform == "darwin":
            self.whisper_device_combo.addItems(["auto", "cpu"])
        else:
            self.whisper_device_combo.addItems(["auto", "cuda", "cpu"])
        self.whisper_device_combo.setMinimumHeight(36)
        layout.addWidget(self.whisper_device_combo)

        # Compute type selection
        layout.addSpacing(8)
        compute_label = QLabel("Compute Type:")
        layout.addWidget(compute_label)

        self.whisper_compute_combo = QComboBox()
        self.whisper_compute_combo.addItems(["auto", "float16", "float32", "int8"])
        self.whisper_compute_combo.setMinimumHeight(36)
        layout.addWidget(self.whisper_compute_combo)

        # Info label
        compute_info = QLabel("Changes require restarting the whisper engine")
        compute_info.setObjectName("infoLabel")
        layout.addWidget(compute_info)

        # Max file size
        layout.addSpacing(12)
        max_size_label = QLabel("Maximum File Size (MB):")
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
        layout.addWidget(self.logging_check)

        # Hugging Face model download policy
        layout.addSpacing(16)
        hf_title = QLabel("Hugging Face Downloads")
        hf_title.setObjectName("sectionLabel")
        layout.addWidget(hf_title)

        hf_policy_label = QLabel("When a model is missing from this computer:")
        layout.addWidget(hf_policy_label)

        self.hf_policy_combo = QComboBox()
        self.hf_policy_combo.setObjectName("hfPolicyCombo")
        self.hf_policy_combo.addItem(
            "Ask before downloading", HuggingFaceAccessPolicy.ASK
        )
        self.hf_policy_combo.addItem(
            "Always allow downloads", HuggingFaceAccessPolicy.ALWAYS
        )
        self.hf_policy_combo.addItem(
            "Never connect (fully offline)", HuggingFaceAccessPolicy.NEVER
        )
        self.hf_policy_combo.setMinimumHeight(36)
        layout.addWidget(self.hf_policy_combo)

        hf_info = QLabel(
            "Models already on this computer always load locally without any "
            "network checks. Hugging Face is only contacted to download a "
            "missing model, and only when this policy (or a one-time approval) "
            "allows it. An external HF_HUB_OFFLINE=1 environment variable "
            "disables downloads entirely."
        )
        hf_info.setObjectName("infoLabel")
        hf_info.setWordWrap(True)
        layout.addWidget(hf_info)

        layout.addStretch()

        # Wire up scroll area
        scroll_area.setWidget(content)
        tab_layout.addWidget(scroll_area)
        self._advanced_tab_index = self.tabs.addTab(tab, "Advanced")

    def focus_hf_policy(self):
        """Open the Advanced tab with the Hugging Face policy control focused.

        Used by the consent dialog's "Open Settings" action so the user lands
        directly on the download-policy control.
        """
        self.tabs.setCurrentIndex(self._advanced_tab_index)
        self.hf_policy_combo.setFocus()

    def _update_threshold_display(self, value):
        """Update threshold value display."""
        threshold = value / 1000.0
        self.threshold_value_label.setText(f"{threshold:.3f}")

    def _update_recording_retention_ui(self):
        """Enable the custom count spinbox only when Custom is selected."""
        is_custom = (
            self.recording_retention_combo.currentData()
            == RecordingRetentionMode.CUSTOM
        )
        self.max_recordings_label.setEnabled(is_custom)
        self.max_recordings_spinbox.setEnabled(is_custom)

    def _update_streaming_font_ui(self):
        """Enable the preview font size control only when streaming is on."""
        enabled = self.streaming_enabled_check.isChecked()
        self.streaming_font_size_label.setEnabled(enabled)
        self.streaming_font_size_spinbox.setEnabled(enabled)

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
        logger.info("Opening hotkey configuration dialog")
        from ui_qt.dialogs.hotkey_dialog import HotkeyDialog

        dialog = HotkeyDialog(self)
        dialog.exec()

    def _load_settings(self):
        """Load settings from configuration."""
        try:
            settings = settings_manager.load_all_settings()

            # Load model selection
            saved_model = settings.get(SettingsKey.SELECTED_MODEL, 'local_whisper')
            # Find display name for saved model
            for display_name, internal_value in config.MODEL_VALUE_MAP.items():
                if internal_value == saved_model:
                    index = self.model_combo.findText(display_name)
                    if index >= 0:
                        self.model_combo.setCurrentIndex(index)
                    break

            # Load checkboxes
            self.auto_paste_check.setChecked(settings.get(SettingsKey.AUTO_PASTE, True))
            self.copy_clipboard_check.setChecked(settings.get(SettingsKey.COPY_CLIPBOARD, True))
            self.minimize_tray_check.setChecked(settings.get(SettingsKey.MINIMIZE_TRAY, True))

            # Load recording retention
            retention_mode = settings.get(
                SettingsKey.RECORDING_RETENTION_MODE,
                RecordingRetentionMode.CUSTOM,
            )
            retention_index = self.recording_retention_combo.findData(retention_mode)
            if retention_index < 0:
                retention_index = self.recording_retention_combo.findData(
                    RecordingRetentionMode.CUSTOM
                )
            self.recording_retention_combo.setCurrentIndex(max(0, retention_index))
            max_recordings = settings.get(
                SettingsKey.MAX_SAVED_RECORDINGS,
                config.MAX_SAVED_RECORDINGS,
            )
            try:
                self.max_recordings_spinbox.setValue(max(1, int(max_recordings)))
            except (TypeError, ValueError):
                self.max_recordings_spinbox.setValue(config.MAX_SAVED_RECORDINGS)
            self._update_recording_retention_ui()

            # Load streaming settings
            streaming_enabled = settings.get(SettingsKey.STREAMING_ENABLED, config.STREAMING_ENABLED)
            self.streaming_enabled_check.setChecked(streaming_enabled)
            self.streaming_font_size_spinbox.setValue(
                resolve_streaming_overlay_font_size(settings)
            )
            self._update_streaming_font_ui()

            # Load whisper engine settings
            whisper_model = settings.get(SettingsKey.WHISPER_MODEL, config.DEFAULT_WHISPER_MODEL)
            whisper_device = settings.get(SettingsKey.WHISPER_DEVICE, 'auto')
            whisper_compute = settings.get(SettingsKey.WHISPER_COMPUTE_TYPE, 'auto')

            model_index = self.whisper_model_combo.findText(whisper_model)
            if model_index >= 0:
                self.whisper_model_combo.setCurrentIndex(model_index)

            device_index = self.whisper_device_combo.findText(whisper_device)
            if device_index < 0:
                logger.warning("Unsupported whisper device '%s'; falling back to auto", whisper_device)
                device_index = self.whisper_device_combo.findText('auto')
            if device_index >= 0:
                self.whisper_device_combo.setCurrentIndex(device_index)

            compute_index = self.whisper_compute_combo.findText(whisper_compute)
            if compute_index >= 0:
                self.whisper_compute_combo.setCurrentIndex(compute_index)

            # Typed load performs legacy hf_hub_offline migration
            policy = settings_manager.load_hf_access_policy()
            policy_index = self.hf_policy_combo.findData(policy)
            self.hf_policy_combo.setCurrentIndex(max(0, policy_index))

            # Load audio input device
            saved_device_id = settings.get(SettingsKey.AUDIO_INPUT_DEVICE)
            if saved_device_id is not None:
                # Find the device in the combo box by its data (device ID)
                for i in range(self.audio_device_combo.count()):
                    if self.audio_device_combo.itemData(i) == saved_device_id:
                        self.audio_device_combo.setCurrentIndex(i)
                        break

            logger.info("Settings loaded successfully")
        except Exception as e:
            logger.error(f"Failed to load settings: {e}")
            # Use defaults on error
            self.auto_paste_check.setChecked(True)
            self.copy_clipboard_check.setChecked(True)
            self.minimize_tray_check.setChecked(True)
            retention_index = self.recording_retention_combo.findData(
                RecordingRetentionMode.CUSTOM
            )
            self.recording_retention_combo.setCurrentIndex(max(0, retention_index))
            self.max_recordings_spinbox.setValue(config.MAX_SAVED_RECORDINGS)
            self._update_recording_retention_ui()
            self.streaming_enabled_check.setChecked(config.STREAMING_ENABLED)
            self.streaming_font_size_spinbox.setValue(config.STREAMING_OVERLAY_FONT_SIZE)
            self._update_streaming_font_ui()
            self.hf_policy_combo.setCurrentIndex(
                max(0, self.hf_policy_combo.findData(HuggingFaceAccessPolicy.ASK))
            )

    def _save_settings(self):
        """Save settings and close dialog."""
        try:
            # Get current display name and convert to internal value
            model_display = self.model_combo.currentText()
            model_internal = config.MODEL_VALUE_MAP.get(model_display, 'local_whisper')

            # Load existing settings
            settings = settings_manager.load_all_settings()

            # Check if whisper engine settings changed
            old_whisper_model = settings.get(SettingsKey.WHISPER_MODEL, config.DEFAULT_WHISPER_MODEL)
            old_device = settings.get(SettingsKey.WHISPER_DEVICE, 'auto')
            old_compute = settings.get(SettingsKey.WHISPER_COMPUTE_TYPE, 'auto')
            new_whisper_model = self.whisper_model_combo.currentText()
            new_device = self.whisper_device_combo.currentText()
            new_compute = self.whisper_compute_combo.currentText()
            whisper_settings_changed = (
                old_whisper_model != new_whisper_model or
                old_device != new_device or
                old_compute != new_compute
            )

            # Check if the Hugging Face access policy changed
            old_hf_policy = settings_manager.load_hf_access_policy()
            new_hf_policy = self.hf_policy_combo.currentData()
            hf_policy_changed = old_hf_policy != new_hf_policy

            # Check if audio input device changed
            old_audio_device = settings.get(SettingsKey.AUDIO_INPUT_DEVICE)
            new_audio_device = self.audio_device_combo.currentData()
            audio_device_changed = old_audio_device != new_audio_device

            # Check if streaming settings changed
            old_streaming_enabled = settings.get(SettingsKey.STREAMING_ENABLED, False)
            streaming_settings_changed = (
                old_streaming_enabled != self.streaming_enabled_check.isChecked()
            )

            # Update with new values
            settings[SettingsKey.SELECTED_MODEL] = model_internal
            settings[SettingsKey.AUTO_PASTE] = self.auto_paste_check.isChecked()
            settings[SettingsKey.COPY_CLIPBOARD] = self.copy_clipboard_check.isChecked()
            settings[SettingsKey.MINIMIZE_TRAY] = self.minimize_tray_check.isChecked()
            settings[SettingsKey.STREAMING_ENABLED] = self.streaming_enabled_check.isChecked()
            settings[SettingsKey.STREAMING_OVERLAY_FONT_SIZE] = (
                self.streaming_font_size_spinbox.value()
            )
            # Drop legacy keys so streaming_enabled is the single source of truth
            settings.pop(SettingsKey.STREAMING_OVERLAY_ENABLED, None)
            settings.pop(SettingsKey.STREAMING_PASTE_ENABLED, None)
            settings.pop("streaming_tiny_model_enabled", None)
            settings.pop("live_typing_enabled", None)
            settings[SettingsKey.WHISPER_MODEL] = new_whisper_model
            settings[SettingsKey.WHISPER_DEVICE] = new_device
            settings[SettingsKey.WHISPER_COMPUTE_TYPE] = new_compute
            settings[SettingsKey.HF_ACCESS_POLICY] = new_hf_policy
            # Legacy key superseded by hf_access_policy
            settings.pop(SettingsKey.HF_HUB_OFFLINE, None)
            settings[SettingsKey.RECORDING_RETENTION_MODE] = (
                self.recording_retention_combo.currentData()
            )
            settings[SettingsKey.MAX_SAVED_RECORDINGS] = self.max_recordings_spinbox.value()

            # Save audio input device (None for system default)
            if new_audio_device is None:
                settings.pop(SettingsKey.AUDIO_INPUT_DEVICE, None)
            else:
                settings[SettingsKey.AUDIO_INPUT_DEVICE] = new_audio_device

            # Save to file (policy is read live at each model request, so the
            # new hf_access_policy takes effect immediately)
            settings_manager.save_all_settings(settings)

            # Apply retention limit immediately (may delete oldest files if lowered)
            history_manager.set_max_recordings(resolve_max_saved_recordings(settings))

            logger.info("Settings saved successfully")

            # Call callback if set
            if self.on_settings_save:
                self.on_settings_save(settings)

            # Emit signal with change flags
            settings['_whisper_settings_changed'] = whisper_settings_changed
            settings['_audio_device_changed'] = audio_device_changed
            settings['_streaming_settings_changed'] = streaming_settings_changed
            settings['_hf_policy_changed'] = hf_policy_changed
            self.settings_changed.emit(settings)

            self.accept()
        except Exception as e:
            logger.error(f"Failed to save settings: {e}")
            self.reject()

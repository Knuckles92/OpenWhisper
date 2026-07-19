"""
Settings dialog for PyQt6 UI.
Tabbed interface for managing application settings.
"""
import logging
import os
import tempfile
import threading
from typing import Optional, Callable
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTabWidget,
    QWidget, QLabel, QCheckBox,
    QSlider, QFrame, QScrollArea, QTextEdit,
    QLineEdit, QListWidget,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont

from config import config
from services.settings import (
    HuggingFaceAccessPolicy,
    RecordingRetentionMode,
    SettingsKey,
    TranscriptCleanupModelSort,
    TranscriptCleanupProvider,
    TranscriptCleanupReasoning,
    default_transcript_cleanup_model,
    resolve_max_saved_recordings,
    resolve_streaming_overlay_font_size,
    resolve_transcript_cleanup_model,
    resolve_transcript_cleanup_model_sort,
    resolve_transcript_cleanup_prompt,
    resolve_transcript_cleanup_provider,
    resolve_transcript_cleanup_reasoning,
    resolve_transcript_cleanup_rules,
    settings_manager,
)
from services.history_manager import history_manager
from services.recorder import AudioRecorder
from ui_qt.dialogs.cleanup_prompt_dialog import CleanupPromptDialog
from ui_qt.dialogs.cleanup_rule_dialog import CleanupRuleDialog
from ui_qt.widgets import (
    NoWheelComboBox, NoWheelSpinBox, PrimaryButton, Button, SearchableComboBox,
)

logger = logging.getLogger(__name__)


class SettingsDialog(QDialog):
    """Settings dialog with tabbed interface."""

    settings_changed = pyqtSignal(dict)

    #: Internal: emitted from the model-list worker thread
    #: (provider, sort, models, error).
    _cleanup_models_loaded = pyqtSignal(str, str, list, str)

    #: Internal: emitted from the rule-polish worker thread
    #: (raw instruction, polished rule, error).
    _cleanup_rule_polished = pyqtSignal(str, str, str)

    #: Internal: emitted from the rule-dictation worker thread (text, error).
    _rule_dictation_finished = pyqtSignal(str, str)

    def __init__(self, parent=None):
        """Initialize settings dialog."""
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumSize(600, 500)
        self.setMaximumWidth(800)

        # Callbacks
        self.on_settings_save: Optional[Callable] = None
        # Transcribes a short dictated clip; wired by UIController.
        self.on_dictation_transcribe: Optional[Callable[[str], str]] = None

        # Learned-rules worker state (AI polish + dictation)
        self._rule_polishing = False
        self._rule_dictation_state = "idle"  # idle | recording | transcribing
        self._rule_recorder: Optional[AudioRecorder] = None
        self._rule_recorder_device: Optional[int] = None
        self._rule_dictation_path = os.path.join(
            tempfile.gettempdir(), "openwhisper_rule_dictation.wav"
        )
        self._rule_dictation_timer = QTimer(self)
        self._rule_dictation_timer.setSingleShot(True)
        self._rule_dictation_timer.setInterval(60_000)
        self._rule_dictation_timer.timeout.connect(self._stop_rule_dictation)

        # Live-loaded cleanup model lists, keyed by (provider, sort).
        self._cleanup_models_cache: dict = {}
        self._cleanup_models_loading = False
        # Model text remembered per provider so switching providers never
        # carries one provider's model id over to the other.
        self._cleanup_model_by_provider: dict = {}
        self._active_cleanup_provider: str = ""

        self._setup_ui()
        self._load_settings()

        self._cleanup_models_loaded.connect(self._on_cleanup_models_loaded)
        self._cleanup_rule_polished.connect(self._on_cleanup_rule_polished)
        self._rule_dictation_finished.connect(self._on_rule_dictation_finished)
        self.finished.connect(self._release_rule_recorder)
        self.tabs.currentChanged.connect(self._on_tab_changed)

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
        self._create_cleanup_tab()
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

        self.recording_retention_combo = NoWheelComboBox()
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

        self.max_recordings_spinbox = NoWheelSpinBox()
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

        self.streaming_font_size_spinbox = NoWheelSpinBox()
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

        self.sample_rate_combo = NoWheelComboBox()
        self.sample_rate_combo.addItems(["16000", "22050", "44100", "48000"])
        self.sample_rate_combo.setMinimumHeight(36)
        layout.addWidget(self.sample_rate_combo)

        # Channels
        layout.addSpacing(12)
        channels_label = QLabel("Channels:")
        layout.addWidget(channels_label)

        self.channels_combo = NoWheelComboBox()
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

        self.audio_device_combo = NoWheelComboBox()
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

    def _create_cleanup_tab(self):
        """Create AI transcript cleanup (post-processing) settings tab.

        Split into subtabs so the growing cleanup feature set stays
        scannable: General holds provider/model/prompt settings, Learned
        Rules holds the rule-teaching UI.
        """
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(0, 0, 0, 0)
        tab_layout.setSpacing(0)

        subtabs = QTabWidget()
        subtabs.setObjectName("cleanupSubTabs")
        subtabs.addTab(self._create_cleanup_general_subtab(), "General")
        subtabs.addTab(self._create_cleanup_rules_subtab(), "Learned Rules")
        tab_layout.addWidget(subtabs)

        self._cleanup_tab_index = self.tabs.addTab(tab, "Cleanup")

    def _cleanup_subtab_scaffold(self):
        """Create the scrollable scaffold shared by Cleanup subtabs.

        Returns:
            Tuple of (scroll_area, content_layout). The scroll area is the
            widget to hand to addTab; the layout receives the content.
        """
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        scroll_area.setWidget(content)
        return scroll_area, layout

    def _create_cleanup_general_subtab(self):
        """Build the General cleanup subtab (provider, model, prompt)."""
        scroll_area, layout = self._cleanup_subtab_scaffold()

        # Title
        title = QLabel("AI Transcript Cleanup")
        title.setObjectName("headerLabel")
        layout.addWidget(title)

        layout.addSpacing(12)
        self.transcript_cleanup_check = QCheckBox(
            "Clean up transcript with AI after transcription"
        )
        self.transcript_cleanup_check.toggled.connect(self._update_cleanup_prompt_ui)
        layout.addWidget(self.transcript_cleanup_check)

        # Provider selection
        layout.addSpacing(8)
        self.cleanup_provider_label = QLabel("Provider:")
        layout.addWidget(self.cleanup_provider_label)

        self.cleanup_provider_combo = NoWheelComboBox()
        self.cleanup_provider_combo.addItem("OpenAI", TranscriptCleanupProvider.OPENAI)
        self.cleanup_provider_combo.addItem(
            "OpenRouter", TranscriptCleanupProvider.OPENROUTER
        )
        self.cleanup_provider_combo.setMinimumHeight(36)
        self.cleanup_provider_combo.currentIndexChanged.connect(
            self._on_cleanup_provider_changed
        )
        layout.addWidget(self.cleanup_provider_combo)

        # Model-list sort order (OpenRouter supports server-side ranking;
        # OpenAI's models endpoint does not, so this row hides for OpenAI).
        sort_row = QHBoxLayout()
        sort_row.setSpacing(8)
        self.cleanup_model_sort_label = QLabel("Sort models by:")
        sort_row.addWidget(self.cleanup_model_sort_label)

        self.cleanup_model_sort_combo = NoWheelComboBox()
        self.cleanup_model_sort_combo.addItem(
            "A → Z", TranscriptCleanupModelSort.ALPHABETICAL
        )
        self.cleanup_model_sort_combo.addItem(
            "Most popular", TranscriptCleanupModelSort.MOST_POPULAR
        )
        self.cleanup_model_sort_combo.addItem(
            "Top this week", TranscriptCleanupModelSort.TOP_WEEKLY
        )
        self.cleanup_model_sort_combo.addItem(
            "Newest", TranscriptCleanupModelSort.NEWEST
        )
        self.cleanup_model_sort_combo.addItem(
            "Cheapest first", TranscriptCleanupModelSort.PRICING_LOW_TO_HIGH
        )
        self.cleanup_model_sort_combo.addItem(
            "Priciest first", TranscriptCleanupModelSort.PRICING_HIGH_TO_LOW
        )
        self.cleanup_model_sort_combo.addItem(
            "Largest context", TranscriptCleanupModelSort.CONTEXT_HIGH_TO_LOW
        )
        self.cleanup_model_sort_combo.addItem(
            "Highest throughput",
            TranscriptCleanupModelSort.THROUGHPUT_HIGH_TO_LOW,
        )
        self.cleanup_model_sort_combo.addItem(
            "Lowest latency", TranscriptCleanupModelSort.LATENCY_LOW_TO_HIGH
        )
        self.cleanup_model_sort_combo.setMinimumHeight(36)
        self.cleanup_model_sort_combo.setToolTip(
            "How the model dropdown is ordered (OpenRouter server-side ranking)"
        )
        self.cleanup_model_sort_combo.currentIndexChanged.connect(
            self._on_cleanup_sort_changed
        )
        sort_row.addWidget(self.cleanup_model_sort_combo, stretch=1)
        layout.addLayout(sort_row)

        # Model selection (live-loaded from the provider's API)
        self.cleanup_model_label = QLabel("Model:")
        layout.addWidget(self.cleanup_model_label)

        model_row = QHBoxLayout()
        model_row.setSpacing(8)
        # Type-to-search: typing narrows the combo's own dropdown
        # (case-insensitive substring match on the live-loaded list).
        self.cleanup_model_combo = SearchableComboBox()
        self.cleanup_model_combo.setMinimumHeight(36)
        model_row.addWidget(self.cleanup_model_combo, stretch=1)

        self.cleanup_model_refresh_btn = Button("Refresh")
        self.cleanup_model_refresh_btn.setToolTip(
            "Reload the model list from the provider's API"
        )
        self.cleanup_model_refresh_btn.clicked.connect(
            lambda: self._fetch_cleanup_models(force=True)
        )
        model_row.addWidget(self.cleanup_model_refresh_btn)
        layout.addLayout(model_row)

        self.cleanup_models_status = QLabel("")
        self.cleanup_models_status.setObjectName("infoLabel")
        self.cleanup_models_status.setWordWrap(True)
        layout.addWidget(self.cleanup_models_status)

        # Thinking / reasoning effort
        self.cleanup_reasoning_label = QLabel("Thinking level:")
        layout.addWidget(self.cleanup_reasoning_label)

        self.cleanup_reasoning_combo = NoWheelComboBox()
        self.cleanup_reasoning_combo.addItem("Off", TranscriptCleanupReasoning.OFF)
        self.cleanup_reasoning_combo.addItem("Low", TranscriptCleanupReasoning.LOW)
        self.cleanup_reasoning_combo.addItem(
            "Medium", TranscriptCleanupReasoning.MEDIUM
        )
        self.cleanup_reasoning_combo.addItem("High", TranscriptCleanupReasoning.HIGH)
        self.cleanup_reasoning_combo.setMinimumHeight(36)
        layout.addWidget(self.cleanup_reasoning_combo)

        reasoning_info = QLabel(
            "Requests extra thinking effort from reasoning models (e.g. o4-mini). "
            "Leave Off for regular chat models."
        )
        reasoning_info.setObjectName("infoLabel")
        reasoning_info.setWordWrap(True)
        self.cleanup_reasoning_info = reasoning_info
        layout.addWidget(reasoning_info)

        layout.addSpacing(8)
        self.cleanup_prompt_label = QLabel("Cleanup prompt:")
        layout.addWidget(self.cleanup_prompt_label)

        self.cleanup_prompt_edit = QTextEdit()
        self.cleanup_prompt_edit.setAcceptRichText(False)
        self.cleanup_prompt_edit.setFont(QFont("Segoe UI", 11))
        self.cleanup_prompt_edit.setMinimumHeight(140)
        self.cleanup_prompt_edit.setPlaceholderText(
            "Instructions for how the AI should clean up transcripts…"
        )
        layout.addWidget(self.cleanup_prompt_edit)

        cleanup_btn_row = QHBoxLayout()
        cleanup_btn_row.setSpacing(8)
        self.cleanup_prompt_edit_btn = Button("Open editor…")
        self.cleanup_prompt_edit_btn.clicked.connect(self._open_cleanup_prompt_editor)
        cleanup_btn_row.addWidget(self.cleanup_prompt_edit_btn)

        self.cleanup_prompt_reset_btn = Button("Reset to default")
        self.cleanup_prompt_reset_btn.clicked.connect(self._reset_cleanup_prompt)
        cleanup_btn_row.addWidget(self.cleanup_prompt_reset_btn)
        cleanup_btn_row.addStretch()
        layout.addLayout(cleanup_btn_row)

        cleanup_info = QLabel(
            "Runs the selected chat model on each transcript after transcription. "
            "OpenAI needs OPENAI_API_KEY; OpenRouter needs OPENROUTER_API_KEY "
            "(environment or .env). Edit the prompt to change cleanup style "
            "(e.g. bullets, email tone)."
        )
        cleanup_info.setObjectName("infoLabel")
        cleanup_info.setWordWrap(True)
        self.cleanup_prompt_info = cleanup_info
        layout.addWidget(cleanup_info)

        layout.addStretch()
        return scroll_area

    def _create_cleanup_rules_subtab(self):
        """Build the Learned Rules cleanup subtab (rule teaching UI)."""
        scroll_area, layout = self._cleanup_subtab_scaffold()

        # Title
        title = QLabel("Learned Rules")
        title.setObjectName("headerLabel")
        layout.addWidget(title)

        # Learned rules: user-taught behaviors appended to the base prompt
        self.cleanup_rules_info = QLabel(
            "Teach the cleanup AI new behaviors — how to spell names, expand "
            "acronyms, or format text. Rules are added to the cleanup prompt "
            "on every transcript."
        )
        self.cleanup_rules_info.setObjectName("infoLabel")
        self.cleanup_rules_info.setWordWrap(True)
        layout.addWidget(self.cleanup_rules_info)

        rule_input_row = QHBoxLayout()
        rule_input_row.setSpacing(8)
        self.cleanup_rule_input = QLineEdit()
        self.cleanup_rule_input.setMinimumHeight(36)
        self.cleanup_rule_input.setPlaceholderText(
            'e.g. Always spell my name "Alex Rivera"'
        )
        self.cleanup_rule_input.returnPressed.connect(self._add_cleanup_rule)
        rule_input_row.addWidget(self.cleanup_rule_input, stretch=1)

        self.cleanup_rule_mic_btn = Button("Dictate")
        self.cleanup_rule_mic_btn.setToolTip(
            "Speak the instruction instead of typing it"
        )
        self.cleanup_rule_mic_btn.clicked.connect(self._toggle_rule_dictation)
        rule_input_row.addWidget(self.cleanup_rule_mic_btn)

        self.cleanup_rule_add_btn = Button("Add Rule")
        self.cleanup_rule_add_btn.clicked.connect(self._add_cleanup_rule)
        rule_input_row.addWidget(self.cleanup_rule_add_btn)
        layout.addLayout(rule_input_row)

        self.cleanup_rule_status = QLabel("")
        self.cleanup_rule_status.setObjectName("infoLabel")
        self.cleanup_rule_status.setWordWrap(True)
        layout.addWidget(self.cleanup_rule_status)

        self.cleanup_rules_label = QLabel("Learned rules:")
        layout.addWidget(self.cleanup_rules_label)

        self.cleanup_rules_list = QListWidget()
        self.cleanup_rules_list.setWordWrap(True)
        self.cleanup_rules_list.setMinimumHeight(120)
        self.cleanup_rules_list.itemSelectionChanged.connect(
            self._update_cleanup_rule_controls
        )
        self.cleanup_rules_list.itemDoubleClicked.connect(
            lambda _item: self._edit_cleanup_rule()
        )
        layout.addWidget(self.cleanup_rules_list)

        rule_btn_row = QHBoxLayout()
        rule_btn_row.setSpacing(8)
        self.cleanup_rule_edit_btn = Button("Edit…")
        self.cleanup_rule_edit_btn.clicked.connect(self._edit_cleanup_rule)
        rule_btn_row.addWidget(self.cleanup_rule_edit_btn)

        self.cleanup_rule_delete_btn = Button("Delete")
        self.cleanup_rule_delete_btn.clicked.connect(self._delete_cleanup_rule)
        rule_btn_row.addWidget(self.cleanup_rule_delete_btn)
        rule_btn_row.addStretch()
        layout.addLayout(rule_btn_row)

        layout.addStretch()
        return scroll_area

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

        # Local engine knobs (model / device / quant) live in the main
        # window's Engine Settings panel and the Model Manager, not here.

        # Max file size
        layout.addSpacing(12)
        max_size_label = QLabel("Maximum File Size (MB):")
        layout.addWidget(max_size_label)

        self.max_size_spinbox = NoWheelSpinBox()
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

        self.hf_policy_combo = NoWheelComboBox()
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

    def _update_cleanup_prompt_ui(self):
        """Enable cleanup controls when AI cleanup is on."""
        enabled = self.transcript_cleanup_check.isChecked()
        for widget in (
            self.cleanup_provider_label,
            self.cleanup_provider_combo,
            self.cleanup_model_sort_label,
            self.cleanup_model_sort_combo,
            self.cleanup_model_label,
            self.cleanup_model_combo,
            self.cleanup_model_refresh_btn,
            self.cleanup_models_status,
            self.cleanup_reasoning_label,
            self.cleanup_reasoning_combo,
            self.cleanup_reasoning_info,
            self.cleanup_prompt_label,
            self.cleanup_prompt_edit,
            self.cleanup_prompt_edit_btn,
            self.cleanup_prompt_reset_btn,
            self.cleanup_prompt_info,
            self.cleanup_rules_label,
            self.cleanup_rules_info,
            self.cleanup_rule_status,
            self.cleanup_rules_list,
        ):
            widget.setEnabled(enabled)
        if self._cleanup_models_loading:
            self.cleanup_model_refresh_btn.setEnabled(False)
        self._update_cleanup_rule_controls()

    # ── Cleanup model live loading ─────────────────────────────────

    def _current_cleanup_provider(self) -> str:
        """Return the provider currently selected in the Cleanup tab."""
        return (
            self.cleanup_provider_combo.currentData()
            or TranscriptCleanupProvider.OPENAI
        )

    def _current_cleanup_sort(self) -> str:
        """Return the model-list sort for the current provider selection.

        OpenAI's models endpoint has no server-side sort, so anything but
        OpenRouter always resolves to alphabetical.
        """
        if (
            self._current_cleanup_provider()
            != TranscriptCleanupProvider.OPENROUTER
        ):
            return TranscriptCleanupModelSort.ALPHABETICAL
        return (
            self.cleanup_model_sort_combo.currentData()
            or TranscriptCleanupModelSort.ALPHABETICAL
        )

    def _update_cleanup_sort_visibility(self):
        """Show the sort selector only for OpenRouter."""
        is_openrouter = (
            self._current_cleanup_provider()
            == TranscriptCleanupProvider.OPENROUTER
        )
        self.cleanup_model_sort_label.setVisible(is_openrouter)
        self.cleanup_model_sort_combo.setVisible(is_openrouter)

    def _on_tab_changed(self, index: int):
        """Lazily fetch the model list the first time the Cleanup tab opens."""
        if index != self._cleanup_tab_index:
            return
        provider = self._current_cleanup_provider()
        sort = self._current_cleanup_sort()
        if (provider, sort) not in self._cleanup_models_cache:
            self._fetch_cleanup_models()

    def _on_cleanup_provider_changed(self):
        """Swap to the new provider's remembered model and reload the list."""
        provider = self._current_cleanup_provider()
        self._update_cleanup_sort_visibility()
        # Remember the outgoing provider's model and restore the incoming
        # provider's last one — model ids are provider-specific, so text
        # must never carry over between providers.
        current = self.cleanup_model_combo.currentText().strip()
        if self._active_cleanup_provider and current:
            self._cleanup_model_by_provider[self._active_cleanup_provider] = current
        self._active_cleanup_provider = provider
        self.cleanup_model_combo.setCurrentText(
            self._cleanup_model_by_provider.get(provider)
            or default_transcript_cleanup_model(provider)
        )
        # Drop the previous provider's items immediately so a slow or failed
        # fetch never leaves the other provider's model list in the combo.
        self._populate_cleanup_models(provider, self._current_cleanup_sort())
        self._fetch_cleanup_models()

    def _on_cleanup_sort_changed(self):
        """Reload the model list in the newly selected order."""
        self._fetch_cleanup_models()

    def _fetch_cleanup_models(self, force: bool = False):
        """Load the provider's model list in a background thread.

        Args:
            force: Bypass the in-dialog cache and hit the API again.
        """
        provider = self._current_cleanup_provider()
        sort = self._current_cleanup_sort()
        if not force and (provider, sort) in self._cleanup_models_cache:
            self.cleanup_models_status.setText(
                f"{len(self._cleanup_models_cache[(provider, sort)])} "
                "models available"
            )
            self._populate_cleanup_models(provider, sort)
            return
        if self._cleanup_models_loading:
            return

        self._cleanup_models_loading = True
        self.cleanup_model_refresh_btn.setEnabled(False)
        self.cleanup_models_status.setText("Loading models…")

        def worker():
            try:
                from services.transcript_cleanup import list_cleanup_models

                models = list_cleanup_models(provider, sort=sort)
                error = ""
            except Exception as exc:
                models = []
                error = str(exc)
            try:
                self._cleanup_models_loaded.emit(provider, sort, models, error)
            except RuntimeError:
                pass  # Dialog was closed before the fetch finished.

        threading.Thread(
            target=worker, name="cleanup-models-fetch", daemon=True
        ).start()

    def _on_cleanup_models_loaded(
        self, provider: str, sort: str, models: list, error: str
    ):
        """Apply a finished model-list fetch on the main thread."""
        self._cleanup_models_loading = False
        self.cleanup_model_refresh_btn.setEnabled(
            self.transcript_cleanup_check.isChecked()
        )
        if not error:
            self._cleanup_models_cache[(provider, sort)] = models
        if (provider, sort) != (
            self._current_cleanup_provider(),
            self._current_cleanup_sort(),
        ):
            # Selection changed mid-fetch; the switch's own fetch was skipped
            # by the loading guard, so start it now for the current selection.
            self._fetch_cleanup_models()
            return
        if error:
            self.cleanup_models_status.setText(f"Couldn't load models: {error}")
            return
        self.cleanup_models_status.setText(f"{len(models)} models available")
        self._populate_cleanup_models(provider, sort)

    def _populate_cleanup_models(self, provider: str, sort: str):
        """Fill the model combo from the cache, preserving the current text."""
        models = self._cleanup_models_cache.get((provider, sort), [])
        current = self.cleanup_model_combo.currentText().strip()
        self.cleanup_model_combo.blockSignals(True)
        self.cleanup_model_combo.clear()
        self.cleanup_model_combo.addItems(models)
        self.cleanup_model_combo.setCurrentText(
            current or default_transcript_cleanup_model(provider)
        )
        self.cleanup_model_combo.blockSignals(False)

    def _open_cleanup_prompt_editor(self):
        """Open a larger popup editor for the cleanup prompt."""
        dialog = CleanupPromptDialog(self.cleanup_prompt_edit.toPlainText(), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.cleanup_prompt_edit.setPlainText(dialog.prompt_text())

    def _reset_cleanup_prompt(self):
        """Restore the built-in default cleanup prompt."""
        self.cleanup_prompt_edit.setPlainText(config.TRANSCRIPT_CLEANUP_PROMPT)

    # ── Learned rules ──────────────────────────────────────────────

    def _staged_cleanup_rules(self) -> list:
        """Return the rules currently staged in the list widget."""
        return [
            self.cleanup_rules_list.item(i).text()
            for i in range(self.cleanup_rules_list.count())
        ]

    def _update_cleanup_rule_controls(self):
        """Gate rule controls on the master toggle and worker activity."""
        enabled = self.transcript_cleanup_check.isChecked()
        busy = self._rule_polishing or self._rule_dictation_state != "idle"
        self.cleanup_rule_input.setEnabled(enabled and not busy)
        self.cleanup_rule_add_btn.setEnabled(enabled and not busy)
        # While recording, the mic button is the Stop control and must stay
        # enabled; it only locks during polish and transcription.
        self.cleanup_rule_mic_btn.setEnabled(
            enabled
            and not self._rule_polishing
            and self._rule_dictation_state != "transcribing"
        )
        has_selection = bool(self.cleanup_rules_list.selectedItems())
        self.cleanup_rule_edit_btn.setEnabled(enabled and has_selection)
        self.cleanup_rule_delete_btn.setEnabled(enabled and has_selection)

    def _add_cleanup_rule(self):
        """Polish the typed instruction with AI, then confirm and stage it."""
        self._polish_cleanup_rule(self.cleanup_rule_input.text())

    def _polish_cleanup_rule(self, raw: str):
        """Polish an instruction with AI, then confirm and stage it.

        Args:
            raw: Raw instruction text, typed or dictated.
        """
        raw = raw.strip()
        if (
            not raw
            or self._rule_polishing
            or self._rule_dictation_state != "idle"
        ):
            return
        staged = {r.casefold() for r in self._staged_cleanup_rules()}
        if raw.casefold() in staged:
            self.cleanup_rule_status.setText("That rule already exists.")
            return
        if self.cleanup_rules_list.count() >= config.MAX_TRANSCRIPT_CLEANUP_RULES:
            self.cleanup_rule_status.setText(
                f"Rule limit reached ({config.MAX_TRANSCRIPT_CLEANUP_RULES})."
            )
            return

        self._rule_polishing = True
        self.cleanup_rule_status.setText("Polishing rule with AI…")
        self._update_cleanup_rule_controls()

        # Use the dialog's current (possibly unsaved) selections so polish
        # matches what the user sees in the tab.
        provider = self._current_cleanup_provider()
        model = self.cleanup_model_combo.currentText().strip() or None
        reasoning = self.cleanup_reasoning_combo.currentData()

        def worker():
            try:
                from services.transcript_cleanup import polish_cleanup_rule

                polished, error = polish_cleanup_rule(
                    raw, provider=provider, model=model, reasoning=reasoning
                )
            except Exception as exc:
                polished, error = raw, str(exc)
            try:
                self._cleanup_rule_polished.emit(raw, polished or raw, error or "")
            except RuntimeError:
                pass  # Dialog was closed before the polish finished.

        threading.Thread(
            target=worker, name="cleanup-rule-polish", daemon=True
        ).start()

    def _on_cleanup_rule_polished(self, raw: str, polished: str, error: str):
        """Confirm a finished polish on the main thread and stage the rule."""
        self._rule_polishing = False
        self.cleanup_rule_status.setText("")
        self._update_cleanup_rule_controls()

        notice = (
            "AI polish unavailable — your wording will be saved as written."
            if error
            else None
        )
        dialog = CleanupRuleDialog(polished, original=raw, notice=notice, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        rule = dialog.rule_text()
        if not rule:
            return
        staged = {r.casefold() for r in self._staged_cleanup_rules()}
        if rule.casefold() in staged:
            self.cleanup_rule_status.setText("That rule already exists.")
            return
        self.cleanup_rules_list.addItem(rule)
        self.cleanup_rule_input.clear()

    def _edit_cleanup_rule(self):
        """Open the rule editor for the selected rule."""
        items = self.cleanup_rules_list.selectedItems()
        if not items:
            return
        item = items[0]
        dialog = CleanupRuleDialog(item.text(), parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        rule = dialog.rule_text()
        if rule:
            item.setText(rule)

    def _delete_cleanup_rule(self):
        """Remove the selected rules from the staged list."""
        for item in self.cleanup_rules_list.selectedItems():
            self.cleanup_rules_list.takeItem(self.cleanup_rules_list.row(item))
        self._update_cleanup_rule_controls()

    # ── Rule dictation ─────────────────────────────────────────────

    def _toggle_rule_dictation(self):
        """Start or stop dictating a rule instruction."""
        if self._rule_dictation_state == "recording":
            self._stop_rule_dictation()
            return
        if self._rule_dictation_state != "idle" or self._rule_polishing:
            return
        if self.on_dictation_transcribe is None:
            self.cleanup_rule_status.setText("Dictation is unavailable.")
            return

        # Own a private recorder writing to a temp file so dictation never
        # touches the main flow's recording, even if a hotkey recording is
        # running at the same time.
        device_id = self.audio_device_combo.currentData()
        if self._rule_recorder is None or self._rule_recorder_device != device_id:
            if self._rule_recorder is not None:
                self._rule_recorder.cleanup()
            self._rule_recorder = AudioRecorder(
                device_id=device_id, output_file=self._rule_dictation_path
            )
            self._rule_recorder_device = device_id

        if not self._rule_recorder.start_recording():
            self.cleanup_rule_status.setText("Couldn't start recording.")
            return
        self._rule_dictation_state = "recording"
        self.cleanup_rule_mic_btn.setText("Stop")
        self.cleanup_rule_status.setText("Recording… click Stop when done.")
        self._rule_dictation_timer.start()
        self._update_cleanup_rule_controls()

    def _stop_rule_dictation(self):
        """Stop the dictation recording and transcribe it on a worker thread."""
        if self._rule_dictation_state != "recording":
            return
        self._rule_dictation_timer.stop()
        self._rule_dictation_state = "transcribing"
        self.cleanup_rule_mic_btn.setText("Transcribing…")
        self.cleanup_rule_status.setText("Transcribing dictation…")
        self._update_cleanup_rule_controls()

        recorder = self._rule_recorder
        transcribe = self.on_dictation_transcribe
        audio_path = self._rule_dictation_path

        def worker():
            text = ""
            error = ""
            try:
                recorder.stop_recording()
                recorder.wait_for_stop_completion()
                if not recorder.has_recording_data():
                    error = "No audio was captured."
                elif not recorder.save_recording(audio_path):
                    error = "Couldn't save the dictation audio."
                else:
                    text = transcribe(audio_path) or ""
                    if not text.strip():
                        error = "Nothing was transcribed."
            except Exception as exc:
                error = str(exc) or "Transcription failed."
            finally:
                recorder.clear_recording_data()
                try:
                    if os.path.exists(audio_path):
                        os.remove(audio_path)
                except OSError:
                    pass
            try:
                self._rule_dictation_finished.emit(text.strip(), error)
            except RuntimeError:
                pass  # Dialog was closed before dictation finished.

        threading.Thread(
            target=worker, name="rule-dictation", daemon=True
        ).start()

    def _on_rule_dictation_finished(self, text: str, error: str):
        """Apply a finished dictation on the main thread."""
        self._rule_dictation_state = "idle"
        self.cleanup_rule_mic_btn.setText("Dictate")
        self._update_cleanup_rule_controls()
        if error:
            self.cleanup_rule_status.setText(error)
            return
        # Skip the input box: polish the dictation and open the confirm
        # popup right away. Any text already typed is folded in, matching
        # the previous append-then-add behavior.
        current = self.cleanup_rule_input.text().strip()
        raw = f"{current} {text}".strip() if current else text
        self._polish_cleanup_rule(raw)

    def _release_rule_recorder(self):
        """Release the dictation recorder when the dialog closes."""
        self._rule_dictation_timer.stop()
        if self._rule_recorder is not None:
            self._rule_recorder.cleanup()
            self._rule_recorder = None

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

            # Load checkboxes
            self.auto_paste_check.setChecked(settings.get(SettingsKey.AUTO_PASTE, True))
            self.copy_clipboard_check.setChecked(settings.get(SettingsKey.COPY_CLIPBOARD, True))
            self.transcript_cleanup_check.setChecked(
                settings.get(
                    SettingsKey.TRANSCRIPT_CLEANUP_ENABLED,
                    config.TRANSCRIPT_CLEANUP_ENABLED,
                )
            )
            self.cleanup_prompt_edit.setPlainText(
                resolve_transcript_cleanup_prompt(settings)
            )
            self.cleanup_rules_list.clear()
            self.cleanup_rules_list.addItems(
                resolve_transcript_cleanup_rules(settings)
            )

            # Cleanup provider / model / thinking level. Signals stay blocked
            # so loading never triggers a network fetch; that happens lazily
            # when the Cleanup tab is opened.
            saved_provider = resolve_transcript_cleanup_provider(settings)
            provider_index = self.cleanup_provider_combo.findData(saved_provider)
            self.cleanup_provider_combo.blockSignals(True)
            self.cleanup_provider_combo.setCurrentIndex(max(0, provider_index))
            self.cleanup_provider_combo.blockSignals(False)
            sort_index = self.cleanup_model_sort_combo.findData(
                resolve_transcript_cleanup_model_sort(settings)
            )
            self.cleanup_model_sort_combo.blockSignals(True)
            self.cleanup_model_sort_combo.setCurrentIndex(max(0, sort_index))
            self.cleanup_model_sort_combo.blockSignals(False)
            self._update_cleanup_sort_visibility()
            # Only one model is persisted and it belongs to the saved
            # provider; the other provider starts from its default.
            self._cleanup_model_by_provider = {
                provider: default_transcript_cleanup_model(provider)
                for provider in TranscriptCleanupProvider.ALL
            }
            self._cleanup_model_by_provider[saved_provider] = (
                resolve_transcript_cleanup_model(settings)
            )
            self._active_cleanup_provider = saved_provider
            self.cleanup_model_combo.setCurrentText(
                self._cleanup_model_by_provider[saved_provider]
            )
            reasoning_index = self.cleanup_reasoning_combo.findData(
                resolve_transcript_cleanup_reasoning(settings)
            )
            self.cleanup_reasoning_combo.setCurrentIndex(max(0, reasoning_index))

            self._update_cleanup_prompt_ui()
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
            self.transcript_cleanup_check.setChecked(config.TRANSCRIPT_CLEANUP_ENABLED)
            self.cleanup_prompt_edit.setPlainText(config.TRANSCRIPT_CLEANUP_PROMPT)
            self.cleanup_rules_list.clear()
            self.cleanup_provider_combo.blockSignals(True)
            self.cleanup_provider_combo.setCurrentIndex(0)
            self.cleanup_provider_combo.blockSignals(False)
            self.cleanup_model_sort_combo.blockSignals(True)
            self.cleanup_model_sort_combo.setCurrentIndex(0)
            self.cleanup_model_sort_combo.blockSignals(False)
            self._update_cleanup_sort_visibility()
            self._cleanup_model_by_provider = {
                provider: default_transcript_cleanup_model(provider)
                for provider in TranscriptCleanupProvider.ALL
            }
            self._active_cleanup_provider = self._current_cleanup_provider()
            self.cleanup_model_combo.setCurrentText(config.TRANSCRIPT_CLEANUP_MODEL)
            self.cleanup_reasoning_combo.setCurrentIndex(0)
            self._update_cleanup_prompt_ui()
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
            # Load existing settings. The transcription engine and local
            # whisper model/device/compute are owned by the main-window
            # controls, so their keys pass through untouched.
            settings = settings_manager.load_all_settings()

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
            settings[SettingsKey.AUTO_PASTE] = self.auto_paste_check.isChecked()
            settings[SettingsKey.COPY_CLIPBOARD] = self.copy_clipboard_check.isChecked()
            settings[SettingsKey.TRANSCRIPT_CLEANUP_ENABLED] = (
                self.transcript_cleanup_check.isChecked()
            )
            prompt_text = self.cleanup_prompt_edit.toPlainText().strip()
            if prompt_text and prompt_text != config.TRANSCRIPT_CLEANUP_PROMPT:
                settings[SettingsKey.TRANSCRIPT_CLEANUP_PROMPT] = prompt_text
            else:
                # Store default (or clear custom) so resolve falls back cleanly
                settings[SettingsKey.TRANSCRIPT_CLEANUP_PROMPT] = (
                    prompt_text or config.TRANSCRIPT_CLEANUP_PROMPT
                )
            cleanup_provider = self._current_cleanup_provider()
            settings[SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER] = cleanup_provider
            settings[SettingsKey.TRANSCRIPT_CLEANUP_MODEL] = (
                self.cleanup_model_combo.currentText().strip()
                or resolve_transcript_cleanup_model(
                    {SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: cleanup_provider}
                )
            )
            settings[SettingsKey.TRANSCRIPT_CLEANUP_MODEL_SORT] = (
                self.cleanup_model_sort_combo.currentData()
                or config.TRANSCRIPT_CLEANUP_MODEL_SORT
            )
            settings[SettingsKey.TRANSCRIPT_CLEANUP_REASONING] = (
                self.cleanup_reasoning_combo.currentData()
            )
            settings[SettingsKey.TRANSCRIPT_CLEANUP_RULES] = (
                self._staged_cleanup_rules()
            )
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
            settings['_audio_device_changed'] = audio_device_changed
            settings['_streaming_settings_changed'] = streaming_settings_changed
            settings['_hf_policy_changed'] = hf_policy_changed
            self.settings_changed.emit(settings)

            self.accept()
        except Exception as e:
            logger.error(f"Failed to save settings: {e}")
            self.reject()

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
    QWidget, QLabel, QCheckBox, QPushButton,
    QSlider, QFrame, QScrollArea, QTextEdit,
    QLineEdit, QListWidget, QStackedWidget,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont

from config import config
from services.settings import (
    HuggingFaceAccessPolicy,
    MeetingAgentCore,
    MeetingServerBind,
    RecordingRetentionMode,
    SettingsKey,
    TranscriptCleanupProvider,
    TranscriptCleanupReasoning,
    resolve_max_saved_recordings,
    resolve_meeting_agent_core,
    resolve_meeting_llm_model,
    resolve_meeting_llm_provider,
    resolve_meeting_server_bind,
    resolve_meeting_server_port,
    resolve_meeting_whisper_model,
    resolve_streaming_overlay_font_size,
    resolve_transcript_cleanup_model,
    resolve_transcript_cleanup_prompt,
    resolve_transcript_cleanup_provider,
    resolve_transcript_cleanup_reasoning,
    resolve_transcript_cleanup_rules,
    settings_manager,
)
from services.history_manager import history_manager
from services.components import meeting_agent_payload_dir
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

    #: Internal: emitted from the rule-polish worker thread
    #: (raw instruction, polished rule, error).
    _cleanup_rule_polished = pyqtSignal(str, str, str)

    #: Internal: emitted from the rule-dictation worker thread (text, error).
    _rule_dictation_finished = pyqtSignal(str, str)

    #: Internal: emitted from the meeting model-catalog worker thread
    #: (provider, model ids, error).
    _meeting_models_loaded = pyqtSignal(str, list, str)

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
        # Reports whether Meeting Mode owns the mic; wired by UIController.
        self.get_meeting_active: Optional[Callable[[], bool]] = None
        # Set by the Cleanup → Model Manager link; read after exec() returns.
        self.open_model_manager_on_close = False

        # Meeting intelligence model catalog (provider -> model ids), fetched
        # lazily when the Meeting tab is opened.
        self._meeting_models_cache: dict = {}
        self._meeting_models_loading: set = set()

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

        self._setup_ui()
        self._load_settings()

        self._cleanup_rule_polished.connect(self._on_cleanup_rule_polished)
        self._rule_dictation_finished.connect(self._on_rule_dictation_finished)
        self._meeting_models_loaded.connect(self._on_meeting_models_loaded)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.finished.connect(self._release_rule_recorder)

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
        self._create_meeting_tab()
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
        scannable: General holds behavior/prompt settings, while Learned Rules
        holds the rule-teaching UI. Provider and model selection live in the
        centralized Model Manager.
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
        """Build the General cleanup subtab (behavior and prompt)."""
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

        model_card = QFrame()
        model_card.setObjectName("cleanupModelSummaryCard")
        model_card_layout = QVBoxLayout(model_card)
        model_card_layout.setContentsMargins(16, 14, 16, 14)
        model_card_layout.setSpacing(6)
        model_header = QHBoxLayout()
        model_header.setContentsMargins(0, 0, 0, 0)
        model_header.setSpacing(8)
        model_eyebrow = QLabel("TEXT MODEL")
        model_eyebrow.setObjectName("cleanupModelSummaryEyebrow")
        model_header.addWidget(model_eyebrow)
        model_header.addStretch()
        self.open_model_manager_btn = QPushButton("Open Model Manager…")
        self.open_model_manager_btn.setObjectName("cleanupModelManagerLink")
        self.open_model_manager_btn.setFlat(True)
        self.open_model_manager_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_model_manager_btn.setToolTip(
            "Save settings and open Model Manager → Text to choose the "
            "cleanup provider and chat model"
        )
        self.open_model_manager_btn.clicked.connect(
            self._open_model_manager_from_cleanup
        )
        model_header.addWidget(self.open_model_manager_btn)
        model_card_layout.addLayout(model_header)
        self.cleanup_model_summary = QLabel("")
        self.cleanup_model_summary.setObjectName("cleanupModelSummary")
        self.cleanup_model_summary.setWordWrap(True)
        model_card_layout.addWidget(self.cleanup_model_summary)
        model_hint = QLabel(
            "Provider and model selection live in Model Manager → Text."
        )
        model_hint.setObjectName("cleanupModelSummaryHint")
        model_hint.setWordWrap(True)
        model_card_layout.addWidget(model_hint)
        layout.addWidget(model_card)

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

        hero = QFrame()
        hero.setObjectName("cleanupRulesHero")
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(20, 18, 20, 18)
        hero_layout.setSpacing(16)

        hero_copy = QVBoxLayout()
        hero_copy.setContentsMargins(0, 0, 0, 0)
        hero_copy.setSpacing(5)

        eyebrow = QLabel("PERSONAL CLEANUP PROFILE")
        eyebrow.setObjectName("cleanupRulesEyebrow")
        hero_copy.addWidget(eyebrow)

        title = QLabel("Make every transcript sound like you")
        title.setObjectName("cleanupRulesTitle")
        title.setWordWrap(True)
        hero_copy.addWidget(title)

        self.cleanup_rules_info = QLabel(
            "Teach OpenWhisper your preferred spellings, terminology, and "
            "formatting. These instructions are applied automatically whenever "
            "AI cleanup runs."
        )
        self.cleanup_rules_info.setObjectName("cleanupRulesDescription")
        self.cleanup_rules_info.setWordWrap(True)
        hero_copy.addWidget(self.cleanup_rules_info)
        hero_layout.addLayout(hero_copy, stretch=1)

        hero_mark = QLabel("AI")
        hero_mark.setObjectName("cleanupRulesHeroMark")
        hero_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hero_mark.setFixedSize(52, 52)
        hero_layout.addWidget(hero_mark, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addWidget(hero)

        composer = QFrame()
        composer.setObjectName("cleanupRulesCard")
        composer_layout = QVBoxLayout(composer)
        composer_layout.setContentsMargins(18, 16, 18, 18)
        composer_layout.setSpacing(12)

        composer_title = QLabel("Teach a new rule")
        composer_title.setObjectName("cleanupRulesSectionTitle")
        composer_layout.addWidget(composer_title)

        composer_hint = QLabel(
            "Write a natural instruction. The AI will turn it into a clear, "
            "reusable rule before saving."
        )
        composer_hint.setObjectName("cleanupRulesSectionHint")
        composer_hint.setWordWrap(True)
        composer_layout.addWidget(composer_hint)

        rule_input_row = QHBoxLayout()
        rule_input_row.setSpacing(10)
        self.cleanup_rule_input = QLineEdit()
        self.cleanup_rule_input.setObjectName("cleanupRuleInput")
        self.cleanup_rule_input.setMinimumHeight(44)
        self.cleanup_rule_input.setPlaceholderText(
            'Try: Always spell my name "Alex Rivera"'
        )
        self.cleanup_rule_input.returnPressed.connect(self._add_cleanup_rule)
        rule_input_row.addWidget(self.cleanup_rule_input, stretch=1)

        self.cleanup_rule_mic_btn = Button("Dictate")
        self.cleanup_rule_mic_btn.setObjectName("cleanupRuleDictateButton")
        self.cleanup_rule_mic_btn.set_base_minimum_size(92, 44)
        self.cleanup_rule_mic_btn.setToolTip(
            "Speak the instruction instead of typing it"
        )
        self.cleanup_rule_mic_btn.clicked.connect(self._toggle_rule_dictation)
        rule_input_row.addWidget(self.cleanup_rule_mic_btn)

        self.cleanup_rule_add_btn = PrimaryButton("+ Add rule")
        self.cleanup_rule_add_btn.setObjectName("cleanupRuleAddButton")
        self.cleanup_rule_add_btn.set_base_minimum_size(112, 44)
        self.cleanup_rule_add_btn.clicked.connect(self._add_cleanup_rule)
        rule_input_row.addWidget(self.cleanup_rule_add_btn)
        composer_layout.addLayout(rule_input_row)

        self.cleanup_rule_status = QLabel("")
        self.cleanup_rule_status.setObjectName("cleanupRuleStatus")
        self.cleanup_rule_status.setWordWrap(True)
        composer_layout.addWidget(self.cleanup_rule_status)
        layout.addWidget(composer)

        library = QFrame()
        library.setObjectName("cleanupRulesCard")
        library_layout = QVBoxLayout(library)
        library_layout.setContentsMargins(18, 16, 18, 18)
        library_layout.setSpacing(12)

        library_header = QHBoxLayout()
        library_header.setContentsMargins(0, 0, 0, 0)
        self.cleanup_rules_label = QLabel("Your rule library")
        self.cleanup_rules_label.setObjectName("cleanupRulesSectionTitle")
        library_header.addWidget(self.cleanup_rules_label)
        library_header.addStretch()
        self.cleanup_rules_count = QLabel()
        self.cleanup_rules_count.setObjectName("cleanupRulesCount")
        self.cleanup_rules_count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        library_header.addWidget(self.cleanup_rules_count)
        library_layout.addLayout(library_header)

        self.cleanup_rules_list = QListWidget()
        self.cleanup_rules_list.setObjectName("cleanupRulesList")
        self.cleanup_rules_list.setWordWrap(True)
        self.cleanup_rules_list.setSpacing(6)
        self.cleanup_rules_list.setMinimumHeight(176)
        self.cleanup_rules_list.itemSelectionChanged.connect(
            self._update_cleanup_rule_controls
        )
        self.cleanup_rules_list.itemDoubleClicked.connect(
            lambda _item: self._edit_cleanup_rule()
        )
        library_layout.addWidget(self.cleanup_rules_list)

        self.cleanup_rules_empty = QLabel(
            "No rules yet\n\nAdd your first instruction above to start building "
            "a personal cleanup profile."
        )
        self.cleanup_rules_empty.setObjectName("cleanupRulesEmpty")
        self.cleanup_rules_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cleanup_rules_empty.setWordWrap(True)
        self.cleanup_rules_empty.setMinimumHeight(176)
        library_layout.addWidget(self.cleanup_rules_empty)

        interaction_hint = QLabel("Select a rule or double-click it to edit")
        interaction_hint.setObjectName("cleanupRulesInteractionHint")
        interaction_hint.setWordWrap(True)
        library_layout.addWidget(interaction_hint)

        rule_btn_row = QHBoxLayout()
        rule_btn_row.setSpacing(8)
        rule_btn_row.addStretch()

        self.cleanup_rule_edit_btn = Button("Edit rule")
        self.cleanup_rule_edit_btn.setObjectName("cleanupRuleEditButton")
        self.cleanup_rule_edit_btn.set_base_minimum_size(96, 38)
        self.cleanup_rule_edit_btn.clicked.connect(self._edit_cleanup_rule)
        rule_btn_row.addWidget(self.cleanup_rule_edit_btn)

        self.cleanup_rule_delete_btn = Button("Delete")
        self.cleanup_rule_delete_btn.setObjectName("cleanupRuleDeleteButton")
        self.cleanup_rule_delete_btn.set_base_minimum_size(88, 38)
        self.cleanup_rule_delete_btn.clicked.connect(self._delete_cleanup_rule)
        rule_btn_row.addWidget(self.cleanup_rule_delete_btn)
        library_layout.addLayout(rule_btn_row)
        layout.addWidget(library)

        layout.addStretch()
        return scroll_area

    def _create_meeting_tab(self):
        """Create the Meeting Mode settings tab with scrollable content.

        Covers the meeting-only ASR model, the intelligence provider/model and
        agent core, and the dashboard's network exposure — the settings the
        engine reads through ``resolve_meeting_*()``.
        """
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(0, 0, 0, 0)
        tab_layout.setSpacing(0)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        title = QLabel("Meeting Mode")
        title.setObjectName("headerLabel")
        layout.addWidget(title)

        # Transcription
        layout.addSpacing(12)
        asr_title = QLabel("Transcription")
        asr_title.setObjectName("sectionLabel")
        layout.addWidget(asr_title)

        meeting_model_label = QLabel("Meeting transcription model:")
        layout.addWidget(meeting_model_label)

        self.meeting_model_combo = NoWheelComboBox()
        self.meeting_model_combo.addItems(config.WHISPER_MODEL_CHOICES)
        self.meeting_model_combo.setMinimumHeight(36)
        layout.addWidget(self.meeting_model_combo)

        meeting_model_info = QLabel(
            "Meetings load their own Whisper instance, separate from the "
            "dictation engine. \"auto\" picks turbo on a GPU and base on the "
            "CPU; a large model alongside your dictation model can exhaust "
            "GPU memory."
        )
        meeting_model_info.setObjectName("infoLabel")
        meeting_model_info.setWordWrap(True)
        layout.addWidget(meeting_model_info)

        # Intelligence (provider + chat model + agent core)
        layout.addSpacing(24)
        intelligence_title = QLabel("Intelligence")
        intelligence_title.setObjectName("sectionLabel")
        layout.addWidget(intelligence_title)

        meeting_provider_label = QLabel("Provider:")
        layout.addWidget(meeting_provider_label)

        self.meeting_provider_combo = NoWheelComboBox()
        self.meeting_provider_combo.addItem(
            "OpenAI", TranscriptCleanupProvider.OPENAI
        )
        self.meeting_provider_combo.addItem(
            "OpenRouter", TranscriptCleanupProvider.OPENROUTER
        )
        self.meeting_provider_combo.setMinimumHeight(36)
        self.meeting_provider_combo.currentIndexChanged.connect(
            self._on_meeting_provider_changed
        )
        layout.addWidget(self.meeting_provider_combo)

        meeting_llm_model_label = QLabel("Model:")
        layout.addWidget(meeting_llm_model_label)

        meeting_model_row = QHBoxLayout()
        meeting_model_row.setSpacing(8)
        self.meeting_llm_model_combo = SearchableComboBox()
        self.meeting_llm_model_combo.setObjectName("meetingLlmModelCombo")
        self.meeting_llm_model_combo.setMinimumHeight(36)
        meeting_model_row.addWidget(self.meeting_llm_model_combo, stretch=1)

        self.meeting_models_refresh_btn = Button("Refresh")
        self.meeting_models_refresh_btn.setObjectName("meetingModelsRefreshButton")
        self.meeting_models_refresh_btn.set_base_minimum_size(92, 36)
        self.meeting_models_refresh_btn.setToolTip(
            "Reload the provider's model list"
        )
        self.meeting_models_refresh_btn.clicked.connect(self._refresh_meeting_models)
        meeting_model_row.addWidget(self.meeting_models_refresh_btn)
        layout.addLayout(meeting_model_row)

        self.meeting_models_status = QLabel("")
        self.meeting_models_status.setObjectName("meetingModelsStatus")
        self.meeting_models_status.setWordWrap(True)
        layout.addWidget(self.meeting_models_status)

        meeting_core_label = QLabel("Agent core:")
        layout.addWidget(meeting_core_label)

        self.meeting_agent_core_combo = NoWheelComboBox()
        self._pi_payload_available = meeting_agent_payload_dir() is not None
        pi_label = (
            "Pi (sidecar)" if self._pi_payload_available
            else "Pi (sidecar not built)"
        )
        self.meeting_agent_core_combo.addItem(pi_label, MeetingAgentCore.PI)
        pi_index = self.meeting_agent_core_combo.count() - 1
        model = self.meeting_agent_core_combo.model()
        item = model.item(pi_index) if hasattr(model, "item") else None
        if item is not None:
            item.setEnabled(self._pi_payload_available)
        self.meeting_agent_core_combo.addItem(
            "Direct (no sidecar)", MeetingAgentCore.DIRECT
        )
        self.meeting_agent_core_combo.setMinimumHeight(36)
        layout.addWidget(self.meeting_agent_core_combo)

        meeting_intelligence_info = QLabel(
            "Meeting insights run on the selected chat model. Pi is the "
            "default agent core (sidecar bundle or Meeting Agent component) "
            "and falls back to Direct when no payload is available. OpenAI "
            "needs OPENAI_API_KEY; OpenRouter needs OPENROUTER_API_KEY "
            "(environment or .env). Transcript text and meeting state are "
            "sent to the provider — audio never leaves this computer, and "
            "nothing is sent until you enable cloud intelligence for a "
            "meeting."
        )
        meeting_intelligence_info.setObjectName("infoLabel")
        meeting_intelligence_info.setWordWrap(True)
        layout.addWidget(meeting_intelligence_info)

        # Dashboard access (privacy control)
        layout.addSpacing(24)
        meeting_server_title = QLabel("Dashboard Access")
        meeting_server_title.setObjectName("sectionLabel")
        layout.addWidget(meeting_server_title)

        meeting_bind_label = QLabel("Who can open the meeting dashboard:")
        layout.addWidget(meeting_bind_label)

        self.meeting_bind_combo = NoWheelComboBox()
        self.meeting_bind_combo.setObjectName("meetingBindCombo")
        self.meeting_bind_combo.addItem(
            "Localhost only (this computer)", MeetingServerBind.LOCALHOST
        )
        self.meeting_bind_combo.addItem(
            "Share on local network", MeetingServerBind.LAN
        )
        self.meeting_bind_combo.setMinimumHeight(36)
        self.meeting_bind_combo.currentIndexChanged.connect(
            self._update_meeting_bind_ui
        )
        layout.addWidget(self.meeting_bind_combo)

        self.meeting_bind_warning = QLabel(
            "Sharing on the local network serves the live meeting — running "
            "transcript, notes, insights, and audio playback — over plain, "
            "unencrypted HTTP. Anyone holding the guest link can read and "
            "edit the meeting and play the raw meeting recording."
        )
        self.meeting_bind_warning.setObjectName("meetingBindWarning")
        self.meeting_bind_warning.setWordWrap(True)
        layout.addWidget(self.meeting_bind_warning)

        meeting_port_label = QLabel("Dashboard port:")
        layout.addWidget(meeting_port_label)

        meeting_port_row = QHBoxLayout()
        meeting_port_row.setSpacing(8)
        self.meeting_port_spinbox = NoWheelSpinBox()
        self.meeting_port_spinbox.setMinimum(0)
        self.meeting_port_spinbox.setMaximum(65535)
        self.meeting_port_spinbox.setSpecialValueText("Automatic")
        self.meeting_port_spinbox.setValue(config.MEETING_SERVER_PORT)
        self.meeting_port_spinbox.setMinimumHeight(36)
        meeting_port_row.addWidget(self.meeting_port_spinbox)
        meeting_port_row.addStretch()
        layout.addLayout(meeting_port_row)

        meeting_port_info = QLabel(
            "0 (Automatic) lets the meeting server pick a free port each "
            "session. Pick a fixed port only if you need a stable link."
        )
        meeting_port_info.setObjectName("infoLabel")
        meeting_port_info.setWordWrap(True)
        layout.addWidget(meeting_port_info)

        layout.addStretch()

        scroll_area.setWidget(content)
        tab_layout.addWidget(scroll_area)
        self._meeting_tab_index = self.tabs.addTab(tab, "Meeting")

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

    def _open_model_manager_from_cleanup(self):
        """Save Settings, close it, then open Model Manager → Text.

        Model Manager is non-modal and cannot share an ``exec()`` session with
        Settings — opening it mid-modal stacks behind the main window and
        becomes unresponsive on Windows. The controller opens it after
        ``exec()`` returns when ``open_model_manager_on_close`` is set.
        """
        self.open_model_manager_on_close = True
        self._save_settings()

    def _refresh_cleanup_model_summary(self):
        """Reload the read-only cleanup provider/model line from settings."""
        try:
            settings = settings_manager.load_all_settings()
            saved_provider = resolve_transcript_cleanup_provider(settings)
            saved_model = resolve_transcript_cleanup_model(settings)
        except Exception:
            saved_provider = "openai"
            saved_model = config.TRANSCRIPT_CLEANUP_MODEL
        provider_name = (
            "OpenAI" if saved_provider == "openai" else "OpenRouter"
        )
        self.cleanup_model_summary.setText(f"{provider_name} · {saved_model}")

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
        self._update_cleanup_rule_controls()

    # ── Cleanup prompt ───────────────────────────────────────────────

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
        rule_count = self.cleanup_rules_list.count()
        self.cleanup_rules_count.setText(
            f"{rule_count} / {config.MAX_TRANSCRIPT_CLEANUP_RULES}"
        )
        self.cleanup_rules_list.setVisible(rule_count > 0)
        self.cleanup_rules_empty.setVisible(rule_count == 0)
        self.cleanup_rules_empty.setEnabled(enabled)
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

        # Model ownership lives in Model Manager; use its persisted selection.
        # Reasoning remains staged here so an unsaved thinking-level change is
        # reflected when polishing a learned rule.
        provider = resolve_transcript_cleanup_provider()
        model = resolve_transcript_cleanup_model()
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
        self._update_cleanup_rule_controls()

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
        if self.get_meeting_active is not None and self.get_meeting_active():
            # Exclusive mode: a meeting already owns the microphone and a
            # Whisper instance, so a second capture stream would fight it.
            self.cleanup_rule_status.setText(
                "Meeting Mode is active — end the meeting to dictate a rule."
            )
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

    # ── Meeting Mode ───────────────────────────────────────────────

    def _on_tab_changed(self, index: int):
        """Load the meeting model catalog the first time its tab is shown."""
        if index == getattr(self, "_meeting_tab_index", -1):
            self._fetch_meeting_models(self.meeting_provider_combo.currentData())

    def _on_meeting_provider_changed(self):
        """Load the newly selected provider's catalog into the same picker."""
        self.meeting_llm_model_combo.clear()
        self._fetch_meeting_models(self.meeting_provider_combo.currentData())

    def _refresh_meeting_models(self):
        """Reload the current provider's catalog, bypassing the cache."""
        self._fetch_meeting_models(
            self.meeting_provider_combo.currentData(), force=True
        )

    def _fetch_meeting_models(self, provider: str, force: bool = False):
        """Load one provider's chat-model catalog on a worker thread.

        Uses the same ``list_cleanup_models`` fetcher as the cleanup model
        picker, so provider base URLs and API-key lookup stay in one place.

        Args:
            provider: A ``TranscriptCleanupProvider`` value.
            force: Bypass the in-dialog cache when true.
        """
        if not provider:
            return
        if not force and provider in self._meeting_models_cache:
            self._apply_meeting_models(self._meeting_models_cache[provider])
            return
        if provider in self._meeting_models_loading:
            return

        self._meeting_models_loading.add(provider)
        self.meeting_models_status.setText("Loading models…")

        def worker():
            try:
                from services.transcript_cleanup import list_cleanup_models

                models = list_cleanup_models(provider)
                error = ""
            except Exception as exc:
                models = []
                error = str(exc)
            try:
                self._meeting_models_loaded.emit(provider, models, error)
            except RuntimeError:
                pass  # Dialog was closed before the catalog finished.

        threading.Thread(
            target=worker, name=f"meeting-models-{provider}", daemon=True
        ).start()

    def _on_meeting_models_loaded(self, provider: str, models: list, error: str):
        """Apply a finished catalog fetch on the main thread."""
        self._meeting_models_loading.discard(provider)
        if not error:
            self._meeting_models_cache[provider] = list(models)
        if provider != self.meeting_provider_combo.currentData():
            return
        if error:
            self.meeting_models_status.setText(
                f"Couldn't load models: {error}. Type a model id instead."
            )
            return
        self._apply_meeting_models(models)

    def _apply_meeting_models(self, models: list):
        """Fill the model picker, keeping any model id already selected."""
        current = self.meeting_llm_model_combo.currentText().strip()
        self.meeting_llm_model_combo.clear()
        self.meeting_llm_model_combo.addItems(list(models))
        if current:
            self.meeting_llm_model_combo.setCurrentText(current)
        self.meeting_models_status.setText(f"{len(models)} models available")

    def _update_meeting_bind_ui(self):
        """Show the LAN exposure warning only when sharing is selected."""
        is_lan = self.meeting_bind_combo.currentData() == MeetingServerBind.LAN
        self.meeting_bind_warning.setVisible(is_lan)

    def _load_meeting_settings(self, settings: dict):
        """Apply the stored Meeting Mode settings to their controls.

        Args:
            settings: Loaded settings dict; an empty dict yields the defaults.
        """
        model_index = self.meeting_model_combo.findText(
            resolve_meeting_whisper_model(settings)
        )
        self.meeting_model_combo.setCurrentIndex(max(0, model_index))

        provider_index = self.meeting_provider_combo.findData(
            resolve_meeting_llm_provider(settings)
        )
        # Signals blocked: the catalog is fetched lazily when the tab opens,
        # never while the dialog is still being populated.
        self.meeting_provider_combo.blockSignals(True)
        self.meeting_provider_combo.setCurrentIndex(max(0, provider_index))
        self.meeting_provider_combo.blockSignals(False)
        self.meeting_llm_model_combo.setCurrentText(
            resolve_meeting_llm_model(settings)
        )

        core = resolve_meeting_agent_core(settings)
        if (core == MeetingAgentCore.PI
                and not getattr(self, "_pi_payload_available", False)):
            core = MeetingAgentCore.DIRECT
        core_index = self.meeting_agent_core_combo.findData(core)
        self.meeting_agent_core_combo.setCurrentIndex(max(0, core_index))

        bind_index = self.meeting_bind_combo.findData(
            resolve_meeting_server_bind(settings)
        )
        self.meeting_bind_combo.setCurrentIndex(max(0, bind_index))
        self.meeting_port_spinbox.setValue(resolve_meeting_server_port(settings))
        self._update_meeting_bind_ui()

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

            # Provider/model are read-only here; Model Manager owns selection.
            self._refresh_cleanup_model_summary()
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

            self._load_meeting_settings(settings)

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
            self.cleanup_model_summary.setText(
                f"OpenAI · {config.TRANSCRIPT_CLEANUP_MODEL}"
            )
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
            self._load_meeting_settings({})
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
            # Provider, model, and catalog order pass through untouched because
            # Model Manager is their single owner.
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

            # Meeting Mode. An empty model id is dropped so the resolver falls
            # back to the configured default instead of storing a blank.
            settings[SettingsKey.MEETING_WHISPER_MODEL] = (
                self.meeting_model_combo.currentText()
            )
            settings[SettingsKey.MEETING_LLM_PROVIDER] = (
                self.meeting_provider_combo.currentData()
            )
            meeting_llm_model = self.meeting_llm_model_combo.currentText().strip()
            if meeting_llm_model:
                settings[SettingsKey.MEETING_LLM_MODEL] = meeting_llm_model
            else:
                settings.pop(SettingsKey.MEETING_LLM_MODEL, None)
            settings[SettingsKey.MEETING_AGENT_CORE] = (
                self.meeting_agent_core_combo.currentData()
            )
            settings[SettingsKey.MEETING_SERVER_BIND] = (
                self.meeting_bind_combo.currentData()
            )
            settings[SettingsKey.MEETING_SERVER_PORT] = (
                self.meeting_port_spinbox.value()
            )

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

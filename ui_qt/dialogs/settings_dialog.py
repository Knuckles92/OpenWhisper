import logging
import os
import tempfile
import threading
from typing import Optional, Callable
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTabWidget,
    QWidget, QLabel, QCheckBox, QPushButton,
    QSlider, QFrame, QScrollArea, QTextEdit,
    QLineEdit, QListWidget, QStackedWidget, QSizePolicy,
    QFileDialog, QFormLayout,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont

from config import config
from services.settings import (
    HuggingFaceAccessPolicy,
    MeetingAgentCore,
    MeetingLanguage,
    MeetingServerBind,
    MeetingSpeakerIdBackend,
    RecordingRetentionMode,
    SettingsKey,
    TranscriptCleanupReasoning,
    resolve_max_saved_recordings,
    resolve_meeting_agent_core,
    resolve_meeting_context_folder_enabled,
    resolve_meeting_context_folder_path,
    resolve_meeting_past_recall_enabled,
    resolve_meeting_end_polish,
    resolve_meeting_end_redecode,
    resolve_meeting_end_report,
    resolve_meeting_report_brief,
    resolve_meeting_report_ribbon,
    resolve_meeting_report_signal,
    resolve_meeting_llm_model,
    resolve_meeting_llm_provider,
    resolve_meeting_language,
    resolve_meeting_server_bind,
    resolve_meeting_server_port,
    resolve_meeting_speaker_id_backend,
    resolve_meeting_whisper_model,
    resolve_developer_mode,
    resolve_update_check_enabled,
    resolve_update_notify_enabled,
    resolve_streaming_overlay_font_size,
    resolve_transcript_cleanup_model,
    resolve_transcript_cleanup_prompt,
    resolve_transcript_cleanup_provider,
    resolve_transcript_cleanup_reasoning,
    resolve_transcript_cleanup_rules,
    settings_manager,
)
from services.history_manager import history_manager
from services.recorder import AudioRecorder
from services.text_llm import profile_display_name
from ui_qt.dialogs.cleanup_prompt_dialog import CleanupPromptDialog
from ui_qt.dialogs.cleanup_rule_dialog import CleanupRuleDialog
from ui_qt.widgets import (
    NoWheelComboBox, NoWheelSpinBox, PrimaryButton, Button, WrappedLabel,
)

logger = logging.getLogger(__name__)


class SettingsDialog(QDialog):
    settings_changed = pyqtSignal(dict)

    #: Internal: emitted from the rule-polish worker thread
    #: (raw instruction, polished rule, error).
    _cleanup_rule_polished = pyqtSignal(str, str, str)

    #: Internal: emitted from the rule-dictation worker thread (text, error).
    _rule_dictation_finished = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumSize(600, 500)
        self.setMaximumWidth(800)

        self.on_settings_save: Optional[Callable] = None
        # Transcribes a short dictated clip; wired by UIController.
        self.on_dictation_transcribe: Optional[Callable[[str], str]] = None
        # Reports whether Meeting Mode owns the mic; wired by UIController.
        self.get_meeting_active: Optional[Callable[[], bool]] = None
        # Set by Model Manager links; read after exec() returns ("text" /
        # "meeting"), or None when Settings should not open the manager.
        self.open_model_manager_on_close: Optional[str] = None

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
        self.finished.connect(self._release_rule_recorder)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Tab widget: segmented-button tab styling lives in theme.qss
        # under the #settingsTabs rules.
        self.tabs = QTabWidget()
        self.tabs.setObjectName("settingsTabs")
        self.tabs.tabBar().setCursor(Qt.CursorShape.PointingHandCursor)

        self._create_general_tab()
        self._create_audio_tab()
        self._create_hotkeys_tab()
        self._create_cleanup_tab()
        self._create_meeting_tab()
        self._create_advanced_tab()

        layout.addWidget(self.tabs)

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

    def _add_scrollable_tab(self, inner: QWidget, title: str) -> None:
        """Host a settings page in a scroll area so labels never overlap."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum
        )
        scroll.setWidget(inner)
        self.tabs.addTab(scroll, title)

    def _create_general_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        title = QLabel("General Settings")
        title.setObjectName("headerLabel")
        layout.addWidget(title)

        layout.addSpacing(12)
        self.auto_paste_check = QCheckBox("Auto-paste transcription to active window")
        self.copy_clipboard_check = QCheckBox("Copy transcription to clipboard")
        self.minimize_tray_check = QCheckBox("Minimize to system tray on close")
        self.update_check_check = QCheckBox("Check for updates automatically")
        self.update_check_check.setObjectName("updateCheckEnabledCheck")
        self.update_notify_check = QCheckBox(
            "Notify me when an update is available"
        )
        self.update_notify_check.setObjectName("updateNotifyEnabledCheck")
        for box in (
            self.auto_paste_check,
            self.copy_clipboard_check,
            self.minimize_tray_check,
            self.update_check_check,
            self.update_notify_check,
        ):
            box.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum
            )
        layout.addWidget(self.auto_paste_check)
        layout.addWidget(self.copy_clipboard_check)
        layout.addSpacing(12)
        layout.addWidget(self.minimize_tray_check)

        layout.addSpacing(24)
        updates_label = QLabel("Updates")
        updates_label.setObjectName("sectionLabel")
        layout.addWidget(updates_label)
        layout.addSpacing(8)
        layout.addWidget(self.update_check_check)
        layout.addWidget(self.update_notify_check)
        self.update_check_check.toggled.connect(self._on_update_check_toggled)

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

        self.max_recordings_label = QLabel("Number to keep:")
        self.max_recordings_spinbox = NoWheelSpinBox()
        self.max_recordings_spinbox.setMinimum(1)
        self.max_recordings_spinbox.setMaximum(1000)
        self.max_recordings_spinbox.setValue(config.MAX_SAVED_RECORDINGS)
        self.max_recordings_spinbox.setMinimumHeight(36)
        self.max_recordings_spinbox.setMinimumWidth(110)
        retention_form = QFormLayout()
        retention_form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        retention_form.setFormAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        retention_form.setHorizontalSpacing(16)
        retention_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint
        )
        retention_form.addRow(self.max_recordings_label, self.max_recordings_spinbox)
        layout.addLayout(retention_form)

        retention_info = WrappedLabel(
            "Older audio files are deleted automatically when the limit is exceeded. "
            "Transcription history text is kept separately."
        )
        retention_info.setObjectName("infoLabel")
        layout.addWidget(retention_info)

        layout.addSpacing(24)
        streaming_label = QLabel("Real-Time Transcription (Experimental)")
        streaming_label.setObjectName("sectionLabel")
        layout.addWidget(streaming_label)

        layout.addSpacing(8)
        self.streaming_enabled_check = QCheckBox(
            "Enable real-time transcription preview (while recording)"
        )
        self.streaming_enabled_check.toggled.connect(self._update_streaming_font_ui)
        layout.addWidget(self.streaming_enabled_check)

        self.streaming_font_size_label = QLabel("Preview font size:")
        self.streaming_font_size_spinbox = NoWheelSpinBox()
        self.streaming_font_size_spinbox.setMinimum(10)
        self.streaming_font_size_spinbox.setMaximum(48)
        self.streaming_font_size_spinbox.setSuffix(" pt")
        self.streaming_font_size_spinbox.setValue(config.STREAMING_OVERLAY_FONT_SIZE)
        self.streaming_font_size_spinbox.setMinimumHeight(36)
        self.streaming_font_size_spinbox.setMinimumWidth(110)
        font_form = QFormLayout()
        font_form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        font_form.setFormAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        font_form.setHorizontalSpacing(16)
        font_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint
        )
        font_form.addRow(
            self.streaming_font_size_label, self.streaming_font_size_spinbox
        )
        layout.addLayout(font_form)

        streaming_info = WrappedLabel(
            "Shows transcribed text as you speak on the near-cursor overlay using a dedicated "
            "tiny.en preview model. Requires Local Whisper backend. Final transcription still "
            "uses your selected model and normal auto-paste / clipboard settings."
        )
        streaming_info.setObjectName("infoLabel")
        layout.addWidget(streaming_info)

        self._update_streaming_font_ui()
        layout.addStretch()
        self._add_scrollable_tab(tab, "General")

    def _create_audio_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        title = QLabel("Audio Settings")
        title.setObjectName("headerLabel")
        layout.addWidget(title)

        layout.addSpacing(12)
        sample_rate_label = QLabel("Sample Rate (Hz):")
        layout.addWidget(sample_rate_label)

        self.sample_rate_combo = NoWheelComboBox()
        self.sample_rate_combo.addItems(["16000", "22050", "44100", "48000"])
        self.sample_rate_combo.setMinimumHeight(36)
        layout.addWidget(self.sample_rate_combo)

        layout.addSpacing(12)
        channels_label = QLabel("Channels:")
        layout.addWidget(channels_label)

        self.channels_combo = NoWheelComboBox()
        self.channels_combo.addItems(["Mono (1)", "Stereo (2)"])
        self.channels_combo.setMinimumHeight(36)
        layout.addWidget(self.channels_combo)

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
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

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
        scroll_area, layout = self._cleanup_subtab_scaffold()

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
            "Save settings and open Model Manager → On-demand to choose the "
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
            "Provider and model selection live in Model Manager → On-demand."
        )
        model_hint.setObjectName("cleanupModelSummaryHint")
        model_hint.setWordWrap(True)
        model_card_layout.addWidget(model_hint)
        layout.addWidget(model_card)

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
            "Built-in OpenAI/OpenRouter keys and any custom endpoint variable "
            "come from the environment or .env. Local servers can leave the "
            "key variable blank. Edit the prompt to change cleanup style "
            "(e.g. bullets, email tone)."
        )
        cleanup_info.setObjectName("infoLabel")
        cleanup_info.setWordWrap(True)
        self.cleanup_prompt_info = cleanup_info
        layout.addWidget(cleanup_info)

        layout.addStretch()
        return scroll_area

    def _create_cleanup_rules_subtab(self):
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

        Model selection lives in Model Manager → Meeting Mode. This tab keeps
        consent, knowledge-folder, report-view, and dashboard network
        settings the engine still reads through ``resolve_meeting_*()``.
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

        layout.addSpacing(12)
        model_card = QFrame()
        model_card.setObjectName("meetingModelSummaryCard")
        model_card_layout = QVBoxLayout(model_card)
        model_card_layout.setContentsMargins(16, 14, 16, 14)
        model_card_layout.setSpacing(6)
        model_header = QHBoxLayout()
        model_header.setContentsMargins(0, 0, 0, 0)
        model_header.setSpacing(8)
        model_eyebrow = QLabel("MEETING MODELS")
        model_eyebrow.setObjectName("meetingModelSummaryEyebrow")
        model_header.addWidget(model_eyebrow)
        model_header.addStretch()
        self.open_meeting_model_manager_btn = QPushButton("Open Model Manager…")
        self.open_meeting_model_manager_btn.setObjectName(
            "meetingModelManagerLink"
        )
        self.open_meeting_model_manager_btn.setFlat(True)
        self.open_meeting_model_manager_btn.setCursor(
            Qt.CursorShape.PointingHandCursor
        )
        self.open_meeting_model_manager_btn.setToolTip(
            "Save settings and open Model Manager → Meeting Mode to choose "
            "transcription, language, speaker ID, and intelligence models"
        )
        self.open_meeting_model_manager_btn.clicked.connect(
            self._open_model_manager_from_meeting
        )
        model_header.addWidget(self.open_meeting_model_manager_btn)
        model_card_layout.addLayout(model_header)
        self.meeting_model_summary = QLabel("")
        self.meeting_model_summary.setObjectName("meetingModelSummary")
        self.meeting_model_summary.setWordWrap(True)
        model_card_layout.addWidget(self.meeting_model_summary)

        model_hint = QLabel(
            "Whisper, spoken language, speaker identification, the chat "
            "model, and the agent core live in Model Manager → Meeting Mode."
        )
        model_hint.setObjectName("meetingModelSummaryHint")
        model_hint.setWordWrap(True)
        model_card_layout.addWidget(model_hint)
        layout.addWidget(model_card)

        layout.addSpacing(12)
        intelligence_title = QLabel("Intelligence")
        intelligence_title.setObjectName("sectionLabel")
        layout.addWidget(intelligence_title)

        meeting_intelligence_info = QLabel(
            "Transcript text and meeting state are sent to the provider — "
            "cloud intelligence does not upload audio, and nothing is sent "
            "until you enable it for a meeting."
        )
        meeting_intelligence_info.setObjectName("infoLabel")
        meeting_intelligence_info.setWordWrap(True)
        layout.addWidget(meeting_intelligence_info)

        self.meeting_past_recall_check = QCheckBox(
            "Let the meeting agent search past transcripts"
        )
        self.meeting_past_recall_check.setObjectName("meetingPastRecallCheck")
        self.meeting_past_recall_check.setToolTip(
            "When enabled, cloud intelligence may send excerpts from earlier "
            "meetings to the model. Off by default."
        )
        layout.addWidget(self.meeting_past_recall_check)
        past_recall_info = QLabel(
            "Off by default. The agent can then look up names and prior "
            "decisions from stored meetings. Excerpts leave this machine "
            "the same way the current transcript does."
        )
        past_recall_info.setObjectName("infoLabel")
        past_recall_info.setWordWrap(True)
        layout.addWidget(past_recall_info)

        self.meeting_context_folder_check = QCheckBox(
            "Let the meeting agent search a knowledge folder"
        )
        self.meeting_context_folder_check.setObjectName(
            "meetingContextFolderCheck"
        )
        self.meeting_context_folder_check.setToolTip(
            "When enabled, cloud intelligence may send excerpts from files "
            "in the selected folder to the model. Off by default."
        )
        layout.addWidget(self.meeting_context_folder_check)

        folder_row = QHBoxLayout()
        folder_row.setSpacing(8)
        self.meeting_context_folder_path = QLineEdit()
        self.meeting_context_folder_path.setObjectName(
            "meetingContextFolderPath"
        )
        self.meeting_context_folder_path.setPlaceholderText(
            "No folder selected"
        )
        folder_row.addWidget(self.meeting_context_folder_path, 1)
        browse_btn = Button("Browse…")
        browse_btn.setObjectName("meetingContextFolderBrowse")
        browse_btn.clicked.connect(self._browse_context_folder)
        folder_row.addWidget(browse_btn)
        clear_btn = Button("Clear")
        clear_btn.setObjectName("meetingContextFolderClear")
        clear_btn.clicked.connect(self._clear_context_folder)
        folder_row.addWidget(clear_btn)
        layout.addLayout(folder_row)

        context_folder_info = QLabel(
            "Off by default. Choose a local folder (for example an Obsidian "
            "vault). The agent can search files there for names and project "
            "context. Matched excerpts leave this machine the same way the "
            "current transcript does. Images, audio, and video are not read."
        )
        context_folder_info.setObjectName("infoLabel")
        context_folder_info.setWordWrap(True)
        layout.addWidget(context_folder_info)

        layout.addSpacing(16)
        after_title = QLabel("After the meeting")
        after_title.setObjectName("sectionLabel")
        layout.addWidget(after_title)

        after_info = QLabel(
            "Live captions stay on short chunks so text appears quickly. "
            "These steps run after End, once you have time. Cloud "
            "intelligence must be on for polish and the final report."
        )
        after_info.setObjectName("infoLabel")
        after_info.setWordWrap(True)
        layout.addWidget(after_info)

        self.meeting_end_redecode_check = QCheckBox(
            "Re-transcribe with longer pauses (full recording)"
        )
        self.meeting_end_redecode_check.setToolTip(
            "After End, recut the continuous session audio on longer quiet "
            "gaps and run Whisper again. Live capture is unchanged. This "
            "can take a few minutes and did not beat live word error on AMI."
        )
        layout.addWidget(self.meeting_end_redecode_check)

        self.meeting_end_polish_check = QCheckBox(
            "Clean up the transcript with the LLM"
        )
        layout.addWidget(self.meeting_end_polish_check)

        self.meeting_end_report_check = QCheckBox(
            "Write the final report (topic, summary, cards)"
        )
        layout.addWidget(self.meeting_end_report_check)

        views_title = QLabel("Report views")
        views_title.setObjectName("sectionLabel")
        layout.addWidget(views_title)
        self.meeting_report_views_title = views_title

        views_info = QLabel(
            "Each enabled view is generated at End. Turning Ribbon off "
            "skips timeline beats and polished minutes, which is the "
            "main token cost. Brief and Signal reuse the same cards."
        )
        views_info.setObjectName("infoLabel")
        views_info.setWordWrap(True)
        layout.addWidget(views_info)
        self.meeting_report_views_info = views_info

        self.meeting_report_ribbon_check = QCheckBox(
            "Ribbon — timeline walk (adds timeline beats and polished minutes)"
        )
        layout.addWidget(self.meeting_report_ribbon_check)

        self.meeting_report_brief_check = QCheckBox(
            "Brief — one-page editorial summary"
        )
        layout.addWidget(self.meeting_report_brief_check)

        self.meeting_report_signal_check = QCheckBox(
            "Signal — one-screen glance"
        )
        layout.addWidget(self.meeting_report_signal_check)

        self.meeting_report_views_hint = QLabel(
            "At least one view is required. Ribbon stays on."
        )
        self.meeting_report_views_hint.setObjectName("infoLabel")
        self.meeting_report_views_hint.setWordWrap(True)
        self.meeting_report_views_hint.hide()
        layout.addWidget(self.meeting_report_views_hint)

        self.meeting_end_report_check.toggled.connect(
            self._update_report_views_enabled
        )
        for check in (
            self.meeting_report_ribbon_check,
            self.meeting_report_brief_check,
            self.meeting_report_signal_check,
        ):
            check.toggled.connect(self._guard_report_views)

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

        title = QLabel("Advanced Settings")
        title.setObjectName("headerLabel")
        layout.addWidget(title)

        # Local engine knobs (model / device / quant) live in the main
        # window's Engine Settings panel and the Model Manager, not here.

        layout.addSpacing(12)
        max_size_label = QLabel("Maximum File Size (MB):")
        layout.addWidget(max_size_label)

        self.max_size_spinbox = NoWheelSpinBox()
        self.max_size_spinbox.setMinimum(1)
        self.max_size_spinbox.setMaximum(500)
        self.max_size_spinbox.setValue(23)
        self.max_size_spinbox.setMinimumHeight(36)
        layout.addWidget(self.max_size_spinbox)

        layout.addSpacing(12)
        self.logging_check = QCheckBox("Enable detailed logging")
        layout.addWidget(self.logging_check)

        layout.addSpacing(16)
        self.developer_mode_check = QCheckBox("Developer mode")
        self.developer_mode_check.setObjectName("developerModeCheck")
        layout.addWidget(self.developer_mode_check)
        developer_info = QLabel(
            "Unlocks a Load demo meeting control on the Meeting Mode tab. "
            "The demo opens the dashboard with a fake transcript so you can "
            "test end-of-meeting cleanup and the final report without "
            "recording a real meeting."
        )
        developer_info.setObjectName("infoLabel")
        developer_info.setWordWrap(True)
        layout.addWidget(developer_info)

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
        """Save Settings, close it, then open Model Manager → On-demand.

        Model Manager is non-modal and cannot share an ``exec()`` session with
        Settings — opening it mid-modal stacks behind the main window and
        becomes unresponsive on Windows. The controller opens it after
        ``exec()`` returns when ``open_model_manager_on_close`` is set.
        """
        self.open_model_manager_on_close = "text"
        self._save_settings()

    def _open_model_manager_from_meeting(self):
        self.open_model_manager_on_close = "meeting"
        self._save_settings()

    def _refresh_cleanup_model_summary(self):
        try:
            settings = settings_manager.load_all_settings()
            saved_provider = resolve_transcript_cleanup_provider(settings)
            saved_model = resolve_transcript_cleanup_model(settings)
        except Exception:
            settings = {}
            saved_provider = config.TRANSCRIPT_CLEANUP_PROVIDER
            saved_model = config.TRANSCRIPT_CLEANUP_OPENROUTER_MODEL
        provider_name = profile_display_name(saved_provider, settings)
        self.cleanup_model_summary.setText(f"{provider_name} · {saved_model}")

    def _refresh_meeting_model_summary(self):
        try:
            settings = settings_manager.load_all_settings()
        except Exception:
            settings = {}
        whisper = resolve_meeting_whisper_model(settings)
        language = resolve_meeting_language(settings)
        provider = resolve_meeting_llm_provider(settings)
        llm_model = resolve_meeting_llm_model(settings)
        core = resolve_meeting_agent_core(settings)
        speaker = resolve_meeting_speaker_id_backend(settings)
        language_label = next(
            (label for code, label in MeetingLanguage.CHOICES if code == language),
            language,
        )
        provider_name = profile_display_name(provider, settings)
        core_label = (
            "Pi (sidecar)" if core == MeetingAgentCore.PI
            else "Direct (no sidecar)"
        )
        speaker_label = (
            "On-device (WeSpeaker)"
            if speaker == MeetingSpeakerIdBackend.LOCAL
            else "OpenAI (gpt-4o-transcribe-diarize)"
        )
        self.meeting_model_summary.setText(
            f"Whisper · {whisper}\n"
            f"Spoken language · {language_label}\n"
            f"{provider_name} · {llm_model}\n"
            f"Agent core · {core_label}\n"
            f"Speaker ID · {speaker_label}"
        )

    def _update_threshold_display(self, value):
        threshold = value / 1000.0
        self.threshold_value_label.setText(f"{threshold:.3f}")

    def _on_update_check_toggled(self, checked: bool) -> None:
        """Notify is meaningless when automatic checks are off."""
        self.update_notify_check.setEnabled(bool(checked))

    def _update_recording_retention_ui(self):
        is_custom = (
            self.recording_retention_combo.currentData()
            == RecordingRetentionMode.CUSTOM
        )
        self.max_recordings_label.setEnabled(is_custom)
        self.max_recordings_spinbox.setEnabled(is_custom)

    def _update_streaming_font_ui(self):
        enabled = self.streaming_enabled_check.isChecked()
        self.streaming_font_size_label.setEnabled(enabled)
        self.streaming_font_size_spinbox.setEnabled(enabled)

    def _update_cleanup_prompt_ui(self):
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

    def _open_cleanup_prompt_editor(self):
        dialog = CleanupPromptDialog(self.cleanup_prompt_edit.toPlainText(), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.cleanup_prompt_edit.setPlainText(dialog.prompt_text())

    def _reset_cleanup_prompt(self):
        self.cleanup_prompt_edit.setPlainText(config.TRANSCRIPT_CLEANUP_PROMPT)

    def _staged_cleanup_rules(self) -> list:
        return [
            self.cleanup_rules_list.item(i).text()
            for i in range(self.cleanup_rules_list.count())
        ]

    def _update_cleanup_rule_controls(self):
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
        for item in self.cleanup_rules_list.selectedItems():
            self.cleanup_rules_list.takeItem(self.cleanup_rules_list.row(item))
        self._update_cleanup_rule_controls()

    def _toggle_rule_dictation(self):
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
        self._rule_dictation_timer.stop()
        if self._rule_recorder is not None:
            self._rule_recorder.cleanup()
            self._rule_recorder = None

    def _update_meeting_bind_ui(self):
        is_lan = self.meeting_bind_combo.currentData() == MeetingServerBind.LAN
        self.meeting_bind_warning.setVisible(is_lan)

    def _report_view_checks(self):
        return (
            self.meeting_report_ribbon_check,
            self.meeting_report_brief_check,
            self.meeting_report_signal_check,
        )

    def _update_report_views_enabled(self):
        enabled = self.meeting_end_report_check.isChecked()
        self.meeting_report_views_title.setEnabled(enabled)
        self.meeting_report_views_info.setEnabled(enabled)
        for check in self._report_view_checks():
            check.setEnabled(enabled)
        if not enabled:
            self.meeting_report_views_hint.hide()

    def _guard_report_views(self):
        """Keep at least one report view checked; restore Ribbon if needed."""
        if any(check.isChecked() for check in self._report_view_checks()):
            self.meeting_report_views_hint.hide()
            return
        blocker = self.meeting_report_ribbon_check.blockSignals(True)
        self.meeting_report_ribbon_check.setChecked(True)
        self.meeting_report_ribbon_check.blockSignals(blocker)
        self.meeting_report_views_hint.show()

    def _browse_context_folder(self):
        current = self.meeting_context_folder_path.text().strip()
        start = current if os.path.isdir(current) else os.path.expanduser("~")
        chosen = QFileDialog.getExistingDirectory(
            self, "Select knowledge folder", start,
        )
        if not chosen:
            return
        self.meeting_context_folder_path.setText(os.path.normpath(chosen))
        self.meeting_context_folder_check.setChecked(True)

    def _clear_context_folder(self):
        self.meeting_context_folder_path.clear()
        self.meeting_context_folder_check.setChecked(False)

    def _load_meeting_settings(self, settings: dict):
        """Apply the stored Meeting Mode settings to their controls.

        Args:
            settings: Loaded settings dict; an empty dict yields the defaults.
        """
        self._refresh_meeting_model_summary()

        bind_index = self.meeting_bind_combo.findData(
            resolve_meeting_server_bind(settings)
        )
        self.meeting_bind_combo.setCurrentIndex(max(0, bind_index))
        self.meeting_port_spinbox.setValue(resolve_meeting_server_port(settings))
        self.meeting_past_recall_check.setChecked(
            resolve_meeting_past_recall_enabled(settings)
        )
        self.meeting_context_folder_check.setChecked(
            resolve_meeting_context_folder_enabled(settings)
        )
        self.meeting_context_folder_path.setText(
            resolve_meeting_context_folder_path(settings)
        )
        self.meeting_end_redecode_check.setChecked(
            resolve_meeting_end_redecode(settings)
        )
        self.meeting_end_polish_check.setChecked(
            resolve_meeting_end_polish(settings)
        )
        self.meeting_end_report_check.setChecked(
            resolve_meeting_end_report(settings)
        )
        self.meeting_report_ribbon_check.setChecked(
            resolve_meeting_report_ribbon(settings)
        )
        self.meeting_report_brief_check.setChecked(
            resolve_meeting_report_brief(settings)
        )
        self.meeting_report_signal_check.setChecked(
            resolve_meeting_report_signal(settings)
        )
        self._guard_report_views()
        self._update_report_views_enabled()
        self._update_meeting_bind_ui()

    def _populate_audio_devices(self):
        self.audio_device_combo.clear()
        self.audio_device_combo.addItem("System Default", None)

        devices = AudioRecorder.get_input_devices()
        for device_id, device_name in devices:
            self.audio_device_combo.addItem(device_name, device_id)

    def _open_hotkey_dialog(self):
        logger.info("Opening hotkey configuration dialog")
        from ui_qt.dialogs.hotkey_dialog import HotkeyDialog

        dialog = HotkeyDialog(self)
        dialog.exec()

    def _load_settings(self):
        try:
            settings = settings_manager.load_all_settings()

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
            self.update_check_check.setChecked(resolve_update_check_enabled(settings))
            self.update_notify_check.setChecked(resolve_update_notify_enabled(settings))
            self._on_update_check_toggled(self.update_check_check.isChecked())

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

            streaming_enabled = settings.get(SettingsKey.STREAMING_ENABLED, config.STREAMING_ENABLED)
            self.streaming_enabled_check.setChecked(streaming_enabled)
            self.streaming_font_size_spinbox.setValue(
                resolve_streaming_overlay_font_size(settings)
            )
            self._update_streaming_font_ui()

            self._load_meeting_settings(settings)

            self.developer_mode_check.setChecked(resolve_developer_mode(settings))

            # Typed load performs legacy hf_hub_offline migration
            policy = settings_manager.load_hf_access_policy()
            policy_index = self.hf_policy_combo.findData(policy)
            self.hf_policy_combo.setCurrentIndex(max(0, policy_index))

            saved_device_id = settings.get(SettingsKey.AUDIO_INPUT_DEVICE)
            if saved_device_id is not None:
                for i in range(self.audio_device_combo.count()):
                    if self.audio_device_combo.itemData(i) == saved_device_id:
                        self.audio_device_combo.setCurrentIndex(i)
                        break

            logger.info("Settings loaded successfully")
        except Exception as e:
            logger.error(f"Failed to load settings: {e}")
            self.auto_paste_check.setChecked(True)
            self.copy_clipboard_check.setChecked(True)
            self.transcript_cleanup_check.setChecked(config.TRANSCRIPT_CLEANUP_ENABLED)
            self.cleanup_prompt_edit.setPlainText(config.TRANSCRIPT_CLEANUP_PROMPT)
            self.cleanup_rules_list.clear()
            self.cleanup_model_summary.setText(
                f"OpenRouter · {config.TRANSCRIPT_CLEANUP_OPENROUTER_MODEL}"
            )
            self.cleanup_reasoning_combo.setCurrentIndex(0)
            self._update_cleanup_prompt_ui()
            self.minimize_tray_check.setChecked(True)
            self.update_check_check.setChecked(config.UPDATE_CHECK_ENABLED)
            self.update_notify_check.setChecked(config.UPDATE_NOTIFY_ENABLED)
            self._on_update_check_toggled(self.update_check_check.isChecked())
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
            self.developer_mode_check.setChecked(config.DEVELOPER_MODE)
            self.hf_policy_combo.setCurrentIndex(
                max(0, self.hf_policy_combo.findData(HuggingFaceAccessPolicy.ASK))
            )

    def _save_settings(self):
        try:
            # Load existing settings. The transcription engine and local
            # whisper model/device/compute are owned by the main-window
            # controls, so their keys pass through untouched.
            settings = settings_manager.load_all_settings()

            old_hf_policy = settings_manager.load_hf_access_policy()
            new_hf_policy = self.hf_policy_combo.currentData()
            hf_policy_changed = old_hf_policy != new_hf_policy

            old_audio_device = settings.get(SettingsKey.AUDIO_INPUT_DEVICE)
            new_audio_device = self.audio_device_combo.currentData()
            audio_device_changed = old_audio_device != new_audio_device

            old_streaming_enabled = settings.get(SettingsKey.STREAMING_ENABLED, False)
            streaming_settings_changed = (
                old_streaming_enabled != self.streaming_enabled_check.isChecked()
            )

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
            settings[SettingsKey.UPDATE_CHECK_ENABLED] = (
                self.update_check_check.isChecked()
            )
            settings[SettingsKey.UPDATE_NOTIFY_ENABLED] = (
                self.update_notify_check.isChecked()
            )
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

            # Meeting Mode. Whisper, language, speaker ID, agent core, and
            # LLM keys pass through untouched because Model Manager is
            # their single owner.
            settings[SettingsKey.MEETING_SERVER_BIND] = (
                self.meeting_bind_combo.currentData()
            )
            settings[SettingsKey.MEETING_SERVER_PORT] = (
                self.meeting_port_spinbox.value()
            )
            settings[SettingsKey.MEETING_PAST_RECALL_ENABLED] = (
                self.meeting_past_recall_check.isChecked()
            )
            settings[SettingsKey.MEETING_CONTEXT_FOLDER_ENABLED] = (
                self.meeting_context_folder_check.isChecked()
            )
            settings[SettingsKey.MEETING_CONTEXT_FOLDER_PATH] = (
                resolve_meeting_context_folder_path({
                    SettingsKey.MEETING_CONTEXT_FOLDER_PATH: (
                        self.meeting_context_folder_path.text()
                    ),
                })
            )
            settings[SettingsKey.MEETING_END_REDECODE] = (
                self.meeting_end_redecode_check.isChecked()
            )
            settings[SettingsKey.MEETING_END_POLISH] = (
                self.meeting_end_polish_check.isChecked()
            )
            settings[SettingsKey.MEETING_END_REPORT] = (
                self.meeting_end_report_check.isChecked()
            )
            settings[SettingsKey.MEETING_REPORT_RIBBON] = (
                self.meeting_report_ribbon_check.isChecked()
            )
            settings[SettingsKey.MEETING_REPORT_BRIEF] = (
                self.meeting_report_brief_check.isChecked()
            )
            settings[SettingsKey.MEETING_REPORT_SIGNAL] = (
                self.meeting_report_signal_check.isChecked()
            )
            settings[SettingsKey.DEVELOPER_MODE] = (
                self.developer_mode_check.isChecked()
            )

            if new_audio_device is None:
                settings.pop(SettingsKey.AUDIO_INPUT_DEVICE, None)
            else:
                settings[SettingsKey.AUDIO_INPUT_DEVICE] = new_audio_device

            # Save to file (policy is read live at each model request, so the
            # new hf_access_policy takes effect immediately)
            settings_manager.save_all_settings(settings)

            history_manager.set_max_recordings(resolve_max_saved_recordings(settings))

            logger.info("Settings saved successfully")

            if self.on_settings_save:
                self.on_settings_save(settings)

            settings['_audio_device_changed'] = audio_device_changed
            settings['_streaming_settings_changed'] = streaming_settings_changed
            settings['_hf_policy_changed'] = hf_policy_changed
            self.settings_changed.emit(settings)

            self.accept()
        except Exception as e:
            logger.error(f"Failed to save settings: {e}")
            self.reject()

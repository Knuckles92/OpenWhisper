import logging
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Callable, Dict, Optional

from PyQt6.QtCore import QEvent, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QIcon
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QSystemTrayIcon,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from config import bundle_root, config
from services.credentials import (
    MAX_API_KEY_LEN,
    CredentialSource,
    CredentialStoreError,
    credential_source,
    environment_shadowed,
    mask_key,
    resolve_credential,
    validate_api_key,
)
from services.credentials import store as credential_store
from services.history_manager import history_manager
from services.hotkey_manager import USE_PYNPUT_BACKEND, format_hotkey_display
from services.recorder import AudioRecorder
from services.settings import (
    HuggingFaceAccessPolicy,
    MeetingAgentCore,
    MeetingLanguage,
    MeetingServerBind,
    MeetingSpeakerIdBackend,
    RecordingRetentionMode,
    RecordingTriggerMode,
    SettingsKey,
    TranscriptCleanupReasoning,
    resolve_developer_mode,
    resolve_max_saved_recordings,
    resolve_meeting_agent_core,
    resolve_meeting_context_folder_enabled,
    resolve_meeting_context_folder_path,
    resolve_meeting_end_polish,
    resolve_meeting_end_redecode,
    resolve_meeting_end_report,
    resolve_meeting_language,
    resolve_meeting_llm_model,
    resolve_meeting_llm_provider,
    resolve_meeting_past_recall_enabled,
    resolve_meeting_report_brief,
    resolve_meeting_report_ribbon,
    resolve_meeting_report_signal,
    resolve_recording_trigger_mode,
    resolve_meeting_server_bind,
    resolve_meeting_server_port,
    resolve_meeting_speaker_id_backend,
    resolve_meeting_whisper_model,
    resolve_streaming_overlay_font_size,
    resolve_transcript_cleanup_model,
    resolve_transcript_cleanup_prompt,
    resolve_transcript_cleanup_provider,
    resolve_transcript_cleanup_reasoning,
    resolve_transcript_cleanup_rules,
    resolve_update_check_enabled,
    resolve_update_notify_enabled,
    settings_manager,
)
from services.text_llm import (
    credential_label,
    list_profiles,
    profile_display_name,
    verify_api_key,
)
from ui_qt.dialogs.cleanup_prompt_dialog import CleanupPromptDialog
from ui_qt.dialogs.cleanup_rule_dialog import CleanupRuleDialog
from ui_qt.utils.app_icon import app_icon
from ui_qt.widgets import (
    Button,
    DangerButton,
    ElidingComboBox,
    NoWheelSpinBox,
    FieldTile,
    PrimaryButton,
    SettingTile,
    WrappedLabel,
)
from ui_qt.widgets.hotkey_capture import HotkeyCaptureInput, HotkeyCaptureThread
from ui_qt.widgets.nav_rail import NavRail

logger = logging.getLogger(__name__)


def is_native_wayland_session(
    platform_name: Optional[str] = None,
    environment: Optional[Dict[str, str]] = None,
) -> bool:
    """Return whether Linux is running inside a native Wayland session."""
    platform_id = platform_name if platform_name is not None else sys.platform
    env = environment if environment is not None else os.environ
    if not platform_id.startswith("linux"):
        return False
    return (
        env.get("XDG_SESSION_TYPE", "").strip().lower() == "wayland"
        or bool(env.get("WAYLAND_DISPLAY"))
    )


GENERAL = "general"
RECORDING = "recording"
CLEANUP = "cleanup"
CLEANUP_RULES = "cleanup_rules"
MEETING_INTELLIGENCE = "meeting_intelligence"
MEETING_AFTER = "meeting_after"
MEETING_DASHBOARD = "meeting_dashboard"
API_KEYS = "api_keys"
HOTKEYS = "hotkeys"
ADVANCED = "advanced"

_HF_POLICY_RAIL = {
    HuggingFaceAccessPolicy.ASK: "Ask first",
    HuggingFaceAccessPolicy.ALWAYS: "Always allow",
    HuggingFaceAccessPolicy.NEVER: "Offline",
}


def _design_icon(filename: str) -> QIcon:
    path = Path(bundle_root()) / "ui_qt" / "assets" / "tabler" / filename
    icon = QIcon(str(path))
    icon.addPixmap(icon.pixmap(24, 24), QIcon.Mode.Disabled, QIcon.State.Off)
    return icon


class SettingsDialog(QDialog):
    """Non-modal Settings window with a Model Manager-style rail.

    Changes persist immediately. ``UIController`` holds a single instance and
    re-raises it instead of stacking copies.
    """

    #: 760 clears the 566 px the Cleanup destination needs under themed
    #: fonts, plus chrome. Learned rules is next at 493 once the empty
    #: state and the rule list are mutually exclusive.
    DEFAULT_SIZE = QSize(980, 760)
    MINIMUM_SIZE = QSize(840, 700)

    model_manager_requested = pyqtSignal(str)
    _cleanup_rule_polished = pyqtSignal(str, str, str)
    _rule_dictation_finished = pyqtSignal(str, str)
    _api_key_verified = pyqtSignal(str, bool, str)

    on_audio_device_changed: Optional[Callable] = None
    on_streaming_settings_changed: Optional[Callable] = None
    on_streaming_font_changed: Optional[Callable] = None
    on_hf_policy_changed: Optional[Callable] = None
    on_api_keys_changed: Optional[Callable[[], None]] = None
    on_developer_mode_changed: Optional[Callable] = None
    on_cleanup_changed: Optional[Callable] = None
    on_hotkeys_changed: Optional[Callable[[Dict[str, str]], None]] = None
    on_recording_trigger_mode_changed: Optional[Callable[[str], None]] = None
    on_dictation_transcribe: Optional[Callable[[str], str]] = None
    get_meeting_active: Optional[Callable[[], bool]] = None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setWindowIcon(app_icon())
        self.setObjectName("settingsDialog")
        self.setModal(False)
        self.setWindowFlag(Qt.WindowType.MSWindowsFixedSizeDialogHint, False)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.setSizeGripEnabled(True)

        self._loading = False
        self._tray_available = bool(QSystemTrayIcon.isSystemTrayAvailable())
        self._native_wayland = is_native_wayland_session()
        self.current_hotkeys: Dict[str, str] = {}
        self.capturing: Optional[str] = None
        self.capture_thread: Optional[HotkeyCaptureThread] = None
        self.current_hotkey_input: Optional[HotkeyCaptureInput] = None
        self.hotkey_inputs: Dict[str, HotkeyCaptureInput] = {}
        self.hotkey_row_descriptions: Dict[str, WrappedLabel] = {}
        self._saved_cleanup_prompt = ""
        self._rule_polishing = False
        self._rule_dictation_state = "idle"
        self._rule_recorder: Optional[AudioRecorder] = None
        self._rule_recorder_device: Optional[int] = None
        self._api_key_testing = False
        self._api_key_source = CredentialSource.NONE
        self._rule_dictation_path = os.path.join(
            tempfile.gettempdir(), "openwhisper_rule_dictation.wav"
        )
        self._rule_dictation_timer = QTimer(self)
        self._rule_dictation_timer.setSingleShot(True)
        self._rule_dictation_timer.setInterval(60_000)
        self._rule_dictation_timer.timeout.connect(self._stop_rule_dictation)

        self._setup_ui()
        self.setMinimumSize(self.MINIMUM_SIZE)
        self.resize(self.DEFAULT_SIZE)

        self._cleanup_rule_polished.connect(self._on_cleanup_rule_polished)
        self._rule_dictation_finished.connect(self._on_rule_dictation_finished)
        self._api_key_verified.connect(self._on_api_key_verified)
        self.finished.connect(self._release_rule_recorder)
        self.rail.select(GENERAL)
        self.refresh()

    def _setup_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_rail_pane())
        root.addWidget(self._build_body(), stretch=1)

    def _build_rail_pane(self) -> QWidget:
        pane = QWidget()
        pane.setObjectName("modelManagerRailPane")
        pane.setFixedWidth(NavRail.RAIL_WIDTH)
        column = QVBoxLayout(pane)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        brand = QHBoxLayout()
        brand.setContentsMargins(16, 16, 16, 12)
        brand.setSpacing(10)
        brand_icon = QLabel()
        brand_icon.setObjectName("modelManagerHeaderIcon")
        brand_icon.setPixmap(app_icon().pixmap(26, 26))
        brand_icon.setFixedSize(28, 28)
        brand.addWidget(brand_icon)
        brand_title = QLabel("Settings")
        brand_title.setObjectName("modelManagerRailBrand")
        brand.addWidget(brand_title)
        brand.addStretch()
        column.addLayout(brand)

        self.rail = NavRail()
        self.rail.add_group("General")
        self.rail.add_destination(GENERAL, "General", _design_icon("bolt-green.svg"))
        self.rail.add_destination(
            RECORDING, "Recording", _design_icon("microphone-blue.svg")
        )
        self.rail.add_group("Dictation cleanup")
        self.rail.add_destination(
            CLEANUP, "Cleanup", _design_icon("stack-purple.svg")
        )
        self.rail.add_destination(
            CLEANUP_RULES, "Learned rules", _design_icon("stack-slate.svg")
        )
        self.rail.add_group("Meeting Mode")
        self.rail.add_destination(
            MEETING_INTELLIGENCE, "Intelligence", _design_icon("stack-purple.svg")
        )
        self.rail.add_destination(
            MEETING_AFTER, "After the meeting", _design_icon("check-green.svg")
        )
        self.rail.add_destination(
            MEETING_DASHBOARD, "Dashboard", _design_icon("box-blue.svg")
        )
        self.rail.add_group("Cloud services")
        self.rail.add_destination(API_KEYS, "API keys", _design_icon("key-blue.svg"))
        self.rail.add_group("System")
        self.rail.add_destination(HOTKEYS, "Hotkeys", _design_icon("bolt-green.svg"))
        self.rail.add_destination(ADVANCED, "Advanced", _design_icon("box-blue.svg"))
        self.rail.destination_changed.connect(self._on_destination_changed)
        column.addWidget(self.rail, stretch=1)
        return pane

    def _build_body(self) -> QWidget:
        body = QWidget()
        body.setObjectName("modelManagerBody")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(28, 20, 28, 16)
        layout.setSpacing(14)

        self.page_title = QLabel("")
        self.page_title.setObjectName("modelManagerTitle")
        self.page_subtitle = WrappedLabel("")
        self.page_subtitle.setObjectName("modelManagerSubtitle")
        layout.addWidget(self.page_title)
        layout.addWidget(self.page_subtitle)

        self.stack = QStackedWidget()
        self.stack.setObjectName("modelManagerStack")
        self._pages: Dict[str, QWidget] = {}
        self._headings: Dict[str, tuple] = {}
        self._add_page(
            GENERAL,
            "General",
            "How finished transcriptions leave the app, and how the window "
            "behaves when you close it.",
            self._build_general_page,
        )
        self._add_page(
            RECORDING,
            "Recording",
            "Microphone, saved audio retention, and the live preview overlay.",
            self._build_recording_page,
        )
        self._add_page(
            CLEANUP,
            "AI transcript cleanup",
            "Rewrite a finished dictation with a chat model. Provider and "
            "model live in Model Manager.",
            self._build_cleanup_page,
        )
        self._add_page(
            CLEANUP_RULES,
            "Learned rules",
            "Teach OpenWhisper your preferred spellings, terminology, and "
            "formatting. Applied whenever AI cleanup runs.",
            self._build_cleanup_rules_page,
        )
        self._add_page(
            MEETING_INTELLIGENCE,
            "Meeting intelligence",
            "What the meeting agent may search, and which models it uses. "
            "Nothing is sent until you enable intelligence for a meeting.",
            self._build_meeting_intelligence_page,
        )
        self._add_page(
            MEETING_AFTER,
            "After the meeting",
            "Steps that run after End, once live captions have finished.",
            self._build_meeting_after_page,
        )
        self._add_page(
            MEETING_DASHBOARD,
            "Dashboard access",
            "Who can open the live meeting dashboard, and on which port.",
            self._build_meeting_dashboard_page,
        )
        self._add_page(
            API_KEYS,
            "API keys",
            "Credentials for OpenAI, OpenRouter, and custom endpoints. Saved "
            "keys live in your operating system's credential manager, never "
            "in the settings file.",
            self._build_api_keys_page,
        )
        self._add_page(
            HOTKEYS,
            "Hotkeys",
            "Global shortcuts for record, cancel, enable/disable, and "
            "minimize.",
            self._build_hotkeys_page,
        )
        self._add_page(
            ADVANCED,
            "Advanced",
            "Developer tools and when Hugging Face may download a missing "
            "model.",
            self._build_advanced_page,
        )
        layout.addWidget(self.stack, stretch=1)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.message_label = WrappedLabel("")
        self.message_label.setObjectName("modelManagerMessage")
        footer.addWidget(self.message_label, stretch=1)
        close_btn = Button("Close")
        close_btn.setObjectName("modelManagerCloseButton")
        self._compact_button(close_btn, 110)
        close_btn.clicked.connect(self.close)
        footer.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignBottom)
        layout.addLayout(footer)
        return body

    def _add_page(
        self, key: str, title: str, subtitle: str, builder: Callable
    ) -> None:
        page = QWidget()
        page.setObjectName(f"settingsPage_{key}")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        builder(layout)
        layout.addStretch()
        self._pages[key] = page
        self._headings[key] = (title, subtitle)
        self.stack.addWidget(page)

    def _field(self, label: str, widget: QWidget) -> QWidget:
        wrapper = QWidget()
        wrapper.setObjectName("modelManagerFieldGroup")
        col = QVBoxLayout(wrapper)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(5)
        caption = QLabel(label)
        caption.setObjectName("textModelFieldLabel")
        col.addWidget(caption)
        col.addWidget(widget)
        return wrapper

    @staticmethod
    def _caption(text: str) -> WrappedLabel:
        label = WrappedLabel(text)
        label.setObjectName("infoLabel")
        return label

    @staticmethod
    def _compact_button(button: Button, width: int) -> None:
        button.set_base_minimum_size(width, 34)
        button.ensurePolished()
        height = max(34, button.sizeHint().height())
        button.setMinimumHeight(height)
        button.setMaximumHeight(height)
        fitted = max(width, button.minimumWidth(), button.sizeHint().width())
        button.setMinimumWidth(fitted)
        button.setMaximumWidth(fitted)

    def _tile_group(
        self,
        layout: QVBoxLayout,
        title: str,
        tiles: list,
        *,
        columns: int = 2,
        intro: str = "",
    ) -> tuple:
        """Caption, optional intro line, and a grid of tiles.

        A trailing odd tile spans the rest of its row so no column is left
        empty. Returns the caption and intro labels for callers that gate them.
        """
        caption = QLabel(title.upper())
        caption.setObjectName("settingsTileGroupTitle")
        layout.addWidget(caption)
        intro_label = None
        if intro:
            intro_label = self._caption(intro)
            layout.addWidget(intro_label)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        for column in range(columns):
            grid.setColumnStretch(column, 1)
        for index, tile in enumerate(tiles):
            row, column = divmod(index, columns)
            is_last = index == len(tiles) - 1
            if is_last and column < columns - 1:
                grid.addWidget(tile, row, column, 1, columns - column)
            else:
                grid.addWidget(tile, row, column)
        layout.addLayout(grid)
        layout.addSpacing(6)
        return caption, intro_label

    def _build_general_page(self, layout: QVBoxLayout) -> None:
        auto_paste_description = (
            "Types the transcript into whichever window has focus. The "
            "clipboard is borrowed for the paste and restored afterward."
        )
        if self._native_wayland:
            auto_paste_description += (
                " Native Wayland blocks cross-application key injection; use "
                "clipboard copy or an X11 session if pasting does not work."
            )
        self.auto_paste_tile = SettingTile(
            "Paste into the active window",
            auto_paste_description,
            _design_icon("bolt-green.svg"),
        )
        self.auto_paste_check = self.auto_paste_tile.checkbox

        self.copy_clipboard_tile = SettingTile(
            "Copy to the clipboard",
            "Keeps the transcript on the clipboard when auto-paste is off or "
            "unavailable. A successful paste restores what was there before.",
            _design_icon("stack-slate.svg"),
        )
        self.copy_clipboard_check = self.copy_clipboard_tile.checkbox

        self.minimize_tray_tile = SettingTile(
            "Minimize to the system tray on close",
            "Closing the window keeps OpenWhisper running in the tray, so "
            "hotkeys and recording stay available.",
            _design_icon("box-blue.svg"),
        )
        self.minimize_tray_check = self.minimize_tray_tile.checkbox
        if not self._tray_available:
            self.minimize_tray_tile.setEnabled(False)
            self.minimize_tray_tile.set_description(
                "This desktop session does not provide a system tray."
            )

        self.update_check_tile = SettingTile(
            "Check for updates automatically",
            "Looks for new OpenWhisper releases in the background. Nothing "
            "is installed without your approval.",
            _design_icon("refresh-blue.svg"),
        )
        self.update_check_check = self.update_check_tile.checkbox
        self.update_check_check.setObjectName("updateCheckEnabledCheck")

        self.update_notify_tile = SettingTile(
            "Notify me about updates",
            "Opens the update dialog when a newer release is found. Requires "
            "automatic checks.",
            _design_icon("info-blue.svg"),
        )
        self.update_notify_check = self.update_notify_tile.checkbox
        self.update_notify_check.setObjectName("updateNotifyEnabledCheck")

        self._tile_group(
            layout, "Output", [self.auto_paste_tile, self.copy_clipboard_tile]
        )
        self._tile_group(layout, "Window", [self.minimize_tray_tile])
        self._tile_group(
            layout, "Updates", [self.update_check_tile, self.update_notify_tile]
        )

        self.auto_paste_check.toggled.connect(
            lambda checked: self._persist(SettingsKey.AUTO_PASTE, bool(checked))
        )
        self.copy_clipboard_check.toggled.connect(
            lambda checked: self._persist(
                SettingsKey.COPY_CLIPBOARD, bool(checked)
            )
        )
        self.minimize_tray_check.toggled.connect(
            lambda checked: self._persist(
                SettingsKey.MINIMIZE_TRAY, bool(checked)
            )
        )
        self.update_check_check.toggled.connect(self._on_update_check_toggled)
        self.update_notify_check.toggled.connect(
            lambda checked: self._persist(
                SettingsKey.UPDATE_NOTIFY_ENABLED, bool(checked)
            )
        )

    def _build_recording_page(self, layout: QVBoxLayout) -> None:
        self.audio_device_combo = ElidingComboBox()
        self.audio_device_combo.setObjectName("settingsAudioDeviceCombo")
        self.audio_device_combo.setMinimumHeight(40)
        self._populate_audio_devices()
        self.audio_device_combo.currentIndexChanged.connect(
            self._on_audio_device_changed
        )
        self.audio_device_tile = FieldTile(
            "Input device",
            "The microphone used for dictation and meetings.",
            self.audio_device_combo,
            _design_icon("microphone-blue.svg"),
        )
        self._tile_group(layout, "Microphone", [self.audio_device_tile])

        self.recording_retention_combo = ElidingComboBox()
        self.recording_retention_combo.addItem(
            "Keep all", RecordingRetentionMode.KEEP_ALL
        )
        self.recording_retention_combo.addItem(
            "Custom", RecordingRetentionMode.CUSTOM
        )
        self.recording_retention_combo.setMinimumHeight(40)
        self.recording_retention_combo.currentIndexChanged.connect(
            self._on_retention_mode_changed
        )
        self.recording_retention_tile = FieldTile(
            "Keep recordings",
            "Older audio files are deleted automatically when the limit is "
            "exceeded. Transcription history text is kept separately.",
            self.recording_retention_combo,
            _design_icon("box-blue.svg"),
        )

        self.max_recordings_label = QLabel("Number to keep:")
        self.max_recordings_spinbox = NoWheelSpinBox()
        self.max_recordings_spinbox.setMinimum(1)
        self.max_recordings_spinbox.setMaximum(1000)
        self.max_recordings_spinbox.setValue(config.MAX_SAVED_RECORDINGS)
        self.max_recordings_spinbox.setMinimumHeight(36)
        self.max_recordings_spinbox.setMinimumWidth(110)
        self.max_recordings_spinbox.valueChanged.connect(
            self._on_max_recordings_changed
        )
        self.recording_retention_tile.add_body_layout(
            self._spin_form(self.max_recordings_label, self.max_recordings_spinbox)
        )
        self._tile_group(
            layout, "Saved recordings", [self.recording_retention_tile]
        )

        self.streaming_enabled_tile = SettingTile(
            "Real-time transcription preview",
            "Shows text as you speak on the near-cursor overlay using a "
            "dedicated tiny.en preview model. Requires Local Whisper. The "
            "final transcript still uses your selected model and the General "
            "paste and clipboard settings.",
            _design_icon("bolt-green.svg"),
        )
        self.streaming_enabled_check = self.streaming_enabled_tile.checkbox
        self.streaming_enabled_check.toggled.connect(
            self._on_streaming_enabled_changed
        )

        self.streaming_font_size_label = QLabel("Preview font size:")
        self.streaming_font_size_spinbox = NoWheelSpinBox()
        self.streaming_font_size_spinbox.setMinimum(10)
        self.streaming_font_size_spinbox.setMaximum(48)
        self.streaming_font_size_spinbox.setSuffix(" pt")
        self.streaming_font_size_spinbox.setValue(config.STREAMING_OVERLAY_FONT_SIZE)
        self.streaming_font_size_spinbox.setMinimumHeight(36)
        self.streaming_font_size_spinbox.setMinimumWidth(110)
        self.streaming_font_size_spinbox.valueChanged.connect(
            self._on_streaming_font_changed
        )
        self.streaming_enabled_tile.add_body_layout(
            self._spin_form(
                self.streaming_font_size_label, self.streaming_font_size_spinbox
            )
        )
        self._tile_group(layout, "Live preview", [self.streaming_enabled_tile])

    @staticmethod
    def _spin_form(label: QLabel, spinbox: QWidget) -> QFormLayout:
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        form.setFormAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        form.setHorizontalSpacing(16)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint
        )
        label.setObjectName("settingsTileFieldLabel")
        form.addRow(label, spinbox)
        return form

    def _build_cleanup_page(self, layout: QVBoxLayout) -> None:
        self.transcript_cleanup_check = QCheckBox(
            "Clean up transcript with AI after transcription"
        )
        self.transcript_cleanup_check.toggled.connect(
            self._on_cleanup_enabled_changed
        )
        layout.addWidget(self.transcript_cleanup_check)

        model_card = QFrame()
        model_card.setObjectName("cleanupModelSummaryCard")
        model_card_layout = QVBoxLayout(model_card)
        model_card_layout.setContentsMargins(16, 12, 16, 12)
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
            "Open Model Manager → On-demand to choose the cleanup provider "
            "and chat model"
        )
        self.open_model_manager_btn.clicked.connect(
            lambda: self.model_manager_requested.emit("text")
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

        self.cleanup_reasoning_combo = ElidingComboBox()
        self.cleanup_reasoning_combo.addItem("Off", TranscriptCleanupReasoning.OFF)
        self.cleanup_reasoning_combo.addItem("Low", TranscriptCleanupReasoning.LOW)
        self.cleanup_reasoning_combo.addItem(
            "Medium", TranscriptCleanupReasoning.MEDIUM
        )
        self.cleanup_reasoning_combo.addItem("High", TranscriptCleanupReasoning.HIGH)
        self.cleanup_reasoning_combo.setMinimumHeight(40)
        self.cleanup_reasoning_combo.currentIndexChanged.connect(
            self._on_cleanup_reasoning_changed
        )
        layout.addWidget(
            self._field("Thinking level", self.cleanup_reasoning_combo)
        )
        self.cleanup_reasoning_info = self._caption(
            "Requests extra thinking effort from reasoning models "
            "(e.g. o4-mini). Leave Off for regular chat models."
        )
        layout.addWidget(self.cleanup_reasoning_info)

        self.cleanup_prompt_edit = QTextEdit()
        self.cleanup_prompt_edit.setAcceptRichText(False)
        self.cleanup_prompt_edit.setFont(QFont("Segoe UI", 11))
        self.cleanup_prompt_edit.setMinimumHeight(80)
        self.cleanup_prompt_edit.setPlaceholderText(
            "Instructions for how the AI should clean up transcripts…"
        )
        self.cleanup_prompt_edit.installEventFilter(self)
        layout.addWidget(self._field("Cleanup prompt", self.cleanup_prompt_edit))

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

        self.cleanup_prompt_info = self._caption(
            "Runs the selected chat model on each transcript after "
            "transcription. Built-in OpenAI/OpenRouter keys and any custom "
            "endpoint variable come from the environment or .env. Edit the "
            "prompt to change cleanup style (e.g. bullets, email tone)."
        )
        layout.addWidget(self.cleanup_prompt_info)

    def _build_cleanup_rules_page(self, layout: QVBoxLayout) -> None:
        hero = QFrame()
        hero.setObjectName("cleanupRulesHero")
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(14, 10, 14, 10)
        hero_layout.setSpacing(10)
        hero_copy = QVBoxLayout()
        hero_copy.setContentsMargins(0, 0, 0, 0)
        hero_copy.setSpacing(4)
        eyebrow = QLabel("PERSONAL CLEANUP PROFILE")
        eyebrow.setObjectName("cleanupRulesEyebrow")
        hero_copy.addWidget(eyebrow)
        title = QLabel("Make every transcript sound like you")
        title.setObjectName("cleanupRulesTitle")
        title.setWordWrap(True)
        hero_copy.addWidget(title)
        self.cleanup_rules_info = QLabel(
            "Teach preferred spellings, terminology, and formatting. These "
            "instructions apply automatically whenever AI cleanup runs."
        )
        self.cleanup_rules_info.setObjectName("cleanupRulesDescription")
        self.cleanup_rules_info.setWordWrap(True)
        hero_copy.addWidget(self.cleanup_rules_info)
        hero_layout.addLayout(hero_copy, stretch=1)
        hero_mark = QLabel("AI")
        hero_mark.setObjectName("cleanupRulesHeroMark")
        hero_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hero_mark.setFixedSize(44, 44)
        hero_layout.addWidget(hero_mark, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addWidget(hero)

        composer = QFrame()
        composer.setObjectName("cleanupRulesCard")
        composer_layout = QVBoxLayout(composer)
        composer_layout.setContentsMargins(14, 10, 14, 10)
        composer_layout.setSpacing(6)
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
        rule_input_row.setSpacing(8)
        self.cleanup_rule_input = QLineEdit()
        self.cleanup_rule_input.setObjectName("cleanupRuleInput")
        self.cleanup_rule_input.setMinimumHeight(40)
        self.cleanup_rule_input.setPlaceholderText(
            'Try: Always spell my name "Alex Rivera"'
        )
        self.cleanup_rule_input.returnPressed.connect(self._add_cleanup_rule)
        rule_input_row.addWidget(self.cleanup_rule_input, stretch=1)
        self.cleanup_rule_mic_btn = Button("Dictate")
        self.cleanup_rule_mic_btn.setObjectName("cleanupRuleDictateButton")
        self.cleanup_rule_mic_btn.set_base_minimum_size(92, 40)
        self.cleanup_rule_mic_btn.setToolTip(
            "Speak the instruction instead of typing it"
        )
        self.cleanup_rule_mic_btn.clicked.connect(self._toggle_rule_dictation)
        rule_input_row.addWidget(self.cleanup_rule_mic_btn)
        self.cleanup_rule_add_btn = PrimaryButton("+ Add rule")
        self.cleanup_rule_add_btn.setObjectName("cleanupRuleAddButton")
        self.cleanup_rule_add_btn.set_base_minimum_size(112, 40)
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
        library_layout.setContentsMargins(14, 10, 14, 10)
        library_layout.setSpacing(6)
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
        self.cleanup_rules_list.setMinimumHeight(88)
        self.cleanup_rules_list.itemSelectionChanged.connect(
            self._update_cleanup_rule_controls
        )
        self.cleanup_rules_list.itemDoubleClicked.connect(
            lambda _item: self._edit_cleanup_rule()
        )
        library_layout.addWidget(self.cleanup_rules_list)

        self.cleanup_rules_empty = QLabel(
            "No rules yet\n\nAdd your first instruction above to start "
            "building a personal cleanup profile."
        )
        self.cleanup_rules_empty.setObjectName("cleanupRulesEmpty")
        self.cleanup_rules_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cleanup_rules_empty.setWordWrap(True)
        self.cleanup_rules_empty.setMinimumHeight(88)
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
        self.cleanup_rule_edit_btn.set_base_minimum_size(96, 34)
        self.cleanup_rule_edit_btn.clicked.connect(self._edit_cleanup_rule)
        rule_btn_row.addWidget(self.cleanup_rule_edit_btn)
        self.cleanup_rule_delete_btn = Button("Delete")
        self.cleanup_rule_delete_btn.setObjectName("cleanupRuleDeleteButton")
        self.cleanup_rule_delete_btn.set_base_minimum_size(88, 34)
        self.cleanup_rule_delete_btn.clicked.connect(self._delete_cleanup_rule)
        rule_btn_row.addWidget(self.cleanup_rule_delete_btn)
        library_layout.addLayout(rule_btn_row)
        layout.addWidget(library)
        self._update_cleanup_rule_controls()

    def _build_meeting_intelligence_page(self, layout: QVBoxLayout) -> None:
        model_card = QFrame()
        model_card.setObjectName("meetingModelSummaryCard")
        model_card_layout = QVBoxLayout(model_card)
        model_card_layout.setContentsMargins(16, 12, 16, 12)
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
            "Open Model Manager → Meeting Mode to choose transcription, "
            "language, speaker ID, and intelligence models"
        )
        self.open_meeting_model_manager_btn.clicked.connect(
            lambda: self.model_manager_requested.emit("meeting")
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

        self.meeting_past_recall_tile = SettingTile(
            "Search past transcripts",
            "Off by default. The agent can look up names and prior decisions "
            "from stored meetings. Excerpts leave this machine the same way "
            "the current transcript does.",
            _design_icon("stack-purple.svg"),
        )
        self.meeting_past_recall_check = self.meeting_past_recall_tile.checkbox
        self.meeting_past_recall_check.setObjectName("meetingPastRecallCheck")
        self.meeting_past_recall_check.toggled.connect(
            lambda checked: self._persist(
                SettingsKey.MEETING_PAST_RECALL_ENABLED, bool(checked)
            )
        )

        self.meeting_context_folder_tile = SettingTile(
            "Search a knowledge folder",
            "Choose a local folder, for example an Obsidian vault. Matched "
            "excerpts leave this machine the same way the current transcript "
            "does. Images, audio, and video are not read.",
            _design_icon("stack-slate.svg"),
        )
        self.meeting_context_folder_check = (
            self.meeting_context_folder_tile.checkbox
        )
        self.meeting_context_folder_check.setObjectName(
            "meetingContextFolderCheck"
        )
        self.meeting_context_folder_check.toggled.connect(
            lambda checked: self._persist(
                SettingsKey.MEETING_CONTEXT_FOLDER_ENABLED, bool(checked)
            )
        )

        folder_row = QHBoxLayout()
        folder_row.setContentsMargins(0, 0, 0, 0)
        folder_row.setSpacing(8)
        self.meeting_context_folder_path = QLineEdit()
        self.meeting_context_folder_path.setObjectName(
            "meetingContextFolderPath"
        )
        self.meeting_context_folder_path.setPlaceholderText("No folder selected")
        self.meeting_context_folder_path.editingFinished.connect(
            self._on_context_folder_path_finished
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
        self.meeting_context_folder_tile.add_body_layout(folder_row)

        self._tile_group(
            layout,
            "What the agent may search",
            [self.meeting_past_recall_tile, self.meeting_context_folder_tile],
            columns=1,
            intro=(
                "Transcript text and meeting state are sent to the provider. "
                "Cloud intelligence does not upload audio."
            ),
        )

    def _build_meeting_after_page(self, layout: QVBoxLayout) -> None:
        self.meeting_end_redecode_tile = SettingTile(
            "Re-transcribe the full recording",
            "After End, recut the continuous session audio on longer quiet "
            "gaps and run Whisper again. Live capture is unchanged.",
            _design_icon("microphone-blue.svg"),
        )
        self.meeting_end_redecode_check = self.meeting_end_redecode_tile.checkbox
        self.meeting_end_redecode_check.toggled.connect(
            lambda checked: self._persist(
                SettingsKey.MEETING_END_REDECODE, bool(checked)
            )
        )

        self.meeting_end_polish_tile = SettingTile(
            "Clean up the transcript with the LLM",
            "Rewrites the finished transcript for readability. Needs cloud "
            "intelligence on for the meeting.",
            _design_icon("stack-purple.svg"),
        )
        self.meeting_end_polish_check = self.meeting_end_polish_tile.checkbox
        self.meeting_end_polish_check.toggled.connect(
            lambda checked: self._persist(
                SettingsKey.MEETING_END_POLISH, bool(checked)
            )
        )

        self.meeting_end_report_tile = SettingTile(
            "Write the final report",
            "Topic, summary, and cards, generated once live captions finish. "
            "Needs cloud intelligence on for the meeting.",
            _design_icon("check-green.svg"),
        )
        self.meeting_end_report_check = self.meeting_end_report_tile.checkbox
        self.meeting_end_report_check.toggled.connect(
            self._on_end_report_toggled
        )

        self._tile_group(
            layout,
            "After End",
            [
                self.meeting_end_redecode_tile,
                self.meeting_end_polish_tile,
                self.meeting_end_report_tile,
            ],
            intro=(
                "Live captions stay on short chunks so text appears quickly. "
                "These steps run afterward."
            ),
        )

        self.meeting_report_ribbon_tile = SettingTile(
            "Ribbon",
            "Timeline walk. Adds timeline beats and polished minutes, which "
            "is the main token cost.",
            _design_icon("stack-slate.svg"),
        )
        self.meeting_report_brief_tile = SettingTile(
            "Brief",
            "One-page editorial summary. Reuses the same cards as Signal.",
            _design_icon("box-blue.svg"),
        )
        self.meeting_report_signal_tile = SettingTile(
            "Signal",
            "One-screen glance. Reuses the same cards as Brief.",
            _design_icon("bolt-green.svg"),
        )
        self.meeting_report_ribbon_check = self.meeting_report_ribbon_tile.checkbox
        self.meeting_report_brief_check = self.meeting_report_brief_tile.checkbox
        self.meeting_report_signal_check = self.meeting_report_signal_tile.checkbox
        for check, key in (
            (self.meeting_report_ribbon_check, SettingsKey.MEETING_REPORT_RIBBON),
            (self.meeting_report_brief_check, SettingsKey.MEETING_REPORT_BRIEF),
            (self.meeting_report_signal_check, SettingsKey.MEETING_REPORT_SIGNAL),
        ):
            check.toggled.connect(
                lambda checked, setting=key: self._on_report_view_toggled(
                    setting, checked
                )
            )

        self.meeting_report_views_title, self.meeting_report_views_info = (
            self._tile_group(
                layout,
                "Report views",
                [
                    self.meeting_report_ribbon_tile,
                    self.meeting_report_brief_tile,
                    self.meeting_report_signal_tile,
                ],
                intro=(
                    "Each enabled view is generated at End. At least one view "
                    "stays on."
                ),
            )
        )

        self.meeting_report_views_hint = self._caption(
            "At least one view is required. Ribbon stays on."
        )
        self.meeting_report_views_hint.hide()
        layout.addWidget(self.meeting_report_views_hint)

    def _build_meeting_dashboard_page(self, layout: QVBoxLayout) -> None:
        self.meeting_bind_combo = ElidingComboBox()
        self.meeting_bind_combo.setObjectName("meetingBindCombo")
        self.meeting_bind_combo.addItem(
            "Localhost only (this computer)", MeetingServerBind.LOCALHOST
        )
        self.meeting_bind_combo.addItem(
            "Share on local network", MeetingServerBind.LAN
        )
        self.meeting_bind_combo.setMinimumHeight(40)
        self.meeting_bind_combo.currentIndexChanged.connect(
            self._on_meeting_bind_changed
        )
        self.meeting_bind_tile = FieldTile(
            "Who can open the dashboard",
            "Localhost keeps the live dashboard on this computer. Sharing "
            "serves it to other devices on your local network.",
            self.meeting_bind_combo,
            _design_icon("box-blue.svg"),
        )

        self.meeting_bind_warning = WrappedLabel(
            "Sharing on the local network serves the live meeting — running "
            "transcript, notes, insights, and audio playback — over plain, "
            "unencrypted HTTP. Anyone holding the guest link can read and "
            "edit the meeting and play the raw meeting recording."
        )
        self.meeting_bind_warning.setObjectName("meetingBindWarning")
        self.meeting_bind_tile.add_body(self.meeting_bind_warning)
        self._tile_group(layout, "Access", [self.meeting_bind_tile])

        self.meeting_port_spinbox = NoWheelSpinBox()
        self.meeting_port_spinbox.setMinimum(0)
        self.meeting_port_spinbox.setMaximum(65535)
        self.meeting_port_spinbox.setSpecialValueText("Automatic")
        self.meeting_port_spinbox.setValue(config.MEETING_SERVER_PORT)
        self.meeting_port_spinbox.setMinimumHeight(36)
        self.meeting_port_spinbox.setMinimumWidth(110)
        self.meeting_port_spinbox.valueChanged.connect(self._on_meeting_port_changed)
        self.meeting_port_tile = FieldTile(
            "Dashboard port",
            "Automatic lets the meeting server pick a free port each session. "
            "Pick a fixed port only if you need a stable link.",
            self.meeting_port_spinbox,
            _design_icon("bolt-green.svg"),
            compact=True,
        )
        self._tile_group(layout, "Port", [self.meeting_port_tile])

    def _build_api_keys_page(self, layout: QVBoxLayout) -> None:
        self.api_key_combo = ElidingComboBox()
        self.api_key_combo.setObjectName("apiKeyCredentialCombo")
        self.api_key_combo.setMinimumHeight(40)
        self.api_key_combo.currentIndexChanged.connect(
            self._on_api_key_credential_changed
        )
        layout.addWidget(self._field("Credential", self.api_key_combo))

        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        self.api_key_status_icon = QLabel()
        self.api_key_status_icon.setObjectName("apiKeyStatusIcon")
        self.api_key_status_icon.setFixedSize(16, 16)
        self.api_key_status = WrappedLabel("")
        self.api_key_status.setObjectName("apiKeyStatus")
        status_row.addWidget(
            self.api_key_status_icon, alignment=Qt.AlignmentFlag.AlignTop
        )
        status_row.addWidget(self.api_key_status, stretch=1)
        layout.addLayout(status_row)

        # Password echo also makes Qt refuse copy/cut from the field, and the
        # input-method hints keep IMEs and predictive text from retaining it.
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setObjectName("apiKeyInput")
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("Paste a key to save or test")
        self.api_key_edit.setMaxLength(MAX_API_KEY_LEN)
        self.api_key_edit.setInputMethodHints(
            Qt.InputMethodHint.ImhHiddenText
            | Qt.InputMethodHint.ImhSensitiveData
            | Qt.InputMethodHint.ImhNoAutoUppercase
            | Qt.InputMethodHint.ImhNoPredictiveText
        )
        self.api_key_edit.setMinimumHeight(40)
        self.api_key_edit.textChanged.connect(self._update_api_key_controls)
        self.api_key_edit.returnPressed.connect(self._save_api_key)
        self.api_key_show_button = Button("Show")
        self.api_key_show_button.setObjectName("apiKeyShowButton")
        self.api_key_show_button.setCheckable(True)
        self._compact_button(self.api_key_show_button, 80)
        self.api_key_show_button.toggled.connect(self._on_api_key_show_toggled)
        input_row = QWidget()
        input_layout = QHBoxLayout(input_row)
        input_layout.setContentsMargins(0, 0, 0, 0)
        input_layout.setSpacing(8)
        input_layout.addWidget(self.api_key_edit, stretch=1)
        input_layout.addWidget(self.api_key_show_button)
        layout.addWidget(self._field("New key", input_row))

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.api_key_save_button = PrimaryButton("Save key")
        self.api_key_save_button.setObjectName("apiKeySaveButton")
        self._compact_button(self.api_key_save_button, 120)
        self.api_key_save_button.clicked.connect(self._save_api_key)
        self.api_key_test_button = Button("Test")
        self.api_key_test_button.setObjectName("apiKeyTestButton")
        self._compact_button(self.api_key_test_button, 90)
        self.api_key_test_button.clicked.connect(self._test_api_key)
        self.api_key_remove_button = DangerButton("Remove saved key")
        self.api_key_remove_button.setObjectName("apiKeyRemoveButton")
        self._compact_button(self.api_key_remove_button, 160)
        self.api_key_remove_button.clicked.connect(self._remove_api_key)
        buttons.addWidget(self.api_key_save_button)
        buttons.addWidget(self.api_key_test_button)
        buttons.addStretch()
        buttons.addWidget(self.api_key_remove_button)
        layout.addLayout(buttons)

        self.api_key_uses_caption = self._caption("")
        layout.addWidget(self.api_key_uses_caption)
        self.api_key_store_caption = self._caption(self._api_key_store_copy())
        layout.addWidget(self.api_key_store_caption)

    @staticmethod
    def _api_key_store_copy() -> str:
        status = credential_store().status()
        if not status.available:
            return (
                f"{status.backend_name} isn't available on this computer "
                f"({status.reason}), so keys can't be saved from here. Set the "
                "variable in your environment or a .env file instead; "
                "OpenWhisper never falls back to an unprotected file."
            )
        if sys.platform == "win32":
            where = (
                "Control Panel → Credential Manager → Windows Credentials, "
                "under “OpenWhisper”"
            )
        elif sys.platform == "darwin":
            where = "Keychain Access, under “OpenWhisper”"
        else:
            where = (
                "your keyring app (Seahorse or KWalletManager), under "
                "“OpenWhisper”"
            )
        return (
            f"Saved keys are encrypted by {status.backend_name} for your user "
            "account only and are never written to the settings file or the "
            f"log. You can inspect or remove them in {where}. A saved key "
            "takes precedence over the same variable in your environment or "
            ".env file."
        )

    @staticmethod
    def _api_key_entries(settings: dict) -> list:
        """One row per distinct variable; profiles sharing a variable share it."""
        names: Dict[str, list] = {}
        for profile in list_profiles(settings):
            if profile.api_key_env:
                names.setdefault(profile.api_key_env, []).append(profile.name)
        return [
            (env_name, f"{' / '.join(owners)} · {env_name}")
            for env_name, owners in names.items()
        ]

    def _load_api_key_settings(self, settings: dict) -> None:
        current = self.api_key_combo.currentData()
        self.api_key_combo.blockSignals(True)
        self.api_key_combo.clear()
        for env_name, label in self._api_key_entries(settings):
            self.api_key_combo.addItem(label, env_name)
        index = self.api_key_combo.findData(current) if current else -1
        self.api_key_combo.setCurrentIndex(max(0, index))
        self.api_key_combo.blockSignals(False)
        self._clear_api_key_input()
        self.api_key_store_caption.setText(self._api_key_store_copy())
        self._render_api_key_status()

    def focus_api_keys(self, env_name: Optional[str] = None) -> None:
        """Open the API keys destination, optionally on one credential."""
        self.select_destination(API_KEYS)
        if env_name:
            index = self.api_key_combo.findData(env_name)
            if index >= 0:
                self.api_key_combo.setCurrentIndex(index)
        self.api_key_edit.setFocus()

    def _selected_api_key_name(self) -> str:
        return self.api_key_combo.currentData() or ""

    def _api_key_profile(self, env_name: str):
        for profile in list_profiles(settings_manager.load_all_settings()):
            if profile.api_key_env == env_name:
                return profile
        return None

    def _api_key_label(self, env_name: str) -> str:
        profile = self._api_key_profile(env_name)
        return credential_label(profile) if profile is not None else env_name

    def _clear_api_key_input(self) -> None:
        self.api_key_edit.clear()
        self.api_key_show_button.setChecked(False)

    def _render_api_key_status(self) -> None:
        name = self._selected_api_key_name()
        status = credential_store().status()
        tone = "warning"
        if not name:
            self._api_key_source = CredentialSource.NONE
            text = "No endpoint needs an API key."
        else:
            source = credential_source(name)
            self._api_key_source = source
            suffix = mask_key(resolve_credential(name))
            if source == CredentialSource.STORED:
                text = f"Saved in {status.backend_name} · {suffix}"
                if environment_shadowed(name):
                    text += (
                        f". The {name} variable in your environment or .env "
                        "file is ignored while a key is saved here."
                    )
                tone = "success"
            elif source == CredentialSource.ENVIRONMENT:
                text = (
                    f"Using the {name} environment variable · {suffix}. "
                    "Save a key here to use it instead."
                )
                tone = "success"
            elif source == CredentialSource.DOTENV:
                text = (
                    f"Using the .env file · {suffix}. "
                    "Save a key here to use it instead."
                )
                tone = "success"
            else:
                text = "No key set."
        self.api_key_status.setText(text)
        icon = "check-green.svg" if tone == "success" else "info-warning.svg"
        self.api_key_status_icon.setPixmap(_design_icon(icon).pixmap(16, 16))
        self.api_key_status.setProperty("tone", tone)
        self.api_key_status.style().unpolish(self.api_key_status)
        self.api_key_status.style().polish(self.api_key_status)
        self._render_api_key_uses(name)
        self._update_api_key_controls()

    def _render_api_key_uses(self, env_name: str) -> None:
        if env_name == "OPENAI_API_KEY":
            text = (
                "Used for cloud transcription (Whisper, GPT-4o), OpenAI "
                "transcript cleanup and meeting intelligence, and cloud "
                "speaker identification."
            )
        elif env_name == "OPENROUTER_API_KEY":
            text = (
                "Used for transcript cleanup and meeting intelligence through "
                "OpenRouter."
            )
        elif env_name:
            owners = [
                profile.name
                for profile in list_profiles(settings_manager.load_all_settings())
                if profile.api_key_env == env_name
            ]
            text = (
                f"Sent as the API key to {' and '.join(owners)} for transcript "
                "cleanup and meeting intelligence."
            )
        else:
            text = ""
        self.api_key_uses_caption.setText(text)
        self.api_key_uses_caption.setVisible(bool(text))

    def _update_api_key_controls(self, *_args) -> None:
        name = self._selected_api_key_name()
        can_store = bool(name) and credential_store().status().available
        typed = bool(self.api_key_edit.text().strip())
        busy = self._api_key_testing
        self.api_key_edit.setEnabled(bool(name) and not busy)
        self.api_key_show_button.setEnabled(bool(name) and not busy)
        self.api_key_save_button.setEnabled(can_store and typed and not busy)
        self.api_key_test_button.setEnabled(
            bool(name)
            and not busy
            and (typed or self._api_key_source != CredentialSource.NONE)
        )
        self.api_key_remove_button.setEnabled(
            can_store
            and not busy
            and self._api_key_source == CredentialSource.STORED
        )

    def _api_key_rail_value(self) -> str:
        total = self.api_key_combo.count()
        if total == 0:
            return "None needed"
        available = sum(
            1
            for i in range(total)
            if credential_source(self.api_key_combo.itemData(i))
            != CredentialSource.NONE
        )
        return f"{available} of {total} set"

    def _on_api_key_credential_changed(self, _index: int = 0) -> None:
        if self._loading:
            return
        self._clear_api_key_input()
        self.message_label.setText("")
        self._render_api_key_status()

    def _on_api_key_show_toggled(self, checked: bool) -> None:
        self.api_key_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )
        self.api_key_show_button.setText("Hide" if checked else "Show")

    def _notify_api_keys_changed(self) -> None:
        if not self.on_api_keys_changed:
            return
        try:
            self.on_api_keys_changed()
        except Exception as exc:
            logger.error("API key change callback failed: %s", exc)

    def _save_api_key(self) -> None:
        name = self._selected_api_key_name()
        if not name or not self.api_key_save_button.isEnabled():
            return
        try:
            key = validate_api_key(self.api_key_edit.text())
            credential_store().set(name, key)
        except (ValueError, CredentialStoreError) as exc:
            self.message_label.setText(str(exc))
            return
        self._clear_api_key_input()
        self._render_api_key_status()
        self._refresh_rail_values()
        self.message_label.setText(f"{self._api_key_label(name)} saved.")
        self._notify_api_keys_changed()

    def _remove_api_key(self) -> None:
        name = self._selected_api_key_name()
        if not name:
            return
        label = self._api_key_label(name)
        backend = credential_store().status().backend_name
        reply = QMessageBox.question(
            self,
            "Remove saved key",
            f"Remove the saved {label} from {backend}?\n\n"
            f"OpenWhisper will fall back to a {name} environment variable or "
            ".env entry if one exists; otherwise features that need this key "
            "become unavailable.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            removed = credential_store().delete(name)
        except CredentialStoreError as exc:
            self.message_label.setText(str(exc))
            return
        self._clear_api_key_input()
        self._render_api_key_status()
        self._refresh_rail_values()
        self.message_label.setText(
            f"{label} removed." if removed else f"No saved {label} to remove."
        )
        self._notify_api_keys_changed()

    def _test_api_key(self) -> None:
        name = self._selected_api_key_name()
        profile = self._api_key_profile(name)
        if profile is None or self._api_key_testing:
            return
        label = credential_label(profile)
        typed = self.api_key_edit.text()
        if typed.strip():
            try:
                key = validate_api_key(typed)
            except ValueError as exc:
                self.message_label.setText(str(exc))
                return
        else:
            key = resolve_credential(name)
            if not key:
                self.message_label.setText("Paste a key to test.")
                return
        self._api_key_testing = True
        self._update_api_key_controls()
        self.message_label.setText(f"Testing {label}…")

        def worker():
            ok, detail = verify_api_key(profile, key)
            self._api_key_verified.emit(name, ok, detail)

        threading.Thread(target=worker, daemon=True, name="api-key-verify").start()

    def _on_api_key_verified(self, env_name: str, ok: bool, detail: str) -> None:
        self._api_key_testing = False
        self._update_api_key_controls()
        label = self._api_key_label(env_name)
        self.message_label.setText(
            f"{label} works: {detail}" if ok else f"{label} failed: {detail}"
        )

    def _build_hotkeys_page(self, layout: QVBoxLayout) -> None:
        instruction_card = QFrame()
        instruction_card.setObjectName("hotkeyInstructionCard")
        instruction_row = QHBoxLayout(instruction_card)
        instruction_row.setContentsMargins(14, 9, 14, 9)
        instruction_row.setSpacing(12)
        instruction_icon = QLabel()
        instruction_icon.setObjectName("hotkeyInstructionIcon")
        instruction_icon.setFixedSize(18, 18)
        instruction_icon.setPixmap(_design_icon("info-blue.svg").pixmap(16, 16))
        instruction_row.addWidget(
            instruction_icon, alignment=Qt.AlignmentFlag.AlignTop
        )
        if sys.platform == "darwin":
            instruction_text = (
                "Click a shortcut, hold any modifiers, then press its key. "
                "Control+Option combinations are less likely to conflict with "
                "macOS shortcuts."
            )
        elif self._native_wayland:
            instruction_text = (
                "This is a native Wayland session. Wayland blocks reliable "
                "system-wide key observation and injection, so these hotkeys "
                "may work only in XWayland apps. Use the in-app controls or "
                "sign in to an X11 session for global shortcuts and auto-paste."
            )
        elif USE_PYNPUT_BACKEND:
            instruction_text = (
                "Click a shortcut, hold Ctrl, Alt, Shift, or Super, then press "
                "the desired key."
            )
        else:
            instruction_text = (
                "Click a shortcut, then press the desired key combination. "
                "Numpad keys are distinct from the matching regular keys."
            )
        instruction = WrappedLabel(instruction_text)
        instruction.setObjectName("hotkeyInstructionText")
        instruction_row.addWidget(instruction, stretch=1)
        layout.addWidget(instruction_card)

        layout.addWidget(self._hotkey_group_title("Recording"))
        layout.addWidget(
            self._hotkey_shortcut_row(
                "record_toggle",
                "Record hotkey",
                self._record_hotkey_description(RecordingTriggerMode.TOGGLE),
            )
        )
        layout.addWidget(self._build_recording_trigger_mode_row())
        layout.addWidget(
            self._hotkey_shortcut_row(
                "cancel",
                "Cancel",
                "Discard an active recording or interrupt transcription.",
            )
        )
        layout.addWidget(
            self._hotkey_shortcut_row(
                "meeting_toggle",
                "Meeting Mode",
                "Start or end Meeting Mode. Leave empty to disable this shortcut.",
                optional=True,
            )
        )

        layout.addWidget(self._hotkey_group_title("OpenWhisper"))
        layout.addWidget(
            self._hotkey_shortcut_row(
                "enable_disable",
                "Enable or disable hotkeys",
                "Temporarily enable or disable every OpenWhisper hotkey.",
            )
        )
        layout.addWidget(
            self._hotkey_shortcut_row(
                "minimize_tray",
                "Minimize to tray",
                "Hide the main window in the system tray.",
            )
        )

        actions = QHBoxLayout()
        actions.addStretch()
        reset_button = Button("Reset to defaults")
        reset_button.setObjectName("hotkeyResetButton")
        self._compact_button(reset_button, 150)
        reset_button.clicked.connect(self._confirm_reset_hotkeys)
        actions.addWidget(reset_button)
        layout.addLayout(actions)

    def _build_recording_trigger_mode_row(self) -> QWidget:
        self.record_mode_combo = ElidingComboBox()
        self.record_mode_combo.setObjectName("recordModeCombo")
        self.record_mode_combo.addItem(
            "Toggle — press to start and stop", RecordingTriggerMode.TOGGLE
        )
        self.record_mode_combo.addItem(
            "Push and hold — release to stop", RecordingTriggerMode.PUSH_HOLD
        )
        self.record_mode_combo.setMinimumHeight(40)
        self.record_mode_combo.currentIndexChanged.connect(
            self._on_recording_trigger_mode_changed
        )
        return self._field("How the record hotkey activates", self.record_mode_combo)

    def _on_recording_trigger_mode_changed(self, _index: int = 0) -> None:
        mode = self.record_mode_combo.currentData()
        if mode is None:
            return
        if not self._persist(SettingsKey.RECORDING_TRIGGER_MODE, mode):
            return
        self._update_record_row_description(mode)
        if self.on_recording_trigger_mode_changed:
            self.on_recording_trigger_mode_changed(mode)

    @staticmethod
    def _record_hotkey_description(mode: str) -> str:
        if mode == RecordingTriggerMode.PUSH_HOLD:
            return (
                "Hold to record; release to stop and transcribe. "
                "Quick taps are canceled."
            )
        return "Start recording when idle; stop and transcribe while recording."

    def _update_record_row_description(self, mode: str) -> None:
        label = self.hotkey_row_descriptions.get("record_toggle")
        if label is not None:
            label.setText(self._record_hotkey_description(mode))

    @staticmethod
    def _hotkey_group_title(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("hotkeyGroupTitle")
        return label

    def _hotkey_shortcut_row(
        self,
        key: str,
        title: str,
        description: str,
        *,
        optional: bool = False,
    ) -> QFrame:
        card = QFrame()
        card.setObjectName("hotkeyShortcutCard")
        row = QHBoxLayout(card)
        row.setContentsMargins(14, 8, 12, 8)
        row.setSpacing(16)

        copy = QVBoxLayout()
        copy.setSpacing(2)
        name = QLabel(title)
        name.setObjectName("hotkeyShortcutName")
        copy.addWidget(name)
        detail = WrappedLabel(description)
        detail.setObjectName("hotkeyShortcutDescription")
        copy.addWidget(detail)
        self.hotkey_row_descriptions[key] = detail
        row.addLayout(copy, stretch=1)

        field = HotkeyCaptureInput()
        field.setProperty("optional", optional)
        field.setMinimumWidth(190)
        field.setMaximumWidth(220)
        field.capture_requested.connect(
            lambda key=key, field=field: self._start_hotkey_capture(key, field)
        )
        self.hotkey_inputs[key] = field
        row.addWidget(field, alignment=Qt.AlignmentFlag.AlignVCenter)

        if optional:
            clear_button = Button("Clear")
            clear_button.setObjectName("hotkeyClearButton")
            self._compact_button(clear_button, 68)
            clear_button.clicked.connect(self._clear_meeting_hotkey)
            self.clear_meeting_hotkey_button = clear_button
            row.addWidget(clear_button, alignment=Qt.AlignmentFlag.AlignVCenter)
        return card

    def _build_advanced_page(self, layout: QVBoxLayout) -> None:
        self.developer_mode_check = QCheckBox("Developer mode")
        self.developer_mode_check.setObjectName("developerModeCheck")
        self.developer_mode_check.toggled.connect(self._on_developer_mode_changed)
        layout.addWidget(self.developer_mode_check)
        layout.addWidget(
            self._caption(
                "Unlocks a Load demo meeting control on the Meeting Mode tab. "
                "The demo opens the dashboard with a fake transcript so you "
                "can test end-of-meeting cleanup and the final report without "
                "recording a real meeting."
            )
        )

        self.hf_policy_combo = ElidingComboBox()
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
        self.hf_policy_combo.setMinimumHeight(40)
        self.hf_policy_combo.currentIndexChanged.connect(self._on_hf_policy_changed)
        layout.addWidget(
            self._field(
                "When a model is missing from this computer",
                self.hf_policy_combo,
            )
        )
        layout.addWidget(
            self._caption(
                "Models already on this computer always load locally without "
                "any network checks. Hugging Face is only contacted to "
                "download a missing model, and only when this policy (or a "
                "one-time approval) allows it. An external HF_HUB_OFFLINE=1 "
                "environment variable disables downloads entirely."
            )
        )

    def select_destination(self, key: str) -> None:
        """Show one rail destination by stable key."""
        if key in self._pages:
            self.rail.select(key)

    def focus_hf_policy(self) -> None:
        """Open Advanced with the Hugging Face policy control focused."""
        self.select_destination(ADVANCED)
        self.hf_policy_combo.setFocus()

    def _on_destination_changed(self, key: str) -> None:
        if key != HOTKEYS:
            self._cancel_hotkey_capture()
        page = self._pages.get(key)
        if page is None:
            return
        self.stack.setCurrentWidget(page)
        title, subtitle = self._headings[key]
        self.page_title.setText(title)
        self.page_subtitle.setText(subtitle)

    def refresh(self) -> None:
        """Reload persisted values and rail captions."""
        self._cancel_hotkey_capture()
        self._loading = True
        try:
            self._load_settings()
        finally:
            self._loading = False
        self._refresh_rail_values()

    def eventFilter(self, obj, event):
        if (
            obj is getattr(self, "cleanup_prompt_edit", None)
            and event.type() == QEvent.Type.FocusOut
        ):
            self._persist_cleanup_prompt()
        return super().eventFilter(obj, event)

    def closeEvent(self, event):
        self._cancel_hotkey_capture()
        self._persist_cleanup_prompt()
        self._release_rule_recorder()
        super().closeEvent(event)

    def _persist(self, key: str, value, *, message: str = "") -> bool:
        if self._loading:
            return False
        try:
            if settings_manager.get(key, object()) == value:
                self._refresh_rail_values()
                return True
            settings_manager.save_setting(key, value)
        except Exception as exc:
            logger.error("Couldn't save setting %s: %s", key, exc)
            self.message_label.setText(f"Couldn't save setting: {exc}")
            return False
        if message:
            self.message_label.setText(message)
        self._refresh_rail_values()
        return True

    def _persist_many(self, updates: dict, drops: tuple = ()) -> bool:
        if self._loading:
            return False
        try:
            settings_manager.update_settings(updates, remove=tuple(drops))
        except Exception as exc:
            logger.error("Couldn't save settings: %s", exc)
            self.message_label.setText(f"Couldn't save settings: {exc}")
            return False
        self._refresh_rail_values()
        return True

    def _refresh_rail_values(self) -> None:
        if self.auto_paste_check.isChecked():
            general = "Auto-paste on"
        elif self.copy_clipboard_check.isChecked():
            general = "Clipboard"
        else:
            general = "Manual"
        self.rail.set_value(GENERAL, general)
        self.rail.set_value(
            RECORDING,
            self.audio_device_combo.currentText() or "System Default",
        )
        if self.transcript_cleanup_check.isChecked():
            model = self.cleanup_model_summary.text().split(" · ")[-1].strip()
            self.rail.set_value(CLEANUP, f"On · {model}" if model else "On")
        else:
            self.rail.set_value(CLEANUP, "Off")
        rule_count = self.cleanup_rules_list.count()
        self.rail.set_value(
            CLEANUP_RULES,
            "No rules" if rule_count == 0 else f"{rule_count} rules",
        )
        meeting_lines = self.meeting_model_summary.text().splitlines()
        intel = next(
            (line for line in meeting_lines if " · " in line and "Whisper" not in line),
            "",
        )
        if not intel:
            intel = meeting_lines[2] if len(meeting_lines) > 2 else "Meeting models"
        self.rail.set_value(MEETING_INTELLIGENCE, intel)
        if self.meeting_end_report_check.isChecked():
            after = "Report"
        elif self.meeting_end_polish_check.isChecked():
            after = "Polish"
        elif self.meeting_end_redecode_check.isChecked():
            after = "Re-decode"
        else:
            after = "Off"
        self.rail.set_value(MEETING_AFTER, after)
        bind = self.meeting_bind_combo.currentData()
        port = self.meeting_port_spinbox.value()
        if bind == MeetingServerBind.LAN:
            port_label = "auto" if port == 0 else str(port)
            self.rail.set_value(MEETING_DASHBOARD, f"LAN · {port_label}")
        else:
            self.rail.set_value(MEETING_DASHBOARD, "Localhost")
        self.rail.set_value(API_KEYS, self._api_key_rail_value())
        self.rail.set_value(HOTKEYS, self._hotkey_rail_value())
        policy = self.hf_policy_combo.currentData()
        self.rail.set_value(
            ADVANCED, _HF_POLICY_RAIL.get(policy, "Ask first")
        )

    def _hotkey_rail_value(self) -> str:
        hotkeys = self.current_hotkeys
        if not hotkeys:
            try:
                hotkeys = settings_manager.load_hotkey_settings()
            except Exception:
                hotkeys = config.DEFAULT_HOTKEYS
        raw = (hotkeys or {}).get("record_toggle", "")
        return format_hotkey_display(raw) or "Not set"

    def _on_update_check_toggled(self, checked: bool) -> None:
        self.update_notify_tile.setEnabled(bool(checked))
        self._persist(SettingsKey.UPDATE_CHECK_ENABLED, bool(checked))

    def _on_audio_device_changed(self, _index: int = 0) -> None:
        if self._loading:
            return
        device_id = self.audio_device_combo.currentData()
        updates = {}
        drops = ()
        if device_id is None:
            drops = (SettingsKey.AUDIO_INPUT_DEVICE,)
        else:
            updates[SettingsKey.AUDIO_INPUT_DEVICE] = device_id
        if not self._persist_many(updates, drops=drops):
            return
        if self.on_audio_device_changed:
            self.on_audio_device_changed(device_id)

    def _on_retention_mode_changed(self, _index: int = 0) -> None:
        self._update_recording_retention_ui()
        if self._persist(
            SettingsKey.RECORDING_RETENTION_MODE,
            self.recording_retention_combo.currentData(),
        ):
            self._apply_retention_limit()

    def _on_max_recordings_changed(self, _value: int = 0) -> None:
        if self._persist(
            SettingsKey.MAX_SAVED_RECORDINGS,
            self.max_recordings_spinbox.value(),
        ):
            self._apply_retention_limit()

    def _apply_retention_limit(self) -> None:
        try:
            history_manager.set_max_recordings(
                resolve_max_saved_recordings(settings_manager.load_all_settings())
            )
        except Exception as exc:
            logger.error("Couldn't apply recording retention: %s", exc)

    def _update_recording_retention_ui(self) -> None:
        is_custom = (
            self.recording_retention_combo.currentData()
            == RecordingRetentionMode.CUSTOM
        )
        self.max_recordings_label.setEnabled(is_custom)
        self.max_recordings_spinbox.setEnabled(is_custom)

    def _on_streaming_enabled_changed(self, checked: bool) -> None:
        self._update_streaming_font_ui()
        if not self._persist_many(
            {SettingsKey.STREAMING_ENABLED: bool(checked)},
            drops=(
                SettingsKey.STREAMING_OVERLAY_ENABLED,
                SettingsKey.STREAMING_PASTE_ENABLED,
                "streaming_tiny_model_enabled",
                "live_typing_enabled",
            ),
        ):
            return
        if self.on_streaming_settings_changed:
            self.on_streaming_settings_changed()

    def _on_streaming_font_changed(self, _value: int = 0) -> None:
        if not self._persist(
            SettingsKey.STREAMING_OVERLAY_FONT_SIZE,
            self.streaming_font_size_spinbox.value(),
        ):
            return
        if self.on_streaming_font_changed:
            self.on_streaming_font_changed()

    def _update_streaming_font_ui(self) -> None:
        enabled = self.streaming_enabled_check.isChecked()
        self.streaming_font_size_label.setEnabled(enabled)
        self.streaming_font_size_spinbox.setEnabled(enabled)

    def _on_cleanup_enabled_changed(self, checked: bool) -> None:
        self._update_cleanup_prompt_ui()
        if not self._persist(
            SettingsKey.TRANSCRIPT_CLEANUP_ENABLED, bool(checked)
        ):
            return
        if self.on_cleanup_changed:
            self.on_cleanup_changed()

    def _on_cleanup_reasoning_changed(self, _index: int = 0) -> None:
        self._persist(
            SettingsKey.TRANSCRIPT_CLEANUP_REASONING,
            self.cleanup_reasoning_combo.currentData(),
        )

    def _persist_cleanup_prompt(self) -> None:
        prompt_text = self.cleanup_prompt_edit.toPlainText().strip()
        stored = prompt_text or config.TRANSCRIPT_CLEANUP_PROMPT
        if stored == self._saved_cleanup_prompt:
            return
        if self._persist(SettingsKey.TRANSCRIPT_CLEANUP_PROMPT, stored):
            self._saved_cleanup_prompt = stored

    def _on_end_report_toggled(self, checked: bool) -> None:
        self._update_report_views_enabled()
        self._persist(SettingsKey.MEETING_END_REPORT, bool(checked))

    def _on_report_view_toggled(self, key: str, _checked: bool) -> None:
        self._guard_report_views()
        actual = {
            SettingsKey.MEETING_REPORT_RIBBON: (
                self.meeting_report_ribbon_check.isChecked()
            ),
            SettingsKey.MEETING_REPORT_BRIEF: (
                self.meeting_report_brief_check.isChecked()
            ),
            SettingsKey.MEETING_REPORT_SIGNAL: (
                self.meeting_report_signal_check.isChecked()
            ),
        }
        self._persist(key, actual[key])

    def _on_meeting_bind_changed(self, _index: int = 0) -> None:
        self._update_meeting_bind_ui()
        self._persist(
            SettingsKey.MEETING_SERVER_BIND,
            self.meeting_bind_combo.currentData(),
        )

    def _on_meeting_port_changed(self, value: int) -> None:
        self._persist(SettingsKey.MEETING_SERVER_PORT, int(value))

    def _on_developer_mode_changed(self, checked: bool) -> None:
        if not self._persist(SettingsKey.DEVELOPER_MODE, bool(checked)):
            return
        if self.on_developer_mode_changed:
            self.on_developer_mode_changed(bool(checked))

    def _on_hf_policy_changed(self, _index: int = 0) -> None:
        policy = self.hf_policy_combo.currentData()
        if not self._persist_many(
            {SettingsKey.HF_ACCESS_POLICY: policy},
            drops=(SettingsKey.HF_HUB_OFFLINE,),
        ):
            return
        if self.on_hf_policy_changed:
            self.on_hf_policy_changed(policy)

    def _on_context_folder_path_finished(self) -> None:
        path = resolve_meeting_context_folder_path({
            SettingsKey.MEETING_CONTEXT_FOLDER_PATH: (
                self.meeting_context_folder_path.text()
            ),
        })
        if path != self.meeting_context_folder_path.text():
            blocker = self.meeting_context_folder_path.blockSignals(True)
            self.meeting_context_folder_path.setText(path)
            self.meeting_context_folder_path.blockSignals(blocker)
        self._persist(SettingsKey.MEETING_CONTEXT_FOLDER_PATH, path)

    def _refresh_cleanup_model_summary(self) -> None:
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

    def _refresh_meeting_model_summary(self) -> None:
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

    def _update_cleanup_prompt_ui(self) -> None:
        enabled = self.transcript_cleanup_check.isChecked()
        for widget in (
            self.cleanup_reasoning_combo,
            self.cleanup_reasoning_info,
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

    def _open_cleanup_prompt_editor(self) -> None:
        dialog = CleanupPromptDialog(self.cleanup_prompt_edit.toPlainText(), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.cleanup_prompt_edit.setPlainText(dialog.prompt_text())
            self._persist_cleanup_prompt()

    def _reset_cleanup_prompt(self) -> None:
        self.cleanup_prompt_edit.setPlainText(config.TRANSCRIPT_CLEANUP_PROMPT)
        self._persist_cleanup_prompt()

    def _staged_cleanup_rules(self) -> list:
        return [
            self.cleanup_rules_list.item(i).text()
            for i in range(self.cleanup_rules_list.count())
        ]

    def _persist_cleanup_rules(self) -> None:
        self._persist(
            SettingsKey.TRANSCRIPT_CLEANUP_RULES, self._staged_cleanup_rules()
        )

    def _update_cleanup_rule_controls(self) -> None:
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
        self.cleanup_rule_mic_btn.setEnabled(
            enabled
            and not self._rule_polishing
            and self._rule_dictation_state != "transcribing"
        )
        has_selection = bool(self.cleanup_rules_list.selectedItems())
        self.cleanup_rule_edit_btn.setEnabled(enabled and has_selection)
        self.cleanup_rule_delete_btn.setEnabled(enabled and has_selection)

    def _add_cleanup_rule(self) -> None:
        self._polish_cleanup_rule(self.cleanup_rule_input.text())

    def _polish_cleanup_rule(self, raw: str) -> None:
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
                pass

        threading.Thread(
            target=worker, name="cleanup-rule-polish", daemon=True
        ).start()

    def _on_cleanup_rule_polished(self, raw: str, polished: str, error: str) -> None:
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
        self._persist_cleanup_rules()

    def _edit_cleanup_rule(self) -> None:
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
            self._persist_cleanup_rules()

    def _delete_cleanup_rule(self) -> None:
        for item in self.cleanup_rules_list.selectedItems():
            self.cleanup_rules_list.takeItem(self.cleanup_rules_list.row(item))
        self._update_cleanup_rule_controls()
        self._persist_cleanup_rules()

    def _toggle_rule_dictation(self) -> None:
        if self._rule_dictation_state == "recording":
            self._stop_rule_dictation()
            return
        if self._rule_dictation_state != "idle" or self._rule_polishing:
            return
        if self.on_dictation_transcribe is None:
            self.cleanup_rule_status.setText("Dictation is unavailable.")
            return
        if self.get_meeting_active is not None and self.get_meeting_active():
            self.cleanup_rule_status.setText(
                "Meeting Mode is active — end the meeting to dictate a rule."
            )
            return

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

    def _stop_rule_dictation(self) -> None:
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
                pass

        threading.Thread(
            target=worker, name="rule-dictation", daemon=True
        ).start()

    def _on_rule_dictation_finished(self, text: str, error: str) -> None:
        self._rule_dictation_state = "idle"
        self.cleanup_rule_mic_btn.setText("Dictate")
        self._update_cleanup_rule_controls()
        if error:
            self.cleanup_rule_status.setText(error)
            return
        current = self.cleanup_rule_input.text().strip()
        raw = f"{current} {text}".strip() if current else text
        self._polish_cleanup_rule(raw)

    def _release_rule_recorder(self, *_args) -> None:
        self._rule_dictation_timer.stop()
        if self._rule_recorder is not None:
            self._rule_recorder.cleanup()
            self._rule_recorder = None

    def _update_meeting_bind_ui(self) -> None:
        is_lan = self.meeting_bind_combo.currentData() == MeetingServerBind.LAN
        self.meeting_bind_warning.setVisible(is_lan)

    def _report_view_checks(self):
        return (
            self.meeting_report_ribbon_check,
            self.meeting_report_brief_check,
            self.meeting_report_signal_check,
        )

    def _update_report_views_enabled(self) -> None:
        enabled = self.meeting_end_report_check.isChecked()
        self.meeting_report_views_title.setEnabled(enabled)
        self.meeting_report_views_info.setEnabled(enabled)
        for tile in (
            self.meeting_report_ribbon_tile,
            self.meeting_report_brief_tile,
            self.meeting_report_signal_tile,
        ):
            tile.setEnabled(enabled)
        if not enabled:
            self.meeting_report_views_hint.hide()

    def _guard_report_views(self) -> None:
        if any(check.isChecked() for check in self._report_view_checks()):
            self.meeting_report_views_hint.hide()
            return
        blocker = self.meeting_report_ribbon_check.blockSignals(True)
        self.meeting_report_ribbon_check.setChecked(True)
        self.meeting_report_ribbon_check.blockSignals(blocker)
        self.meeting_report_views_hint.show()

    def _browse_context_folder(self) -> None:
        current = self.meeting_context_folder_path.text().strip()
        start = current if os.path.isdir(current) else os.path.expanduser("~")
        chosen = QFileDialog.getExistingDirectory(
            self, "Select knowledge folder", start,
        )
        if not chosen:
            return
        path = os.path.normpath(chosen)
        self._loading = True
        try:
            self.meeting_context_folder_path.setText(path)
            self.meeting_context_folder_check.setChecked(True)
        finally:
            self._loading = False
        self._persist_many({
            SettingsKey.MEETING_CONTEXT_FOLDER_ENABLED: True,
            SettingsKey.MEETING_CONTEXT_FOLDER_PATH: path,
        })

    def _clear_context_folder(self) -> None:
        self._loading = True
        try:
            self.meeting_context_folder_path.clear()
            self.meeting_context_folder_check.setChecked(False)
        finally:
            self._loading = False
        self._persist_many({
            SettingsKey.MEETING_CONTEXT_FOLDER_ENABLED: False,
            SettingsKey.MEETING_CONTEXT_FOLDER_PATH: "",
        })

    def _load_meeting_settings(self, settings: dict) -> None:
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

    def _populate_audio_devices(self) -> None:
        self.audio_device_combo.clear()
        self.audio_device_combo.addItem("System Default", None)
        devices = AudioRecorder.get_input_devices()
        for device_id, device_name in devices:
            self.audio_device_combo.addItem(device_name, device_id)

    def _start_hotkey_capture(
        self, key: str, input_field: HotkeyCaptureInput
    ) -> None:
        self._cancel_hotkey_capture()
        self.capturing = key
        self.current_hotkey_input = input_field
        input_field.setText("Press keys…")
        input_field.set_capturing(True)

        thread = HotkeyCaptureThread(self)
        self.capture_thread = thread
        thread.finished.connect(thread.deleteLater)
        thread.captured.connect(
            lambda hotkey, thread=thread: self._on_hotkey_captured(
                thread, hotkey
            )
        )
        thread.failed.connect(
            lambda message, thread=thread: self._on_hotkey_capture_failed(
                thread, message
            )
        )
        logger.info("Capturing hotkey for %s", key)
        thread.start()

    def _on_hotkey_captured(
        self, thread: HotkeyCaptureThread, hotkey: str
    ) -> None:
        if thread is not self.capture_thread or self.capturing is None:
            return
        key = self.capturing
        self._finish_hotkey_capture(thread)
        updated = self.current_hotkeys.copy()
        updated[key] = hotkey
        label = {
            "record_toggle": "Recording",
            "cancel": "Cancel",
            "meeting_toggle": "Meeting Mode",
            "enable_disable": "Enable/disable",
            "minimize_tray": "Minimize to tray",
        }.get(key, "Shortcut")
        self._apply_hotkey_settings(updated, f"{label} hotkey updated.")

    def _on_hotkey_capture_failed(
        self, thread: HotkeyCaptureThread, message: str
    ) -> None:
        if thread is not self.capture_thread:
            return
        logger.warning(message)
        self._finish_hotkey_capture(thread)
        self._update_hotkey_displays()
        QMessageBox.warning(self, "Hotkey Capture Failed", message)

    def _finish_hotkey_capture(self, thread: HotkeyCaptureThread) -> None:
        if self.current_hotkey_input is not None:
            self.current_hotkey_input.set_capturing(False)
        self.capturing = None
        self.current_hotkey_input = None
        if thread is self.capture_thread:
            self.capture_thread = None

    def _cancel_hotkey_capture(self) -> None:
        thread = self.capture_thread
        if thread is not None:
            try:
                thread.captured.disconnect()
                thread.failed.disconnect()
            except (RuntimeError, TypeError):
                pass
            if thread.isRunning():
                thread.stop()
                thread.wait(1000)
        if self.current_hotkey_input is not None:
            self.current_hotkey_input.set_capturing(False)
        self.capture_thread = None
        self.capturing = None
        self.current_hotkey_input = None
        if self.hotkey_inputs:
            self._update_hotkey_displays()

    def _apply_hotkey_settings(
        self, hotkeys: Dict[str, str], message: str
    ) -> bool:
        updated = config.DEFAULT_HOTKEYS.copy()
        updated.update(hotkeys)
        try:
            if self.on_hotkeys_changed:
                self.on_hotkeys_changed(updated.copy())
            else:
                settings_manager.save_hotkey_settings(updated)
        except Exception as exc:
            logger.error("Couldn't save hotkeys: %s", exc)
            self.message_label.setText(f"Couldn't save hotkeys: {exc}")
            self._update_hotkey_displays()
            return False
        self.current_hotkeys = updated
        self._update_hotkey_displays()
        self.message_label.setText(message)
        self._refresh_rail_values()
        return True

    def _clear_meeting_hotkey(self) -> None:
        self._cancel_hotkey_capture()
        updated = self.current_hotkeys.copy()
        updated["meeting_toggle"] = ""
        self._apply_hotkey_settings(updated, "Meeting Mode hotkey cleared.")

    def _confirm_reset_hotkeys(self) -> None:
        self._cancel_hotkey_capture()
        answer = QMessageBox.question(
            self,
            "Reset hotkeys?",
            "Replace every shortcut with the platform defaults?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._apply_hotkey_settings(
                config.DEFAULT_HOTKEYS.copy(),
                "Hotkeys reset to platform defaults.",
            )

    def _load_hotkey_settings(self) -> None:
        hotkeys = config.DEFAULT_HOTKEYS.copy()
        try:
            hotkeys.update(settings_manager.load_hotkey_settings() or {})
        except Exception as exc:
            logger.warning("Couldn't load hotkeys: %s", exc)
        self.current_hotkeys = hotkeys
        self._update_hotkey_displays()

    def _update_hotkey_displays(self) -> None:
        for key, input_field in self.hotkey_inputs.items():
            input_field.setText(
                format_hotkey_display(self.current_hotkeys.get(key, ""))
            )

    def _load_settings(self) -> None:
        self._load_hotkey_settings()
        try:
            settings = settings_manager.load_all_settings()

            self.auto_paste_check.setChecked(
                settings.get(SettingsKey.AUTO_PASTE, True)
            )
            self.copy_clipboard_check.setChecked(
                settings.get(SettingsKey.COPY_CLIPBOARD, True)
            )
            self.transcript_cleanup_check.setChecked(
                settings.get(
                    SettingsKey.TRANSCRIPT_CLEANUP_ENABLED,
                    config.TRANSCRIPT_CLEANUP_ENABLED,
                )
            )
            prompt = resolve_transcript_cleanup_prompt(settings)
            self.cleanup_prompt_edit.setPlainText(prompt)
            self._saved_cleanup_prompt = prompt
            self.cleanup_rules_list.clear()
            self.cleanup_rules_list.addItems(
                resolve_transcript_cleanup_rules(settings)
            )

            self._refresh_cleanup_model_summary()
            reasoning_index = self.cleanup_reasoning_combo.findData(
                resolve_transcript_cleanup_reasoning(settings)
            )
            self.cleanup_reasoning_combo.setCurrentIndex(max(0, reasoning_index))
            self._update_cleanup_prompt_ui()
            self.minimize_tray_check.setChecked(
                self._tray_available
                and settings.get(SettingsKey.MINIMIZE_TRAY, True)
            )
            self.update_check_check.setChecked(
                resolve_update_check_enabled(settings)
            )
            self.update_notify_check.setChecked(
                resolve_update_notify_enabled(settings)
            )
            self.update_notify_tile.setEnabled(
                self.update_check_check.isChecked()
            )

            retention_mode = settings.get(
                SettingsKey.RECORDING_RETENTION_MODE,
                RecordingRetentionMode.CUSTOM,
            )
            retention_index = self.recording_retention_combo.findData(
                retention_mode
            )
            if retention_index < 0:
                retention_index = self.recording_retention_combo.findData(
                    RecordingRetentionMode.CUSTOM
                )
            self.recording_retention_combo.setCurrentIndex(
                max(0, retention_index)
            )
            max_recordings = settings.get(
                SettingsKey.MAX_SAVED_RECORDINGS,
                config.MAX_SAVED_RECORDINGS,
            )
            try:
                self.max_recordings_spinbox.setValue(max(1, int(max_recordings)))
            except (TypeError, ValueError):
                self.max_recordings_spinbox.setValue(config.MAX_SAVED_RECORDINGS)
            self._update_recording_retention_ui()

            streaming_enabled = settings.get(
                SettingsKey.STREAMING_ENABLED, config.STREAMING_ENABLED
            )
            self.streaming_enabled_check.setChecked(streaming_enabled)
            self.streaming_font_size_spinbox.setValue(
                resolve_streaming_overlay_font_size(settings)
            )
            self._update_streaming_font_ui()

            self._load_meeting_settings(settings)
            self._load_api_key_settings(settings)
            self.developer_mode_check.setChecked(resolve_developer_mode(settings))

            policy = settings_manager.load_hf_access_policy()
            policy_index = self.hf_policy_combo.findData(policy)
            self.hf_policy_combo.setCurrentIndex(max(0, policy_index))

            trigger_mode = resolve_recording_trigger_mode(settings)
            self.record_mode_combo.setCurrentIndex(
                max(0, self.record_mode_combo.findData(trigger_mode))
            )
            self._update_record_row_description(trigger_mode)

            saved_device_id = settings.get(SettingsKey.AUDIO_INPUT_DEVICE)
            if saved_device_id is not None:
                for i in range(self.audio_device_combo.count()):
                    if self.audio_device_combo.itemData(i) == saved_device_id:
                        self.audio_device_combo.setCurrentIndex(i)
                        break

            logger.info("Settings loaded successfully")
        except Exception as e:
            logger.error("Failed to load settings: %s", e)
            self.auto_paste_check.setChecked(True)
            self.copy_clipboard_check.setChecked(True)
            self.transcript_cleanup_check.setChecked(
                config.TRANSCRIPT_CLEANUP_ENABLED
            )
            self.cleanup_prompt_edit.setPlainText(config.TRANSCRIPT_CLEANUP_PROMPT)
            self._saved_cleanup_prompt = config.TRANSCRIPT_CLEANUP_PROMPT
            self.cleanup_rules_list.clear()
            self.cleanup_model_summary.setText(
                f"OpenRouter · {config.TRANSCRIPT_CLEANUP_OPENROUTER_MODEL}"
            )
            self.cleanup_reasoning_combo.setCurrentIndex(0)
            self._update_cleanup_prompt_ui()
            self.minimize_tray_check.setChecked(self._tray_available)
            self.update_check_check.setChecked(config.UPDATE_CHECK_ENABLED)
            self.update_notify_check.setChecked(config.UPDATE_NOTIFY_ENABLED)
            self.update_notify_tile.setEnabled(
                self.update_check_check.isChecked()
            )
            retention_index = self.recording_retention_combo.findData(
                RecordingRetentionMode.CUSTOM
            )
            self.recording_retention_combo.setCurrentIndex(max(0, retention_index))
            self.max_recordings_spinbox.setValue(config.MAX_SAVED_RECORDINGS)
            self._update_recording_retention_ui()
            self.streaming_enabled_check.setChecked(config.STREAMING_ENABLED)
            self.streaming_font_size_spinbox.setValue(
                config.STREAMING_OVERLAY_FONT_SIZE
            )
            self._update_streaming_font_ui()
            self._load_meeting_settings({})
            self._load_api_key_settings({})
            self.developer_mode_check.setChecked(config.DEVELOPER_MODE)
            self.hf_policy_combo.setCurrentIndex(
                max(0, self.hf_policy_combo.findData(HuggingFaceAccessPolicy.ASK))
            )
            self.record_mode_combo.setCurrentIndex(
                max(
                    0,
                    self.record_mode_combo.findData(RecordingTriggerMode.TOGGLE),
                )
            )
            self._update_record_row_description(RecordingTriggerMode.TOGGLE)

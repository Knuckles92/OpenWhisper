import importlib
import itertools
import logging
import os
import sys
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from PyQt6.QtCore import QEvent, QObject, QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QMenu,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from config import config
from services.components import component_coordinator
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
from services.typesafe import CREDENTIAL_ENV as TYPESAFE_CREDENTIAL_ENV
from services.typesafe import OPENROUTER_ROUTE as JEV_OPENROUTER_ROUTE
from services.typesafe import TEXT_MODEL_TIMEOUT_S
from services.typesafe import TYPESAFE_ROUTE as JEV_TYPESAFE_ROUTE
from services.typesafe import key_present as typesafe_key_present
from services.typesafe import route_from_settings as typesafe_route_from_settings
from services.typesafe import route_key as typesafe_route_key
from services.typesafe import text_model_route as typesafe_text_model_route
from services.typesafe import verify_key as typesafe_verify_key
from services.format_utils import format_file_size
from services.history_manager import history_manager
from services.hotkey_manager import USE_PYNPUT_BACKEND, format_hotkey_display, parse_hotkey
from services.recorder import AudioRecorder
from services.settings import (
    LEGACY_STREAMING_KEYS,
    HuggingFaceAccessPolicy,
    MeetingServerBind,
    RecordingOverlayLook,
    StreamingOverlayLayout,
    RecordingRetentionMode,
    RecordingTriggerMode,
    SettingsKey,
    TypeSafeProvider,
    TypeSafeTextModel,
    UiFontScale,
    UiTheme,
    WalkthroughCopyFormat,
    WalkthroughPageSize,
    resolve_developer_mode,
    resolve_max_saved_recordings,
    resolve_max_saved_recordings_bytes,
    resolve_meeting_context_folder_enabled,
    resolve_meeting_context_folder_path,
    resolve_meeting_end_polish,
    resolve_meeting_end_redecode,
    resolve_meeting_redecode_coverage_guard,
    resolve_meeting_end_report,
    resolve_meeting_insight_review,
    resolve_meeting_past_recall_enabled,
    resolve_typesafe_enabled,
    resolve_typesafe_text_model,
    resolve_meeting_report_brief,
    resolve_meeting_report_ribbon,
    resolve_meeting_report_signal,
    resolve_recording_trigger_mode,
    resolve_meeting_server_bind,
    resolve_meeting_server_port,
    resolve_recording_overlay_clock,
    resolve_recording_overlay_dot,
    resolve_recording_overlay_look,
    resolve_recording_overlay_text,
    resolve_streaming_overlay_font_size,
    resolve_streaming_overlay_layout,
    resolve_ui_font_scale,
    resolve_ui_theme,
    resolve_transcript_cleanup_level,
    resolve_transcript_cleanup_model,
    resolve_transcript_cleanup_provider,
    resolve_transcript_cleanup_reasoning,
    resolve_transcript_cleanup_rules,
    resolve_update_check_enabled,
    resolve_update_notify_enabled,
    resolve_update_restart_prompt_enabled,
    setting_value,
    settings_manager,
)
from services.text_llm import (
    credential_label,
    get_profile as get_text_llm_profile,
    list_profiles,
    profile_display_name,
    verify_api_key,
)
from services import cleanup_prompts
from services.cleanup_prompts import CleanupLevel
from ui_qt.dialogs import cleanup_levels
from ui_qt.dialogs.cleanup_prompt_dialog import CleanupPromptDialog
from ui_qt.dialogs.cleanup_rule_dialog import CleanupRuleDialog
from ui_qt.dialogs.settings_binder import SettingsBinder
from ui_qt.dialogs import settings_microphones
from ui_qt.dialogs.settings_metadata import (
    CONTROL_DESTINATIONS,
    PAGE_HELP_TEXT,
    PAGE_SEARCH_FIELDS,
    merge_page_module,
)
from ui_qt.utils.list_reconcile import HistoryDelivery
from ui_qt.dialogs.settings_destinations import (
    ACTIONS,
    ADVANCED,
    APPEARANCE,
    API_KEYS,
    BACKUP,
    CLEANUP,
    CLEANUP_PROFILES,
    CLEANUP_RULES,
    COMMANDS,
    DICTIONARY,
    DOWNLOADS,
    GENERAL,
    HOOKS,
    HOTKEYS,
    MCP,
    MEETING_AFTER,
    MEETING_DASHBOARD,
    MEETING_FAST,
    MEETING_INTELLIGENCE,
    MEETING_VOICE,
    OVERVIEW,
    RECORDING,
    REMOTE_ENGINE,
    RUNTIME,
    SNIPPETS,
    STYLES,
    VOICE_MODEL,
    WALKTHROUGHS,
    resolve_destination,
)
from ui_qt.dialogs.settings_downloads import DownloadsPage
from ui_qt.dialogs.settings_backup import BackupSettingsPage
from ui_qt.dialogs.settings_fields import (
    group_title,
    settings_caption,
    settings_field,
)
from ui_qt.dialogs.settings_mcp import McpSettingsView
from ui_qt.dialogs.settings_models import ModelAssignments
from ui_qt.dialogs.settings_home import SECTIONS as HOME_SECTIONS
from ui_qt.dialogs.settings_home import HomePage, HomeSummary
from ui_qt.dialogs.settings_remote import RemoteEngineSection
from ui_qt.dialogs.settings_search import (
    SETTING,
    HELP,
    PageSource,
    SearchEntry,
    SearchPalette,
    build_index,
    keyword_entries,
    shortcut_text,
)
from ui_qt.utils.agent_lookup import lookup_relay
from ui_qt.utils.app_icon import app_icon
from ui_qt.utils.font_scale import current_ui_font_scale
from ui_qt.utils.icons import design_icon
from ui_qt.utils.restyle import set_style_property
from ui_qt.widgets.empty_state import EmptyState
from ui_qt.widgets import (
    Button,
    DangerButton,
    ElidingComboBox,
    NoWheelSpinBox,
    FieldTile,
    InfoTile,
    PrimaryButton,
    SettingTile,
    WrappedLabel,
)
from ui_qt.widgets.button_row import ButtonRow
from ui_qt.widgets.buttons import (
    compact_primary_button,
    fit_compact_button,
    neutral_button,
)
from ui_qt.widgets.hotkey_capture import (
    CAPTURE_HINT,
    HotkeyCaptureInput,
    HotkeyCaptureThread,
    is_escape_key,
    mouse_shortcut_note,
)
from ui_qt.widgets.hotkey_bindings import BindingShortcuts
from ui_qt.widgets.hotkey_row import ReflowCard
from ui_qt.widgets.settings_switch import SettingsSwitch
from services.hotkey_conflicts import (
    PROFILE,
    STANDARD,
    STANDARD_LABELS,
    TRANSFORM,
    hotkey_conflict,
)
from services.settings import (
    resolve_recording_hands_free_latch,
    resolve_walkthrough_copy_format,
    resolve_walkthrough_page_size,
    resolve_walkthrough_show_about,
    resolve_walkthrough_show_captions,
    resolve_walkthrough_show_details,
    resolve_walkthrough_title,
    resolve_walkthrough_write_up,
    resolve_walkthrough_write_up_agent,
    resolve_walkthrough_write_up_instructions,
    resolve_walkthrough_write_up_notes,
)
from ui_qt.widgets.cleanup_profiles_panel import CleanupProfilesPanel
from services.cleanup_profiles import load_cleanup_profiles, profile_hotkey_conflict
from ui_qt.widgets.nav_rail import NavRail
from ui_qt.widgets.rule_activity import ItemGlow, RuleActivityStrip
from ui_qt.widgets.recording_look_preview import RecordingLookPreview
from ui_qt.widgets.segmented_bar import SegmentedBar

logger = logging.getLogger(__name__)

#: A dictated rule stops recording on its own after this long.
_RULE_DICTATION_CAP_MS = 60_000

#: The consent version saved when the meeting insight review is turned on.
_INSIGHT_REVIEW_CONSENT = "typesafe-text-v1"


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


_HF_POLICY_LABELS = {
    HuggingFaceAccessPolicy.ASK: "ask first",
    HuggingFaceAccessPolicy.ALWAYS: "always download",
    HuggingFaceAccessPolicy.NEVER: "offline",
}

#: Destinations reached from search by a name the app used to use.
_SEARCH_ALIASES = {
    WALKTHROUGHS: (
        "Walkthrough and screenshot settings",
        "Dictation › Walkthroughs (moved from General)",
        "walkthrough screenshot snap capture pdf document title captions paper "
        "letter a4 ai write-up claude code agent instructions prompt",
    ),
    MCP: (
        "Connect an agent with MCP",
        "Automate › API & MCP",
        "model context protocol agent server connection claude cursor history api "
        "live api events stream webhook subscribe mcp events claude code channel "
        "permissions run actions start recordings change dictated text curl",
    ),
    VOICE_MODEL: (
        "Model assignments",
        "Model Manager's choices now sit on Voice model, AI cleanup, Voice & "
        "speakers, Intelligence, and Runtime",
        "model manager models assign languages i dictate in multilingual language "
        "switcher",
    ),
    CLEANUP: (
        "Spoken corrections and lists",
        "Dictation › AI cleanup › Cleanup level",
        "self-correction scratch that actually bullet numbered list light medium high "
        "rewrite polish formatting",
    ),
    HOTKEYS: (
        "Every shortcut: Command Mode, Scratchpad, walkthroughs, profiles, actions",
        "App › Hotkeys",
        "command mode scratchpad switch language paste original hands-free double tap "
        "latch mouse side button back forward mouse 4 mouse 5 walkthrough screenshot "
        "profile shortcut transform shortcut action shortcut agent hotkey keyboard "
        "shortcuts all shortcuts",
    ),
    DOWNLOADS: (
        "Download models and components",
        "Models & storage › Downloads",
        "download manager library catalog hugging face",
    ),
    MEETING_INTELLIGENCE: (
        "Cloud intelligence",
        "Now AI insights; choose its model on Meeting Mode › Intelligence",
        "cloud insights ai insights meeting model llm",
    ),
}


#: Pages whose content lives in its own module: (destination, module, rail
#: icon). Settings imports each one while registering its pages, for the
#: heading, search copy, and rail value, so a page module keeps its
#: module-level imports light and imports widgets inside ``build``. See
#: docs/design/personalize/design.md for the module protocol.
_PAGE_MODULES = (
    (DICTIONARY, "ui_qt.dialogs.settings_dictionary", "book-blue.svg"),
    (SNIPPETS, "ui_qt.dialogs.settings_snippets", "text-plus-green.svg"),
    (STYLES, "ui_qt.dialogs.settings_styles", "palette-purple.svg"),
    (COMMANDS, "ui_qt.dialogs.settings_commands", "wand-purple.svg"),
    (ACTIONS, "ui_qt.dialogs.settings_actions", "bolt-green.svg"),
    (HOOKS, "ui_qt.dialogs.settings_hooks", "webhook-blue.svg"),
)
_PAGE_MODULE_ICONS = {key: icon for key, _path, icon in _PAGE_MODULES}

#: The rail's chapters below Home: (title, icon, pages), each page as (key,
#: name, icon). A page's icon marks its search results.
_CHAPTERS = (
    ("Dictation", "microphone-blue.svg", (
        (RECORDING, "Recording", "microphone-blue.svg"),
        (VOICE_MODEL, "Voice model", "microphone-blue.svg"),
        (CLEANUP, "AI cleanup", "stack-purple.svg"),
        (REMOTE_ENGINE, "Remote engine", "server-blue.svg"),
        (WALKTHROUGHS, "Walkthroughs", "file-text-blue.svg"),
    )),
    ("Personalize", "wand-purple.svg", (
        (DICTIONARY, "Dictionary", _PAGE_MODULE_ICONS[DICTIONARY]),
        (SNIPPETS, "Snippets", _PAGE_MODULE_ICONS[SNIPPETS]),
        (STYLES, "Styles", _PAGE_MODULE_ICONS[STYLES]),
        (CLEANUP_RULES, "Learned rules", "stack-slate.svg"),
        (CLEANUP_PROFILES, "Profiles", "typography-blue.svg"),
        (COMMANDS, "Commands", _PAGE_MODULE_ICONS[COMMANDS]),
    )),
    ("Automate", "bolt-green.svg", (
        (ACTIONS, "Actions", _PAGE_MODULE_ICONS[ACTIONS]),
        (HOOKS, "Hooks", _PAGE_MODULE_ICONS[HOOKS]),
        (MCP, "API & MCP", "server-blue.svg"),
    )),
    ("Meeting Mode", "users-amber.svg", (
        (MEETING_VOICE, "Voice & speakers", "microphone-blue.svg"),
        (MEETING_INTELLIGENCE, "Intelligence", "stack-purple.svg"),
        (MEETING_FAST, "Fast judgments", "bolt-green.svg"),
        (MEETING_AFTER, "After the meeting", "check-green.svg"),
        (MEETING_DASHBOARD, "Dashboard", "box-blue.svg"),
    )),
    ("Models & storage", "download-blue.svg", (
        (DOWNLOADS, "Downloads", "download-blue.svg"),
        (RUNTIME, "Runtime", "cpu-blue.svg"),
        (BACKUP, "Backup & restore", "box-blue.svg"),
    )),
    ("App", "adjustments-slate.svg", (
        (GENERAL, "General", "bolt-green.svg"),
        (APPEARANCE, "Appearance", "palette-purple.svg"),
        (HOTKEYS, "Hotkeys", "keyboard-green.svg"),
        (API_KEYS, "API keys", "key-blue.svg"),
        (ADVANCED, "Developer options", "box-blue.svg"),
    )),
)

#: How a hotkey message names each standard action.
_HOTKEY_LABELS = STANDARD_LABELS


@dataclass(frozen=True)
class _ShortcutRow:
    """A standard shortcut's box on one page; Hotkeys and its feature's page may both show one."""

    key: str
    page: str
    field: HotkeyCaptureInput
    clear: Optional[QPushButton]
    note: WrappedLabel


class _ThreadRelay(QObject):
    """Brings worker results and microphone levels to the Qt thread for whichever windows exist then.

    It lives for the session, so a worker or the rule recorder's audio thread
    never holds or emits on a window. Holding one let the thread drop the
    last reference and delete the window off the Qt thread, and emitting on
    one raced it closing; both crash the process. Qt drops a deleted window's
    connections.
    """

    api_key_verified = pyqtSignal(int, str, bool, str)
    typesafe_route_verified = pyqtSignal(int, str, bool, str)
    cleanup_rule_polished = pyqtSignal(int, str, str, str)
    rule_dictation_finished = pyqtSignal(int, str, str)
    rule_dictation_level = pyqtSignal(int, float)


_relay: Optional[_ThreadRelay] = None
#: Request tokens, unique across windows because they share the relay.
_tokens = itertools.count(1)
#: Fast judgments routes, in the order the Judge bar shows them.
_JUDGE_TITLES = {
    TypeSafeProvider.TYPESAFE: "TypeSafe",
    TypeSafeProvider.OPENROUTER: "OpenRouter",
    TypeSafeProvider.TEXT_MODEL: "Your text model",
}
#: Which text model answers on the text-model route, in the bar's order.
_JUDGE_TEXT_MODEL_TITLES = {
    TypeSafeTextModel.MEETING: "AI insights model",
    TypeSafeTextModel.CLEANUP: "AI cleanup model",
}


def _thread_relay() -> _ThreadRelay:
    global _relay
    if _relay is None:
        _relay = _ThreadRelay()
    return _relay


def _select_combo_data(combo, value) -> None:
    """Select the combo entry carrying ``value``, or the first entry."""
    combo.setCurrentIndex(max(0, combo.findData(value)))


def _general_rail_value(settings: dict) -> str:
    if setting_value(SettingsKey.AUTO_PASTE, settings):
        return "Auto-paste on"
    if setting_value(SettingsKey.COPY_CLIPBOARD, settings):
        return "Clipboard"
    return "Manual"


def _rule_count_label(rule_count: int) -> str:
    if rule_count == 0:
        return "No rules"
    return f"{rule_count} rule{'' if rule_count == 1 else 's'}"


def _meeting_after_rail_value(settings: dict) -> str:
    if resolve_meeting_end_report(settings):
        return "Report"
    if resolve_meeting_end_polish(settings):
        return "Polish"
    if resolve_meeting_end_redecode(settings):
        return "Re-decode"
    return "Off"


def _meeting_dashboard_rail_value(settings: dict) -> str:
    bind = resolve_meeting_server_bind(settings)
    port = resolve_meeting_server_port(settings)
    if bind != MeetingServerBind.LAN:
        return "Localhost"
    return f"LAN · {'auto' if port == 0 else port}"


def _rule_capture_problem(recorder, audio_path: str) -> Optional[str]:
    """Why a stopped rule dictation can't be transcribed, or None once it's saved."""
    if not recorder.wait_for_stop_completion():
        return "Recording did not finish stopping; audio kept for recovery."
    if getattr(recorder, "last_capture_error", None):
        return str(recorder.last_capture_error)
    if not recorder.has_recording_data():
        return "No audio was captured."
    if not recorder.save_recording(audio_path):
        return "Couldn't save the dictation audio."
    return None


def _remove_dictation_audio(audio_path: str) -> None:
    try:
        if os.path.exists(audio_path):
            os.remove(audio_path)
    except OSError:
        pass


def _transcribe_rule_dictation(recorder, transcribe, audio_path: str):
    """Stop, save and transcribe a rule dictation; returns ``(text, error)``.

    Runs on a worker thread. The recording and its audio file are discarded
    either way.
    """
    text = ""
    error = ""
    try:
        recorder.stop_recording()
        problem = _rule_capture_problem(recorder, audio_path)
        if problem is not None:
            error = problem
        else:
            text = transcribe(audio_path) or ""
            if not text.strip():
                error = "Nothing was transcribed."
    except Exception as exc:
        error = str(exc) or "Transcription failed."
    finally:
        recorder.clear_recording_data()
        _remove_dictation_audio(audio_path)
    return text, error


class _SettingsPage(QWidget):
    def __init__(self):
        super().__init__()
        self._tile_grids = []
        # Let the viewport supply the width before the grids choose their columns.
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def add_tile_grid(self, grid: QGridLayout, tiles: list, columns: int) -> None:
        self._tile_grids.append([grid, tiles, columns, 0])
        self._reflow_tiles()

    def _reflow_tiles(self) -> None:
        minimum_tile_width = round(300 * current_ui_font_scale())
        for entry in self._tile_grids:
            grid, tiles, maximum_columns, current_columns = entry
            spacing = grid.horizontalSpacing()
            columns = min(
                maximum_columns,
                max(1, (self.width() + spacing) // (minimum_tile_width + spacing)),
            )
            if columns == current_columns:
                continue
            while grid.count():
                grid.takeAt(0)
            for column in range(maximum_columns):
                grid.setColumnStretch(column, 1 if column < columns else 0)
            for index, tile in enumerate(tiles):
                row, column = divmod(index, columns)
                span = columns - column if index == len(tiles) - 1 else 1
                grid.addWidget(tile, row, column, 1, span)
            entry[3] = columns

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow_tiles()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._reflow_tiles()


class SettingsDialog(QDialog):
    """The one non-modal window for settings, model choices, and downloads.

    It opens on Home, the everyday settings and where words go. The rail
    holds Home and six chapters of destinations; only the current page's
    chapter unfolds. Home's rows save through the destinations' own
    handlers. ``UIController`` holds a single instance and re-raises it
    instead of stacking copies.
    """

    #: Client area; with the title bar it opens at about 1317x882 on a
    #: 1080p screen, and leaves the Downloads catalog room beside its profile.
    DEFAULT_SIZE = QSize(1315, 850)
    #: The Downloads filter row is the widest fixed content in the window.
    MINIMUM_SIZE = QSize(940, 520)
    #: Narrower than this at 100% text, the rail folds into the header's page
    #: menu, so a tiled or small window gives its whole width to the page.
    NARROW_WIDTH = 800
    #: Room left above the window for a title bar the window manager has not
    #: drawn yet: the frame is unknown until the window is mapped.
    TITLE_BAR_ALLOWANCE = 32

    on_audio_device_changed: Optional[Callable] = None
    on_streaming_settings_changed: Optional[Callable] = None
    on_streaming_font_changed: Optional[Callable] = None
    on_streaming_layout_changed: Optional[Callable] = None
    on_recording_look_changed: Optional[Callable] = None
    on_ui_font_scale_changed: Optional[Callable[[int], None]] = None
    on_ui_theme_changed: Optional[Callable[[str], None]] = None
    on_hf_policy_changed: Optional[Callable] = None
    on_api_keys_changed: Optional[Callable[[], None]] = None
    on_developer_mode_changed: Optional[Callable] = None
    on_cleanup_changed: Optional[Callable] = None
    on_cleanup_profiles_changed: Optional[Callable] = None
    on_profile_hotkey_capture: Optional[Callable] = None
    on_hotkeys_changed: Optional[Callable[[Dict[str, str]], None]] = None
    on_recording_trigger_mode_changed: Optional[Callable[[str], None]] = None
    on_dictation_transcribe: Optional[Callable[[str], str]] = None
    on_settings_changed: Optional[Callable[[str], None]] = None
    # (action, sample_text, done): try a saved action from the Actions page;
    # done(ActionResult) runs on the Qt thread.
    on_try_action: Optional[Callable] = None
    # (): cancel the Actions page's "Try it" runs still going (Settings closed).
    on_cancel_try_runs: Optional[Callable[[], None]] = None
    get_meeting_active: Optional[Callable[[], bool]] = None

    def __init__(
        self,
        parent=None,
        get_loaded_model: Optional[Callable[[], Optional[str]]] = None,
        background_cache_scan: bool = True,
        backup_coordinator=None,
    ):
        """Register destinations and build only the selected page.

        Args:
            parent: Owning window.
            get_loaded_model: Provider returning the model the engine has
                loaded (or None), so "auto" and the Delete lock are accurate.
            background_cache_scan: Scan the model cache on a worker thread.
        """
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setWindowIcon(app_icon())
        self.setObjectName("settingsDialog")
        self.setModal(False)
        self.setWindowFlag(Qt.WindowType.MSWindowsFixedSizeDialogHint, False)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.setSizeGripEnabled(True)

        self._get_loaded_model = get_loaded_model
        self._background_cache_scan = bool(background_cache_scan)
        self._backup_coordinator = backup_coordinator
        self._backup_busy = False
        self.home: Optional[HomePage] = None
        self._narrow: Optional[bool] = None
        self._pages_ready = False
        self._built_pages = set()
        self._building_pages = set()
        self._initializing_page = None
        self._initial_binding_start = 0
        self._initial_meeting_binding_start = 0
        self._refresh_snapshot = None
        self._recording_query_generation = 0
        self._audio_device_generation = 0
        self._microphones: Optional[settings_microphones.MicrophoneSection] = None
        self._rail_batch_depth = 0
        self._rail_refresh_pending = False
        self._search_flash: Optional[QWidget] = None
        self._loading = False
        self._tray_available = bool(QSystemTrayIcon.isSystemTrayAvailable())
        self._native_wayland = is_native_wayland_session()
        self.current_hotkeys: Dict[str, str] = {}
        self.capturing: Optional[str] = None
        self.capture_thread: Optional[HotkeyCaptureThread] = None
        self.current_hotkey_input: Optional[HotkeyCaptureInput] = None
        # Every standard shortcut's box, on whichever page shows it.
        self._shortcut_rows: List[_ShortcutRow] = []
        # The Hotkeys page's own rows, by shortcut.
        self.hotkey_inputs: Dict[str, HotkeyCaptureInput] = {}
        self.hotkey_row_descriptions: Dict[str, WrappedLabel] = {}
        self.hotkey_row_notes: Dict[str, WrappedLabel] = {}
        self.hotkey_clear_buttons: Dict[str, QPushButton] = {}
        self._hotkeys_suspended_for_capture = False
        self._page_modules: Dict[str, object] = {}
        self._saved_cleanup_prompt = ""
        # (tile, button) per "AI cleanup is off" notice; see cleanup_gate_tile.
        self._cleanup_gates: list = []
        self._cleanup_gate_needed: dict = {}
        self._rule_polishing = False
        # Whether the words being polished came (at least partly) from
        # dictation, so the review dialog can say "said" instead of "typed".
        self._rule_polish_dictated = False
        self._rule_dictation_state = "idle"
        self._rule_recorder: Optional[AudioRecorder] = None
        self._rule_recorder_device: tuple = ()
        # The request each relayed result must match; 0 matches none.
        self._rule_level_token = 0
        self._rule_polish_token = 0
        self._rule_dictation_token = 0
        self._api_key_token = 0
        self._typesafe_route_token = 0
        self._api_key_testing = False
        self._typesafe_route_testing = False
        self._api_key_source = CredentialSource.NONE
        self._rule_dictation_path = os.path.join(
            tempfile.gettempdir(), "openwhisper_rule_dictation.wav"
        )
        self._rule_dictation_timer = QTimer(self)
        self._rule_dictation_timer.setSingleShot(True)
        self._rule_dictation_timer.setInterval(_RULE_DICTATION_CAP_MS)
        self._rule_dictation_timer.timeout.connect(self._stop_rule_dictation)
        # Lets a burst of chevron or arrow-key steps settle into one
        # retention change; typed counts commit on Enter or focus-out.
        self._retention_commit_timer = QTimer(self)
        self._retention_commit_timer.setSingleShot(True)
        self._retention_commit_timer.setInterval(800)
        self._retention_commit_timer.timeout.connect(self._commit_retention)
        self._confirming_retention = False
        # Controls whose change only saves their key. _load_settings loads the
        # first group and _load_meeting_settings the second.
        self._bindings = SettingsBinder(self._persist)
        self._meeting_bindings = SettingsBinder(self._persist)

        self._setup_ui()
        self.setMinimumSize(self.MINIMUM_SIZE)
        self.resize(self.DEFAULT_SIZE)

        relay = _thread_relay()
        relay.cleanup_rule_polished.connect(self._on_cleanup_rule_polished)
        relay.rule_dictation_finished.connect(self._on_rule_dictation_finished)
        relay.rule_dictation_level.connect(self._on_rule_dictation_level)
        relay.api_key_verified.connect(self._on_api_key_verified)
        relay.typesafe_route_verified.connect(self._on_typesafe_route_verified)
        lookup_relay().looked_up.connect(self._on_agent_looked_up)
        self.finished.connect(self._release_rule_recorder)
        self.models.assignments_changed.connect(self._refresh_rail_values)
        self.models.downloads_requested.connect(self.show_downloads)
        self.downloads.inventory_changed.connect(self._refresh_rail_values)
        self.downloads.progress_changed.connect(self._refresh_download_progress)

        self.search_palette = SearchPalette(self, self._search_index)
        self.search_palette.activated.connect(self._on_search_activated)
        self._search_shortcuts = []
        for sequence in (QKeySequence("Ctrl+K"), QKeySequence(QKeySequence.StandardKey.Find)):
            shortcut = QShortcut(sequence, self)
            shortcut.activated.connect(self.open_search)
            self._search_shortcuts.append(shortcut)
        if self._backup_coordinator is not None:
            self._backup_coordinator.busy_changed.connect(self._on_backup_busy_changed)
        self._search_flash_timer = QTimer(self)
        self._search_flash_timer.setSingleShot(True)
        self._search_flash_timer.setInterval(1600)
        self._search_flash_timer.timeout.connect(self._clear_search_flash)
        # Owned by the window, so a reveal queued just before the window is
        # destroyed dies with it instead of touching deleted widgets.
        self._reveal_target: Optional[QWidget] = None
        self._reveal_timer = QTimer(self)
        self._reveal_timer.setSingleShot(True)
        self._reveal_timer.setInterval(0)
        self._reveal_timer.timeout.connect(self._run_reveal)

        self._pages_ready = True
        self.rail.select(OVERVIEW)
        self.refresh()
        if self._backup_coordinator is not None:
            try:
                self._on_backup_busy_changed(bool(self._backup_coordinator.snapshot().get("busy")))
            except Exception:
                logger.exception("Could not read backup coordinator state")

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        # Only the shell constrains the window; page contents scroll independently.
        root.setSizeConstraint(QLayout.SizeConstraint.SetDefaultConstraint)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_view_header())
        self.settings_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.settings_splitter.setObjectName("settingsSplitter")
        self.settings_splitter.setHandleWidth(6)
        self.settings_splitter.setChildrenCollapsible(False)
        self.rail_pane = self._build_rail_pane()
        self.settings_splitter.addWidget(self.rail_pane)
        self.settings_splitter.addWidget(self._build_body())
        self.settings_splitter.setStretchFactor(0, 0)
        self.settings_splitter.setStretchFactor(1, 1)
        self.settings_splitter.setSizes(
            [NavRail.RAIL_WIDTH, self.DEFAULT_SIZE.width() - NavRail.RAIL_WIDTH]
        )
        handle = self.settings_splitter.handle(1)
        handle.setCursor(Qt.CursorShape.SizeHorCursor)
        handle.setToolTip("Drag to resize the settings sidebar")
        root.addWidget(self.settings_splitter, stretch=1)

    def _build_view_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("settingsViewHeader")
        row = QHBoxLayout(header)
        row.setContentsMargins(20, 14, 20, 14)
        row.setSpacing(10)
        icon = QLabel()
        # The mark is already a rounded tile; framing it again reads as a ring.
        # 32 px is a native .ico size, so it stays crisp instead of resampled.
        icon.setPixmap(app_icon().pixmap(32, 32))
        row.addWidget(icon)
        brand = QLabel("OpenWhisper")
        self.settings_brand = brand
        brand.setObjectName("settingsViewBrand")
        row.addWidget(brand)
        label = QLabel("Settings")
        self.settings_label = label
        label.setObjectName("settingsViewLabel")
        row.addWidget(label)
        # resizeEvent hides both in a narrow window; until it does, they must
        # not hold the window wider than the narrow width it is going to. An
        # explicit minimum replaces a label's text-wide minimum size hint.
        for name in (brand, label):
            name.setMinimumWidth(1)
        # Stands in for the rail when the window is too narrow for it.
        self.page_menu_button = QPushButton()
        self.page_menu_button.setObjectName("settingsPageMenu")
        self.page_menu_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.page_menu_button.setIcon(design_icon("chevron-down-slate.svg"))
        self.page_menu_button.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.page_menu_button.setAccessibleName("Choose a settings page")
        self.page_menu_button.clicked.connect(self._show_page_menu)
        self.page_menu_button.setVisible(False)
        self._page_menu: Optional[QMenu] = None
        row.addWidget(self.page_menu_button)
        row.addStretch(1)

        self.search_button = QPushButton()
        self.search_button.setObjectName("settingsRailSearch")
        self.search_button.setCursor(Qt.CursorShape.PointingHandCursor)
        # Ctrl+K is the keyboard path; as the first control in the window it
        # would otherwise take focus on open and show a permanent focus ring.
        self.search_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.search_button.setToolTip("Search every setting, model, and help note")
        self.search_button.setAccessibleName("Search settings and models")
        self.search_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.search_button.setMaximumWidth(420)
        search_row = QHBoxLayout(self.search_button)
        search_row.setContentsMargins(12, 0, 8, 0)
        search_row.setSpacing(8)
        search_icon = QLabel()
        search_icon.setObjectName("settingsRailSearchIcon")
        search_icon.setPixmap(design_icon("search-slate.svg").pixmap(14, 14))
        search_row.addWidget(search_icon)
        self.search_text = QLabel("Search every setting, model and help note")
        self.search_text.setObjectName("settingsRailSearchText")
        self.search_text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        search_row.addWidget(self.search_text, stretch=1)
        self.search_hint = QLabel(shortcut_text("Ctrl+K"))
        self.search_hint.setObjectName("settingsRailSearchHint")
        search_row.addWidget(self.search_hint)
        for label in (search_icon, self.search_text, self.search_hint):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.search_button.setFixedHeight(36)
        self.search_button.clicked.connect(lambda: self.open_search())
        row.addWidget(self.search_button, stretch=2)
        return header

    def _build_rail_pane(self) -> QWidget:
        pane = QWidget()
        pane.setObjectName("modelManagerRailPane")
        pane.setMinimumWidth(220)
        pane.setMaximumWidth(560)
        column = QVBoxLayout(pane)
        column.setContentsMargins(0, 12, 0, 0)
        column.setSpacing(0)

        self.rail = NavRail()
        self.rail.add_destination(OVERVIEW, "Home", design_icon("home-blue.svg"))
        self._rail_icons: Dict[str, str] = {OVERVIEW: "home-blue.svg"}
        for chapter, icon, pages in _CHAPTERS:
            self.rail.add_chapter(chapter, design_icon(icon))
            for key, name, page_icon in pages:
                self.rail.add_destination(key, name)
                self._rail_icons[key] = page_icon
        self.rail.destination_changed.connect(self._on_destination_changed)
        column.addWidget(self.rail, stretch=1)
        return pane

    def _show_page_menu(self) -> None:
        """List every page by chapter, for a window too narrow for the rail."""
        menu = QMenu(self)
        menu.setObjectName("settingsPageMenuList")
        current = self.rail.current_key()

        def add(key: str) -> None:
            action = menu.addAction(self.rail.name(key))
            action.setCheckable(True)
            action.setChecked(key == current)
            action.triggered.connect(lambda _checked=False, key=key: self.select_destination(key))

        for key in self.rail.keys():
            if not self.rail.chapter(key):
                add(key)
        for chapter in self.rail.chapters():
            menu.addSeparator()
            heading = menu.addAction(chapter)
            heading.setEnabled(False)
            for key in self.rail.chapter_keys(chapter):
                add(key)
        self._page_menu = menu
        menu.popup(self.page_menu_button.mapToGlobal(self.page_menu_button.rect().bottomLeft()))

    def _build_body(self) -> QWidget:
        body = QWidget()
        body.setObjectName("modelManagerBody")
        layout = QVBoxLayout(body)
        self.body_layout = layout
        layout.setContentsMargins(28, 16, 28, 16)
        layout.setSpacing(14)

        heading = QVBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        heading.setSpacing(2)
        # The chapter a page belongs to, above its title.
        self.page_crumb = QLabel("")
        self.page_crumb.setObjectName("settingsPageCrumb")
        self.page_title = QLabel("")
        self.page_title.setObjectName("modelManagerTitle")
        self.page_subtitle = WrappedLabel("")
        self.page_subtitle.setObjectName("modelManagerSubtitle")
        heading.addWidget(self.page_crumb)
        heading.addWidget(self.page_title)
        heading.addSpacing(2)
        heading.addWidget(self.page_subtitle)
        layout.addLayout(heading)

        # Shared by every page, so model and download messages land in one
        # place. Created before the pages that report into it.
        self.message_label = WrappedLabel("")
        self.message_label.setObjectName("modelManagerMessage")
        self.models = ModelAssignments(
            self,
            self.rail,
            self.message_label,
            get_loaded_model=self._get_loaded_model,
            background_cache_scan=self._background_cache_scan,
        )
        self.downloads = DownloadsPage(
            get_loaded_model=self._get_loaded_model,
            background_cache_scan=self._background_cache_scan,
            defer_build=True,
        )
        self._remote_service = None
        self.remote_section = RemoteEngineSection(self)

        self.stack = QStackedWidget()
        self.stack.setObjectName("modelManagerStack")
        self._pages: Dict[str, QWidget] = {}
        self._page_scrolls: Dict[str, QScrollArea] = {}
        self._headings: Dict[str, tuple] = {}
        self._page_factories = {}
        self._add_page(
            OVERVIEW,
            "Home",
            "Your everyday settings, and where your words go. Everything else is "
            "in the sidebar.",
            self._build_home_page,
        )
        self._add_page(
            VOICE_MODEL,
            "Voice model",
            "The transcription engine used by Quick Record, hotkey dictation, "
            "and Upload File.",
            self.models.build_voice_page,
        )
        self._add_page(
            REMOTE_ENGINE,
            "Remote engine",
            "Transcribe dictation and meetings with another computer's engine over your network or "
            "Tailscale, or share this computer's engine with computers you pair.",
            lambda layout: self.remote_section.build(self, layout, design_icon),
        )
        self._add_page(
            WALKTHROUGHS,
            "Walkthroughs",
            "Talk while you take screenshots, then paste one document into "
            "your agent: its shortcuts, how the document looks, and the "
            "optional AI write-up.",
            self._build_walkthroughs_page,
        )
        self._add_page(
            RECORDING,
            "Recording",
            "Microphone, saved audio retention, and the live preview.",
            self._build_recording_page,
        )
        self._add_page(
            CLEANUP,
            "AI transcript cleanup",
            "Rewrite a finished dictation with a language model, together with "
            "your learned rules.",
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
            CLEANUP_PROFILES,
            "Cleanup profiles",
            "Turn a recording into a support ticket, email, or your own format.",
            self._build_cleanup_profiles_page,
        )
        for key, path, _icon in _PAGE_MODULES:
            module = importlib.import_module(path)
            merge_page_module(key, module)
            self._page_modules[key] = module
            self._add_page(
                key, module.TITLE, module.SUBTITLE,
                lambda layout, module=module: module.build(self, layout),
            )
        self._add_page(
            MEETING_VOICE,
            "Meeting voice & speakers",
            "Choose local or remote speech for live captions and the optional "
            "end-of-meeting re-transcription. Applies to your next meeting.",
            self.models.build_meeting_voice_page,
        )
        self._add_page(
            MEETING_INTELLIGENCE,
            "Meeting intelligence",
            "Pi or a coding agent you already use runs every Meeting Mode "
            "pass. Nothing is sent until you turn on "
            "AI insights for a meeting.",
            self._build_meeting_intelligence_page,
        )
        self._add_page(
            MEETING_FAST,
            "Fast judgments",
            "Optional checks by Jev or your own text model. Each feature describes the text it shares.",
            self._build_meeting_fast_page,
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
            DOWNLOADS,
            "Downloads",
            "Speech models and optional components, downloaded on demand.",
            self._build_downloads_page,
            scroll=False,
        )
        self._add_page(
            RUNTIME,
            "Runtime",
            "How local Whisper models run. Dictation and Meeting Mode both "
            "inherit these.",
            self.models.build_runtime_page,
        )
        self._add_page(
            BACKUP,
            "Backup & restore",
            "Save a local copy of your data or inspect a backup before restoring it.",
            self._build_backup_page,
        )
        self._add_page(
            GENERAL,
            "General",
            "How finished transcriptions leave the app and how the window "
            "behaves when you close it.",
            self._build_general_page,
        )
        self._add_page(
            APPEARANCE,
            "Appearance",
            "Theme, text size, and how the overlay near your pointer looks "
            "while you dictate.",
            self._build_appearance_page,
        )
        self._add_page(
            HOTKEYS,
            "Hotkeys",
            "Every global shortcut in one place, including the ones you give "
            "your profiles, transforms and actions.",
            self._build_hotkeys_page,
        )
        self._add_page(
            API_KEYS,
            "API keys",
            "Credentials for cloud providers and custom endpoints. Saved "
            "keys live in your operating system's credential manager, never "
            "in the settings file.",
            self._build_api_keys_page,
        )
        self._add_page(
            MCP,
            "API & MCP",
            "Let agents and your own programs read history, follow live "
            "events and run actions.",
            self._build_mcp_page,
        )
        self._add_page(
            ADVANCED,
            "Developer options",
            "Meeting re-transcription and developer tools.",
            self._build_advanced_page,
        )
        layout.addWidget(self.stack, stretch=1)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        saved = QLabel("Changes save automatically")
        saved.setObjectName("settingsAutoSaveLabel")
        footer.addWidget(saved)
        footer.addWidget(self.message_label, stretch=1)
        close_btn = Button("Done")
        close_btn.setObjectName("modelManagerCloseButton")
        close_btn.setToolTip("Close Settings (Esc)")
        fit_compact_button(close_btn, 110)
        close_btn.clicked.connect(self.close)
        footer.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignBottom)
        layout.addLayout(footer)
        return body

    def _build_home_page(self, layout) -> None:
        self.home = HomePage(self)
        layout.addWidget(self.home)

    def page_modules(self) -> tuple:
        """The Personalize page modules, in rail order."""
        return tuple(self._page_modules.values())

    def refresh_page(self, key: str) -> None:
        """Re-read one page module's state, if its page has been built."""
        module = self._page_modules.get(key)
        if module is not None and key in self._built_pages:
            self._refresh_page_module(key, module)
        self._refresh_rail_values()

    def _refresh_page_module(self, key: str, module) -> None:
        try:
            module.refresh(self)
        except Exception:
            logger.exception("Couldn't refresh the %s settings page", key)

    def _cancel_page_captures(self, keep: str = "") -> None:
        for key, module in self._page_modules.items():
            cancel = getattr(module, "cancel_capture", None)
            if key == keep or key not in self._built_pages or cancel is None:
                continue
            # Runs while closing and switching pages, which must still finish.
            try:
                cancel(self)
            except Exception:
                logger.exception("Couldn't stop the %s page's shortcut capture", key)

    def _save_page_drafts(self, keep: str = "", *, ask: bool = False) -> bool:
        """Save the editors' unsaved drafts, as the footer promises.

        Returns False, with that page shown, when the user chose to keep
        editing a draft that can't be saved yet.
        """
        for key, module in self._page_modules.items():
            save = getattr(module, "save_drafts", None)
            if key == keep or key not in self._built_pages or save is None:
                continue
            try:
                if save(self, ask=ask):
                    continue
            except Exception:
                logger.exception("Couldn't save the %s page's draft", key)
                continue
            self.select_destination(key)
            return False
        return True

    def set_standard_hotkey(self, action: str, hotkey: str) -> str:
        """Save one standard shortcut through the Hotkeys page's save path.

        Starts from the shortcuts this window holds, so a later edit on the
        Hotkeys page keeps it, and the app re-registers it like any other.

        Returns:
            "" once saved, otherwise the reason it was not.
        """
        if not self.current_hotkeys:
            self._load_hotkey_settings()
        updated = self.current_hotkeys.copy()
        updated[action] = hotkey
        # Blank the action itself so its own default never reads as a conflict.
        conflict = profile_hotkey_conflict(
            hotkey, self._settings_snapshot(), standard_hotkeys={**updated, action: ""},
        )
        if conflict:
            error = f"That shortcut is already used by {conflict}."
            self.message_label.setText(error)
            return error
        return self._save_hotkeys(updated, f"{_HOTKEY_LABELS.get(action, 'Shortcut')} hotkey updated.")

    def set_hotkey_capture_suspended(self, suspended: bool) -> None:
        """Pause global hotkeys while a page captures a shortcut, and resume them."""
        self._on_profile_capture(bool(suspended))

    def notify_changed(self, kind: str) -> None:
        """Tell the app a page changed something it caches or registers.

        Args:
            kind: "transforms", "hotkeys", "dictionary", "snippets",
                "styles", "languages", "microphones", or "updates".
        """
        self._refresh_rail_values()
        if self.on_settings_changed:
            self.on_settings_changed(kind)

    def _add_page(
        self,
        key: str,
        title: str,
        subtitle: str,
        builder: Callable,
        *,
        scroll: bool = True,
    ) -> None:
        """Add one destination.

        Args:
            scroll: Wrap the page in its own scroll area. Downloads opts out
                because its catalog list is the page's only scroller.
        """
        page = _SettingsPage()
        page.setObjectName(f"settingsPage_{key}")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self._page_factories[key] = builder
        self._pages[key] = page
        self._headings[key] = (title, subtitle)
        if not scroll:
            self.stack.addWidget(page)
            return
        layout.addStretch()
        area = QScrollArea()
        area.setObjectName("settingsPageScroll")
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(page)
        self._page_scrolls[key] = area
        self.stack.addWidget(area)

    def __getattr__(self, name):
        destination = CONTROL_DESTINATIONS.get(name)
        factories = self.__dict__.get("_page_factories", {})
        if (destination in factories and destination not in self.__dict__.get("_building_pages", set())):
            self.ensure_page(destination)
            if name in self.__dict__:
                return self.__dict__[name]
        raise AttributeError(name)

    def ensure_page(self, key: str) -> QWidget:
        """Construct one destination once, then load its persisted values."""
        key = resolve_destination(key)
        if key not in self._page_factories:
            raise KeyError(key)
        page = self._pages[key]
        if key in self._built_pages or key in self._building_pages:
            return page
        self._building_pages.add(key)
        loading = self._loading
        initializing = self._initializing_page
        binding_start = self._initial_binding_start
        meeting_binding_start = self._initial_meeting_binding_start
        self._initializing_page = key
        self._initial_binding_start = self._bindings.binding_count
        self._initial_meeting_binding_start = self._meeting_bindings.binding_count
        self._loading = True
        try:
            layout = page.layout()
            if layout.count() and layout.itemAt(layout.count() - 1).spacerItem():
                layout.takeAt(layout.count() - 1)
            self._page_factories[key](layout)
            if key in self._page_scrolls:
                layout.addStretch()
            self._built_pages.add(key)
            self._load_settings()
            self.models.refresh(scan=False)
            page.layout().activate()
            if key == DOWNLOADS:
                self.downloads.refresh(scan=False)
        finally:
            self._loading = loading
            self._initializing_page = initializing
            self._initial_binding_start = binding_start
            self._initial_meeting_binding_start = meeting_binding_start
            self._building_pages.discard(key)
        return page

    def _page_is_loading(self, key: str) -> bool:
        return key in self._built_pages and self._initializing_page in (None, key)

    def ensure_all_pages(self) -> None:
        """Materialize every destination for full-window layout verification."""
        for key in self._page_factories:
            self.ensure_page(key)

    def _build_mcp_page(self, layout: QVBoxLayout) -> None:
        self.mcp_view = McpSettingsView(settings_manager)
        self.mcp_view.settings_changed.connect(self.notify_changed)
        # This computer's own controls; the view adds the paired host's.
        self.mcp_page = self.mcp_view.local
        self.mcp_history_tile = self.mcp_view.share_history_tile
        if self._remote_service is not None:
            self.mcp_view.bind(self._remote_service)
        layout.addWidget(self.mcp_view)
        layout.addStretch()

    def bind_remote_service(self, service) -> None:
        """Give the MCP page the remote engine service, so it can reach a paired host."""
        self._remote_service = service
        view = self.__dict__.get("mcp_view")
        if view is not None:
            view.bind(service)

    def _build_backup_page(self, layout: QVBoxLayout) -> None:
        self.backup_page = BackupSettingsPage(self._backup_coordinator)
        self.backup_page.operation_busy_changed.connect(self._on_backup_busy_changed)
        self.backup_create_tile = self.backup_page.create_tile
        self.backup_restore_tile = self.backup_page.restore_tile
        self.backup_schedule_tile = self.backup_page.schedule_tile
        layout.addWidget(self.backup_page)

    def _build_downloads_page(self, layout: QVBoxLayout) -> None:
        self.downloads.ensure_ui()
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
        self.hf_policy_combo.setMinimumHeight(34)
        self.hf_policy_combo.setMaximumWidth(300)
        self.hf_policy_combo.setToolTip(
            "Models already on this computer always load locally without any "
            "network checks. Hugging Face is only contacted to download a "
            "missing model, and only when this policy or a one-time approval "
            "allows it. An external HF_HUB_OFFLINE=1 environment variable "
            "disables downloads entirely."
        )
        self.hf_policy_combo.currentIndexChanged.connect(self._on_hf_policy_changed)
        self.downloads.add_policy_control(
            "When a model is missing from this computer", self.hf_policy_combo
        )
        layout.addWidget(self.downloads, stretch=1)

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
        caption = group_title(layout, title) if title else None
        intro_label = None
        if intro:
            intro_label = settings_caption(intro)
            layout.addWidget(intro_label)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        layout.addLayout(grid)
        layout.parentWidget().add_tile_grid(grid, tiles, columns)
        layout.addSpacing(6)
        return caption, intro_label

    def _build_general_page(self, layout: QVBoxLayout) -> None:
        auto_paste_description = (
            "Types a dictated transcript into whichever window has focus, "
            "pasting it through the clipboard. Upload File results stay in "
            "the window with their own Copy buttons."
        )
        if self._native_wayland:
            from services.hyprland import available
            auto_paste_description += (
                " On Hyprland, pasting uses the desktop's shortcut dispatcher. "
                "Keep the destination app focused when you stop recording."
                if available() else
                " This Wayland desktop requires manual clipboard paste."
            )
        self.auto_paste_tile = SettingTile(
            "Paste into the active window",
            auto_paste_description,
            design_icon("bolt-green.svg"),
        )
        self.auto_paste_check = self.auto_paste_tile.checkbox

        self.restore_clipboard_tile = SettingTile(
            "Restore the clipboard after pasting",
            "Puts back what you had copied once a paste lands, including "
            "Command Mode rewrites. Turn off to keep the pasted text on the "
            "clipboard.",
            design_icon("refresh-blue.svg"),
        )
        self.restore_clipboard_check = self.restore_clipboard_tile.checkbox

        self.copy_clipboard_tile = SettingTile(
            "Copy to the clipboard",
            "Keeps the transcript on the clipboard when auto-paste is off or "
            "unavailable.",
            design_icon("stack-slate.svg"),
        )
        self.copy_clipboard_check = self.copy_clipboard_tile.checkbox

        self.minimize_tray_tile = SettingTile(
            "Minimize to the system tray on close",
            "Closing the window keeps OpenWhisper running in the tray, so "
            "hotkeys and recording stay available.",
            design_icon("box-blue.svg"),
        )
        self.minimize_tray_check = self.minimize_tray_tile.checkbox
        if not self._tray_available:
            self.minimize_tray_tile.setEnabled(False)
            self.minimize_tray_tile.set_description(
                "This desktop session does not provide a system tray."
            )

        self.update_check_tile = SettingTile(
            "Check for updates automatically",
            "When OpenWhisper starts and every 4 hours while it runs, asks "
            "GitHub (api.github.com) for the latest release. The request "
            "includes the app version, no account or device ID. Nothing is "
            "installed without your approval.",
            design_icon("refresh-blue.svg"),
        )
        self.update_check_check = self.update_check_tile.checkbox
        self.update_check_check.setObjectName("updateCheckEnabledCheck")

        self.update_notify_tile = SettingTile(
            "Notify me about updates",
            "Shows an Update button in the title bar and the tray menu when "
            "a newer release is found. Requires automatic checks.",
            design_icon("info-blue.svg"),
        )
        self.update_notify_check = self.update_notify_tile.checkbox
        self.update_notify_check.setObjectName("updateNotifyEnabledCheck")

        self.update_restart_prompt_tile = SettingTile(
            "Ask me to restart after an update",
            "When OpenWhisper is updated while it is running, offers to "
            "restart so the update finishes. Turn off if you chose "
            "\"Don't show this again\" and changed your mind.",
            design_icon("refresh-blue.svg"),
        )
        self.update_restart_prompt_check = self.update_restart_prompt_tile.checkbox
        self.update_restart_prompt_check.setObjectName("updateRestartPromptCheck")

        self._tile_group(
            layout,
            "Output",
            [self.auto_paste_tile, self.restore_clipboard_tile, self.copy_clipboard_tile],
        )
        if sys.platform == "darwin":
            permission_row = QHBoxLayout()
            self.accessibility_status = QLabel()
            self.accessibility_status.setWordWrap(True)
            permission_row.addWidget(self.accessibility_status, 1)
            self.accessibility_setup_button = QPushButton("Set up auto-paste…")
            self.accessibility_setup_button.clicked.connect(self._open_accessibility_setup)
            permission_row.addWidget(self.accessibility_setup_button)
            layout.addLayout(permission_row)
            self._accessibility_timer = QTimer(self)
            self._accessibility_timer.setInterval(1000)
            self._accessibility_timer.timeout.connect(self._refresh_accessibility_status)
            self._refresh_accessibility_status()
        self._tile_group(layout, "Window", [self.minimize_tray_tile])
        self._tile_group(
            layout,
            "Updates",
            [
                self.update_check_tile,
                self.update_notify_tile,
                self.update_restart_prompt_tile,
            ],
        )

        self._bindings.checkbox(
            self.auto_paste_check,
            SettingsKey.AUTO_PASTE,
            lambda settings: setting_value(SettingsKey.AUTO_PASTE, settings),
        )
        self._bindings.checkbox(
            self.restore_clipboard_check,
            SettingsKey.RESTORE_CLIPBOARD,
            lambda settings: setting_value(SettingsKey.RESTORE_CLIPBOARD, settings),
        )
        self._bindings.checkbox(
            self.copy_clipboard_check,
            SettingsKey.COPY_CLIPBOARD,
            lambda settings: setting_value(SettingsKey.COPY_CLIPBOARD, settings),
        )
        self._bindings.checkbox(
            self.minimize_tray_check,
            SettingsKey.MINIMIZE_TRAY,
            lambda settings: (
                self._tray_available
                and setting_value(SettingsKey.MINIMIZE_TRAY, settings)
            ),
        )
        self.update_check_check.toggled.connect(self._on_update_check_toggled)
        self._bindings.checkbox(
            self.update_notify_check,
            SettingsKey.UPDATE_NOTIFY_ENABLED,
            resolve_update_notify_enabled,
        )
        self.update_notify_check.toggled.connect(self._on_update_notify_toggled)
        self._bindings.checkbox(
            self.update_restart_prompt_check,
            SettingsKey.UPDATE_RESTART_PROMPT_ENABLED,
            resolve_update_restart_prompt_enabled,
        )

    def _build_walkthroughs_page(self, layout: QVBoxLayout) -> None:
        """Everything about walkthroughs: shortcuts, the document, the write-up.

        The shortcut rows are the ones Hotkeys shows too; they share its
        capture, conflict checks and Reset to defaults.
        """
        layout.addWidget(self._hotkey_group_title("Shortcuts"))
        self._add_walkthrough_shortcut_rows(layout, WALKTHROUGHS)
        self._build_walkthrough_group(layout)

    def _add_walkthrough_shortcut_rows(self, layout: QVBoxLayout, page: str) -> None:
        layout.addWidget(
            self._hotkey_shortcut_row(
                "walkthrough",
                "Walkthrough",
                "Talk while you take screenshots. Press again for one file with "
                "your words and pictures, copied to paste into an agent.",
                optional=True,
                page=page,
            )
        )
        layout.addWidget(
            self._hotkey_shortcut_row(
                "screenshot",
                "Screenshot",
                "Freeze the screen and mark it up. During a walkthrough it joins "
                "the walkthrough; otherwise it's copied.",
                optional=True,
                page=page,
            )
        )

    def _build_walkthrough_group(self, layout: QVBoxLayout) -> None:
        """What a walkthrough's PDF and Markdown show besides your words and screenshots."""
        self.walkthrough_copy_format_combo = ElidingComboBox()
        self.walkthrough_copy_format_combo.setObjectName("walkthroughCopyFormatCombo")
        self.walkthrough_copy_format_combo.setAccessibleName("Walkthrough default copy")
        for value in WalkthroughCopyFormat.ALL:
            self.walkthrough_copy_format_combo.addItem(WalkthroughCopyFormat.LABELS[value], value)
        self.walkthrough_copy_format_combo.setMinimumHeight(40)
        self.walkthrough_copy_format_tile = FieldTile(
            "Default copy",
            "What goes on the clipboard when a walkthrough finishes. "
            "PDF copy (the default) attaches the document; Markdown copy is text with a file "
            "link to each screenshot. PDF and Markdown copy copies both at once; "
            "the app you paste into chooses which format to use.",
            self.walkthrough_copy_format_combo,
            design_icon("copy-gray.svg"),
        )
        self._tile_group(layout, "Copy", [self.walkthrough_copy_format_tile])
        self._bindings.combo(
            self.walkthrough_copy_format_combo,
            SettingsKey.WALKTHROUGH_COPY_FORMAT,
            resolve_walkthrough_copy_format,
        )

        self.walkthrough_title_edit = QLineEdit()
        self.walkthrough_title_edit.setObjectName("walkthroughTitleEdit")
        self.walkthrough_title_edit.setPlaceholderText("No title")
        self.walkthrough_title_edit.setAccessibleName("Walkthrough document title")
        self.walkthrough_title_edit.setMinimumHeight(40)
        self.walkthrough_title_tile = FieldTile(
            "Title",
            "The heading at the top of the document. Leave it empty for none.",
            self.walkthrough_title_edit,
            design_icon("typography-blue.svg"),
        )

        self.walkthrough_page_size_combo = ElidingComboBox()
        self.walkthrough_page_size_combo.setObjectName("walkthroughPageSizeCombo")
        for value in WalkthroughPageSize.ALL:
            self.walkthrough_page_size_combo.addItem(WalkthroughPageSize.LABELS[value], value)
        self.walkthrough_page_size_combo.setMinimumHeight(40)
        self.walkthrough_page_size_tile = FieldTile(
            "Paper size",
            "The PDF's page size. The Markdown copy has no pages.",
            self.walkthrough_page_size_combo,
            design_icon("stack-slate.svg"),
        )

        self.walkthrough_details_tile = SettingTile(
            "Date, length and screenshot count",
            "A line under the title, such as \"Tue, Oct 20, 2026, 2:32 PM · "
            "2:16 of speech · 3 screenshots\".",
            design_icon("info-blue.svg"),
        )
        self.walkthrough_details_check = self.walkthrough_details_tile.checkbox

        self.walkthrough_about_tile = SettingTile(
            "How to read it",
            "A note saying the document is spoken notes with screenshots, "
            "each screenshot right after the words about it. It helps an "
            "agent that reads the document follow it.",
            design_icon("info-blue.svg"),
        )
        self.walkthrough_about_check = self.walkthrough_about_tile.checkbox

        self.walkthrough_captions_tile = SettingTile(
            "Screenshot captions",
            "\"Screenshot 2 · 1:12\" under each screenshot: its number and "
            "when you took it.",
            design_icon("palette-purple.svg"),
        )
        self.walkthrough_captions_check = self.walkthrough_captions_tile.checkbox

        self._tile_group(
            layout,
            "Document",
            [
                self.walkthrough_title_tile,
                self.walkthrough_page_size_tile,
                self.walkthrough_details_tile,
                self.walkthrough_about_tile,
                self.walkthrough_captions_tile,
            ],
            intro="How the PDF and Markdown look. Your words and screenshots "
            "are always included; changes apply to the next walkthrough.",
        )
        self._bindings.line_edit(
            self.walkthrough_title_edit,
            SettingsKey.WALKTHROUGH_TITLE,
            resolve_walkthrough_title,
        )
        self._bindings.combo(
            self.walkthrough_page_size_combo,
            SettingsKey.WALKTHROUGH_PAGE_SIZE,
            resolve_walkthrough_page_size,
        )
        self._bindings.checkbox(
            self.walkthrough_details_check,
            SettingsKey.WALKTHROUGH_SHOW_DETAILS,
            resolve_walkthrough_show_details,
        )
        self._bindings.checkbox(
            self.walkthrough_about_check,
            SettingsKey.WALKTHROUGH_SHOW_ABOUT,
            resolve_walkthrough_show_about,
        )
        self._bindings.checkbox(
            self.walkthrough_captions_check,
            SettingsKey.WALKTHROUGH_SHOW_CAPTIONS,
            resolve_walkthrough_show_captions,
        )
        self._build_walkthrough_write_up_group(layout)

    def _build_walkthrough_write_up_group(self, layout: QVBoxLayout) -> None:
        """Whether an installed agent writes a finished walkthrough up, which, and how."""
        from services.walkthrough.write_up import AGENTS, DEFAULT_INSTRUCTIONS, agent_name

        self.walkthrough_write_up_tile = SettingTile(
            "Write it up with an AI agent",
            "When a walkthrough finishes, your coding agent lays out a cleaner "
            "document from your words and screenshots, which is what gets "
            "copied. It uses your own plan and usually takes under a minute. "
            "The as-spoken PDF is kept beside it.",
            design_icon("wand-purple.svg"),
        )
        self.walkthrough_write_up_check = self.walkthrough_write_up_tile.checkbox

        self.walkthrough_write_up_agent_combo = ElidingComboBox()
        self.walkthrough_write_up_agent_combo.setObjectName("walkthroughWriteUpAgentCombo")
        for agent in AGENTS:
            self.walkthrough_write_up_agent_combo.addItem(agent_name(agent), agent)
        self.walkthrough_write_up_agent_combo.setMinimumHeight(40)
        self._write_up_scan_token = 0
        self._label_write_up_agents()
        self.walkthrough_write_up_agent_tile = FieldTile(
            "Agent",
            "Claude Code reads the walkthrough's PDF and can open only that "
            "folder. Codex and OpenCode open no files: they're sent the words "
            "and screenshots, so their model must take images.",
            self.walkthrough_write_up_agent_combo,
            design_icon("cpu-blue.svg"),
        )

        # Plain, not QTextEdit: the placeholder, the default instructions,
        # wraps instead of being clipped to one line.
        self.walkthrough_write_up_edit = QPlainTextEdit()
        self.walkthrough_write_up_edit.setObjectName("walkthroughWriteUpEditor")
        self.walkthrough_write_up_edit.setPlaceholderText(DEFAULT_INSTRUCTIONS)
        self.walkthrough_write_up_edit.setMinimumHeight(96)
        self.walkthrough_write_up_edit.setMaximumHeight(150)
        self.walkthrough_write_up_edit.setAccessibleName("Write-up instructions")
        self.walkthrough_write_up_instructions_tile = FieldTile(
            "Instructions",
            "What to make of your words and screenshots. While it's empty, "
            "the box shows the default.",
            self.walkthrough_write_up_edit,
            design_icon("typography-blue.svg"),
        )
        self.walkthrough_write_up_notes_tile = SettingTile(
            "Follow notes left for the agent",
            "While you talk, speak to the agent, such as \"note for the agent: "
            "make the text purple\" or \"for whoever writes this up, skip the "
            "setup part\". It follows the note instead of writing it into the "
            "document. Notes can change only what the write-up says and how it "
            "looks.",
            design_icon("notes-blue.svg"),
        )
        self.walkthrough_write_up_notes_check = self.walkthrough_write_up_notes_tile.checkbox
        # Notes are for the agent, so they mean nothing without a write-up.
        self.walkthrough_write_up_notes_tile.setEnabled(False)
        self.walkthrough_write_up_check.toggled.connect(
            self.walkthrough_write_up_notes_tile.setEnabled)
        self._tile_group(
            layout,
            "AI write-up",
            [self.walkthrough_write_up_tile, self.walkthrough_write_up_agent_tile,
             self.walkthrough_write_up_instructions_tile,
             self.walkthrough_write_up_notes_tile],
            columns=1,
            intro="Optional. Your words and screenshots stay as spoken unless you turn this on.",
        )
        self._bindings.checkbox(
            self.walkthrough_write_up_check,
            SettingsKey.WALKTHROUGH_WRITE_UP,
            resolve_walkthrough_write_up,
        )
        self._bindings.combo(
            self.walkthrough_write_up_agent_combo,
            SettingsKey.WALKTHROUGH_WRITE_UP_AGENT,
            resolve_walkthrough_write_up_agent,
        )
        self._bindings.plain_text(
            self.walkthrough_write_up_edit,
            SettingsKey.WALKTHROUGH_WRITE_UP_INSTRUCTIONS,
            resolve_walkthrough_write_up_instructions,
        )
        self._bindings.checkbox(
            self.walkthrough_write_up_notes_check,
            SettingsKey.WALKTHROUGH_WRITE_UP_NOTES,
            resolve_walkthrough_write_up_notes,
        )

    def _label_write_up_agents(self) -> None:
        """Mark the write-up agents the last scan didn't find; scan once if none has run."""
        from services.walkthrough.write_up import agent_name
        from ui_qt.widgets import action_relay, agent_picker

        found = agent_picker.cached_agents()
        if found is None:
            if not self._write_up_scan_token:
                action_relay.relay().scan_done.connect(self._on_write_up_scan_done)
                self._write_up_scan_token = token = action_relay.next_token()
                action_relay.start_agent_scan(token)
            return
        combo = self.walkthrough_write_up_agent_combo
        for index in range(combo.count()):
            agent = combo.itemData(index)
            name = agent_name(agent)
            combo.setItemText(index, name if found.get(agent) else f"{name} (not installed)")

    def _on_write_up_scan_done(self, token: int) -> None:
        if token == self._write_up_scan_token:
            self._label_write_up_agents()

    def _refresh_accessibility_status(self):
        from services.hotkey_manager import is_accessibility_trusted

        trusted = is_accessibility_trusted()
        self.accessibility_status.setText(
            "Accessibility access enabled."
            if trusted else "Auto-paste needs Accessibility access. Use ⌘V to paste for now."
        )
        self.accessibility_setup_button.setText(
            "Manage Accessibility…" if trusted else "Set up auto-paste…"
        )

    def _open_accessibility_setup(self):
        from ui_qt.dialogs.accessibility_dialog import show_accessibility_setup

        show_accessibility_setup(self)

    def _fit_to_screen(self) -> None:
        """Shrink the window to fit the screen it opens on, when it doesn't.

        DEFAULT_SIZE is 1315x850, but a 1920x1080 laptop panel at 150% is
        1280x720 with a bar across the top: the window was cut off on the
        right, and Hyprland, which places windows itself, centred it above
        the top edge (y=-34). QDialog centres a dialog over its parent and
        keeps its corner on screen, but never shrinks it.
        """
        parent = self.parentWidget()
        screen = (parent.screen() if parent is not None else None) or self.screen()
        if screen is None:
            return
        available = screen.availableGeometry()
        margin = 32  # Wayland reports the whole output, bars included
        room = QSize(max(1, available.width() - 2 * margin),
                     max(1, available.height() - 2 * margin))
        from services.desktop_session import use_omarchy_ui

        minimum = QSize(360, 240) if use_omarchy_ui() else self.MINIMUM_SIZE
        self.setMinimumSize(minimum.boundedTo(room))
        fitted = self.size().boundedTo(room)
        if fitted != self.size():
            self.resize(fitted)

    def _place_on_screen(self, available: Optional[QRect] = None) -> None:
        """Move the window wholly onto the screen its parent is on.

        QDialog centres it over the main window, and _fit_to_screen only
        shrinks it: on a 1024x640 screen it opened off the right edge and
        over the main window's footer. Left where it is when it fits, else
        centred on the screen. Wayland and tiling desktops place windows
        themselves.
        """
        from ui_qt.utils.desktop import compositor_managed, on_screen_position

        if compositor_managed():
            return
        if available is None:
            parent = self.parentWidget()
            screen = (parent.screen() if parent is not None else None) or self.screen()
            if screen is None:
                return
            available = screen.availableGeometry()
        frame = self.frameGeometry()
        if frame == self.geometry():
            frame = frame.adjusted(0, -self.TITLE_BAR_ALLOWANCE, 0, 0)
        position = on_screen_position(frame, available)
        if position != frame.topLeft():
            self.move(position)

    def showEvent(self, event):
        if not event.spontaneous():
            # Before QDialog's own showEvent, which centres by the size.
            self._fit_to_screen()
        super().showEvent(event)
        if not event.spontaneous():
            self._place_on_screen()
        if self._native_wayland and "_hotkey_instruction" in self.__dict__:
            from PyQt6.QtWidgets import QApplication

            desktop_status = QApplication.instance().property("omarchyShortcutStatus")
            suffix = f"\n\n{desktop_status}" if desktop_status else ""
            self._hotkey_instruction.setText(self._hotkey_instruction_text + suffix)
        if "_accessibility_timer" in self.__dict__:
            self._refresh_accessibility_status()
            self._accessibility_timer.start()
        if self._pages_ready and not event.spontaneous():
            with self._coalesced_rail_refresh():
                self.refresh_models()
                self._refresh_recordings_usage()

    def hideEvent(self, event):
        if "_accessibility_timer" in self.__dict__:
            self._accessibility_timer.stop()
        super().hideEvent(event)

    def _build_recording_page(self, layout: QVBoxLayout) -> None:
        # Looked up when called, so a replaced AudioRecorder is what lists.
        self._microphones = settings_microphones.MicrophoneSection(
            self, lambda: AudioRecorder.get_input_devices()
        )
        self._microphones.build(layout)
        self.audio_device_combo.currentIndexChanged.connect(
            self._on_audio_device_changed
        )

        self.recording_retention_combo = ElidingComboBox()
        self.recording_retention_combo.addItem(
            "Keep all", RecordingRetentionMode.KEEP_ALL
        )
        self.recording_retention_combo.addItem(
            "By count", RecordingRetentionMode.CUSTOM
        )
        self.recording_retention_combo.addItem(
            "By folder size", RecordingRetentionMode.SIZE_LIMIT
        )
        self.recording_retention_combo.setMinimumHeight(40)
        self.recording_retention_combo.currentIndexChanged.connect(
            self._on_retention_mode_changed
        )
        self.recording_retention_tile = FieldTile(
            "Keep recordings",
            "Keep a set number of recordings or cap the recordings folder "
            "size. The oldest audio files are deleted automatically once the "
            "limit is passed; the newest recording is always kept. "
            "Transcription history text is kept separately.",
            self.recording_retention_combo,
            design_icon("box-blue.svg"),
        )

        self.max_recordings_label = QLabel("Number to keep")
        self.max_recordings_spinbox = NoWheelSpinBox()
        self.max_recordings_spinbox.setMinimum(1)
        self.max_recordings_spinbox.setMaximum(1000)
        self.max_recordings_spinbox.setValue(config.MAX_SAVED_RECORDINGS)
        self.max_recordings_spinbox.setMinimumHeight(40)
        # Retention deletes audio, so typed digits commit on Enter or
        # focus-out: tracking every keystroke once applied "1" on the way
        # from 20 to 100 and pruned all but one recording.
        self.max_recordings_spinbox.setKeyboardTracking(False)
        self.max_recordings_spinbox.valueChanged.connect(
            self._schedule_retention_commit
        )
        self.max_recordings_spinbox.editingFinished.connect(
            self._commit_retention
        )
        self.max_recordings_mb_label = QLabel("Folder size limit")
        self.max_recordings_mb_label.setObjectName("settingsTileFieldLabel")
        self.max_recordings_mb_spinbox = NoWheelSpinBox()
        self.max_recordings_mb_spinbox.setMinimum(10)
        self.max_recordings_mb_spinbox.setMaximum(1024 * 1024)
        self.max_recordings_mb_spinbox.setSingleStep(100)
        self.max_recordings_mb_spinbox.setSuffix(" MB")
        self.max_recordings_mb_spinbox.setValue(config.MAX_SAVED_RECORDINGS_MB)
        self.max_recordings_mb_spinbox.setMinimumHeight(40)
        # One width for both limits, wide enough for the largest folder size.
        retention_width = round(156 * current_ui_font_scale())
        self.max_recordings_spinbox.setFixedWidth(retention_width)
        self.max_recordings_mb_spinbox.setFixedWidth(retention_width)
        self.max_recordings_mb_spinbox.setKeyboardTracking(False)
        self.max_recordings_mb_spinbox.valueChanged.connect(
            self._schedule_retention_commit
        )
        self.max_recordings_mb_spinbox.editingFinished.connect(
            self._commit_retention
        )
        retention_form = self._spin_form(
            self.max_recordings_label, self.max_recordings_spinbox
        )
        retention_form.addRow(
            self.max_recordings_mb_label, self.max_recordings_mb_spinbox
        )
        self.recording_retention_tile.add_body_layout(retention_form)
        self.recordings_usage_label = settings_caption("")
        self.recording_retention_tile.add_body(self.recordings_usage_label)
        self._tile_group(
            layout, "Saved recordings", [self.recording_retention_tile]
        )

        self.streaming_enabled_tile = SettingTile(
            "Real-time transcription preview",
            "Shows text as you speak on the near-cursor overlay. Nemotron uses "
            "native streaming; Parakeet transcribes short audio chunks with the "
            "loaded model; Local Whisper uses a separate tiny.en preview model. "
            "The final transcript uses your selected model and follows the "
            "General paste and clipboard settings. Its text size is under "
            "Appearance.",
            design_icon("bolt-green.svg"),
        )
        self.streaming_enabled_check = self.streaming_enabled_tile.checkbox
        self.streaming_enabled_check.toggled.connect(
            self._on_streaming_enabled_changed
        )
        self._tile_group(layout, "Live preview", [self.streaming_enabled_tile])

    def _build_appearance_page(self, layout: QVBoxLayout) -> None:
        self.ui_theme_combo = ElidingComboBox()
        self.ui_theme_combo.setObjectName("settingsUiThemeCombo")
        self.ui_theme_combo.setMinimumHeight(40)
        self.ui_theme_combo.setMinimumWidth(140)
        for theme in UiTheme.ALL:
            self.ui_theme_combo.addItem(UiTheme.LABELS[theme], theme)
        self.ui_theme_combo.setCurrentIndex(
            max(0, self.ui_theme_combo.findData(config.UI_THEME))
        )
        self.ui_theme_combo.currentIndexChanged.connect(self._on_ui_theme_changed)
        self.ui_theme_tile = FieldTile(
            "Theme",
            "Dark, light, or match your operating system.",
            self.ui_theme_combo,
            design_icon("palette-purple.svg"),
        )

        self.ui_font_scale_combo = ElidingComboBox()
        self.ui_font_scale_combo.setObjectName("settingsUiFontScaleCombo")
        self.ui_font_scale_combo.setMinimumHeight(40)
        self.ui_font_scale_combo.setMinimumWidth(140)
        for percent in UiFontScale.ALL:
            self.ui_font_scale_combo.addItem(
                UiFontScale.LABELS[percent], percent
            )
        self.ui_font_scale_combo.setCurrentIndex(
            max(0, self.ui_font_scale_combo.findData(UiFontScale.DEFAULT))
        )
        self.ui_font_scale_combo.currentIndexChanged.connect(
            self._on_ui_font_scale_changed
        )
        self.ui_font_scale_tile = FieldTile(
            "Font size",
            "Text size in windows and dialogs. The live preview has its own "
            "size below.",
            self.ui_font_scale_combo,
            design_icon("typography-blue.svg"),
        )
        self._tile_group(
            layout, "Theme and text", [self.ui_theme_tile, self.ui_font_scale_tile]
        )

        self.recording_look_bar = SegmentedBar([
            (RecordingOverlayLook.LABELS[look], RecordingOverlayLook.DETAILS[look])
            for look in RecordingOverlayLook.ALL
        ])
        self.recording_look_bar.setAccessibleName("Recording overlay look")
        # activated, not currentIndexChanged: loading the saved look must not
        # save it back.
        self.recording_look_bar.activated.connect(self._on_recording_look_activated)
        self.recording_look_preview = RecordingLookPreview(
            recorder_factory=self._recording_look_recorder,
            microphone_name=lambda: settings_microphones.preferred_name(self._settings_snapshot()),
        )
        self.recording_look_tile = FieldTile(
            "Recording overlay",
            "How the overlay near your pointer looks while you speak. Every look "
            "but Classic moves with your voice; Classic is the original particles.",
            self.recording_look_bar,
            design_icon("palette-purple.svg"),
        )
        # The label switches sit beside the preview they change, in a narrow
        # column, so each caption stays next to its switch.
        parts = QWidget()
        parts.setObjectName("recordingLookParts")
        parts_layout = QVBoxLayout(parts)
        parts_layout.setContentsMargins(0, 6, 0, 0)
        parts_layout.setSpacing(6)
        parts_title = QLabel("Show on the overlay")
        parts_title.setObjectName("settingsTileFieldLabel")
        parts_layout.addWidget(parts_title)
        self.recording_dot_switch = self._overlay_part_switch(
            parts_layout, "Live dot", SettingsKey.RECORDING_OVERLAY_DOT)
        self.recording_text_switch = self._overlay_part_switch(
            parts_layout, "\u201cRecording\u201d label", SettingsKey.RECORDING_OVERLAY_TEXT)
        self.recording_clock_switch = self._overlay_part_switch(
            parts_layout, "Elapsed time", SettingsKey.RECORDING_OVERLAY_CLOCK)
        parts_layout.addStretch(1)
        self.recording_parts = parts
        look_row = QHBoxLayout()
        look_row.setContentsMargins(0, 4, 0, 0)
        look_row.setSpacing(24)
        look_row.addWidget(self.recording_look_preview, 1)
        look_row.addWidget(parts, 0)
        self.recording_look_tile.add_body_layout(look_row)

        self.streaming_font_size_spinbox = NoWheelSpinBox()
        self.streaming_font_size_spinbox.setAccessibleName("Live preview text size")
        self.streaming_font_size_spinbox.setMinimum(10)
        self.streaming_font_size_spinbox.setMaximum(48)
        self.streaming_font_size_spinbox.setSuffix(" pt")
        self.streaming_font_size_spinbox.setValue(config.STREAMING_OVERLAY_FONT_SIZE)
        self.streaming_font_size_spinbox.setMinimumHeight(40)
        self.streaming_font_size_spinbox.setMinimumWidth(120)
        self.streaming_font_size_spinbox.setKeyboardTracking(False)
        self.streaming_font_size_spinbox.valueChanged.connect(
            self._on_streaming_font_changed
        )
        # Left enabled while the preview is off: the switch is on another
        # page, and a size picked ahead of time is kept.
        self.streaming_font_size_tile = FieldTile(
            "Live preview text size",
            "The words that appear near your pointer as you speak, when "
            "Real-time transcription preview is on under Recording.",
            self.streaming_font_size_spinbox,
            design_icon("typography-blue.svg"),
            compact=True,
        )
        self.streaming_layout_bar = SegmentedBar(
            [
                (StreamingOverlayLayout.LABELS[choice], StreamingOverlayLayout.DETAILS[choice])
                for choice in StreamingOverlayLayout.ALL
            ],
            compact=True,
        )
        self.streaming_layout_bar.setAccessibleName("Live preview layout")
        self.streaming_layout_bar.activated.connect(self._on_streaming_layout_activated)
        self.streaming_layout_tile = FieldTile(
            "Live preview layout",
            "Where the look goes once your words appear: shrunk into a header "
            "above them, or full size under them.",
            self.streaming_layout_bar,
            design_icon("palette-purple.svg"),
        )
        self._tile_group(
            layout,
            "Overlay",
            [self.recording_look_tile, self.streaming_layout_tile, self.streaming_font_size_tile],
            columns=1,
        )

    def _overlay_part_switch(self, layout: QVBoxLayout, text: str, key: str) -> SettingsSwitch:
        """A labelled switch for one part of the live recording label."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(16)
        label = QLabel(text)
        row.addWidget(label, 1)
        switch = SettingsSwitch()
        switch.setAccessibleName(f"Show the {text}")
        switch.setChecked(True)
        switch.toggled.connect(lambda on, key=key: self._on_overlay_part_toggled(key, on))
        row.addWidget(switch, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(row)
        return switch

    def _on_overlay_part_toggled(self, key: str, on: bool) -> None:
        self._show_overlay_parts()
        if not self._persist(key, bool(on)):
            return
        if self.on_recording_look_changed:
            self.on_recording_look_changed()

    def _show_overlay_parts(self, look: Optional[str] = None) -> None:
        """Pass the label switches to the preview; Classic has no live label.

        ``look`` is the one just clicked: the bar's current index changes only
        after its ``activated`` signal.
        """
        self.recording_look_preview.set_label_parts(
            self.recording_dot_switch.isChecked(),
            self.recording_text_switch.isChecked(),
            self.recording_clock_switch.isChecked(),
        )
        look = look or RecordingOverlayLook.ALL[self.recording_look_bar.currentIndex()]
        self.recording_parts.setEnabled(look != RecordingOverlayLook.LEGACY)

    def _recording_look_recorder(self) -> AudioRecorder:
        """A throwaway recorder on the saved microphone, for the look preview."""
        return AudioRecorder(
            device_priority=settings_microphones.saved_priority(self._settings_snapshot()),
            output_file=RecordingLookPreview.preview_path(),
        )

    def _on_recording_look_activated(self, index: int) -> None:
        look = RecordingOverlayLook.ALL[index]
        self.recording_look_preview.set_look(look)
        self._show_overlay_parts(look)
        if not self._persist(SettingsKey.RECORDING_OVERLAY_LOOK, look):
            return
        if self.on_recording_look_changed:
            self.on_recording_look_changed()

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
        self.transcript_cleanup_tile = SettingTile(
            "Clean up transcripts with AI",
            "Runs the selected language model on each dictated transcript after "
            "transcription, together with your learned rules. The provider's "
            "key lives under API keys.",
            design_icon("stack-purple.svg"),
        )
        self.transcript_cleanup_check = self.transcript_cleanup_tile.checkbox
        self.transcript_cleanup_check.toggled.connect(
            self._on_cleanup_enabled_changed
        )
        self.cleanup_level_bar = SegmentedBar(
            [(title, detail) for _value, title, detail in cleanup_levels.SEGMENTS]
        )
        self.cleanup_level_bar.setAccessibleName("Cleanup level")
        # activated, not currentIndexChanged: loading the saved level must
        # not save it back or ask about a custom prompt.
        self.cleanup_level_bar.activated.connect(self._on_cleanup_level_activated)
        custom_segment = self.cleanup_level_bar.buttons[-1]
        custom_segment.setToolTip("Write a custom prompt below to use it instead of a level")
        self.cleanup_level_tile = FieldTile(
            "Cleanup level",
            "How much the AI changes your words. Every level keeps the "
            "language you spoke and never adds anything you didn't say.",
            self.cleanup_level_bar,
            design_icon("wand-purple.svg"),
        )
        self.cleanup_level_note = WrappedLabel("")
        self.cleanup_level_note.setObjectName("cleanupLevelNote")
        self.cleanup_level_tile.add_body(self.cleanup_level_note)
        # No captions: each card's own title already says what it is.
        self._tile_group(
            layout, "", [self.transcript_cleanup_tile, self.cleanup_level_tile], columns=1
        )

        self.cleanup_model_tile = self.models.build_cleanup_model_section(layout)
        layout.addSpacing(6)

        # Plain, not QTextEdit: its placeholder, the level's instructions,
        # wraps instead of being clipped to one line.
        self.cleanup_prompt_edit = QPlainTextEdit()
        self.cleanup_prompt_edit.setObjectName("cleanupPromptEditor")
        self.cleanup_prompt_edit.setMinimumHeight(96)
        self.cleanup_prompt_edit.setMaximumHeight(120)
        self.cleanup_prompt_edit.setAccessibleName("Custom prompt")
        self.cleanup_prompt_edit.installEventFilter(self)
        self.cleanup_prompt_tile = FieldTile(
            "Custom prompt (optional)",
            "Replaces the level's instructions with your own. Learned rules "
            "still apply. While it's empty, the box previews the level's "
            "instructions.",
            self.cleanup_prompt_edit,
            design_icon("typography-blue.svg"),
        )

        self.cleanup_prompt_edit_btn = neutral_button(Button("Open editor…"))
        self.cleanup_prompt_edit_btn.setToolTip(
            "Edit in a larger window, starting from the level's instructions"
        )
        fit_compact_button(self.cleanup_prompt_edit_btn, 0)
        self.cleanup_prompt_edit_btn.clicked.connect(self._open_cleanup_prompt_editor)
        self.cleanup_prompt_reset_btn = neutral_button(Button("Remove custom prompt"))
        self.cleanup_prompt_reset_btn.setToolTip("Go back to the level's instructions")
        fit_compact_button(self.cleanup_prompt_reset_btn, 0)
        self.cleanup_prompt_reset_btn.clicked.connect(self._reset_cleanup_prompt)
        # Wraps rather than overflowing a narrow tiled window at 130%.
        self.cleanup_prompt_tile.add_body(
            ButtonRow((self.cleanup_prompt_edit_btn, self.cleanup_prompt_reset_btn))
        )
        self.cleanup_prompt_edit.textChanged.connect(self._update_cleanup_prompt_buttons)
        self._tile_group(layout, "", [self.cleanup_prompt_tile])
        # The bar selects a clicked segment after its activated handlers run,
        # so a declined switch away from Custom is put back on the next turn.
        self._cleanup_level_resync = QTimer(self)
        self._cleanup_level_resync.setSingleShot(True)
        self._cleanup_level_resync.setInterval(0)
        self._cleanup_level_resync.timeout.connect(self._update_cleanup_level_ui)

    def cleanup_gate_tile(self, description: str, *, needed: Optional[Callable[[], bool]] = None) -> InfoTile:
        """An "AI cleanup is off" notice whose button turns cleanup on.

        It is hidden while cleanup is on. Pages whose settings only matter
        when cleanup runs (Learned rules, Apps & styles) put one above them.
        ``needed``, when given, must also return True for it to show; call
        :meth:`refresh_cleanup_gates` when its answer may have changed.
        """
        tile = InfoTile(
            "AI cleanup is off", description, design_icon("info-warning.svg")
        )
        tile.setProperty("kind", "notice")
        button = compact_primary_button(Button("Turn on"))
        button.clicked.connect(self.turn_on_cleanup)
        tile.add_actions(button)
        self._cleanup_gates.append((tile, button))
        if needed is not None:
            self._cleanup_gate_needed[tile] = needed
        self.refresh_cleanup_gates()
        return tile

    def refresh_cleanup_gates(self) -> None:
        """Show or hide every "AI cleanup is off" notice for the saved settings."""
        level = cleanup_prompts.resolve_level(self._settings_snapshot())
        self._update_cleanup_gates(level != CleanupLevel.NONE)

    def turn_on_cleanup(self) -> None:
        """Turn AI cleanup on at its saved level, as its switch does."""
        switch = self.__dict__.get("transcript_cleanup_check")
        if switch is not None:
            switch.setChecked(True)
        else:
            self._on_cleanup_enabled_changed(True)

    def _update_cleanup_gates(self, enabled: bool) -> None:
        settings = self._settings_snapshot()
        if cleanup_prompts.custom_prompt(settings):
            name = cleanup_prompts.CUSTOM_LABEL
        else:
            name = CleanupLevel.LABELS[resolve_transcript_cleanup_level(settings)]
        for tile, button in self._cleanup_gates:
            button.setText(f"Turn on ({name})")
            needed = self._cleanup_gate_needed.get(tile)
            if enabled or (needed is not None and not needed()):
                tile.hide()
            elif tile.parentWidget() is not None:
                # Showing a tile before its page adopts it would open it
                # as a window of its own.
                tile.show()

    def _build_cleanup_profiles_page(self, layout: QVBoxLayout) -> None:
        self.cleanup_profiles_panel = CleanupProfilesPanel(manager=settings_manager)
        self.cleanup_profiles_panel.profiles_changed.connect(self._on_profiles_saved)
        self.cleanup_profiles_panel.capture_changed.connect(self._on_profile_capture)
        self.cleanup_profiles_panel.model_requested.connect(self.focus_cleanup_model)
        layout.addWidget(self.cleanup_profiles_panel)

    def _on_profiles_saved(self) -> None:
        self._refresh_rail_values()
        if self.on_cleanup_profiles_changed:
            self.on_cleanup_profiles_changed()

    def _on_profile_capture(self, suspended: bool) -> None:
        if self.on_profile_hotkey_capture:
            self.on_profile_hotkey_capture(suspended)

    def _build_cleanup_rules_page(self, layout: QVBoxLayout) -> None:
        self.cleanup_rules_gate_tile = self.cleanup_gate_tile(
            "Learned rules only apply when AI cleanup runs, so teaching and "
            "editing them waits until it's on."
        )
        self.cleanup_rules_turn_on_btn = self._cleanup_gates[-1][1]
        layout.addWidget(self.cleanup_rules_gate_tile)

        self.cleanup_rules_composer_tile = InfoTile(
            "Teach a new rule",
            "Type or dictate an instruction in your own words. Your AI "
            "cleanup model rewrites it as a clear rule, and you review it "
            "before it's saved.",
            design_icon("plus-blue.svg"),
        )
        rule_input_row = QHBoxLayout()
        rule_input_row.setContentsMargins(0, 0, 0, 0)
        rule_input_row.setSpacing(8)
        self.cleanup_rule_input = QLineEdit()
        self.cleanup_rule_input.setObjectName("cleanupRuleInput")
        self.cleanup_rule_input.setMinimumHeight(40)
        self.cleanup_rule_input.setPlaceholderText(
            'Try: Always spell my name "Alex Rivera"'
        )
        self.cleanup_rule_input.returnPressed.connect(self._add_cleanup_rule)
        self.cleanup_rule_input.textEdited.connect(
            lambda _text: self._set_rule_notice("")
        )
        rule_input_row.addWidget(self.cleanup_rule_input, stretch=1)
        self.cleanup_rule_mic_btn = Button("Dictate")
        self.cleanup_rule_mic_btn.setObjectName("cleanupRuleDictateButton")
        # theme.qss swaps the icon (microphone / stop) on this property.
        self.cleanup_rule_mic_btn.setProperty("recording", False)
        self.cleanup_rule_mic_btn.setIconSize(QSize(16, 16))
        self.cleanup_rule_mic_btn.set_base_minimum_size(108, 40)
        self.cleanup_rule_mic_btn.setToolTip(
            "Speak the instruction instead of typing it"
        )
        self.cleanup_rule_mic_btn.clicked.connect(self._toggle_rule_dictation)
        rule_input_row.addWidget(self.cleanup_rule_mic_btn)
        self.cleanup_rule_add_btn = PrimaryButton("Add rule")
        self.cleanup_rule_add_btn.setObjectName("cleanupRuleAddButton")
        self.cleanup_rule_add_btn.set_base_minimum_size(104, 40)
        self.cleanup_rule_add_btn.clicked.connect(self._add_cleanup_rule)
        rule_input_row.addWidget(self.cleanup_rule_add_btn)
        # No spacing between the input row and the activity strip: the strip
        # carries its own gap inside the height it animates, so it slides
        # open from nothing.
        composer_column = QVBoxLayout()
        composer_column.setContentsMargins(0, 0, 0, 0)
        composer_column.setSpacing(0)
        composer_column.addLayout(rule_input_row)
        self.cleanup_rule_activity = RuleActivityStrip(
            _RULE_DICTATION_CAP_MS // 1000
        )
        composer_column.addWidget(self.cleanup_rule_activity)
        self.cleanup_rules_composer_tile.add_body_layout(composer_column)
        # Problems only; progress is narrated by the activity strip.
        self.cleanup_rule_status = QLabel("")
        self.cleanup_rule_status.setObjectName("cleanupRuleStatus")
        self.cleanup_rule_status.setWordWrap(True)
        self.cleanup_rule_status.hide()
        self.cleanup_rules_composer_tile.add_body(self.cleanup_rule_status)
        self._tile_group(
            layout, "", [self.cleanup_rules_composer_tile], columns=1
        )

        self.cleanup_rules_library_tile = InfoTile(
            "Your rules",
            "Select a rule or double-click it to edit. Every rule applies to "
            "AI cleanup, and to profiles set to also apply learned rules.",
            design_icon("stack-slate.svg"),
        )
        self.cleanup_rules_count = QLabel()
        self.cleanup_rules_count.setObjectName("cleanupRulesCount")
        self.cleanup_rules_count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cleanup_rules_library_tile.add_trailing(self.cleanup_rules_count)

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
        self.cleanup_rules_library_tile.add_body(self.cleanup_rules_list)
        self._rule_glow = ItemGlow(self.cleanup_rules_list)

        self.cleanup_rules_empty = EmptyState(
            "No rules yet",
            "Add your first instruction above to start building a personal cleanup profile.",
        )
        self.cleanup_rules_library_tile.add_body(self.cleanup_rules_empty)

        rule_btn_row = QHBoxLayout()
        rule_btn_row.setContentsMargins(0, 0, 0, 0)
        rule_btn_row.setSpacing(8)
        rule_btn_row.addStretch()
        self.cleanup_rule_edit_btn = neutral_button(Button("Edit rule"))
        self.cleanup_rule_edit_btn.setObjectName("cleanupRuleEditButton")
        self.cleanup_rule_edit_btn.set_base_minimum_size(96, 34)
        self.cleanup_rule_edit_btn.clicked.connect(self._edit_cleanup_rule)
        rule_btn_row.addWidget(self.cleanup_rule_edit_btn)
        self.cleanup_rule_delete_btn = neutral_button(Button("Delete"))
        self.cleanup_rule_delete_btn.setObjectName("cleanupRuleDeleteButton")
        self.cleanup_rule_delete_btn.set_base_minimum_size(88, 34)
        self.cleanup_rule_delete_btn.clicked.connect(self._delete_cleanup_rule)
        rule_btn_row.addWidget(self.cleanup_rule_delete_btn)
        self.cleanup_rules_library_tile.add_body_layout(rule_btn_row)
        self._tile_group(
            layout, "", [self.cleanup_rules_library_tile], columns=1
        )
        self._update_cleanup_prompt_ui()

    def _build_meeting_intelligence_page(self, layout: QVBoxLayout) -> None:
        # Who runs AI insights, then the packaged SDK's insights model.
        self.meeting_model_tile = self.models.build_meeting_model_section(layout)
        self.meeting_agent_picker = self.models.meeting_agent_picker
        layout.addSpacing(6)

        self.meeting_past_recall_tile = SettingTile(
            "Search past transcripts",
            "Off by default. The agent can look up names and prior decisions "
            "from stored meetings. Excerpts leave this machine the same way "
            "the current transcript does.",
            design_icon("stack-purple.svg"),
        )
        self.meeting_past_recall_check = self.meeting_past_recall_tile.checkbox
        self.meeting_past_recall_check.setObjectName("meetingPastRecallCheck")
        self._meeting_bindings.checkbox(
            self.meeting_past_recall_check,
            SettingsKey.MEETING_PAST_RECALL_ENABLED,
            resolve_meeting_past_recall_enabled,
        )

        self.meeting_context_folder_tile = SettingTile(
            "Search a knowledge folder",
            "Choose a local folder, for example an Obsidian vault. Matched "
            "excerpts leave this machine the same way the current transcript "
            "does. Images, audio, and video are not read.",
            design_icon("stack-slate.svg"),
        )
        self.meeting_context_folder_check = (
            self.meeting_context_folder_tile.checkbox
        )
        self.meeting_context_folder_check.setObjectName(
            "meetingContextFolderCheck"
        )
        self._meeting_bindings.checkbox(
            self.meeting_context_folder_check,
            SettingsKey.MEETING_CONTEXT_FOLDER_ENABLED,
            resolve_meeting_context_folder_enabled,
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
                "AI insights do not upload audio."
            ),
        )

    def _build_meeting_fast_page(self, layout: QVBoxLayout) -> None:
        # Every switch on this page is inert without a key, and the features
        # themselves degrade silently by design, so the page has to say so.
        self.typesafe_key_notice = InfoTile(
            "No TypeSafe API key",
            "",
            design_icon("info-warning.svg"),
        )
        self.typesafe_key_notice.setProperty("kind", "notice")
        self.open_typesafe_key_btn = compact_primary_button(Button("Add a key"))
        self.open_typesafe_key_btn.setObjectName("typesafeKeyNoticeLink")
        self.open_typesafe_key_btn.setToolTip(
            "Open API keys with the TypeSafe credential selected"
        )
        self.open_typesafe_key_btn.clicked.connect(
            lambda: self.focus_api_keys(self._selected_typesafe_route().credential_env)
        )
        # The quickest fix when the other route already has a key: one click
        # moves Fast judgments onto it. Shown only in that case.
        self.typesafe_switch_route_btn = compact_primary_button(Button("Use OpenRouter"))
        self.typesafe_switch_route_btn.clicked.connect(self._switch_typesafe_route)
        self.typesafe_switch_route_btn.hide()
        self.typesafe_key_notice.add_actions(
            self.typesafe_switch_route_btn, self.open_typesafe_key_btn
        )
        layout.addWidget(self.typesafe_key_notice)

        # Jev either way for the first two, the choice being whose key pays
        # and whether OpenRouter sits in between; or one of the user's own
        # text models, which can keep every judgment on this computer.
        self.typesafe_route_tile = InfoTile(
            "Judge",
            "",
            design_icon("world-blue.svg"),
        )
        self.typesafe_route_bar = SegmentedBar(
            [(_JUDGE_TITLES[route_id], "") for route_id in TypeSafeProvider.ALL]
        )
        self.typesafe_route_bar.setAccessibleName("Run fast judgments on")
        self.typesafe_route_bar.activated.connect(self._on_typesafe_route_activated)
        self.typesafe_route_tile.add_body(self.typesafe_route_bar)
        # The text models are set up on the Models page; this only picks one.
        self.typesafe_text_model_bar = SegmentedBar(
            [(_JUDGE_TEXT_MODEL_TITLES[source], "") for source in TypeSafeTextModel.ALL],
            compact=True,
        )
        self.typesafe_text_model_bar.setAccessibleName("Text model that answers fast judgments")
        self.typesafe_text_model_bar.activated.connect(self._on_typesafe_text_model_activated)
        self.typesafe_text_model_bar.hide()
        self.typesafe_route_tile.add_body(self.typesafe_text_model_bar)
        self.typesafe_route_test_btn = neutral_button(Button("Test"))
        self.typesafe_route_test_btn.setToolTip(
            "Send one tiny judgment (the word “ok”) over this connection"
        )
        self.typesafe_route_test_btn.clicked.connect(self._test_typesafe_route)
        self.typesafe_route_tile.add_trailing(self.typesafe_route_test_btn)
        self.typesafe_route_status = WrappedLabel("")
        # Shares the API keys page's warning/success colours.
        self.typesafe_route_status.setObjectName("apiKeyStatus")
        self.typesafe_route_status.hide()
        self.typesafe_route_tile.add_body(self.typesafe_route_status)
        self._tile_group(layout, "", [self.typesafe_route_tile], columns=1)

        self.typesafe_enabled_tile = SettingTile(
            "Fast judgments (Experimental)",
            "Answers narrow yes/no questions about a minute of transcript: "
            "about 0.2 s with Jev, seconds with a text model. It never writes text.",
            design_icon("bolt-green.svg"),
        )
        self.typesafe_enabled_check = self.typesafe_enabled_tile.checkbox
        self.typesafe_enabled_check.setObjectName("typesafeEnabledCheck")
        self.typesafe_enabled_check.toggled.connect(self._on_typesafe_enabled_changed)

        self.typesafe_topic_shift_tile = SettingTile(
            "Semantic topic changes (Experimental)",
            "Fires early checkpoints on a judged topic change instead of word "
            "overlap, and falls back to word overlap if no answer arrives.",
            design_icon("stack-purple.svg"),
        )
        self.typesafe_topic_shift_check = self.typesafe_topic_shift_tile.checkbox
        self.typesafe_topic_shift_check.setObjectName("typesafeTopicShiftCheck")
        self._meeting_bindings.checkbox(
            self.typesafe_topic_shift_check,
            SettingsKey.TYPESAFE_TOPIC_SHIFT_ENABLED,
            lambda settings: setting_value(SettingsKey.TYPESAFE_TOPIC_SHIFT_ENABLED, settings) is True,
        )

        self.typesafe_voice_commands_tile = SettingTile(
            "Spoken instructions (Experimental)",
            "Say “Note taker, mark that as a decision”, “…add an action item” or "
            "“…new topic: budget”. Only segments naming the assistant are judged. "
            "“Replace X with Y” applies a reversible transcript correction.",
            design_icon("stack-slate.svg"),
        )
        self.typesafe_voice_commands_check = (
            self.typesafe_voice_commands_tile.checkbox
        )
        self.typesafe_voice_commands_check.setObjectName(
            "typesafeVoiceCommandsCheck"
        )
        self._meeting_bindings.checkbox(
            self.typesafe_voice_commands_check,
            SettingsKey.TYPESAFE_VOICE_COMMANDS_ENABLED,
            lambda settings: setting_value(SettingsKey.TYPESAFE_VOICE_COMMANDS_ENABLED, settings) is True,
        )
        self.typesafe_feature_tiles = {}
        for feature, key, title, description in (
            ("citations", SettingsKey.TYPESAFE_CITATIONS_ENABLED, "Advisory citation checks",
             "Gives the judge generated claims and their cited excerpts. "
             "Flags weak evidence without changing the claim."),
            ("semantic_search", SettingsKey.TYPESAFE_SEMANTIC_SEARCH_ENABLED, "Semantic history search",
             "Gives the judge your search and shortlisted past excerpts to rank by meaning: "
             "only from AI-insight meetings unless it runs on this computer. "
             "Keyword search stays available."),
            ("question_radar", SettingsKey.TYPESAFE_QUESTION_RADAR_ENABLED, "Open questions radar",
             "Gives the judge a minute of transcript and tracked questions "
             "to find unanswered questions and suggest answers."),
            ("highlights", SettingsKey.TYPESAFE_HIGHLIGHTS_ENABLED, "Live highlight pulses",
             "Gives the judge a minute of transcript to mark decisions, disagreements, "
             "dated commitments, numbers and takeaways. Click a pulse to play that moment."),
        ):
            tile = SettingTile(title, description, design_icon("bolt-green.svg"))
            self._meeting_bindings.checkbox(
                tile.checkbox,
                key,
                lambda settings, key=key: settings.get(key, False) is True,
            )
            self.typesafe_feature_tiles[feature] = tile
        _caption, self.typesafe_destination_label = self._tile_group(
            layout,
            "Fast judgments",
            [
                self.typesafe_enabled_tile,
                self.typesafe_topic_shift_tile,
                self.typesafe_voice_commands_tile,
                *self.typesafe_feature_tiles.values(),
            ],
            columns=3,
            intro=self._typesafe_destination_copy(JEV_TYPESAFE_ROUTE),
        )

    def _build_meeting_after_page(self, layout: QVBoxLayout) -> None:
        self.meeting_end_redecode_tile = SettingTile(
            "Re-transcribe the full recording",
            "After End, recut the continuous session audio on longer quiet "
            "gaps and run Whisper again. Live capture is unchanged.",
            design_icon("microphone-blue.svg"),
        )
        self.meeting_end_redecode_check = self.meeting_end_redecode_tile.checkbox
        self._meeting_bindings.checkbox(
            self.meeting_end_redecode_check,
            SettingsKey.MEETING_END_REDECODE,
            resolve_meeting_end_redecode,
        )

        self.meeting_end_polish_tile = SettingTile(
            "Clean up the transcript with the LLM",
            "Rewrites the finished transcript for readability. Needs AI "
            "insights on for the meeting.",
            design_icon("stack-purple.svg"),
        )
        self.meeting_end_polish_check = self.meeting_end_polish_tile.checkbox
        self._meeting_bindings.checkbox(
            self.meeting_end_polish_check,
            SettingsKey.MEETING_END_POLISH,
            resolve_meeting_end_polish,
        )

        self.meeting_end_report_tile = SettingTile(
            "Write the final report",
            "Topic, summary, and cards, generated once live captions finish. "
            "Needs AI insights on for the meeting.",
            design_icon("check-green.svg"),
        )
        self.meeting_end_report_check = self.meeting_end_report_tile.checkbox
        self.meeting_end_report_check.toggled.connect(
            self._on_end_report_toggled
        )

        self.meeting_review_tile = SettingTile(
            "Review uncertain insights at the end (Experimental)",
            "For new meetings. Gives the Fast judgments judge relevant excerpts, speaker "
            "names and insights to find ambiguities, and asks the three highest-priority "
            "questions first. No audio is sent. Needs AI insights and Fast judgments.",
            design_icon("check-green.svg"),
        )
        self.meeting_review_check = self.meeting_review_tile.checkbox
        self.meeting_review_check.toggled.connect(self._on_meeting_review_toggled)
        review_row = QHBoxLayout()
        self.meeting_review_sensitivity = ElidingComboBox()
        self.meeting_review_sensitivity.addItem("Normal", "normal")
        self.meeting_review_sensitivity.addItem("Thorough", "thorough")
        self._meeting_bindings.combo(
            self.meeting_review_sensitivity,
            SettingsKey.MEETING_INSIGHT_REVIEW_SENSITIVITY,
            lambda settings: resolve_meeting_insight_review(settings)["sensitivity"],
        )
        review_row.addWidget(
            settings_field("Sensitivity", self.meeting_review_sensitivity)
        )
        self.meeting_review_tile.add_body_layout(review_row)

        self._tile_group(
            layout,
            "After End",
            [
                self.meeting_end_redecode_tile,
                self.meeting_end_polish_tile,
                self.meeting_end_report_tile,
                self.meeting_review_tile,
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
            design_icon("stack-slate.svg"),
        )
        self.meeting_report_brief_tile = SettingTile(
            "Brief",
            "One-page editorial summary. Reuses the same cards as Signal.",
            design_icon("box-blue.svg"),
        )
        self.meeting_report_signal_tile = SettingTile(
            "Signal",
            "One-screen glance. Reuses the same cards as Brief.",
            design_icon("bolt-green.svg"),
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

        self.meeting_report_views_hint = settings_caption(
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
            design_icon("box-blue.svg"),
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
        self.meeting_port_spinbox.setMinimumHeight(40)
        self.meeting_port_spinbox.setMinimumWidth(120)
        # Save the finished port, not 8, 80, and 808 on the way to 8080.
        self.meeting_port_spinbox.setKeyboardTracking(False)
        self._meeting_bindings.spin(
            self.meeting_port_spinbox,
            SettingsKey.MEETING_SERVER_PORT,
            resolve_meeting_server_port,
        )
        self.meeting_port_tile = FieldTile(
            "Dashboard port",
            "Automatic lets the meeting server pick a free port each session. "
            "Pick a fixed port only if you need a stable link.",
            self.meeting_port_spinbox,
            design_icon("bolt-green.svg"),
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
        self.api_key_credential_tile = FieldTile(
            "Credential",
            "Choose the provider whose key you want to save or test.",
            self.api_key_combo,
            design_icon("key-blue.svg"),
        )

        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
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
        self.api_key_credential_tile.add_body_layout(status_row)
        self.api_key_uses_caption = settings_caption("")
        self.api_key_credential_tile.add_body(self.api_key_uses_caption)
        self._tile_group(
            layout, "", [self.api_key_credential_tile], columns=1
        )

        self.api_key_entry_tile = InfoTile(
            "Add or replace a key",
            self._api_key_store_copy(),
            design_icon("plus-blue.svg"),
        )
        self.api_key_store_caption = self.api_key_entry_tile.description_label
        self.api_key_store_caption.setToolTip(self._api_key_store_detail())

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
        # The key itself is set in a spaced monospace; the placeholder keeps
        # the normal field font (QSS can't style a placeholder on its own).
        self.api_key_edit.textChanged.connect(
            lambda text: set_style_property(self.api_key_edit, "hasText", bool(text))
        )
        self.api_key_edit.textChanged.connect(self._update_api_key_controls)
        self.api_key_edit.returnPressed.connect(self._save_api_key)
        self.api_key_show_button = neutral_button(Button("Show"))
        self.api_key_show_button.setObjectName("apiKeyShowButton")
        self.api_key_show_button.setCheckable(True)
        self.api_key_show_button.set_base_minimum_size(80, 40)
        self.api_key_show_button.setMinimumHeight(40)
        self.api_key_show_button.setMaximumHeight(40)
        self.api_key_show_button.toggled.connect(self._on_api_key_show_toggled)
        input_row = QWidget()
        input_row.setObjectName("modelManagerFieldGroup")
        input_layout = QHBoxLayout(input_row)
        input_layout.setContentsMargins(0, 0, 0, 0)
        input_layout.setSpacing(8)
        input_layout.addWidget(self.api_key_edit, stretch=1)
        input_layout.addWidget(self.api_key_show_button)
        self.api_key_entry_tile.add_body(input_row)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(8)
        # A plain Button: PrimaryButton's own padding would make it taller than Test.
        self.api_key_save_button = Button("Save key")
        self.api_key_save_button.setObjectName("apiKeySaveButton")
        compact_primary_button(self.api_key_save_button)
        fit_compact_button(self.api_key_save_button, 120)
        self.api_key_save_button.clicked.connect(self._save_api_key)
        self.api_key_test_button = neutral_button(Button("Test"))
        self.api_key_test_button.setObjectName("apiKeyTestButton")
        fit_compact_button(self.api_key_test_button, 90)
        self.api_key_test_button.clicked.connect(self._test_api_key)
        self.api_key_remove_button = DangerButton("Remove saved key")
        self.api_key_remove_button.setProperty("buttonId", "apiKeyRemoveButton")
        fit_compact_button(self.api_key_remove_button, 160)
        self.api_key_remove_button.clicked.connect(self._remove_api_key)
        buttons.addWidget(self.api_key_save_button)
        buttons.addWidget(self.api_key_test_button)
        buttons.addStretch()
        buttons.addWidget(self.api_key_remove_button)
        self.api_key_entry_tile.add_body_layout(buttons)
        self._tile_group(layout, "", [self.api_key_entry_tile], columns=1)

    @staticmethod
    def _api_key_store_copy() -> str:
        status = credential_store().status()
        if not status.available:
            # The error itself goes in the tooltip (_api_key_store_detail).
            return (
                f"{status.backend_name} isn't available on this computer, so "
                "keys can't be saved from here. Set the variable in your "
                "environment or a .env file instead; OpenWhisper never falls "
                "back to an unprotected file."
            )
        copy = (
            f"Encrypted by {status.backend_name} for your account. "
            "Saved keys override environment variables and .env, and never "
            "enter settings or logs."
        )
        if sys.platform == "darwin":
            # "Allow" lasts one read, so macOS asks again every launch.
            copy += (
                " When macOS asks to use your Keychain, choose Always Allow so "
                "it doesn't ask every launch."
            )
        return copy

    @staticmethod
    def _api_key_store_detail() -> str:
        """The store's own error, for the caption's tooltip; "" when it works."""
        status = credential_store().status()
        if status.available or not status.reason:
            return ""
        return f"Details: {status.reason}"

    @staticmethod
    def _api_key_entries(settings: dict) -> list:
        """One row per distinct variable; profiles sharing a variable share it."""
        from services import speech_endpoints

        names: Dict[str, list] = {}
        owners = [(p.api_key_env, p.name) for p in list_profiles(settings) if p.api_key_env]
        for env_name, owner in owners + speech_endpoints.credential_owners(settings):
            if owner not in names.setdefault(env_name, []):
                names[env_name].append(owner)
        entries = [
            (env_name, f"{' / '.join(owners)} · {env_name}")
            for env_name, owners in names.items()
        ]
        # TypeSafe is a decision service rather than a text-model profile, so
        # it is not in ``list_profiles`` but still needs a home for its key.
        entries.append((TYPESAFE_CREDENTIAL_ENV, f"TypeSafe · {TYPESAFE_CREDENTIAL_ENV}"))
        return entries

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
        self.api_key_store_caption.setToolTip(self._api_key_store_detail())
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
        """The text endpoint, else the speech server, that sends ``env_name``."""
        from services import speech_endpoints

        settings = settings_manager.load_all_settings()
        speech = [
            speech_endpoints.configured_endpoint(endpoint_id, settings).connection
            for endpoint_id in speech_endpoints.ENDPOINT_IDS
        ]
        for profile in [*list_profiles(settings), *speech]:
            if profile.api_key_env == env_name:
                return profile
        return None

    def _api_key_label(self, env_name: str) -> str:
        if env_name == TYPESAFE_CREDENTIAL_ENV:
            return "TypeSafe API key"
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
        self.api_key_status_icon.setPixmap(design_icon(icon).pixmap(16, 16))
        set_style_property(self.api_key_status, "tone", tone)
        self._render_api_key_uses(name)
        self._update_api_key_controls()

    def _render_api_key_uses(self, env_name: str) -> None:
        if env_name == "OPENAI_API_KEY":
            text = (
                "Used for API transcription with OpenAI, OpenAI transcript "
                "cleanup and meeting intelligence, and cloud speaker "
                "identification."
            )
        elif env_name == JEV_OPENROUTER_ROUTE.credential_env:
            text = (
                "Used for transcript cleanup and meeting intelligence through "
                "OpenRouter. It can also run fast judgments (Jev): choose "
                "OpenRouter under Meeting Mode → Fast judgments."
            )
        elif env_name == TYPESAFE_CREDENTIAL_ENV:
            text = (
                "Used for TypeSafe fast judgments: topic changes, spoken "
                "instructions, live highlights, the questions radar, citation "
                "checks, semantic search, and insight review. Enable them under "
                "Meeting Mode → Fast judgments, where an OpenRouter key can "
                "run the same model instead."
            )
        elif env_name:
            from services import speech_endpoints

            settings = settings_manager.load_all_settings()
            uses = []
            owners = [p.name for p in list_profiles(settings) if p.api_key_env == env_name]
            if owners:
                uses.append(f"to {' and '.join(owners)} for transcript cleanup and meeting intelligence")
            speech = [name for env, name in speech_endpoints.credential_owners(settings) if env == env_name]
            if speech:
                uses.append(f"to {' and '.join(speech)} for API transcription")
            text = f"Sent as the API key {', and '.join(uses)}."
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
        entries = self._api_key_entries(self._settings_snapshot())
        if not entries:
            return "None needed"
        available = sum(credential_source(name) != CredentialSource.NONE for name, _label in entries)
        return f"{available} of {len(entries)} set"

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
        if self._api_key_testing or not name:
            return
        # TypeSafe is not an OpenAI-compatible endpoint and so has no entry in
        # ``list_profiles``; it verifies through its own one-judgment probe.
        profile = self._api_key_profile(name)
        if profile is None and name != TYPESAFE_CREDENTIAL_ENV:
            return
        label = self._api_key_label(name)
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
        self._api_key_token = token = next(_tokens)
        # Bound here: the worker must not reach back into the window.
        verify_typesafe = typesafe_verify_key
        verify = verify_api_key
        done = _thread_relay().api_key_verified

        def worker():
            if profile is None:
                ok, detail = verify_typesafe(key)
            else:
                ok, detail = verify(profile, key)
            done.emit(token, name, ok, detail)

        threading.Thread(target=worker, daemon=True, name="api-key-verify").start()

    def _on_api_key_verified(
        self, token: int, env_name: str, ok: bool, detail: str
    ) -> None:
        if token != self._api_key_token:
            return
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
        instruction_icon.setPixmap(design_icon("info-blue.svg").pixmap(16, 16))
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
                "Click a shortcut, hold Ctrl, Alt, Shift, or Super, then press its key. "
                "On Omarchy, free keys are registered with Hyprland for desktop-wide use. "
                "Existing desktop bindings are preserved; conflicting keys still work "
                "while OpenWhisper is focused."
            )
        elif USE_PYNPUT_BACKEND:
            instruction_text = (
                "Click a shortcut, hold Ctrl, Alt, Shift, or Super, then press "
                "the desired key. A side mouse button works too: click it on "
                "the shortcut."
            )
        else:
            instruction_text = (
                "Click a shortcut, then press the desired key combination, or "
                "click a side mouse button on it. Numpad keys are distinct from "
                "the matching regular keys."
            )
        instruction = WrappedLabel(instruction_text)
        self._hotkey_instruction = instruction
        self._hotkey_instruction_text = instruction_text
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
                "command_mode",
                "Command Mode",
                "Say how to rewrite the selected text, or what to write at the "
                "cursor. Works like the record hotkey.",
                optional=True,
            )
        )
        layout.addWidget(
            self._hotkey_shortcut_row(
                "route_record",
                "Route by voice",
                "Record, then send your words to the action they name, such as "
                "“Ask Claude…”. Turn on Route by voice on the Actions page.",
                optional=True,
            )
        )
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

        layout.addWidget(self._hotkey_group_title("Dictation tools"))
        layout.addWidget(
            self._hotkey_shortcut_row(
                "scratchpad_toggle",
                "Scratchpad",
                "Show or hide the floating Scratchpad.",
                optional=True,
            )
        )
        layout.addWidget(
            self._hotkey_shortcut_row(
                "cycle_language",
                "Switch language",
                "Move to the next language you dictate in.",
                optional=True,
            )
        )
        layout.addWidget(
            self._hotkey_shortcut_row(
                "paste_last_original",
                "Paste original of last dictation",
                "Paste your last dictation as you said it, before AI cleanup.",
                optional=True,
            )
        )

        layout.addWidget(self._hotkey_group_title("Walkthroughs"))
        self._add_walkthrough_shortcut_rows(layout, HOTKEYS)

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
        reset_button = neutral_button(Button("Reset to defaults"))
        reset_button.setObjectName("hotkeyResetButton")
        reset_button.setToolTip("Put the shortcuts above back to the platform defaults")
        fit_compact_button(reset_button, 150)
        reset_button.clicked.connect(self._confirm_reset_hotkeys)
        layout.addWidget(reset_button, alignment=Qt.AlignmentFlag.AlignRight)

        # The shortcuts you give your own profiles, transforms and actions,
        # changed here or on their own pages.
        self.hotkey_bindings = BindingShortcuts(manager=settings_manager)
        self.hotkey_bindings.capture_changed.connect(self._on_binding_capture_changed)
        self.hotkey_bindings.saved.connect(self._on_binding_shortcut_saved)
        self.hotkey_bindings.open_requested.connect(self.select_destination)
        layout.addWidget(self.hotkey_bindings)

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
        self.record_mode_combo.setMinimumWidth(round(260 * current_ui_font_scale()))
        self.record_mode_combo.currentIndexChanged.connect(
            self._on_recording_trigger_mode_changed
        )
        card = ReflowCard()
        card.setObjectName("hotkeyModeCard")
        name = WrappedLabel("How the record hotkey activates")
        name.setObjectName("hotkeyShortcutName")
        card.copy.addWidget(name)
        detail = WrappedLabel(
            "Toggle starts and stops with each press. Push and hold records while "
            "the key is down."
        )
        detail.setObjectName("hotkeyShortcutDescription")
        card.copy.addWidget(detail)
        card.add_control(self.record_mode_combo)

        self.hands_free_latch_row = QWidget()
        self.hands_free_latch_row.setObjectName("hotkeyLatchRow")
        latch = QHBoxLayout(self.hands_free_latch_row)
        latch.setContentsMargins(0, 8, 0, 0)
        latch.setSpacing(16)
        latch_copy = QVBoxLayout()
        latch_copy.setSpacing(2)
        latch_name = WrappedLabel("Double-tap to keep recording")
        latch_name.setObjectName("hotkeyShortcutName")
        latch_copy.addWidget(latch_name)
        latch_detail = WrappedLabel(
            "Tap the record hotkey twice quickly to record hands-free, then "
            "press it once more to stop."
        )
        latch_detail.setObjectName("hotkeyShortcutDescription")
        latch_copy.addWidget(latch_detail)
        latch.addLayout(latch_copy, stretch=1)
        self.hands_free_latch_switch = SettingsSwitch()
        self.hands_free_latch_switch.setAccessibleName("Double-tap to keep recording")
        self.hands_free_latch_switch.toggled.connect(self._on_hands_free_latch_toggled)
        latch.addWidget(
            self.hands_free_latch_switch, alignment=Qt.AlignmentFlag.AlignVCenter
        )
        self.hands_free_latch_row.setVisible(False)
        card.add_footer(self.hands_free_latch_row)
        return card

    def _on_recording_trigger_mode_changed(self, _index: int = 0) -> None:
        mode = self.record_mode_combo.currentData()
        if mode is None:
            return
        self.hands_free_latch_row.setVisible(mode == RecordingTriggerMode.PUSH_HOLD)
        if not self._persist(SettingsKey.RECORDING_TRIGGER_MODE, mode):
            return
        self._update_record_row_description(mode)
        if self.on_recording_trigger_mode_changed:
            self.on_recording_trigger_mode_changed(mode)

    def _on_hands_free_latch_toggled(self, checked: bool) -> None:
        # The hotkey runtime reads this at each short tap, so saving is enough.
        if self._persist(SettingsKey.RECORDING_HANDS_FREE_LATCH, bool(checked)):
            self._update_record_row_description(self.record_mode_combo.currentData())
            if self.home is not None:
                self.home.refresh_shortcut()

    def _load_hands_free_latch(self, settings: dict) -> None:
        mode = resolve_recording_trigger_mode(settings)
        blocker = self.hands_free_latch_switch.blockSignals(True)
        self.hands_free_latch_switch.setChecked(resolve_recording_hands_free_latch(settings))
        self.hands_free_latch_switch.blockSignals(blocker)
        self.hands_free_latch_row.setVisible(mode == RecordingTriggerMode.PUSH_HOLD)
        self._update_record_row_description(mode)

    @staticmethod
    def _record_hotkey_description(mode: str, hands_free: bool = False) -> str:
        if mode == RecordingTriggerMode.PUSH_HOLD:
            if hands_free:
                return (
                    "Hold to record; release to stop and transcribe. "
                    "Double-tap to record hands-free."
                )
            return (
                "Hold to record; release to stop and transcribe. "
                "Quick taps are canceled."
            )
        return "Start recording when idle; stop and transcribe while recording."

    def _update_record_row_description(self, mode: str) -> None:
        label = self.hotkey_row_descriptions.get("record_toggle")
        if label is not None:
            label.setText(
                self._record_hotkey_description(
                    mode, self.hands_free_latch_switch.isChecked()
                )
            )

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
        page: str = HOTKEYS,
    ) -> QFrame:
        card = ReflowCard()
        card.setObjectName("hotkeyShortcutCard")
        name = WrappedLabel(title)
        name.setObjectName("hotkeyShortcutName")
        card.copy.addWidget(name)
        detail = WrappedLabel(description)
        detail.setObjectName("hotkeyShortcutDescription")
        card.copy.addWidget(detail)
        note = WrappedLabel("")
        note.setObjectName("hotkeyShortcutNote")
        note.setVisible(False)
        card.copy.addWidget(note)

        if self._native_wayland:
            from ui_qt.widgets.profile_hotkey_input import ProfileHotkeyInput

            field = ProfileHotkeyInput()
            field.setAccessibleName(f"{title} shortcut")
            field.setToolTip("Click, then press a shortcut. Escape cancels.")
            field.capture_changed.connect(self._on_profile_capture)
            field.captured.connect(lambda hotkey, key=key: self._on_local_hotkey_captured(key, hotkey))
        else:
            field = HotkeyCaptureInput()
            field.setAccessibleName(f"{title} shortcut")
            field.capture_requested.connect(
                lambda key=key, field=field: self._start_hotkey_capture(key, field)
            )
            field.capture_cancelled.connect(self._cancel_hotkey_capture)
            field.mouse_captured.connect(
                lambda hotkey, key=key: self._on_mouse_hotkey_captured(key, hotkey)
            )
        field.notice.connect(
            lambda text, key=key: self._show_hotkey_note(key, text, warning=True)
        )
        field.setProperty("optional", optional)
        field.setMinimumWidth(190)
        field.setMaximumWidth(220)
        card.add_control(field)

        clear_button = None
        if optional:
            clear_button = Button("Clear")
            clear_button.setObjectName("hotkeyClearButton")
            clear_button.setAccessibleName(f"Clear {title} shortcut")
            fit_compact_button(clear_button, 68)
            clear_button.clicked.connect(lambda _checked=False, key=key: self._clear_hotkey(key))
            card.add_control(clear_button)
        self._shortcut_rows.append(_ShortcutRow(key, page, field, clear_button, note))
        if page == HOTKEYS:
            self.hotkey_inputs[key] = field
            self.hotkey_row_descriptions[key] = detail
            self.hotkey_row_notes[key] = note
            if clear_button is not None:
                self.hotkey_clear_buttons[key] = clear_button
        return card

    def shortcut_input(self, key: str, page: str = HOTKEYS) -> Optional[HotkeyCaptureInput]:
        """The box for standard shortcut ``key`` on ``page``, once that page is built."""
        return next(
            (row.field for row in self._shortcut_rows if row.key == key and row.page == page),
            None,
        )

    def _show_hotkey_note(self, key: str, text: str, *, warning: bool = False) -> None:
        for row in self._shortcut_rows:
            if row.key == key:
                self._set_row_note(row, text, warning=warning)

    @staticmethod
    def _set_row_note(row: _ShortcutRow, text: str, *, warning: bool = False) -> None:
        row.note.setText(text)
        set_style_property(row.note, "tone", "warning" if warning else "info")
        row.note.setVisible(bool(text))

    def _on_binding_capture_changed(self, capturing: bool) -> None:
        if capturing:
            # A standard row's capture would take the same keys.
            self._cancel_hotkey_capture()
        self.set_hotkey_capture_suspended(capturing)

    def _on_binding_shortcut_saved(self, kind: str, message: str) -> None:
        """A profile's, transform's or action's shortcut changed on the Hotkeys page."""
        self.message_label.setText(message)
        if kind == PROFILE:
            if CLEANUP_PROFILES in self._built_pages:
                self.cleanup_profiles_panel.refresh()
            self._on_profiles_saved()
        elif kind == TRANSFORM:
            self.refresh_page(COMMANDS)
            self.notify_changed("transforms")
        else:
            self.refresh_page(ACTIONS)
            self.notify_changed("actions")

    def _refresh_binding_shortcuts(self) -> None:
        bindings = self.__dict__.get("hotkey_bindings")
        if bindings is not None:
            bindings.load(self._settings_snapshot())

    def _build_advanced_page(self, layout: QVBoxLayout) -> None:
        self.meeting_redecode_coverage_guard_tile = SettingTile(
            "Keep the live transcript if re-transcription is much shorter",
            "Reject a re-transcription with fewer than 80% of the live transcript's "
            "words. Word count does not measure accuracy. Applies after End and "
            "when retrying saved meetings. Off by default.",
            design_icon("microphone-blue.svg"),
        )
        self.meeting_redecode_coverage_guard_check = self.meeting_redecode_coverage_guard_tile.checkbox
        self._meeting_bindings.checkbox(
            self.meeting_redecode_coverage_guard_check,
            SettingsKey.MEETING_REDECODE_COVERAGE_GUARD,
            resolve_meeting_redecode_coverage_guard,
        )
        self._tile_group(
            layout, "Meeting re-transcription", [self.meeting_redecode_coverage_guard_tile],
        )

        self.developer_mode_tile = SettingTile(
            "Developer mode",
            "Unlocks a Load demo meeting control on the Meeting Mode tab. The "
            "demo opens the dashboard with a fake transcript so you can test "
            "end-of-meeting cleanup and the final report without recording a "
            "real meeting.",
            design_icon("bolt-green.svg"),
        )
        self.developer_mode_check = self.developer_mode_tile.checkbox
        self.developer_mode_check.setObjectName("developerModeCheck")
        self.developer_mode_check.toggled.connect(self._on_developer_mode_changed)
        self._tile_group(layout, "Developer", [self.developer_mode_tile])

    # ---- navigation ----

    def show_home(self) -> None:
        """Open Settings on Home, scrolled to the top."""
        self.select_destination(OVERVIEW)
        area = self._page_scrolls.get(OVERVIEW)
        if area is not None and self.rail.current_key() == OVERVIEW:
            area.verticalScrollBar().setValue(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        scale = current_ui_font_scale()
        self.settings_brand.setVisible(self.width() >= round(560 * scale))
        self.settings_label.setVisible(self.width() >= round(700 * scale))
        self.search_text.setVisible(self.width() >= round(720 * scale))
        narrow = self.width() < round(self.NARROW_WIDTH * scale)
        if narrow != self._narrow:
            self._narrow = narrow
            self.rail_pane.setVisible(not narrow)
            self.page_menu_button.setVisible(narrow)

    def select_destination(self, key: str) -> None:
        """Show one destination by stable key or legacy alias."""
        key = resolve_destination(key)
        if self._backup_busy and key != BACKUP:
            return
        if key in HOME_SECTIONS:
            # The old Basic tabs live on as Home's cards.
            self.select_destination(OVERVIEW)
            self._reveal(self.home.section(key))
            return
        if key in self._pages:
            self.ensure_page(key)
            self.rail.select(key)

    def focus_hf_policy(self) -> None:
        """Open Downloads with the Hugging Face policy control focused."""
        self.select_destination(DOWNLOADS)
        self.hf_policy_combo.setFocus()

    def focus_cleanup_model(self) -> None:
        """Open AI cleanup scrolled to its cleanup model."""
        self.select_destination(CLEANUP)
        self._reveal(self.cleanup_model_tile)

    def show_downloads(self, backend: str = "", component_id: str = "") -> None:
        """Open Downloads, filtered to one backend or focused on a component."""
        self.select_destination(DOWNLOADS)
        if backend:
            self.downloads.show_backend(backend)
        if component_id:
            self.downloads.focus_component(component_id)

    def _on_destination_changed(self, key: str) -> None:
        if self._backup_busy and key != BACKUP:
            self.rail.select(BACKUP)
            return
        # Hotkeys and Walkthroughs both hold shortcut fields; a capture
        # belongs to the page it started on.
        self._cancel_hotkey_capture()
        if key != OVERVIEW and self.home is not None:
            self.home.cancel_capture()
        self._cancel_page_captures(keep=key)
        self._save_page_drafts(keep=key)
        page = self._pages.get(key)
        if page is None:
            return
        self.ensure_page(key)
        self.stack.setCurrentWidget(self._page_scrolls.get(key, page))
        self.rail.scrollToItem(self.rail.currentItem())
        title, subtitle = self._headings[key]
        self.page_crumb.setText((self.rail.chapter(key) or "Settings").upper())
        self.page_title.setText(title)
        self.page_subtitle.setText(subtitle)
        self.page_menu_button.setText(self.rail.name(key))
        if self._pages_ready:
            self.models.on_destination_shown(key)
            if key == OVERVIEW:
                self._refresh_home()
            elif key == REMOTE_ENGINE:
                self.remote_section.on_shown()

    # ---- search ----

    def open_search(self, text: str = "") -> None:
        """Show the search palette over the window."""
        if self._backup_busy:
            return
        self.search_palette.open(text)

    def _on_backup_busy_changed(self, busy: bool) -> None:
        """Keep Settings on Backup & restore during a snapshot or restore."""
        if busy and self.rail.current_key() != BACKUP:
            self.select_destination(BACKUP)
        self._backup_busy = bool(busy)
        self.rail.setEnabled(not busy)
        self.search_button.setEnabled(not busy)
        self.page_menu_button.setEnabled(not busy)
        for shortcut in self._search_shortcuts:
            shortcut.setEnabled(not busy)
        if busy:
            self.search_palette.close_palette()

    def _crumb(self, key: str) -> str:
        """Where a destination sits, as search results name it."""
        chapter, name = self.rail.chapter(key), self.rail.name(key)
        return f"{chapter} › {name}" if chapter else name

    def _search_index(self) -> list:
        pages = []
        for key in self.rail.keys():
            if key not in self._pages:
                continue
            title, subtitle = self._headings[key]
            pages.append(PageSource(
                key, self._crumb(key), title, subtitle, self._pages[key],
                self._rail_icons.get(key, "box-blue.svg"),
            ))
        keywords = keyword_entries(_SEARCH_ALIASES) + self._agent_search_entries()
        entries = build_index(pages, self.downloads, keywords)
        for key in self.rail.keys():
            if key in self._built_pages:
                continue
            title, _subtitle = self._headings[key]
            crumb = self._crumb(key)
            icon = self._rail_icons.get(key, "box-blue.svg")
            for target_name, label, description in PAGE_SEARCH_FIELDS.get(key, ()):
                entries.append(SearchEntry(
                    SETTING, label, crumb, key,
                    target_name=target_name, keywords=description, icon=icon,
                ))
            for text in PAGE_HELP_TEXT.get(key, ()):
                entries.append(SearchEntry(HELP, title, text, key, icon="info-blue.svg"))
        return entries

    def _agent_search_entries(self) -> list:
        """The meeting agent picker, which is tiles rather than a setting tile."""
        return [SearchEntry(
            SETTING,
            "Who runs AI insights",
            self._crumb(MEETING_INTELLIGENCE),
            MEETING_INTELLIGENCE,
            target=self.__dict__.get("meeting_agent_picker"),
            target_name="meeting_agent_picker",
            keywords=(
                "meeting agent installed coding agent claude code codex opencode "
                "sign-in pi sdk built-in"
            ),
            icon=self._rail_icons.get(MEETING_INTELLIGENCE, "stack-purple.svg"),
        )]

    def _on_search_activated(self, entry: SearchEntry) -> None:
        if entry.model_name:
            self.select_destination(DOWNLOADS)
            self.downloads.reveal_model(entry.model_name)
            return
        if entry.component_id:
            self.show_downloads(component_id=entry.component_id)
            return
        self.select_destination(entry.destination)
        target = entry.target or (getattr(self, entry.target_name, None) if entry.target_name else None)
        if target is not None:
            self._reveal(target)

    def _reveal(self, target: QWidget) -> None:
        """Scroll a control into view, tint its card briefly, and focus it.

        Runs once the event loop has laid out the newly selected page.
        """
        self._reveal_target = target
        self._reveal_timer.start()

    def _run_reveal(self) -> None:
        target, self._reveal_target = self._reveal_target, None
        if target is None:
            return
        try:
            area = self._page_scrolls.get(self.rail.current_key())
            if area is not None:
                area.ensureWidgetVisible(target, 0, 40)
            card = target
            while card is not None and card.objectName() not in (
                "settingsTile", "textModelFootnoteCard", "homeCard"
            ):
                card = card.parentWidget()
            self._clear_search_flash()
            if card is not None:
                set_style_property(card, "searchHit", True)
                self._search_flash = card
                self._search_flash_timer.start()
            focus = next(
                (
                    widget for widget in [target, *target.findChildren(QWidget)]
                    if widget.focusPolicy() & Qt.FocusPolicy.TabFocus
                    and widget.isEnabled() and widget.isVisibleTo(self)
                ),
                None,
            )
            if focus is not None:
                focus.setFocus(Qt.FocusReason.ShortcutFocusReason)
        except RuntimeError:
            pass  # The target's page was rebuilt before the reveal ran.

    def _clear_search_flash(self) -> None:
        card, self._search_flash = self._search_flash, None
        if card is None:
            return
        try:
            set_style_property(card, "searchHit", False)
        except RuntimeError:
            pass  # The card was destroyed with its page.

    # ---- refresh ----

    def refresh(self) -> None:
        """Reload persisted values, model assignments, and rail captions."""
        with self._coalesced_rail_refresh():
            self._cancel_hotkey_capture()
            if CLEANUP_PROFILES in self._built_pages:
                self.cleanup_profiles_panel.refresh()
            self._loading = True
            try:
                self._refresh_snapshot = self._settings_snapshot()
                try:
                    self._load_settings()
                finally:
                    self._loading = False
                # A hidden window reuses the last cache scan; showEvent rescans.
                visible = self.isVisible()
                self.models.refresh(scan=visible)
                self.downloads.refresh(scan=visible)
                if BACKUP in self._built_pages:
                    self.backup_page.refresh()
                for key, module in self._page_modules.items():
                    if key in self._built_pages:
                        self._refresh_page_module(key, module)
                self._refresh_rail_values()
            finally:
                self._loading = False
                self._refresh_snapshot = None

    @contextmanager
    def _coalesced_rail_refresh(self):
        """Run the rail and Overview redraws requested inside once, at the end.

        A refresh fans out through model and download signals that each
        redraw the rail, and every redraw rebuilds the Overview.
        """
        self._rail_batch_depth += 1
        try:
            yield
        finally:
            self._rail_batch_depth -= 1
            if not self._rail_batch_depth and self._rail_refresh_pending:
                self._rail_refresh_pending = False
                self._refresh_rail_values()

    def refresh_models(self) -> None:
        """Re-read model assignments and downloads after the engine changes."""
        self.models.refresh()
        self.downloads.refresh()

    def eventFilter(self, obj, event):
        if (
            obj is self.__dict__.get("cleanup_prompt_edit")
            and event.type() == QEvent.Type.FocusOut
        ):
            self._persist_cleanup_prompt()
        return super().eventFilter(obj, event)

    def reject(self) -> None:
        # Escape closes through here without a close event.
        if self._save_page_drafts(ask=self.isVisible()):
            super().reject()

    def closeEvent(self, event):
        # Quitting the app closes a visible Settings window this way too. A
        # hidden one has nobody to ask, so only its valid drafts are saved.
        if not self._save_page_drafts(ask=self.isVisible()):
            event.ignore()
            return
        self.search_palette.close_palette()
        if self.home is not None:
            self.home.cancel_capture()
        if CLEANUP_PROFILES in self._built_pages:
            self.cleanup_profiles_panel.hotkey_input.cancel_capture()
        self._cancel_page_captures()
        self._cancel_hotkey_capture()
        if CLEANUP in self._built_pages:
            self._persist_cleanup_prompt()
        if self._retention_commit_timer.isActive():
            self._commit_retention()
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
        self.message_label.setText("")
        self._refresh_rail_values()
        return True

    def _refresh_rail_values(self) -> None:
        if self._rail_batch_depth:
            self._rail_refresh_pending = True
            return
        settings = self._settings_snapshot()
        profile_count = len(load_cleanup_profiles(settings))
        self.rail.set_value(CLEANUP_PROFILES, f"{profile_count} profile{'' if profile_count == 1 else 's'}")
        self.rail.set_value(GENERAL, _general_rail_value(settings))
        self.rail.set_value(APPEARANCE, f"{UiTheme.LABELS[resolve_ui_theme(settings)]} · "
                            f"{RecordingOverlayLook.LABELS[resolve_recording_overlay_look(settings)]}")
        self.rail.set_value(RECORDING, settings_microphones.rail_value(settings))
        model = resolve_transcript_cleanup_model(settings)
        cleanup = cleanup_prompts.level_label(settings)
        if cleanup_prompts.resolve_level(settings) != CleanupLevel.NONE and model:
            cleanup = f"{cleanup} · {model}"
        self.rail.set_value(CLEANUP, cleanup)
        rule_list = self.__dict__.get("cleanup_rules_list")
        if rule_list is not None:
            rule_count = rule_list.count()
        else:
            rule_count = len(resolve_transcript_cleanup_rules(settings))
        self.rail.set_value(CLEANUP_RULES, _rule_count_label(rule_count))
        self.rail.set_value(MEETING_AFTER, _meeting_after_rail_value(settings))
        self._render_typesafe_key_state()
        self.rail.set_value(MEETING_FAST, self._meeting_fast_rail_value())
        self.rail.set_value(MEETING_DASHBOARD, _meeting_dashboard_rail_value(settings))
        self.rail.set_value(API_KEYS, self._api_key_rail_value())
        self.rail.set_value(HOTKEYS, self._hotkey_rail_value())
        self.rail.set_value(
            WALKTHROUGHS, "AI write-up on" if resolve_walkthrough_write_up(settings) else "As spoken"
        )
        self.rail.set_value(DOWNLOADS, self.downloads.rail_value())
        if BACKUP in self._built_pages:
            frequency = self.backup_page.frequency_combo.currentData()
            self.rail.set_value(BACKUP, "Off" if frequency == "off" else str(frequency).title())
        else:
            self.rail.set_value(BACKUP, "Local backups")
        self.rail.set_value(ADVANCED, "Developer mode on" if resolve_developer_mode(settings) else "Developer mode off")
        for key, module in self._page_modules.items():
            # Every save redraws the rail, so one page's bad value must not
            # turn into a failed save on another page.
            try:
                value = module.rail_value(settings)
            except Exception:
                logger.exception("Couldn't read the %s rail value", key)
                value = ""
            self.rail.set_value(key, value)
        if self.rail.current_key() == OVERVIEW:
            self._refresh_home()

    def _refresh_download_progress(self) -> None:
        # Once per percent: nothing but the Downloads caption changes.
        self.rail.set_value(DOWNLOADS, self.downloads.rail_value())

    def _on_agent_looked_up(self, _agent_id: str) -> None:
        # Home and Commands read the agent's readiness without waiting for it.
        from ui_qt.dialogs import settings_commands

        settings_commands.refresh(self)
        self._refresh_rail_values()

    def _refresh_home(self) -> None:
        """Re-read Home's rows, and its where strip and Storage card from
        the values every destination reports. Home is only read while shown."""
        if not self._pages_ready or self.home is None:
            return
        self.home.refresh()
        models = self.models
        settings = self._settings_snapshot()
        cleanup_on = cleanup_prompts.resolve_level(settings) != CleanupLevel.NONE
        cleanup_remote = self._provider_is_remote(models.active_text_provider)
        meeting_remote = models.meeting_intelligence_is_remote(self._provider_is_remote)

        local_items, cloud_items = [], []
        if models.voice_is_remote() and not models.voice_is_cloud():
            # A paired computer: audio leaves this one, but not for a cloud.
            cloud_items.append(f"Dictation voice ({models.voice_detail().lower()})")
        else:
            (cloud_items if models.voice_is_remote() else local_items).append(
                "Dictation voice"
            )
        from services.settings import resolve_meeting_asr_source
        if resolve_meeting_asr_source(settings) == "remote":
            cloud_items.append("Meeting voice (audio sent to paired computer)")
        else:
            local_items.append("Meeting voice")
        if models.speaker_id_is_remote():
            cloud_items.append("Speaker labels (system audio after End)")
        cleanup_item = f"AI cleanup ({'on' if cleanup_on else 'off'})"
        (cloud_items if cleanup_remote else local_items).append(cleanup_item)
        from ui_qt.dialogs.settings_styles import reads_text_near_cursor
        if not reads_text_near_cursor(settings):
            local_items.append("Text near the cursor (off)")
        elif cleanup_remote:
            cloud_items.append("Text near the cursor (with AI cleanup)")
        else:
            local_items.append("Text near the cursor")
        (cloud_items if meeting_remote else local_items).append(
            "Meeting intelligence (transcript text, when enabled)"
            if meeting_remote else "Meeting intelligence"
        )
        local_items.extend(["Learned rules", "Recordings"])

        storage = self.downloads.storage_summary()
        try:
            components = [
                (info.display_name, info.is_usable)
                for info in component_coordinator.list_components()
            ]
        except Exception:
            components = []
        policy = settings.get(SettingsKey.HF_ACCESS_POLICY, HuggingFaceAccessPolicy.ASK)
        self.home.update_summary(HomeSummary(
            local_items=local_items,
            cloud_items=cloud_items,
            storage_downloaded=storage["downloaded"],
            storage_total=storage["total"],
            storage_bytes=storage["bytes"],
            storage_by_backend=storage["by_backend"],
            storage_checking=self.downloads.is_checking(),
            components=components,
            footer=(
                f"API keys: {self._api_key_rail_value()}  ·  Runtime: "
                f"{models.runtime_summary()}  ·  Missing models: "
                f"{_HF_POLICY_LABELS.get(policy, 'ask first')}"
            ),
        ))

    def _settings_snapshot(self) -> dict:
        if self._refresh_snapshot is not None:
            return self._refresh_snapshot
        try:
            return settings_manager.load_all_settings()
        except Exception:
            return {}

    @staticmethod
    def _provider_is_remote(provider: str) -> bool:
        """Whether a text provider sends text off this machine."""
        try:
            profile = get_text_llm_profile(provider)
        except Exception:
            return True
        return profile is None or not profile.is_local

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
        if self._persist(SettingsKey.UPDATE_CHECK_ENABLED, bool(checked)):
            self.notify_changed("updates")

    def _on_update_notify_toggled(self, _checked: bool) -> None:
        # The binding connected first, so the choice is already saved.
        if not self._loading:
            self.notify_changed("updates")

    def _on_audio_device_changed(self, _index: int = 0) -> None:
        if self._loading or self._microphones is None:
            return
        self._microphones.choose_preferred(self.audio_device_combo.currentData())

    def _on_retention_mode_changed(self, _index: int = 0) -> None:
        self._update_recording_retention_ui()
        self._commit_retention()

    def _schedule_retention_commit(self, _value: int = 0) -> None:
        # Typed values arrive only on Enter or focus-out, but chevron and
        # arrow-key steps arrive one at a time; let a burst settle so it
        # asks and prunes once.
        if not self._loading:
            self._retention_commit_timer.start()

    def _commit_retention(self) -> None:
        """Save finished retention edits, asking first if they delete audio."""
        self._retention_commit_timer.stop()
        if self._loading or self._confirming_retention:
            return
        edits = {
            SettingsKey.RECORDING_RETENTION_MODE: (
                self.recording_retention_combo.currentData()
            ),
            SettingsKey.MAX_SAVED_RECORDINGS: self.max_recordings_spinbox.value(),
            SettingsKey.MAX_SAVED_RECORDINGS_MB: (
                self.max_recordings_mb_spinbox.value()
            ),
        }
        saved = settings_manager.load_all_settings()
        pending = {**saved, **edits}
        limits = (
            resolve_max_saved_recordings(pending),
            resolve_max_saved_recordings_bytes(pending),
        )
        if limits == (
            resolve_max_saved_recordings(saved),
            resolve_max_saved_recordings_bytes(saved),
        ):
            return
        if not self._confirm_recording_removal(*limits):
            self._loading = True
            try:
                self._load_retention_settings(saved)
            finally:
                self._loading = False
            return
        if self._persist_many(edits):
            self._apply_retention_limit()

    def _confirm_recording_removal(
        self, max_recordings: Optional[int], max_bytes: Optional[int]
    ) -> bool:
        """Return True when these limits delete nothing or the user agrees."""
        if max_recordings is None and max_bytes is None:
            return True
        try:
            removed = history_manager.recordings_over_limit(
                max_recordings, max_bytes
            )
        except Exception as exc:
            logger.error("Couldn't preview recording retention: %s", exc)
            self.message_label.setText(f"Couldn't check saved recordings: {exc}")
            return False
        if not removed:
            return True
        count = len(removed)
        noun = "recording" if count == 1 else "recordings"
        size = format_file_size(sum(rec.size_bytes for rec in removed))
        # The modal takes focus from the spinbox, whose editingFinished would
        # otherwise re-enter and stack a second prompt.
        self._confirming_retention = True
        try:
            reply = QMessageBox.question(
                self,
                f"Delete {count} saved {noun}?",
                f"The new limit permanently deletes {count} older saved "
                f"{noun} ({size}) from disk right away. This can't be "
                "undone.\n\nTranscription history text is kept.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
        finally:
            self._confirming_retention = False
        return reply == QMessageBox.StandardButton.Yes

    def _load_retention_settings(self, settings: dict) -> None:
        retention_mode = setting_value(SettingsKey.RECORDING_RETENTION_MODE, settings)
        retention_index = self.recording_retention_combo.findData(retention_mode)
        if retention_index < 0:
            retention_index = self.recording_retention_combo.findData(
                RecordingRetentionMode.CUSTOM
            )
        self.recording_retention_combo.setCurrentIndex(max(0, retention_index))
        max_recordings = setting_value(SettingsKey.MAX_SAVED_RECORDINGS, settings)
        try:
            self.max_recordings_spinbox.setValue(max(1, int(max_recordings)))
        except (TypeError, ValueError):
            self.max_recordings_spinbox.setValue(config.MAX_SAVED_RECORDINGS)
        max_recordings_mb = setting_value(SettingsKey.MAX_SAVED_RECORDINGS_MB, settings)
        try:
            self.max_recordings_mb_spinbox.setValue(int(max_recordings_mb))
        except (TypeError, ValueError):
            self.max_recordings_mb_spinbox.setValue(config.MAX_SAVED_RECORDINGS_MB)
        self._update_recording_retention_ui()
        self._refresh_recordings_usage()

    def _apply_retention_limit(self) -> None:
        try:
            settings = settings_manager.load_all_settings()
            history_manager.set_retention(
                resolve_max_saved_recordings(settings),
                resolve_max_saved_recordings_bytes(settings),
            )
        except Exception as exc:
            logger.error("Couldn't apply recording retention: %s", exc)
        self._refresh_recordings_usage()

    def _update_recording_retention_ui(self) -> None:
        mode = self.recording_retention_combo.currentData()
        is_count = mode == RecordingRetentionMode.CUSTOM
        is_size = mode == RecordingRetentionMode.SIZE_LIMIT
        self.max_recordings_label.setEnabled(is_count)
        self.max_recordings_spinbox.setEnabled(is_count)
        self.max_recordings_mb_label.setEnabled(is_size)
        self.max_recordings_mb_spinbox.setEnabled(is_size)

    def _refresh_recordings_usage(self) -> None:
        if "recordings_usage_label" not in self.__dict__:
            return
        self._recording_query_generation += 1
        generation = self._recording_query_generation
        delivery = HistoryDelivery()
        delivery.loaded.connect(self._apply_recordings_usage)
        def load():
            try:
                usage = history_manager.get_recordings_usage()
            except Exception:
                logger.warning("Couldn't measure saved recordings", exc_info=True)
                usage = None
            delivery.loaded.emit(generation, "", usage, "")
        threading.Thread(target=load, name="settings-recording-usage", daemon=True).start()

    def _apply_recordings_usage(self, generation, _query, usage, _error):
        if generation != self._recording_query_generation or usage is None:
            return
        count, total_bytes = usage
        noun = "recording" if count == 1 else "recordings"
        self.recordings_usage_label.setText(f"Currently {count} {noun} using {format_file_size(total_bytes)}.")

    def _on_streaming_enabled_changed(self, checked: bool) -> None:
        if not self._persist_many(
            {SettingsKey.STREAMING_ENABLED: bool(checked)},
            drops=LEGACY_STREAMING_KEYS,
        ):
            return
        if self.on_streaming_settings_changed:
            self.on_streaming_settings_changed()

    def _on_ui_font_scale_changed(self, _index: int = 0) -> None:
        percent = self.ui_font_scale_combo.currentData()
        if percent is None:
            return
        if not self._persist(SettingsKey.UI_FONT_SCALE, int(percent)):
            return
        if self.on_ui_font_scale_changed:
            self.on_ui_font_scale_changed(int(percent))

    def _on_ui_theme_changed(self, _index: int = 0) -> None:
        theme = self.ui_theme_combo.currentData()
        if theme not in UiTheme.ALL:
            return
        if not self._persist(SettingsKey.UI_THEME, theme):
            return
        if self.on_ui_theme_changed:
            self.on_ui_theme_changed(theme)

    def _on_streaming_layout_activated(self, index: int) -> None:
        if not self._persist(SettingsKey.STREAMING_OVERLAY_LAYOUT, StreamingOverlayLayout.ALL[index]):
            return
        if self.on_streaming_layout_changed:
            self.on_streaming_layout_changed()

    def _on_streaming_font_changed(self, _value: int = 0) -> None:
        if not self._persist(
            SettingsKey.STREAMING_OVERLAY_FONT_SIZE,
            self.streaming_font_size_spinbox.value(),
        ):
            return
        if self.on_streaming_font_changed:
            self.on_streaming_font_changed()

    def _on_cleanup_enabled_changed(self, checked: bool) -> None:
        saved = self._persist(SettingsKey.TRANSCRIPT_CLEANUP_ENABLED, bool(checked))
        # After saving: without the switch built, the saved value is the state.
        self._update_cleanup_prompt_ui()
        if saved and self.on_cleanup_changed:
            self.on_cleanup_changed()

    def _on_cleanup_level_activated(self, index: int) -> None:
        level = cleanup_levels.SEGMENTS[index][0]
        # Custom can only be clicked while the custom prompt is in use.
        if level != cleanup_levels.CUSTOM:
            self.choose_cleanup_level(level)
        # Started after the confirmation's own event loop has closed, so it
        # runs once the bar has selected the clicked segment.
        self._cleanup_level_resync.start()

    def choose_cleanup_level(self, level: str) -> bool:
        """Use ``level``'s preset for AI cleanup; Advanced and Basic share this.

        A saved custom prompt replaces every preset, so choosing one removes
        it, after asking. Returns whether the level was saved.
        """
        drops = ()
        if cleanup_prompts.custom_prompt(self._settings_snapshot()):
            if not self._confirm_custom_prompt_removal(level):
                self._update_cleanup_level_ui()
                return False
            drops = (SettingsKey.TRANSCRIPT_CLEANUP_PROMPT,)
        saved = self._persist_many(
            {SettingsKey.TRANSCRIPT_CLEANUP_LEVEL: level}, drops=drops
        )
        if saved and drops:
            self._forget_custom_prompt()
        self._update_cleanup_prompt_ui()
        if saved and self.on_cleanup_changed:
            self.on_cleanup_changed()
        return saved

    def _confirm_custom_prompt_removal(self, level: str) -> bool:
        name = CleanupLevel.LABELS.get(level, CleanupLevel.LABELS[CleanupLevel.MEDIUM])
        reply = QMessageBox.question(
            self,
            "Remove your custom prompt?",
            f"AI cleanup will go back to the {name} level's instructions. "
            "Your learned rules stay.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    def _forget_custom_prompt(self) -> None:
        self._saved_cleanup_prompt = ""
        editor = self.__dict__.get("cleanup_prompt_edit")
        if editor is not None:
            editor.clear()

    def _persist_cleanup_prompt(self) -> None:
        """Save the editor as the custom prompt, or remove the saved one.

        Empty text or a copy of a built-in prompt removes the key rather than
        saving preset text, so the level's preset applies and stays current.
        """
        editor = self.cleanup_prompt_edit
        custom = cleanup_prompts.custom_prompt(
            {SettingsKey.TRANSCRIPT_CLEANUP_PROMPT: editor.toPlainText()}
        )
        if not custom and editor.toPlainText():
            editor.clear()
        if custom == self._saved_cleanup_prompt:
            return
        if custom:
            saved = self._persist(SettingsKey.TRANSCRIPT_CLEANUP_PROMPT, custom)
        else:
            saved = self._persist_many(
                {}, drops=(SettingsKey.TRANSCRIPT_CLEANUP_PROMPT,)
            )
        if saved:
            self._saved_cleanup_prompt = custom
            self._update_cleanup_prompt_ui()

    def _on_meeting_review_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        if checked:
            route = self._selected_typesafe_route()
            if route.is_local:
                destination = f"{route.name} on this computer"
            elif route.profile is not None:
                destination = f"{route.name} ({route.host})"
            elif route is JEV_TYPESAFE_ROUTE:
                destination = "TypeSafe"
            else:
                destination = f"TypeSafe through {route.name}"
            separate = ("" if route.profile is not None
                        else "This is a separate service from your meeting LLM. ")
            reply = QMessageBox.question(
                self, "Enable experimental insight review?",
                "For future meetings with AI insights on, send relevant transcript "
                f"excerpts, speaker names, and generated insights to {destination} after the meeting? "
                f"{separate}No audio is sent. "
                "Review is optional and does not block saving your recording.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                self.meeting_review_check.blockSignals(True)
                self.meeting_review_check.setChecked(False)
                self.meeting_review_check.blockSignals(False)
                return
        self._persist(SettingsKey.MEETING_INSIGHT_REVIEW_CONSENT,
                      _INSIGHT_REVIEW_CONSENT if checked else "")
        self._persist(SettingsKey.MEETING_INSIGHT_REVIEW, bool(checked))

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

    def _on_typesafe_enabled_changed(self, checked: bool) -> None:
        self._persist(SettingsKey.TYPESAFE_ENABLED, bool(checked))
        self._update_typesafe_feature_tiles()

    def _selected_typesafe_route(self):
        """The Fast judgments route Settings name now: TypeSafe unless another was chosen."""
        return typesafe_route_from_settings(self._settings_snapshot())

    def _typesafe_key_present(self, route=None) -> bool:
        """Whether ``route``'s key (default: the chosen route's) resolves, never raising into the UI."""
        try:
            return typesafe_key_present(route=route or self._selected_typesafe_route())
        except Exception:
            logger.exception("TypeSafe key lookup failed")
            return False

    @staticmethod
    def _other_typesafe_route(route):
        """The other way to Jev, whose key can stand in; None for a text model."""
        if route.profile is not None:
            return None
        return JEV_OPENROUTER_ROUTE if route is JEV_TYPESAFE_ROUTE else JEV_TYPESAFE_ROUTE

    @staticmethod
    def _typesafe_destination_copy(route) -> str:
        """Who receives excerpts over ``route``; OpenRouter is a hop, not the model."""
        if route.is_local:
            return (
                f"Transcript excerpts stay on this computer: {route.name} answers "
                "here, even in meetings with AI insights off."
            )
        if route.profile is not None:
            return (
                f"Transcript excerpts go to {route.name} ({route.host}) only "
                "while AI insights are on for the meeting."
            )
        if route is JEV_TYPESAFE_ROUTE:
            return (
                f"Transcript excerpts go to TypeSafe ({route.host}) only "
                "while AI insights are on for the meeting."
            )
        return (
            f"Transcript excerpts go to {route.name} ({route.host}), which "
            "passes them to TypeSafe, only while AI insights are on for the "
            "meeting."
        )

    def _on_typesafe_route_activated(self, index: int) -> None:
        route_id = TypeSafeProvider.ALL[index]
        if route_id == self._selected_typesafe_route().id:
            return
        self._persist(SettingsKey.TYPESAFE_PROVIDER, route_id)
        # A result for the other route would now describe the wrong thing.
        self.typesafe_route_status.hide()
        self._update_typesafe_feature_tiles()

    def _on_typesafe_text_model_activated(self, index: int) -> None:
        source = TypeSafeTextModel.ALL[index]
        if source == resolve_typesafe_text_model(self._settings_snapshot()):
            return
        self._persist(SettingsKey.TYPESAFE_TEXT_MODEL, source)
        self.typesafe_route_status.hide()
        self._update_typesafe_feature_tiles()

    def _switch_typesafe_route(self) -> None:
        other = self._other_typesafe_route(self._selected_typesafe_route())
        if other is None:
            return
        self.typesafe_route_bar.setCurrentIndex(TypeSafeProvider.ALL.index(other.id))
        self._on_typesafe_route_activated(self.typesafe_route_bar.currentIndex())

    @staticmethod
    def _text_model_label(route) -> str:
        # OpenRouter ids carry the vendor; the profile already names the route.
        model = (route.model or "").rsplit("/", 1)[-1]
        return f"{route.name} · {model}" if model else route.name

    def _render_typesafe_route(self) -> None:
        """Show the chosen route, each route's key state, and where excerpts go."""
        # The destination line is the page's last widget, so a refresh during
        # construction waits for the whole page.
        if "typesafe_destination_label" not in self.__dict__:
            return
        settings = self._settings_snapshot()
        route = self._selected_typesafe_route()
        text_route = typesafe_text_model_route(settings)
        self.typesafe_route_bar.setCurrentIndex(TypeSafeProvider.ALL.index(route.id))
        routes = {**{r.id: r for r in (JEV_TYPESAFE_ROUTE, JEV_OPENROUTER_ROUTE)},
                  TypeSafeProvider.TEXT_MODEL: text_route}
        for index, candidate in enumerate(routes[route_id] for route_id in TypeSafeProvider.ALL):
            state = "key set" if self._typesafe_key_present(candidate) else "no key"
            detail = (
                f"Direct · {state}" if candidate is JEV_TYPESAFE_ROUTE else
                f"Same model · {state}" if candidate is JEV_OPENROUTER_ROUTE else
                f"{candidate.name} · on this PC" if candidate.is_local else
                f"{candidate.name} · {state}"
            )
            self.typesafe_route_bar.set_detail(index, detail)
        self.typesafe_text_model_bar.setVisible(route.profile is not None)
        self.typesafe_text_model_bar.setCurrentIndex(
            TypeSafeTextModel.ALL.index(resolve_typesafe_text_model(settings)))
        for index, source in enumerate(TypeSafeTextModel.ALL):
            candidate = typesafe_text_model_route({**settings, SettingsKey.TYPESAFE_TEXT_MODEL: source})
            self.typesafe_text_model_bar.set_detail(index, self._text_model_label(candidate))
        if route is JEV_TYPESAFE_ROUTE:
            description = (
                f"Answers come straight from TypeSafe ({route.host}) and are "
                "billed to your TypeSafe account."
            )
        elif route is JEV_OPENROUTER_ROUTE:
            description = (
                f"The same Jev model, reached through {route.name} ({route.host}) "
                f"and billed to your {route.name} credits. No TypeSafe account needed."
            )
        else:
            where = "on this computer" if route.is_local else f"at {route.host}"
            description = (
                f"{self._text_model_label(route)} answers {where}, as set under AI "
                "cleanup or Meeting Mode → Intelligence. Slower than Jev and not benchmarked like it: a "
                f"judgment that takes over {TEXT_MODEL_TIMEOUT_S:.0f} s is skipped."
            )
        self.typesafe_route_tile.set_description(description)
        self.typesafe_destination_label.setText(self._typesafe_destination_copy(route))
        self.typesafe_route_test_btn.setEnabled(not self._typesafe_route_testing)

    def _test_typesafe_route(self) -> None:
        if self._typesafe_route_testing:
            return
        route = self._selected_typesafe_route()
        key = typesafe_route_key(route)
        if not key:
            self._show_typesafe_route_result(
                False, f"No {route.key_label} to test. Add one under API keys → {route.name}."
            )
            return
        self._typesafe_route_testing = True
        self.typesafe_route_test_btn.setEnabled(False)
        self._show_typesafe_route_result(None, f"Testing {route.name}…")
        self._typesafe_route_token = token = next(_tokens)
        # Bound here: the worker must not reach back into the window.
        verify = typesafe_verify_key
        done = _thread_relay().typesafe_route_verified

        def worker():
            ok, detail = verify(key, route=route)
            done.emit(token, route.id, ok, detail)

        threading.Thread(target=worker, daemon=True, name="jev-route-verify").start()

    def _on_typesafe_route_verified(
        self, token: int, route_id: str, ok: bool, detail: str
    ) -> None:
        if token != self._typesafe_route_token:
            return
        self._typesafe_route_testing = False
        self.typesafe_route_test_btn.setEnabled(True)
        # The person switched routes while this one was being tested.
        if route_id != self._selected_typesafe_route().id:
            return
        self._show_typesafe_route_result(ok, detail)

    def _show_typesafe_route_result(self, ok: Optional[bool], text: str) -> None:
        self.typesafe_route_status.setText(text)
        set_style_property(self.typesafe_route_status, "tone", "success" if ok else "warning")
        self.typesafe_route_status.show()

    def _render_typesafe_key_state(self) -> None:
        """Say plainly when a switch is on but the key that powers it is missing.

        The features themselves are built to degrade to "no judgment", which
        is right at runtime and wrong in Settings: without this the page looks
        identical whether or not anything will ever run.
        """
        # Rail refreshes can run before the fast-judgments page is built.
        if "typesafe_key_notice" not in self.__dict__:
            return
        self._render_typesafe_route()
        route = self._selected_typesafe_route()
        other = self._other_typesafe_route(route)
        present = self._typesafe_key_present(route)
        other_present = other is not None and not present and self._typesafe_key_present(other)
        master_on = self.typesafe_enabled_check.isChecked()
        self.typesafe_key_notice.setVisible(not present)
        self.typesafe_switch_route_btn.setVisible(other_present)
        # One blue action per banner: switching routes leads when it can.
        set_style_property(
            self.open_typesafe_key_btn, "tone", "neutral" if other_present else "primary"
        )
        if other is not None:
            self.typesafe_switch_route_btn.setText(f"Use {other.name}")
        self.open_typesafe_key_btn.setToolTip(
            f"Open API keys with the {route.name} credential selected"
        )
        if present:
            return
        self.typesafe_key_notice.title_label.setText(f"No {route.key_label}")
        situation = (
            "Nothing on this page runs without one, and these checks fail "
            "quietly by design — no meeting will warn you."
            if master_on else
            "Fast judgments are off and no key is set. Turning anything on "
            "below has no effect until there is one."
        )
        remedy = (
            f"Your {other.key_label} can run the same model instead, or add a "
            f"key under API keys → {route.name}."
            if other_present else
            f"Add a key under API keys → {route.name}, or set "
            f"{route.credential_env}."
        )
        self.typesafe_key_notice.set_description(f"{situation} {remedy}")

    def _update_typesafe_feature_tiles(self) -> None:
        """Feature switches only mean something while the master switch is on."""
        enabled = self.typesafe_enabled_check.isChecked()
        for tile in (
            self.typesafe_topic_shift_tile,
            self.typesafe_voice_commands_tile,
            *self.typesafe_feature_tiles.values(),
        ):
            tile.setEnabled(enabled)
        self._render_typesafe_key_state()
        self.rail.set_value(MEETING_FAST, self._meeting_fast_rail_value())

    def _meeting_fast_rail_value(self) -> str:
        settings = self._settings_snapshot()
        if not resolve_typesafe_enabled(settings):
            return "Off"
        if not self._typesafe_key_present():
            return "No key"
        keys = (SettingsKey.TYPESAFE_TOPIC_SHIFT_ENABLED, SettingsKey.TYPESAFE_VOICE_COMMANDS_ENABLED,
                SettingsKey.TYPESAFE_CITATIONS_ENABLED, SettingsKey.TYPESAFE_SEMANTIC_SEARCH_ENABLED,
                SettingsKey.TYPESAFE_QUESTION_RADAR_ENABLED, SettingsKey.TYPESAFE_HIGHLIGHTS_ENABLED)
        active = sum(setting_value(key, settings) is True for key in keys)
        value = "On · no features" if active == 0 else f"On · {active}"
        route = typesafe_route_from_settings(settings)
        return value if route is JEV_TYPESAFE_ROUTE else f"{value} · {route.name}"

    def _update_cleanup_prompt_ui(self) -> None:
        control = self.__dict__.get("transcript_cleanup_check")
        if control is not None:
            enabled = control.isChecked()
        else:
            level = cleanup_prompts.resolve_level(self._settings_snapshot())
            enabled = level != CleanupLevel.NONE
        for name in (
            "cleanup_level_tile",
            "cleanup_prompt_tile",
            "cleanup_rules_composer_tile",
            "cleanup_rules_library_tile",
        ):
            widget = self.__dict__.get(name)
            if widget is not None:
                widget.setEnabled(enabled)
        self._update_cleanup_gates(enabled)
        if STYLES in self._built_pages:
            # Styles lock while cleanup is off, also when it changes on their page.
            self._refresh_page_module(STYLES, self._page_modules[STYLES])
        if self.__dict__.get("cleanup_rules_gate_tile") is not None:
            self._update_cleanup_rule_controls()
        self._update_cleanup_level_ui()

    def _update_cleanup_level_ui(self) -> None:
        """Show the saved level, or Custom while a custom prompt replaces it."""
        bar = self.__dict__.get("cleanup_level_bar")
        if bar is None:
            return
        settings = self._settings_snapshot()
        level = resolve_transcript_cleanup_level(settings)
        custom = bool(cleanup_prompts.custom_prompt(settings))
        choice = cleanup_levels.CUSTOM if custom else level
        bar.buttons[-1].setEnabled(custom)
        bar.setCurrentIndex(cleanup_levels.segment_index(choice))
        self.cleanup_level_note.setText(cleanup_levels.NOTES[choice])
        self.cleanup_prompt_edit.setPlaceholderText(cleanup_prompts.preset(level))
        self._update_cleanup_prompt_buttons()

    def _update_cleanup_prompt_buttons(self) -> None:
        self.cleanup_prompt_reset_btn.setEnabled(
            bool(self._saved_cleanup_prompt or self.cleanup_prompt_edit.toPlainText().strip())
        )

    def _open_cleanup_prompt_editor(self) -> None:
        # With no custom prompt yet, start from the level's instructions.
        text = self.cleanup_prompt_edit.toPlainText().strip() or cleanup_prompts.preset(
            resolve_transcript_cleanup_level(self._settings_snapshot())
        )
        dialog = CleanupPromptDialog(text, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.cleanup_prompt_edit.setPlainText(dialog.prompt_text())
            self._persist_cleanup_prompt()

    def _reset_cleanup_prompt(self) -> None:
        """Remove the custom prompt, after asking, so the level applies again."""
        settings = self._settings_snapshot()
        if cleanup_prompts.custom_prompt(settings) and not self._confirm_custom_prompt_removal(
            resolve_transcript_cleanup_level(settings)
        ):
            return
        if self._persist_many({}, drops=(SettingsKey.TRANSCRIPT_CLEANUP_PROMPT,)):
            self._forget_custom_prompt()
            self._update_cleanup_prompt_ui()

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

    def _set_rule_notice(self, text: str) -> None:
        """Show a problem under the composer, or clear it with ``""``."""
        self.cleanup_rule_status.setText(text)
        self.cleanup_rule_status.setVisible(bool(text))

    def _set_rule_mic_recording(self, recording: bool) -> None:
        button = self.cleanup_rule_mic_btn
        set_style_property(button, "recording", recording)
        button.setText("Stop" if recording else "Dictate")
        button.setToolTip(
            "Stop and transcribe · Esc throws the recording away"
            if recording else
            "Speak the instruction instead of typing it"
        )

    def _rule_transcribe_detail(self) -> str:
        """Where the dictation engine runs, named as the rail names it."""
        summary = self.models.voice_summary()
        if self.models.voice_is_cloud():
            # The rail names the server first: "Groq · whisper-large-v3".
            return f"Sent to {summary}"
        if self.models.voice_is_remote():
            return self.models.voice_detail()
        return f"On this computer · {summary}"

    @staticmethod
    def _rule_polish_detail(provider: str, model: str, settings) -> str:
        """The endpoint and model that will rewrite the words."""
        name = profile_display_name(provider, settings)
        return " · ".join(part for part in (name, model) if part)

    def _add_cleanup_rule(self) -> None:
        self._polish_cleanup_rule(self.cleanup_rule_input.text())

    def _polish_cleanup_rule(self, raw: str, dictated: bool = False) -> None:
        raw = raw.strip()
        if self._rule_polishing or self._rule_dictation_state != "idle":
            return
        if not raw:
            self.cleanup_rule_activity.finish()
            return
        staged = {r.casefold() for r in self._staged_cleanup_rules()}
        if raw.casefold() in staged:
            self.cleanup_rule_activity.finish()
            self._set_rule_notice("That rule already exists.")
            return
        if self.cleanup_rules_list.count() >= config.MAX_TRANSCRIPT_CLEANUP_RULES:
            self.cleanup_rule_activity.finish()
            self._set_rule_notice(
                f"Rule limit reached ({config.MAX_TRANSCRIPT_CLEANUP_RULES})."
            )
            return

        self._rule_polishing = True
        self._rule_polish_dictated = dictated
        self._set_rule_notice("")

        settings = settings_manager.load_all_settings()
        provider = resolve_transcript_cleanup_provider(settings)
        model = resolve_transcript_cleanup_model(settings)
        reasoning = resolve_transcript_cleanup_reasoning(settings)
        self.cleanup_rule_activity.show_polishing(
            self._rule_polish_detail(provider, model, settings)
        )
        self._update_cleanup_rule_controls()
        self._rule_polish_token = token = next(_tokens)
        done = _thread_relay().cleanup_rule_polished

        def worker():
            try:
                from services.transcript_cleanup import polish_cleanup_rule

                polished, error = polish_cleanup_rule(
                    raw, provider=provider, model=model, reasoning=reasoning
                )
            except Exception as exc:
                polished, error = raw, str(exc)
            done.emit(token, raw, polished or raw, error or "")

        threading.Thread(
            target=worker, name="cleanup-rule-polish", daemon=True
        ).start()

    def _on_cleanup_rule_polished(
        self, token: int, raw: str, polished: str, error: str
    ) -> None:
        if token != self._rule_polish_token:
            return
        self._rule_polishing = False
        dictated, self._rule_polish_dictated = self._rule_polish_dictated, False
        self.cleanup_rule_activity.finish()
        self._update_cleanup_rule_controls()
        if not self.isVisible():
            # Settings closed while the model worked. The words are still in
            # the input for next time; a review dialog now would pop up alone.
            return

        notice = None
        if error:
            notice = (
                "AI polish unavailable — your words will be saved as transcribed."
                if dictated else
                "AI polish unavailable — your wording will be saved as written."
            )
        dialog = CleanupRuleDialog(
            polished, original=raw, notice=notice, dictated=dictated, parent=self
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        rule = dialog.rule_text()
        if not rule:
            return
        staged = {r.casefold() for r in self._staged_cleanup_rules()}
        if rule.casefold() in staged:
            self._set_rule_notice("That rule already exists.")
            return
        self.cleanup_rules_list.addItem(rule)
        item = self.cleanup_rules_list.item(self.cleanup_rules_list.count() - 1)
        self.cleanup_rule_input.clear()
        self._update_cleanup_rule_controls()
        self._persist_cleanup_rules()
        self._rule_glow.flash(item)

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
            self._set_rule_notice("Dictation is unavailable.")
            return
        if self.get_meeting_active is not None and self.get_meeting_active():
            self._set_rule_notice(
                "Meeting Mode is active — end the meeting to dictate a rule."
            )
            return

        settings = self._settings_snapshot()
        priority = settings_microphones.saved_priority(settings)
        order = tuple((entry["hostapi"], entry["name"]) for entry in priority)
        if self._rule_recorder is None or self._rule_recorder_device != order:
            if self._rule_recorder is not None:
                self._rule_recorder.cleanup()
            self._rule_recorder = AudioRecorder(
                device_priority=priority, output_file=self._rule_dictation_path
            )
            # Levels come from the audio thread, which must not reach back
            # into the window; the relay hands them to the composer's trace.
            self._rule_level_token = token = next(_tokens)
            level = _thread_relay().rule_dictation_level
            self._rule_recorder.set_audio_level_callback(
                lambda value: level.emit(token, value)
            )
            self._rule_recorder_device = order

        if not self._rule_recorder.start_recording():
            reason = self._rule_recorder.last_start_error
            self._set_rule_notice(
                f"Couldn't start recording: {reason}." if reason
                else "Couldn't start recording."
            )
            return
        self._rule_dictation_state = "recording"
        self._set_rule_notice("")
        self._set_rule_mic_recording(True)
        self.cleanup_rule_activity.show_listening(
            settings_microphones.preferred_name(settings)
        )
        self._rule_dictation_timer.start()
        self._update_cleanup_rule_controls()

    def _on_rule_dictation_level(self, token: int, level: float) -> None:
        if token == self._rule_level_token:
            self.cleanup_rule_activity.push_level(level)

    def _cancel_rule_dictation(self) -> None:
        """Throw the take away without transcribing it."""
        if self._rule_dictation_state != "recording":
            return
        self._rule_dictation_timer.stop()
        if self._rule_recorder is not None:
            self._rule_recorder.cancel_recording()
        self._rule_dictation_state = "idle"
        self._set_rule_mic_recording(False)
        self.cleanup_rule_activity.finish()
        self._update_cleanup_rule_controls()

    def event(self, event):
        # While a global capture is waiting, Esc must not reject Settings.
        # The field also accepts ShortcutOverride; this covers focus elsewhere
        # on the page. capturing is set after QDialog.__init__, which already
        # delivers events, so look it up only once it exists.
        if (
            getattr(self, "capturing", None) is not None
            and event.type() == QEvent.Type.ShortcutOverride
            and event.key() == Qt.Key.Key_Escape
        ):
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event) -> None:
        # Esc while a rule is being dictated discards just the take; Settings
        # stays open.
        if (
            event.key() == Qt.Key.Key_Escape
            and self._rule_dictation_state == "recording"
        ):
            self._cancel_rule_dictation()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape and self.capturing is not None:
            self._cancel_hotkey_capture()
            event.accept()
            return
        # Settings saves as you go and has no default action, but QDialog
        # makes the first auto-default button in the focus chain its default
        # and clicks it for every Enter a field ignores. A focused button
        # still takes Enter itself before it gets here.
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.accept()
            return
        super().keyPressEvent(event)

    def _stop_rule_dictation(self) -> None:
        if self._rule_dictation_state != "recording":
            return
        self._rule_dictation_timer.stop()
        self._rule_dictation_state = "transcribing"
        self._set_rule_mic_recording(False)
        self.cleanup_rule_activity.show_transcribing(self._rule_transcribe_detail())
        self._update_cleanup_rule_controls()

        recorder = self._rule_recorder
        transcribe = self.on_dictation_transcribe
        audio_path = self._rule_dictation_path
        self._rule_dictation_token = token = next(_tokens)
        done = _thread_relay().rule_dictation_finished

        def worker():
            text, error = _transcribe_rule_dictation(recorder, transcribe, audio_path)
            done.emit(token, text.strip(), error)

        threading.Thread(
            target=worker, name="rule-dictation", daemon=True
        ).start()

    def _on_rule_dictation_finished(self, token: int, text: str, error: str) -> None:
        if token != self._rule_dictation_token:
            return
        self._rule_dictation_state = "idle"
        self._update_cleanup_rule_controls()
        if error:
            self.cleanup_rule_activity.finish()
            self._set_rule_notice(error)
            return
        current = self.cleanup_rule_input.text().strip()
        raw = f"{current} {text}".strip() if current else text
        # The words land in the input first, so a canceled review (or a
        # Settings window closed mid-polish) keeps them for editing.
        self.cleanup_rule_input.setText(raw)
        if not self.isVisible():
            self.cleanup_rule_activity.finish(animate=False)
            return
        self._polish_cleanup_rule(raw, dictated=True)

    def _release_rule_recorder(self, *_args) -> None:
        self._rule_dictation_timer.stop()
        if self._rule_dictation_state == "recording":
            # Settings is reused when it reopens, so a take still recording
            # at close is discarded here rather than left showing "Stop".
            self._rule_dictation_state = "idle"
            self._set_rule_mic_recording(False)
            self.cleanup_rule_activity.finish(animate=False)
            self._update_cleanup_rule_controls()
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
        self._meeting_bindings.load(settings, start=self._initial_meeting_binding_start)
        if self._page_is_loading(MEETING_AFTER):
            review_on = (
                settings.get(SettingsKey.MEETING_INSIGHT_REVIEW) is True
                and settings.get(SettingsKey.MEETING_INSIGHT_REVIEW_CONSENT) == _INSIGHT_REVIEW_CONSENT
            )
            self.meeting_review_check.setChecked(review_on)
            self.meeting_end_report_check.setChecked(resolve_meeting_end_report(settings))
            self.meeting_report_ribbon_check.setChecked(resolve_meeting_report_ribbon(settings))
            self.meeting_report_brief_check.setChecked(resolve_meeting_report_brief(settings))
            self.meeting_report_signal_check.setChecked(resolve_meeting_report_signal(settings))
            self._guard_report_views()
            self._update_report_views_enabled()
        if self._page_is_loading(MEETING_DASHBOARD):
            _select_combo_data(self.meeting_bind_combo, resolve_meeting_server_bind(settings))
            self._update_meeting_bind_ui()
        if self._page_is_loading(MEETING_FAST):
            self.typesafe_enabled_check.setChecked(resolve_typesafe_enabled(settings))
            self._update_typesafe_feature_tiles()
        if self._page_is_loading(MEETING_INTELLIGENCE):
            self.meeting_context_folder_path.setText(resolve_meeting_context_folder_path(settings))

    def _apply_audio_devices(self, generation, outcome, devices, _error):
        """Take a device list from the enumeration worker; stale lists are dropped."""
        if generation != self._audio_device_generation or self._microphones is None:
            return
        self._microphones.apply_devices(devices, outcome)
        self._refresh_rail_values()

    def _start_hotkey_capture(
        self, key: str, input_field: HotkeyCaptureInput
    ) -> None:
        self._cancel_hotkey_capture()
        bindings = self.__dict__.get("hotkey_bindings")
        if bindings is not None:
            bindings.cancel_capture()
        self.capturing = key
        self.current_hotkey_input = input_field
        input_field.setText(CAPTURE_HINT)
        input_field.set_capturing(True)
        # A key or side button that is already bound would fire its action
        # (or be swallowed by the hook) instead of reaching the capture.
        self._suspend_hotkeys_for_capture(True)

        thread = HotkeyCaptureThread(self)
        self.capture_thread = thread
        thread.finished.connect(thread.deleteLater)
        thread.captured.connect(
            lambda hotkey, thread=thread: self._on_hotkey_captured(
                thread, hotkey
            )
        )
        thread.cancelled.connect(
            lambda thread=thread: self._on_hotkey_capture_cancelled(thread)
        )
        thread.failed.connect(
            lambda message, thread=thread: self._on_hotkey_capture_failed(
                thread, message
            )
        )
        logger.info("Capturing hotkey for %s", key)
        thread.start()

    def _suspend_hotkeys_for_capture(self, suspended: bool) -> None:
        if suspended == self._hotkeys_suspended_for_capture:
            return
        self._hotkeys_suspended_for_capture = suspended
        self.set_hotkey_capture_suspended(suspended)

    def _on_local_hotkey_captured(self, key: str, hotkey: str) -> None:
        self._apply_captured_hotkey(key, hotkey)

    def _on_hotkey_captured(
        self, thread: HotkeyCaptureThread, hotkey: str
    ) -> None:
        if thread is not self.capture_thread or self.capturing is None:
            return
        modifiers, main_key = parse_hotkey(hotkey or "")
        if is_escape_key(main_key) and not modifiers:
            self._cancel_hotkey_capture()
            return
        key = self.capturing
        self._finish_hotkey_capture(thread)
        self._apply_captured_hotkey(key, hotkey)

    def _on_hotkey_capture_cancelled(self, thread: HotkeyCaptureThread) -> None:
        if thread is not self.capture_thread:
            return
        self._cancel_hotkey_capture()

    def _on_mouse_hotkey_captured(self, key: str, hotkey: str) -> None:
        if self.capturing != key:
            return
        self._cancel_hotkey_capture()
        self._apply_captured_hotkey(key, hotkey)

    def _apply_captured_hotkey(self, key: str, hotkey: str) -> bool:
        conflict = self._standard_hotkey_conflict(key, hotkey)
        if conflict:
            self.message_label.setText(conflict)
            self._update_hotkey_displays()
            self._show_hotkey_note(key, conflict, warning=True)
            return False
        updated = self.current_hotkeys.copy()
        updated[key] = hotkey
        label = _HOTKEY_LABELS.get(key, "Shortcut")
        return self._apply_hotkey_settings(updated, f"{label} hotkey updated.")

    def _standard_hotkey_conflict(self, key: str, hotkey: str) -> str:
        """Why ``hotkey`` cannot be ``key``'s shortcut, or "" when it is free."""
        conflict = hotkey_conflict(
            hotkey,
            self._settings_snapshot(),
            exclude=(STANDARD, key),
            standard_hotkeys=self.current_hotkeys,
        )
        if not conflict:
            return ""
        return f"That shortcut is already used by {conflict}."

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
        self._suspend_hotkeys_for_capture(False)

    def _cancel_hotkey_capture(self) -> None:
        thread = self.capture_thread
        if thread is not None:
            try:
                thread.captured.disconnect()
                thread.cancelled.disconnect()
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
        self._suspend_hotkeys_for_capture(False)
        if self._shortcut_rows:
            self._update_hotkey_displays()

    def _apply_hotkey_settings(
        self, hotkeys: Dict[str, str], message: str
    ) -> bool:
        return not self._save_hotkeys(hotkeys, message)

    def _save_hotkeys(self, hotkeys: Dict[str, str], message: str) -> str:
        """Save the standard shortcuts; "" on success, else the shown error."""
        updated = config.DEFAULT_HOTKEYS.copy()
        updated.update(hotkeys)
        try:
            settings = settings_manager.load_all_settings()
            for profile in load_cleanup_profiles(settings):
                conflict = profile_hotkey_conflict(
                    profile.hotkey, settings, exclude_id=profile.id,
                    standard_hotkeys=updated,
                )
                if conflict:
                    raise ValueError(
                        f"{profile.name}'s shortcut is already used by {conflict}. "
                        "Choose another."
                    )
            if self.on_hotkeys_changed:
                self.on_hotkeys_changed(updated.copy())
            else:
                settings_manager.save_hotkey_settings(updated)
        except Exception as exc:
            logger.error("Couldn't save hotkeys: %s", exc)
            error = f"Couldn't save hotkeys: {exc}"
            self.message_label.setText(error)
            self._update_hotkey_displays()
            return error
        self.current_hotkeys = updated
        self._update_hotkey_displays()
        # A profile's, transform's or action's shortcut may now clash with one.
        self._refresh_binding_shortcuts()
        self.message_label.setText(message)
        self._refresh_rail_values()
        return ""

    def _clear_hotkey(self, key: str) -> None:
        self._cancel_hotkey_capture()
        updated = self.current_hotkeys.copy()
        updated[key] = ""
        label = _HOTKEY_LABELS.get(key, "Shortcut")
        self._apply_hotkey_settings(updated, f"{label} hotkey cleared.")

    def _confirm_reset_hotkeys(self) -> None:
        self._cancel_hotkey_capture()
        answer = QMessageBox.question(
            self,
            "Reset hotkeys?",
            "Put OpenWhisper's own shortcuts back to the platform defaults? "
            "Profile, transform and action shortcuts stay as they are.",
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
        if self.home is not None:
            self.home.refresh_shortcut()
        for row in self._shortcut_rows:
            hotkey = self.current_hotkeys.get(row.key, "")
            if self._native_wayland:
                row.field.set_hotkey(hotkey)
            else:
                row.field.setText(format_hotkey_display(hotkey))
            self._set_row_note(row, mouse_shortcut_note(hotkey))
            if row.clear is not None:
                row.clear.setEnabled(bool(hotkey))

    def _load_settings(self) -> None:
        settings = self._settings_snapshot()
        if not self.current_hotkeys or self._initializing_page in (None, HOTKEYS, WALKTHROUGHS):
            self._load_hotkey_settings()
        self._bindings.load(settings, start=self._initial_binding_start)
        self._load_cleanup_pages(settings)
        if self._page_is_loading(GENERAL):
            self.update_check_check.setChecked(resolve_update_check_enabled(settings))
            self.update_notify_tile.setEnabled(self.update_check_check.isChecked())
        if self._page_is_loading(RECORDING):
            self._load_retention_settings(settings)
            self.streaming_enabled_check.setChecked(setting_value(SettingsKey.STREAMING_ENABLED, settings))
            self._microphones.load(settings)
        if self._page_is_loading(APPEARANCE):
            self._load_appearance_settings(settings)
        self._load_meeting_settings(settings)
        if self._page_is_loading(API_KEYS):
            self._load_api_key_settings(settings)
        if self._page_is_loading(ADVANCED):
            self.developer_mode_check.setChecked(resolve_developer_mode(settings))
        if self._page_is_loading(DOWNLOADS):
            policy = settings_manager.load_hf_access_policy()
            _select_combo_data(self.hf_policy_combo, policy)
        if self._page_is_loading(HOTKEYS):
            trigger_mode = resolve_recording_trigger_mode(settings)
            _select_combo_data(self.record_mode_combo, trigger_mode)
            self._load_hands_free_latch(settings)
            self.hotkey_bindings.load(settings)
        self._load_page_modules(settings)

    def _load_cleanup_pages(self, settings: dict) -> None:
        if self._page_is_loading(CLEANUP):
            self.transcript_cleanup_check.setChecked(setting_value(SettingsKey.TRANSCRIPT_CLEANUP_ENABLED, settings))
            # Empty unless a real custom prompt is saved; the preset shows
            # as the placeholder.
            prompt = cleanup_prompts.custom_prompt(settings)
            self.cleanup_prompt_edit.setPlainText(prompt)
            self._saved_cleanup_prompt = prompt
        if self._page_is_loading(CLEANUP_RULES):
            self.cleanup_rules_list.clear()
            self.cleanup_rules_list.addItems(resolve_transcript_cleanup_rules(settings))
        self._update_cleanup_prompt_ui()

    def _load_appearance_settings(self, settings: dict) -> None:
        _select_combo_data(self.ui_font_scale_combo, resolve_ui_font_scale(settings))
        _select_combo_data(self.ui_theme_combo, resolve_ui_theme(settings))
        self.streaming_font_size_spinbox.setValue(resolve_streaming_overlay_font_size(settings))
        self.streaming_layout_bar.setCurrentIndex(
            StreamingOverlayLayout.ALL.index(resolve_streaming_overlay_layout(settings))
        )
        look = resolve_recording_overlay_look(settings)
        self.recording_look_bar.setCurrentIndex(RecordingOverlayLook.ALL.index(look))
        self.recording_look_preview.set_look(look)
        self.recording_dot_switch.setChecked(resolve_recording_overlay_dot(settings))
        self.recording_text_switch.setChecked(resolve_recording_overlay_text(settings))
        self.recording_clock_switch.setChecked(resolve_recording_overlay_clock(settings))
        self._show_overlay_parts()

    def _load_page_modules(self, settings: dict) -> None:
        for key, module in self._page_modules.items():
            if not self._page_is_loading(key):
                continue
            # One page module's bad state must not stop Settings from opening.
            try:
                module.load(self, settings)
            except Exception:
                logger.exception("Couldn't load the %s settings page", key)

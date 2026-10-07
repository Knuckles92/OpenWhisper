import logging
import os
import sys
import tempfile
import threading
from contextlib import contextmanager
from typing import Callable, Dict, Optional

from PyQt6.QtCore import QEvent, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QDialog,
    QButtonGroup,
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
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QSystemTrayIcon,
    QTabBar,
    QTextEdit,
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
from services.typesafe import ROUTES as JEV_ROUTES
from services.typesafe import TYPESAFE_ROUTE as JEV_TYPESAFE_ROUTE
from services.typesafe import key_present as typesafe_key_present
from services.typesafe import route_from_settings as typesafe_route_from_settings
from services.typesafe import verify_key as typesafe_verify_key
from services.format_utils import format_file_size
from services.history_manager import history_manager
from services.hotkey_manager import USE_PYNPUT_BACKEND, format_hotkey_display
from services.recorder import AudioRecorder
from services.settings import (
    LEGACY_STREAMING_KEYS,
    HuggingFaceAccessPolicy,
    MeetingServerBind,
    RecordingRetentionMode,
    RecordingTriggerMode,
    SettingsKey,
    SettingsView,
    UiFontScale,
    UiTheme,
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
    resolve_meeting_report_brief,
    resolve_meeting_report_ribbon,
    resolve_meeting_report_signal,
    resolve_recording_trigger_mode,
    resolve_meeting_server_bind,
    resolve_meeting_server_port,
    resolve_streaming_overlay_font_size,
    resolve_ui_font_scale,
    resolve_ui_theme,
    resolve_settings_view,
    resolve_transcript_cleanup_model,
    resolve_transcript_cleanup_prompt,
    resolve_transcript_cleanup_provider,
    resolve_transcript_cleanup_reasoning,
    resolve_transcript_cleanup_rules,
    resolve_update_check_enabled,
    resolve_update_notify_enabled,
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
from ui_qt.dialogs.cleanup_prompt_dialog import CleanupPromptDialog
from ui_qt.dialogs.cleanup_rule_dialog import CleanupRuleDialog
from ui_qt.dialogs.settings_binder import SettingsBinder
from ui_qt.dialogs.settings_metadata import CONTROL_DESTINATIONS, PAGE_SEARCH_FIELDS, PAGE_HELP_TEXT
from ui_qt.utils.list_reconcile import HistoryDelivery
from ui_qt.dialogs.settings_destinations import (
    ADVANCED,
    BASIC_APP,
    BASIC_DICTATION,
    BASIC_MEETINGS,
    API_KEYS,
    BACKUP,
    CLEANUP,
    CLEANUP_PROFILES,
    CLEANUP_RULES,
    DOWNLOADS,
    GENERAL,
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
    VOICE_MODEL,
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
from ui_qt.dialogs.settings_overview import OverviewPage, OverviewSummary
from ui_qt.dialogs.settings_basic import BasicSettingsPage
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
from ui_qt.utils.app_icon import app_icon
from ui_qt.utils.font_scale import current_ui_font_scale
from ui_qt.utils.icons import design_icon
from ui_qt.utils.restyle import set_style_property
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
from ui_qt.widgets.buttons import (
    compact_primary_button,
    fit_compact_button,
    neutral_button,
)
from ui_qt.widgets.segmented_bar import SegmentedBar
from ui_qt.widgets.hotkey_capture import HotkeyCaptureInput, HotkeyCaptureThread
from ui_qt.widgets.cleanup_profiles_panel import CleanupProfilesPanel
from services.cleanup_profiles import load_cleanup_profiles, profile_hotkey_conflict
from ui_qt.widgets.nav_rail import NavRail
from ui_qt.widgets.rule_activity import ItemGlow, RuleActivityStrip

logger = logging.getLogger(__name__)

#: A dictated rule stops recording on its own after this long.
_RULE_DICTATION_CAP_MS = 60_000


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
    MCP: (
        "Connect an agent with MCP",
        "App › MCP",
        "model context protocol agent server connection claude cursor history api",
    ),
    VOICE_MODEL: (
        "Model assignments",
        "Model Manager's choices now sit on Voice model, AI cleanup, Voice & "
        "speakers, Intelligence, and Runtime",
        "model manager models assign",
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

    Basic curates common controls; Advanced groups every destination by
    feature under an Overview landing page. Both save through the same
    handlers, and the selected view persists between sessions.
    ``UIController`` holds a single instance and re-raises it instead of
    stacking copies.
    """

    #: Client area; with the title bar it opens at about 1317x882 on a
    #: 1080p screen, and leaves the Downloads catalog room beside its profile.
    DEFAULT_SIZE = QSize(1315, 850)
    #: The Downloads filter row is the widest fixed content in the window.
    MINIMUM_SIZE = QSize(940, 520)
    BASIC_DEFAULT_SIZE = QSize(1040, 850)
    BASIC_MINIMUM_SIZE = QSize(720, 520)

    _cleanup_rule_polished = pyqtSignal(str, str, str)
    _rule_dictation_finished = pyqtSignal(str, str)
    _rule_dictation_level = pyqtSignal(float)
    _api_key_verified = pyqtSignal(str, bool, str)
    _typesafe_route_verified = pyqtSignal(str, bool, str)

    on_audio_device_changed: Optional[Callable] = None
    on_streaming_settings_changed: Optional[Callable] = None
    on_streaming_font_changed: Optional[Callable] = None
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
        self._settings_view = SettingsView.ADVANCED
        self._basic_pages = {}
        self._advanced_rail_width = NavRail.RAIL_WIDTH
        self._pages_ready = False
        self._built_pages = set()
        self._building_pages = set()
        self._initializing_page = None
        self._initial_binding_start = 0
        self._initial_meeting_binding_start = 0
        self._refresh_snapshot = None
        self._recording_query_generation = 0
        self._audio_device_generation = 0
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
        self.hotkey_inputs: Dict[str, HotkeyCaptureInput] = {}
        self.hotkey_row_descriptions: Dict[str, WrappedLabel] = {}
        self._saved_cleanup_prompt = ""
        self._rule_polishing = False
        # Whether the words being polished came (at least partly) from
        # dictation, so the review dialog can say "said" instead of "typed".
        self._rule_polish_dictated = False
        self._rule_dictation_state = "idle"
        self._rule_recorder: Optional[AudioRecorder] = None
        self._rule_recorder_device: Optional[int] = None
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

        self._cleanup_rule_polished.connect(self._on_cleanup_rule_polished)
        self._rule_dictation_finished.connect(self._on_rule_dictation_finished)
        self._api_key_verified.connect(self._on_api_key_verified)
        self._typesafe_route_verified.connect(self._on_typesafe_route_verified)
        self.finished.connect(self._release_rule_recorder)
        self.models.assignments_changed.connect(self._refresh_rail_values)
        self.models.downloads_requested.connect(self.show_downloads)
        self.downloads.inventory_changed.connect(self._refresh_rail_values)
        self.overview.destination_requested.connect(self.select_destination)

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
        self.set_settings_view(resolve_settings_view(self._settings_snapshot()), persist=False)
        if self._settings_view == SettingsView.BASIC:
            self.resize(self.BASIC_DEFAULT_SIZE)
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
        row.addStretch()
        segment = QFrame()
        segment.setObjectName("settingsViewSegment")
        buttons = QHBoxLayout(segment)
        buttons.setContentsMargins(3, 3, 3, 3)
        buttons.setSpacing(2)
        self.view_button_group = QButtonGroup(self)
        self.view_buttons = {}
        for view in SettingsView.ALL:
            button = QPushButton(view.title())
            button.setObjectName("settingsViewButton")
            button.setCheckable(True)
            button.setMinimumWidth(100)
            button.setAccessibleName(f"{view.title()} settings")
            button.clicked.connect(lambda _checked, view=view: self.set_settings_view(view))
            self.view_button_group.addButton(button)
            self.view_buttons[view] = button
            buttons.addWidget(button)
        row.addWidget(segment)
        return header

    def _build_rail_pane(self) -> QWidget:
        pane = QWidget()
        pane.setObjectName("modelManagerRailPane")
        pane.setMinimumWidth(220)
        pane.setMaximumWidth(560)
        column = QVBoxLayout(pane)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        self.search_button = QPushButton()
        self.search_button.setObjectName("settingsRailSearch")
        self.search_button.setCursor(Qt.CursorShape.PointingHandCursor)
        # Ctrl+K is the keyboard path; as the first control in the window it
        # would otherwise take focus on open and show a permanent focus ring.
        self.search_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.search_button.setToolTip("Search every setting, model, and help note")
        self.search_button.setAccessibleName("Search settings and models")
        search_row = QHBoxLayout(self.search_button)
        search_row.setContentsMargins(10, 0, 8, 0)
        search_row.setSpacing(8)
        search_icon = QLabel()
        search_icon.setObjectName("settingsRailSearchIcon")
        search_icon.setPixmap(design_icon("search-slate.svg").pixmap(14, 14))
        search_row.addWidget(search_icon)
        search_text = QLabel("Search settings")
        search_text.setObjectName("settingsRailSearchText")
        search_row.addWidget(search_text, stretch=1)
        self.search_hint = QLabel(shortcut_text("Ctrl+K"))
        self.search_hint.setObjectName("settingsRailSearchHint")
        search_row.addWidget(self.search_hint)
        for label in (search_icon, search_text, self.search_hint):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.search_button.setFixedHeight(34)
        self.search_button.clicked.connect(lambda: self.open_search())
        search_holder = QHBoxLayout()
        search_holder.setContentsMargins(12, 12, 12, 8)
        search_holder.addWidget(self.search_button)
        column.addLayout(search_holder)

        self.rail = NavRail()
        self._rail_groups: Dict[str, str] = {}
        self._rail_icons: Dict[str, str] = {}

        def destination(key: str, name: str, icon: str, group: str) -> None:
            self.rail.add_destination(key, name, design_icon(icon))
            self._rail_groups[key] = group
            self._rail_icons[key] = icon

        destination(OVERVIEW, "Overview", "layout-grid-blue.svg", "")
        for group, items in (
            ("Dictation", (
                (VOICE_MODEL, "Voice model", "microphone-blue.svg"),
                (REMOTE_ENGINE, "Remote engine", "server-blue.svg"),
                (RECORDING, "Recording", "microphone-blue.svg"),
                (CLEANUP, "AI cleanup", "stack-purple.svg"),
                (CLEANUP_RULES, "Learned rules", "stack-slate.svg"),
                (CLEANUP_PROFILES, "Profiles", "typography-blue.svg"),
            )),
            ("Meeting Mode", (
                (MEETING_VOICE, "Voice & speakers", "microphone-blue.svg"),
                (MEETING_INTELLIGENCE, "Intelligence", "stack-purple.svg"),
                (MEETING_FAST, "Fast judgments", "bolt-green.svg"),
                (MEETING_AFTER, "After the meeting", "check-green.svg"),
                (MEETING_DASHBOARD, "Dashboard", "box-blue.svg"),
            )),
            ("Models & storage", (
                (DOWNLOADS, "Downloads", "download-blue.svg"),
                (RUNTIME, "Runtime", "cpu-blue.svg"),
                (BACKUP, "Backup & restore", "box-blue.svg"),
            )),
            ("App", (
                (GENERAL, "General", "bolt-green.svg"),
                (HOTKEYS, "Hotkeys", "keyboard-green.svg"),
                (API_KEYS, "API keys", "key-blue.svg"),
                (MCP, "MCP", "server-blue.svg"),
                (ADVANCED, "Developer options", "box-blue.svg"),
            )),
        ):
            self.rail.add_group(group)
            for key, name, icon in items:
                destination(key, name, icon, group)
        self.rail.destination_changed.connect(self._on_destination_changed)
        column.addWidget(self.rail, stretch=1)
        return pane

    def _build_body(self) -> QWidget:
        body = QWidget()
        body.setObjectName("modelManagerBody")
        layout = QVBoxLayout(body)
        self.body_layout = layout
        layout.setContentsMargins(28, 20, 28, 16)
        layout.setSpacing(14)

        self.basic_tabs = QTabBar()
        self.basic_tabs.setObjectName("basicSettingsTabs")
        self.basic_tabs.setExpanding(False)
        self.basic_tabs.setDrawBase(False)
        self._basic_destinations = (BASIC_DICTATION, BASIC_MEETINGS, BASIC_APP)
        for title, icon in (("Dictation", "microphone-blue.svg"),
                            ("Meetings", "microphone-blue.svg"), ("App", "box-blue.svg")):
            self.basic_tabs.addTab(design_icon(icon), title)
        self.basic_tabs.currentChanged.connect(self._on_basic_tab_changed)
        self.basic_tabs.setVisible(False)
        layout.addWidget(self.basic_tabs)

        self.page_title = QLabel("")
        self.page_title.setObjectName("modelManagerTitle")
        self.page_subtitle = WrappedLabel("")
        self.page_subtitle.setObjectName("modelManagerSubtitle")
        layout.addWidget(self.page_title)
        layout.addWidget(self.page_subtitle)

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
        self.overview = OverviewPage()
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
            "Overview",
            "What OpenWhisper is running right now. Click any card to change it.",
            lambda layout: layout.addWidget(self.overview),
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
            RECORDING,
            "Recording",
            "Microphone, saved audio retention, and the live preview overlay.",
            self._build_recording_page,
        )
        self._add_page(
            CLEANUP,
            "AI transcript cleanup",
            "Rewrite a finished dictation with a chat model, together with your "
            "learned rules.",
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
            "Optional TypeSafe/Jev checks. Each feature describes the text it shares.",
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
            "How finished transcriptions leave the app, and how the window "
            "behaves when you close it.",
            self._build_general_page,
        )
        self._add_page(
            HOTKEYS,
            "Hotkeys",
            "Global shortcuts for record, cancel, enable/disable, and "
            "minimize.",
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
            "Connect your AI assistant",
            "MCP · Search your saved dictations and meetings.",
            self._build_mcp_page,
        )
        self._add_page(
            ADVANCED,
            "Developer options",
            "Meeting re-transcription and developer tools.",
            self._build_advanced_page,
        )
        for key, title, subtitle in (
            (BASIC_DICTATION, "Dictation", "The essentials for everyday voice typing."),
            (BASIC_MEETINGS, "Meetings", "The essentials for recording and reviewing meetings."),
            (BASIC_APP, "App", "Make OpenWhisper feel at home on your computer."),
        ):
            self._add_page(key, title, subtitle,
                           lambda layout, key=key: self._build_basic_page(layout, key))
        layout.addWidget(self.stack, stretch=1)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        saved = QLabel("Changes save automatically")
        saved.setObjectName("settingsAutoSaveLabel")
        footer.addWidget(saved)
        footer.addWidget(self.message_label, stretch=1)
        close_btn = Button("Done")
        close_btn.setObjectName("modelManagerCloseButton")
        fit_compact_button(close_btn, 110)
        close_btn.clicked.connect(self.close)
        footer.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignBottom)
        layout.addLayout(footer)
        return body

    def _build_basic_page(self, layout, key) -> None:
        page = BasicSettingsPage(self, key)
        self._basic_pages[key] = page
        layout.addWidget(page)

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
            if key == CLEANUP_RULES:
                self._rule_dictation_level.connect(self.cleanup_rule_activity.push_level)
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
            "Types a dictated transcript into whichever window has focus. The "
            "clipboard is borrowed for the paste and restored afterward. "
            "Upload File results stay in the window with their own Copy buttons."
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

        self.copy_clipboard_tile = SettingTile(
            "Copy to the clipboard",
            "Keeps the transcript on the clipboard when auto-paste is off or "
            "unavailable. A successful paste restores what was there before.",
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
            "Looks for new OpenWhisper releases in the background. Nothing "
            "is installed without your approval.",
            design_icon("refresh-blue.svg"),
        )
        self.update_check_check = self.update_check_tile.checkbox
        self.update_check_check.setObjectName("updateCheckEnabledCheck")

        self.update_notify_tile = SettingTile(
            "Notify me about updates",
            "Opens the update dialog when a newer release is found. Requires "
            "automatic checks.",
            design_icon("info-blue.svg"),
        )
        self.update_notify_check = self.update_notify_tile.checkbox
        self.update_notify_check.setObjectName("updateNotifyEnabledCheck")

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
            design_icon("box-blue.svg"),
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
            "Text size in windows and dialogs. The live preview overlay "
            "has its own under Recording.",
            self.ui_font_scale_combo,
            design_icon("typography-blue.svg"),
        )

        self._tile_group(
            layout, "Output", [self.auto_paste_tile, self.copy_clipboard_tile]
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
            layout, "Appearance", [self.ui_theme_tile, self.ui_font_scale_tile]
        )
        self._tile_group(
            layout, "Updates", [self.update_check_tile, self.update_notify_tile]
        )

        self._bindings.checkbox(
            self.auto_paste_check,
            SettingsKey.AUTO_PASTE,
            lambda settings: setting_value(SettingsKey.AUTO_PASTE, settings),
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

        minimum = (QSize(360, 240) if use_omarchy_ui() else
                   self.BASIC_MINIMUM_SIZE if getattr(self, "_settings_view", SettingsView.ADVANCED) == SettingsView.BASIC
                   else self.MINIMUM_SIZE)
        self.setMinimumSize(minimum.boundedTo(room))
        fitted = self.size().boundedTo(room)
        if fitted != self.size():
            self.resize(fitted)

    def showEvent(self, event):
        if not event.spontaneous():
            # Before QDialog's own showEvent, which centres by the size.
            self._fit_to_screen()
        super().showEvent(event)
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
            design_icon("microphone-blue.svg"),
        )
        self._tile_group(layout, "Microphone", [self.audio_device_tile])

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

        self.max_recordings_label = QLabel("Number to keep:")
        self.max_recordings_spinbox = NoWheelSpinBox()
        self.max_recordings_spinbox.setMinimum(1)
        self.max_recordings_spinbox.setMaximum(1000)
        self.max_recordings_spinbox.setValue(config.MAX_SAVED_RECORDINGS)
        self.max_recordings_spinbox.setMinimumHeight(40)
        self.max_recordings_spinbox.setMinimumWidth(120)
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
        self.max_recordings_mb_label = QLabel("Folder size limit:")
        self.max_recordings_mb_label.setObjectName("settingsTileFieldLabel")
        self.max_recordings_mb_spinbox = NoWheelSpinBox()
        self.max_recordings_mb_spinbox.setMinimum(10)
        self.max_recordings_mb_spinbox.setMaximum(1024 * 1024)
        self.max_recordings_mb_spinbox.setSingleStep(100)
        self.max_recordings_mb_spinbox.setSuffix(" MB")
        self.max_recordings_mb_spinbox.setValue(config.MAX_SAVED_RECORDINGS_MB)
        self.max_recordings_mb_spinbox.setMinimumHeight(40)
        self.max_recordings_mb_spinbox.setMinimumWidth(140)
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
            "General paste and clipboard settings.",
            design_icon("bolt-green.svg"),
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
        self.streaming_font_size_spinbox.setMinimumHeight(40)
        self.streaming_font_size_spinbox.setMinimumWidth(120)
        self.streaming_font_size_spinbox.setKeyboardTracking(False)
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
        self.transcript_cleanup_tile = SettingTile(
            "Clean up transcripts with AI",
            "Runs the selected chat model on each dictated transcript after "
            "transcription, together with your learned rules. The provider's "
            "key lives under API keys.",
            design_icon("stack-purple.svg"),
        )
        self.transcript_cleanup_check = self.transcript_cleanup_tile.checkbox
        self.transcript_cleanup_check.toggled.connect(
            self._on_cleanup_enabled_changed
        )
        # No captions: each card's own title already says what it is.
        self._tile_group(layout, "", [self.transcript_cleanup_tile])

        self.cleanup_model_tile = self.models.build_cleanup_model_section(layout)
        layout.addSpacing(6)

        self.cleanup_prompt_edit = QTextEdit()
        self.cleanup_prompt_edit.setAcceptRichText(False)
        self.cleanup_prompt_edit.setFont(QFont("Segoe UI", 11))
        self.cleanup_prompt_edit.setMinimumHeight(96)
        self.cleanup_prompt_edit.setMaximumHeight(120)
        self.cleanup_prompt_edit.setPlaceholderText(
            "Instructions for how the AI should clean up transcripts…"
        )
        self.cleanup_prompt_edit.installEventFilter(self)
        self.cleanup_prompt_tile = FieldTile(
            "Cleanup prompt",
            "Instructions the model follows when rewriting a transcript.",
            self.cleanup_prompt_edit,
            design_icon("typography-blue.svg"),
        )

        cleanup_btn_row = QHBoxLayout()
        cleanup_btn_row.setContentsMargins(0, 0, 0, 0)
        cleanup_btn_row.setSpacing(8)
        self.cleanup_prompt_edit_btn = neutral_button(Button("Open editor…"))
        fit_compact_button(self.cleanup_prompt_edit_btn, 120)
        self.cleanup_prompt_edit_btn.clicked.connect(self._open_cleanup_prompt_editor)
        cleanup_btn_row.addWidget(self.cleanup_prompt_edit_btn)
        self.cleanup_prompt_reset_btn = neutral_button(Button("Reset to default"))
        fit_compact_button(self.cleanup_prompt_reset_btn, 140)
        self.cleanup_prompt_reset_btn.clicked.connect(self._reset_cleanup_prompt)
        cleanup_btn_row.addWidget(self.cleanup_prompt_reset_btn)
        cleanup_btn_row.addStretch()
        self.cleanup_prompt_tile.add_body_layout(cleanup_btn_row)
        self._tile_group(layout, "", [self.cleanup_prompt_tile])

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
        self.cleanup_rules_gate_tile = InfoTile(
            "AI cleanup is off",
            "Learned rules only apply when cleanup runs. Teaching and "
            "editing stay locked until you turn on Clean up transcripts "
            "with AI.",
            design_icon("info-warning.svg"),
        )
        self.cleanup_rules_gate_tile.setProperty("kind", "notice")
        self.open_cleanup_btn = QPushButton("Open Cleanup")
        self.open_cleanup_btn.setObjectName("cleanupRulesOpenCleanupLink")
        self.open_cleanup_btn.setFlat(True)
        self.open_cleanup_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_cleanup_btn.setToolTip(
            "Open Cleanup to turn on Clean up transcripts with AI"
        )
        self.open_cleanup_btn.clicked.connect(self.focus_cleanup_toggle)
        self.cleanup_rules_gate_tile.add_trailing(self.open_cleanup_btn)
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

        self.cleanup_rules_empty = QLabel(
            "No rules yet\n\nAdd your first instruction above to start "
            "building a personal cleanup profile."
        )
        self.cleanup_rules_empty.setObjectName("cleanupRulesEmpty")
        self.cleanup_rules_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cleanup_rules_empty.setWordWrap(True)
        self.cleanup_rules_empty.setMinimumHeight(88)
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
        # Who runs AI insights, then the packaged SDK's chat model.
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
        self.open_typesafe_key_btn = QPushButton("Add a key")
        self.open_typesafe_key_btn.setObjectName("typesafeKeyNoticeLink")
        self.open_typesafe_key_btn.setFlat(True)
        self.open_typesafe_key_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_typesafe_key_btn.setToolTip(
            "Open API keys with the TypeSafe credential selected"
        )
        self.open_typesafe_key_btn.clicked.connect(
            lambda: self.focus_api_keys(self._selected_typesafe_route().credential_env)
        )
        # The quickest fix when the other route already has a key: one click
        # moves Fast judgments onto it. Shown only in that case.
        self.typesafe_switch_route_btn = compact_primary_button(Button("Use OpenRouter"))
        self.typesafe_switch_route_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.typesafe_switch_route_btn.clicked.connect(self._switch_typesafe_route)
        self.typesafe_switch_route_btn.hide()
        self.typesafe_key_notice.add_trailing(self.typesafe_switch_route_btn)
        self.typesafe_key_notice.add_trailing(self.open_typesafe_key_btn)
        layout.addWidget(self.typesafe_key_notice)

        # Same model either way; the choice is whose key pays for it and
        # whether OpenRouter sits in between.
        self.typesafe_route_tile = InfoTile(
            "Jev connection",
            "",
            design_icon("world-blue.svg"),
        )
        self.typesafe_route_bar = SegmentedBar(
            [(route.name, "") for route in JEV_ROUTES.values()]
        )
        self.typesafe_route_bar.setAccessibleName("Connect Jev through")
        self.typesafe_route_bar.activated.connect(self._on_typesafe_route_activated)
        self.typesafe_route_tile.add_body(self.typesafe_route_bar)
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
            "TypeSafe fast judgments (Experimental)",
            "Answers narrow yes/no questions about a minute of transcript in "
            "about 0.2 s. It never writes text.",
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
             "Sends generated claims and their cited excerpts to TypeSafe/Jev. Flags weak evidence without changing the claim."),
            ("semantic_search", SettingsKey.TYPESAFE_SEMANTIC_SEARCH_ENABLED, "Semantic history search",
             "Sends your search and shortlisted excerpts from past AI-insight meetings to TypeSafe/Jev to rank by meaning. Keyword search stays available."),
            ("question_radar", SettingsKey.TYPESAFE_QUESTION_RADAR_ENABLED, "Open questions radar",
             "Sends a minute of transcript and tracked questions to TypeSafe/Jev to find unanswered questions and suggest answers."),
            ("highlights", SettingsKey.TYPESAFE_HIGHLIGHTS_ENABLED, "Live highlight pulses",
             "Sends a minute of transcript to TypeSafe/Jev to mark decisions, disagreements, dated commitments, numbers and takeaways. Click a pulse to play that moment."),
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
            "For new meetings. Sends relevant excerpts, speaker names and insights to "
            "TypeSafe to find ambiguities, and asks the three highest-priority questions "
            "first. No audio is sent. Needs AI insights and Fast judgments with a "
            "TypeSafe or OpenRouter key.",
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
        self.api_key_save_button = compact_primary_button(PrimaryButton("Save key"))
        self.api_key_save_button.setObjectName("apiKeySaveButton")
        fit_compact_button(self.api_key_save_button, 120)
        self.api_key_save_button.clicked.connect(self._save_api_key)
        self.api_key_test_button = neutral_button(Button("Test"))
        self.api_key_test_button.setObjectName("apiKeyTestButton")
        fit_compact_button(self.api_key_test_button, 90)
        self.api_key_test_button.clicked.connect(self._test_api_key)
        self.api_key_remove_button = DangerButton("Remove saved key")
        self.api_key_remove_button.setObjectName("apiKeyRemoveButton")
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
            return (
                f"{status.backend_name} isn't available on this computer "
                f"({status.reason}), so keys can't be saved from here. Set the "
                "variable in your environment or a .env file instead; "
                "OpenWhisper never falls back to an unprotected file."
            )
        return (
            f"Encrypted by {status.backend_name} for your account. "
            "Saved keys override environment variables and .env, and never "
            "enter settings or logs."
        )

    @staticmethod
    def _api_key_entries(settings: dict) -> list:
        """One row per distinct variable; profiles sharing a variable share it."""
        names: Dict[str, list] = {}
        for profile in list_profiles(settings):
            if profile.api_key_env:
                names.setdefault(profile.api_key_env, []).append(profile.name)
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
                "Used for cloud transcription (Whisper, GPT-4o), OpenAI "
                "transcript cleanup and meeting intelligence, and cloud "
                "speaker identification."
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

        def worker():
            if profile is None:
                ok, detail = typesafe_verify_key(key)
            else:
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
                "the desired key."
            )
        else:
            instruction_text = (
                "Click a shortcut, then press the desired key combination. "
                "Numpad keys are distinct from the matching regular keys."
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
        profile_link = neutral_button(Button("Profile recording shortcuts…"))
        profile_link.clicked.connect(lambda: self.select_destination(CLEANUP_PROFILES))
        fit_compact_button(profile_link, 0)
        actions.addWidget(profile_link)
        actions.addStretch()
        reset_button = neutral_button(Button("Reset to defaults"))
        reset_button.setObjectName("hotkeyResetButton")
        fit_compact_button(reset_button, 150)
        reset_button.clicked.connect(self._confirm_reset_hotkeys)
        actions.addWidget(reset_button)
        layout.addSpacing(4)
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
        self.record_mode_combo.setMinimumWidth(round(260 * current_ui_font_scale()))
        self.record_mode_combo.currentIndexChanged.connect(
            self._on_recording_trigger_mode_changed
        )
        card = QFrame()
        card.setObjectName("hotkeyModeCard")
        row = QHBoxLayout(card)
        row.setContentsMargins(14, 8, 12, 8)
        row.setSpacing(16)
        copy = QVBoxLayout()
        copy.setSpacing(2)
        name = QLabel("How the record hotkey activates")
        name.setObjectName("hotkeyShortcutName")
        copy.addWidget(name)
        detail = WrappedLabel(
            "Toggle starts and stops with each press. Push and hold records while "
            "the key is down."
        )
        detail.setObjectName("hotkeyShortcutDescription")
        copy.addWidget(detail)
        row.addLayout(copy, stretch=1)
        row.addWidget(self.record_mode_combo, alignment=Qt.AlignmentFlag.AlignVCenter)
        return card

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

        if self._native_wayland:
            from ui_qt.widgets.profile_hotkey_input import ProfileHotkeyInput

            field = ProfileHotkeyInput()
            field.setAccessibleName(f"{title} shortcut")
            field.setToolTip("Click, then press a shortcut. Escape cancels.")
            field.capture_changed.connect(self._on_profile_capture)
            field.captured.connect(lambda hotkey, key=key: self._on_local_hotkey_captured(key, hotkey))
        else:
            field = HotkeyCaptureInput()
            field.capture_requested.connect(
                lambda key=key, field=field: self._start_hotkey_capture(key, field)
            )
        field.setProperty("optional", optional)
        field.setMinimumWidth(190)
        field.setMaximumWidth(220)
        self.hotkey_inputs[key] = field
        row.addWidget(field, alignment=Qt.AlignmentFlag.AlignVCenter)

        if optional:
            clear_button = Button("Clear")
            clear_button.setObjectName("hotkeyClearButton")
            fit_compact_button(clear_button, 68)
            clear_button.clicked.connect(self._clear_meeting_hotkey)
            self.clear_meeting_hotkey_button = clear_button
            row.addWidget(clear_button, alignment=Qt.AlignmentFlag.AlignVCenter)
        return card

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

    @property
    def settings_view(self) -> str:
        return self._settings_view

    def set_settings_view(self, view: str, *, persist: bool = True) -> None:
        """Switch the presentation while retaining both views' navigation."""
        if view not in SettingsView.ALL or self._backup_busy:
            self.view_buttons[self._settings_view].setChecked(True)
            return
        if persist and view != self._settings_view:
            if not self._persist(SettingsKey.SETTINGS_VIEW, view):
                self.view_buttons[self._settings_view].setChecked(True)
                return
        for page in self._basic_pages.values():
            page.cancel_capture()
        self._cancel_hotkey_capture()
        previous = self._settings_view
        if previous == SettingsView.ADVANCED and view == SettingsView.BASIC and self.isVisible():
            self._advanced_rail_width = self.settings_splitter.sizes()[0]
        self._settings_view = view
        self.view_buttons[view].setChecked(True)
        basic = view == SettingsView.BASIC
        self.rail_pane.setVisible(not basic)
        self.basic_tabs.setVisible(basic)
        self.body_layout.setSpacing(8 if basic else 14)
        self.setMinimumSize(self.BASIC_MINIMUM_SIZE if basic else self.MINIMUM_SIZE)
        if previous == SettingsView.BASIC and not basic:
            self.settings_splitter.setSizes([
                self._advanced_rail_width,
                max(1, self.width() - self._advanced_rail_width),
            ])
        if self.isVisible():
            self._fit_to_screen()
        if basic:
            self._on_basic_tab_changed(self.basic_tabs.currentIndex())
        else:
            self._on_destination_changed(self.rail.current_key() or OVERVIEW)

    def show_home(self) -> None:
        """Open ordinary Settings in the user's last selected view."""
        self.set_settings_view(resolve_settings_view(self._settings_snapshot()), persist=False)
        if self._settings_view == SettingsView.ADVANCED:
            self.select_destination(OVERVIEW)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        scale = current_ui_font_scale()
        self.settings_brand.setVisible(self.width() >= round(560 * scale))
        self.settings_label.setVisible(self.width() >= round(700 * scale))

    def _on_basic_tab_changed(self, index: int) -> None:
        if self._settings_view != SettingsView.BASIC or index < 0:
            return
        for page in self._basic_pages.values():
            page.cancel_capture()
        key = self._basic_destinations[index]
        self.ensure_page(key)
        self._basic_pages[key].refresh()
        self.stack.setCurrentWidget(self._page_scrolls[key])
        title, subtitle = self._headings[key]
        self.page_title.setText(title)
        self.page_subtitle.setText(subtitle)

    def select_destination(self, key: str) -> None:
        """Show one destination by stable key or legacy alias."""
        key = resolve_destination(key)
        if self._backup_busy and key != BACKUP:
            return
        if key in self._pages:
            if key in self._basic_destinations:
                self.basic_tabs.setCurrentIndex(self._basic_destinations.index(key))
                self.set_settings_view(SettingsView.BASIC)
                return
            if self._settings_view == SettingsView.BASIC:
                self.set_settings_view(SettingsView.ADVANCED)
            self.ensure_page(key)
            self.rail.select(key)

    def focus_hf_policy(self) -> None:
        """Open Downloads with the Hugging Face policy control focused."""
        self.select_destination(DOWNLOADS)
        self.hf_policy_combo.setFocus()

    def focus_cleanup_toggle(self) -> None:
        """Open AI cleanup with the AI cleanup toggle focused."""
        self.select_destination(CLEANUP)
        self.transcript_cleanup_check.setFocus()

    def focus_cleanup_model(self) -> None:
        """Open AI cleanup scrolled to its chat model."""
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
        if self._settings_view == SettingsView.BASIC:
            self.set_settings_view(SettingsView.ADVANCED)
            return
        if key != HOTKEYS:
            self._cancel_hotkey_capture()
        page = self._pages.get(key)
        if page is None:
            return
        self.ensure_page(key)
        self.stack.setCurrentWidget(self._page_scrolls.get(key, page))
        self.rail.scrollToItem(self.rail.currentItem())
        title, subtitle = self._headings[key]
        self.page_title.setText(title)
        self.page_subtitle.setText(subtitle)
        if self._pages_ready:
            self.models.on_destination_shown(key)
            if key == OVERVIEW:
                self._refresh_overview()
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
        self.basic_tabs.setEnabled(not busy)
        for button in self.view_buttons.values():
            button.setEnabled(not busy)
        for shortcut in self._search_shortcuts:
            shortcut.setEnabled(not busy)
        if busy:
            self.search_palette.close_palette()

    def _search_index(self) -> list:
        pages = []
        for key in self.rail.keys():
            if key not in self._pages:
                continue
            title, subtitle = self._headings[key]
            name = self.rail.name(key)
            group = self._rail_groups.get(key, "")
            crumb = f"{group} › {name}" if group else name
            pages.append(PageSource(
                key, crumb, title, subtitle, self._pages[key],
                self._rail_icons.get(key, "box-blue.svg"),
            ))
        entries = build_index(pages, self.downloads, keyword_entries(_SEARCH_ALIASES) + self._agent_search_entries())
        for key in self.rail.keys():
            if key in self._built_pages:
                continue
            title, _subtitle = self._headings[key]
            group = self._rail_groups.get(key, "")
            crumb = f"{group} › {self.rail.name(key)}" if group else self.rail.name(key)
            icon = self._rail_icons.get(key, "box-blue.svg")
            for target_name, label, description in PAGE_SEARCH_FIELDS.get(key, ()):
                entries.append(SearchEntry(SETTING, label, crumb, key, target_name=target_name, keywords=description, icon=icon))
            for text in PAGE_HELP_TEXT.get(key, ()):
                entries.append(SearchEntry(HELP, title, text, key, icon="info-blue.svg"))
        return entries

    def _agent_search_entries(self) -> list:
        """The meeting agent picker, which is tiles rather than a setting tile."""
        group = self._rail_groups.get(MEETING_INTELLIGENCE, "")
        name = self.rail.name(MEETING_INTELLIGENCE)
        return [SearchEntry(
            SETTING,
            "Who runs AI insights",
            f"{group} › {name}" if group else name,
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
                "settingsTile", "textModelFootnoteCard"
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

    def closeEvent(self, event):
        self.search_palette.close_palette()
        for page in self._basic_pages.values():
            page.cancel_capture()
        if CLEANUP_PROFILES in self._built_pages:
            self.cleanup_profiles_panel.hotkey_input.cancel_capture()
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
        self.rail.set_value(OVERVIEW, "What is running now")
        settings = self._settings_snapshot()
        profile_count = len(load_cleanup_profiles(settings))
        self.rail.set_value(CLEANUP_PROFILES, f"{profile_count} profile{'' if profile_count == 1 else 's'}")
        general = ("Auto-paste on" if setting_value(SettingsKey.AUTO_PASTE, settings)
                   else "Clipboard" if setting_value(SettingsKey.COPY_CLIPBOARD, settings) else "Manual")
        self.rail.set_value(GENERAL, general)
        device_combo = self.__dict__.get("audio_device_combo")
        self.rail.set_value(RECORDING, device_combo.currentText() if device_combo is not None else "System Default")
        model = resolve_transcript_cleanup_model(settings)
        cleanup = setting_value(SettingsKey.TRANSCRIPT_CLEANUP_ENABLED, settings)
        self.rail.set_value(CLEANUP, (f"On · {model}" if model else "On") if cleanup else "Off")
        rule_list = self.__dict__.get("cleanup_rules_list")
        rule_count = rule_list.count() if rule_list is not None else len(resolve_transcript_cleanup_rules(settings))
        self.rail.set_value(CLEANUP_RULES, "No rules" if rule_count == 0 else f"{rule_count} rule{'' if rule_count == 1 else 's'}")
        after = ("Report" if resolve_meeting_end_report(settings) else "Polish" if resolve_meeting_end_polish(settings)
                 else "Re-decode" if resolve_meeting_end_redecode(settings) else "Off")
        self.rail.set_value(MEETING_AFTER, after)
        self._render_typesafe_key_state()
        self.rail.set_value(MEETING_FAST, self._meeting_fast_rail_value())
        bind = resolve_meeting_server_bind(settings)
        port = resolve_meeting_server_port(settings)
        self.rail.set_value(MEETING_DASHBOARD, f"LAN · {'auto' if port == 0 else port}" if bind == MeetingServerBind.LAN else "Localhost")
        self.rail.set_value(API_KEYS, self._api_key_rail_value())
        self.rail.set_value(HOTKEYS, self._hotkey_rail_value())
        self.rail.set_value(DOWNLOADS, self.downloads.rail_value())
        if BACKUP in self._built_pages:
            frequency = self.backup_page.frequency_combo.currentData()
            self.rail.set_value(BACKUP, "Off" if frequency == "off" else str(frequency).title())
        else:
            self.rail.set_value(BACKUP, "Local backups")
        self.rail.set_value(ADVANCED, "Developer mode on" if resolve_developer_mode(settings) else "Developer mode off")
        for page in self._basic_pages.values():
            page.refresh()
        if self.rail.current_key() == OVERVIEW:
            self._refresh_overview()

    def _refresh_overview(self) -> None:
        """Rebuild the Overview from the values every destination reports."""
        if not self._pages_ready:
            return
        models = self.models
        settings = self._settings_snapshot()
        cleanup_on = setting_value(SettingsKey.TRANSCRIPT_CLEANUP_ENABLED, settings)
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
        (cloud_items if meeting_remote else local_items).append(
            "Meeting intelligence (transcript text, when enabled)"
            if meeting_remote else "Meeting intelligence"
        )
        local_items.extend(["Learned rules", "Recordings"])

        hotkeys = self.current_hotkeys or {}
        record = format_hotkey_display(hotkeys.get("record_toggle", "")) or "Not set"
        cancel = format_hotkey_display(hotkeys.get("cancel", "")) or "not set"
        minimize = format_hotkey_display(hotkeys.get("minimize_tray", "")) or "not set"
        profile_count = len(load_cleanup_profiles(settings))

        storage = self.downloads.storage_summary()
        try:
            components = [
                (info.display_name, info.is_usable)
                for info in component_coordinator.list_components()
            ]
        except Exception:
            components = []
        policy = settings.get(SettingsKey.HF_ACCESS_POLICY, HuggingFaceAccessPolicy.ASK)
        self.overview.update_summary(OverviewSummary(
            voice=(models.voice_summary(), models.voice_detail()),
            cleanup=("On" if cleanup_on else "Off", models.text_summary()),
            cleanup_on=cleanup_on,
            profiles=(
                f"{profile_count} profile{'' if profile_count == 1 else 's'}",
                "A ticket, an email, or your own format",
            ),
            meeting_voice=(models.meeting_model_label(), models.meeting_voice_detail()),
            intelligence=models.meeting_intelligence_overview(),
            hotkeys=(f"Record {record}", f"Cancel {cancel} · Tray {minimize}"),
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
        self._update_streaming_font_ui()
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

    def _persist_cleanup_prompt(self) -> None:
        prompt_text = self.cleanup_prompt_edit.toPlainText().strip()
        stored = prompt_text or config.TRANSCRIPT_CLEANUP_PROMPT
        if stored == self._saved_cleanup_prompt:
            return
        if self._persist(SettingsKey.TRANSCRIPT_CLEANUP_PROMPT, stored):
            self._saved_cleanup_prompt = stored

    def _on_meeting_review_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        if checked:
            route = self._selected_typesafe_route()
            destination = (
                "TypeSafe" if route is JEV_TYPESAFE_ROUTE
                else f"TypeSafe through {route.name}"
            )
            reply = QMessageBox.question(
                self, "Enable experimental TypeSafe insight review?",
                "For future meetings with AI insights on, send relevant transcript "
                f"excerpts, speaker names, and generated insights to {destination} after the meeting? "
                "This is a separate service from your meeting LLM. No audio is sent. "
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
                      "typesafe-text-v1" if checked else "")
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
        """The Jev route Settings name now: TypeSafe unless OpenRouter was chosen."""
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
        return JEV_OPENROUTER_ROUTE if route is JEV_TYPESAFE_ROUTE else JEV_TYPESAFE_ROUTE

    @staticmethod
    def _typesafe_destination_copy(route) -> str:
        """Who receives excerpts over ``route``; OpenRouter is a hop, not the model."""
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
        route = list(JEV_ROUTES.values())[index]
        if route is self._selected_typesafe_route():
            return
        self._persist(SettingsKey.TYPESAFE_PROVIDER, route.id)
        # A result for the other route would now describe the wrong thing.
        self.typesafe_route_status.hide()
        self._update_typesafe_feature_tiles()

    def _switch_typesafe_route(self) -> None:
        other = self._other_typesafe_route(self._selected_typesafe_route())
        self.typesafe_route_bar.setCurrentIndex(list(JEV_ROUTES.values()).index(other))
        self._on_typesafe_route_activated(self.typesafe_route_bar.currentIndex())

    def _render_typesafe_route(self) -> None:
        """Show the chosen route, each route's key state, and where excerpts go."""
        # The destination line is the page's last widget, so a refresh during
        # construction waits for the whole page.
        if "typesafe_destination_label" not in self.__dict__:
            return
        route = self._selected_typesafe_route()
        routes = list(JEV_ROUTES.values())
        self.typesafe_route_bar.setCurrentIndex(routes.index(route))
        for index, candidate in enumerate(routes):
            lead = "Direct" if candidate is JEV_TYPESAFE_ROUTE else "Same model"
            state = "key set" if self._typesafe_key_present(candidate) else "no key"
            self.typesafe_route_bar.set_detail(index, f"{lead} · {state}")
        self.typesafe_route_tile.set_description(
            f"Answers come straight from TypeSafe ({route.host}) and are "
            "billed to your TypeSafe account."
            if route is JEV_TYPESAFE_ROUTE else
            f"The same Jev model, reached through {route.name} ({route.host}) "
            f"and billed to your {route.name} credits. No TypeSafe account needed."
        )
        self.typesafe_destination_label.setText(self._typesafe_destination_copy(route))
        self.typesafe_route_test_btn.setEnabled(not self._typesafe_route_testing)

    def _test_typesafe_route(self) -> None:
        if self._typesafe_route_testing:
            return
        route = self._selected_typesafe_route()
        key = resolve_credential(route.credential_env)
        if not key:
            self._show_typesafe_route_result(
                False, f"No {route.key_label} to test. Add one under API keys → {route.name}."
            )
            return
        self._typesafe_route_testing = True
        self.typesafe_route_test_btn.setEnabled(False)
        self._show_typesafe_route_result(None, f"Testing {route.name}…")

        def worker():
            ok, detail = typesafe_verify_key(key, route=route)
            self._typesafe_route_verified.emit(route.id, ok, detail)

        threading.Thread(target=worker, daemon=True, name="jev-route-verify").start()

    def _on_typesafe_route_verified(self, route_id: str, ok: bool, detail: str) -> None:
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
        other_present = not present and self._typesafe_key_present(other)
        master_on = self.typesafe_enabled_check.isChecked()
        self.typesafe_key_notice.setVisible(not present)
        self.typesafe_switch_route_btn.setVisible(other_present)
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
        enabled = control.isChecked() if control is not None else setting_value(SettingsKey.TRANSCRIPT_CLEANUP_ENABLED, self._settings_snapshot())
        for name in ("cleanup_prompt_tile", "cleanup_rules_composer_tile", "cleanup_rules_library_tile"):
            widget = self.__dict__.get(name)
            if widget is not None:
                widget.setEnabled(enabled)
        gate = self.__dict__.get("cleanup_rules_gate_tile")
        if gate is not None:
            gate.setVisible(not enabled)
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
            return f"Sent to OpenAI · {summary.split(' · ', 1)[-1]}"
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

        device_id = self._settings_snapshot().get(SettingsKey.AUDIO_INPUT_DEVICE)
        if self._rule_recorder is None or self._rule_recorder_device != device_id:
            if self._rule_recorder is not None:
                self._rule_recorder.cleanup()
            self._rule_recorder = AudioRecorder(
                device_id=device_id, output_file=self._rule_dictation_path
            )
            self._rule_recorder.set_audio_level_callback(
                self._emit_rule_dictation_level
            )
            self._rule_recorder_device = device_id

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
            self.rail.value(RECORDING) or "Default microphone"
        )
        self._rule_dictation_timer.start()
        self._update_cleanup_rule_controls()

    def _emit_rule_dictation_level(self, level: float) -> None:
        """Hand a level from the audio thread to the composer's trace."""
        try:
            self._rule_dictation_level.emit(level)
        except RuntimeError:
            pass  # The window is gone; the stream closes with its recorder.

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

        def worker():
            text = ""
            error = ""
            try:
                recorder.stop_recording()
                if not recorder.wait_for_stop_completion():
                    error = "Recording did not finish stopping; audio kept for recovery."
                elif getattr(recorder, "last_capture_error", None):
                    error = str(recorder.last_capture_error)
                elif not recorder.has_recording_data():
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
            self.meeting_review_check.setChecked(settings.get(SettingsKey.MEETING_INSIGHT_REVIEW) is True and settings.get(SettingsKey.MEETING_INSIGHT_REVIEW_CONSENT) == "typesafe-text-v1")
            self.meeting_end_report_check.setChecked(resolve_meeting_end_report(settings))
            self.meeting_report_ribbon_check.setChecked(resolve_meeting_report_ribbon(settings))
            self.meeting_report_brief_check.setChecked(resolve_meeting_report_brief(settings))
            self.meeting_report_signal_check.setChecked(resolve_meeting_report_signal(settings))
            self._guard_report_views()
            self._update_report_views_enabled()
        if self._page_is_loading(MEETING_DASHBOARD):
            self.meeting_bind_combo.setCurrentIndex(max(0, self.meeting_bind_combo.findData(resolve_meeting_server_bind(settings))))
            self._update_meeting_bind_ui()
        if self._page_is_loading(MEETING_FAST):
            self.typesafe_enabled_check.setChecked(resolve_typesafe_enabled(settings))
            self._update_typesafe_feature_tiles()
        if self._page_is_loading(MEETING_INTELLIGENCE):
            self.meeting_context_folder_path.setText(resolve_meeting_context_folder_path(settings))

    def _populate_audio_devices(self) -> None:
        self.audio_device_combo.addItem("System Default", None)
        saved = self._settings_snapshot().get(SettingsKey.AUDIO_INPUT_DEVICE)
        if saved is not None:
            self.audio_device_combo.addItem(f"Input device {saved}", saved)
            self.audio_device_combo.setCurrentIndex(1)
        self._audio_device_generation += 1
        generation = self._audio_device_generation
        delivery = HistoryDelivery()
        delivery.loaded.connect(self._apply_audio_devices)
        def load():
            try:
                devices = AudioRecorder.get_input_devices()
            except Exception:
                logger.warning("Couldn't discover audio inputs", exc_info=True)
                devices = []
            delivery.loaded.emit(generation, "", devices, "")
        threading.Thread(target=load, name="settings-audio-devices", daemon=True).start()

    def _apply_audio_devices(self, generation, _query, devices, _error):
        if generation != self._audio_device_generation:
            return
        selected = self.audio_device_combo.currentData()
        blocker = self.audio_device_combo.blockSignals(True)
        self.audio_device_combo.clear()
        self.audio_device_combo.addItem("System Default", None)
        for device_id, device_name in devices:
            self.audio_device_combo.addItem(device_name, device_id)
        if selected is not None and self.audio_device_combo.findData(selected) < 0:
            self.audio_device_combo.addItem(f"Input device {selected}", selected)
        self.audio_device_combo.setCurrentIndex(max(0, self.audio_device_combo.findData(selected)))
        self.audio_device_combo.blockSignals(blocker)
        self._refresh_rail_values()

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

    def _on_local_hotkey_captured(self, key: str, hotkey: str) -> None:
        updated = self.current_hotkeys.copy()
        updated[key] = hotkey
        self._apply_hotkey_settings(updated, "Shortcut updated.")

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
            settings = settings_manager.load_all_settings()
            for profile in load_cleanup_profiles(settings):
                conflict = profile_hotkey_conflict(
                    profile.hotkey, settings, exclude_id=profile.id,
                    standard_hotkeys=updated,
                )
                if conflict:
                    raise ValueError(f"{profile.name}'s shortcut is already used by {conflict}. Choose another.")
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
        for page in self._basic_pages.values():
            page.refresh_shortcut()
        for key, input_field in self.hotkey_inputs.items():
            if self._native_wayland:
                input_field.set_hotkey(self.current_hotkeys.get(key, ""))
                continue
            input_field.setText(
                format_hotkey_display(self.current_hotkeys.get(key, ""))
            )

    def _load_settings(self) -> None:
        settings = self._settings_snapshot()
        if not self.current_hotkeys or self._initializing_page in (None, HOTKEYS):
            self._load_hotkey_settings()
        self._bindings.load(settings, start=self._initial_binding_start)
        if self._page_is_loading(CLEANUP):
            self.transcript_cleanup_check.setChecked(setting_value(SettingsKey.TRANSCRIPT_CLEANUP_ENABLED, settings))
            prompt = resolve_transcript_cleanup_prompt(settings)
            self.cleanup_prompt_edit.setPlainText(prompt)
            self._saved_cleanup_prompt = prompt
        if self._page_is_loading(CLEANUP_RULES):
            self.cleanup_rules_list.clear()
            self.cleanup_rules_list.addItems(resolve_transcript_cleanup_rules(settings))
        self._update_cleanup_prompt_ui()
        if self._page_is_loading(GENERAL):
            self.update_check_check.setChecked(resolve_update_check_enabled(settings))
            self.update_notify_tile.setEnabled(self.update_check_check.isChecked())
            self.ui_font_scale_combo.setCurrentIndex(max(0, self.ui_font_scale_combo.findData(resolve_ui_font_scale(settings))))
            self.ui_theme_combo.setCurrentIndex(max(0, self.ui_theme_combo.findData(resolve_ui_theme(settings))))
        if self._page_is_loading(RECORDING):
            self._load_retention_settings(settings)
            self.streaming_enabled_check.setChecked(setting_value(SettingsKey.STREAMING_ENABLED, settings))
            self.streaming_font_size_spinbox.setValue(resolve_streaming_overlay_font_size(settings))
            self._update_streaming_font_ui()
            saved_device_id = settings.get(SettingsKey.AUDIO_INPUT_DEVICE)
            index = self.audio_device_combo.findData(saved_device_id)
            if index >= 0:
                self.audio_device_combo.setCurrentIndex(index)
        self._load_meeting_settings(settings)
        if self._page_is_loading(API_KEYS):
            self._load_api_key_settings(settings)
        if self._page_is_loading(ADVANCED):
            self.developer_mode_check.setChecked(resolve_developer_mode(settings))
        if self._page_is_loading(DOWNLOADS):
            policy = settings_manager.load_hf_access_policy()
            self.hf_policy_combo.setCurrentIndex(max(0, self.hf_policy_combo.findData(policy)))
        if self._page_is_loading(HOTKEYS):
            trigger_mode = resolve_recording_trigger_mode(settings)
            self.record_mode_combo.setCurrentIndex(max(0, self.record_mode_combo.findData(trigger_mode)))
            self._update_record_row_description(trigger_mode)

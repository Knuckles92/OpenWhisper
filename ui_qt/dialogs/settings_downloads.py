"""Settings → Models & storage → Downloads: engine-grouped speech models,
optional components, and each model's technical profile.

This is the one page in Settings whose list scrolls on its own: the header,
engine rail, runtime banner, toolbar, and footer stay put while the catalog
moves. Settings is non-modal, so a multi-gigabyte download never locks the
user out of recording.

An Engine Rail on the left separates choosing a speech engine from managing
its models and runtime. The technical profile inspector slides over the
catalog's right edge on demand when a model is selected, leaving the full
catalog width clear when nothing is selected.
"""
import itertools
import logging
import sys
import threading
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from PyQt6.QtCore import QObject, QPoint, QRect, QSize, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from config import config, parakeet_mlx_supported
from services.components import ComponentState, component_coordinator
from services.hf_access import (
    MODEL_DOWNLOAD_SIZE_MB,
    CachedModelInfo,
    custom_model_cache_info,
    format_download_size,
    format_size_bytes,
    get_hf_cache_dir,
    invalidate_cached_models_snapshot,
    peek_cached_models,
    resolve_model_repo,
    scan_cached_models,
)
from services.model_catalog import ModelDetails, custom_model_details, get_model_details
from services.whisper_sources import custom_model_label, custom_models, is_custom_model, parse_source
from services.local_asr.discovery import (
    discover_existing_models,
    dismiss_discoveries,
    unseen_discoveries,
)
from ui_qt.dialogs.custom_whisper_dialog import add_custom_models
from ui_qt.dialogs.custom_speech_dialog import add_custom_speech_model
from ui_qt.dialogs.import_models_dialog import ImportModelsDialog
from services.settings import (
    SettingsKey,
    is_hf_hub_offline_env_set,
    resolve_meeting_whisper_model,
    settings_manager,
)
from ui_qt.dialogs.component_details_dialog import ComponentDetailsDialog
from ui_qt.utils.app_icon import app_icon
from ui_qt.utils.restyle import repolish, set_style_property
from ui_qt.widgets import Button, ElidingComboBox, ElidingLabel, PrimaryButton
from ui_qt.widgets.button_row import ButtonRow
from ui_qt.widgets.buttons import fit_compact_button
from ui_qt.widgets.component_row_widget import ComponentRowWidget
from ui_qt.widgets.empty_state import EmptyState
from ui_qt.widgets.model_row_widget import ModelRowWidget
from ui_qt.widgets.segmented_bar import SegmentedBar

logger = logging.getLogger(__name__)
from ui_qt.widgets.wrapped_label import WrappedLabel
from ui_qt.widgets.engine_field import set_device_choices
from ui_qt.utils.error_messages import format_action_error, friendly_error_message


@dataclass(frozen=True)
class _EngineMeta:
    """Visual identity and brief guidance for one speech engine in the rail."""

    short: str
    rgb: str
    tagline: str
    device: str


_ENGINE_META: Dict[str, _EngineMeta] = {
    "parakeet": _EngineMeta(
        short="Parakeet",
        rgb="56, 189, 248",
        tagline="Fast dictation, live streaming & 25-language models",
        device="GPU / CPU",
    ),
    "parakeet_unified": _EngineMeta(
        short="Parakeet Unified",
        rgb="56, 189, 248",
        tagline="English dictation with live streaming",
        device="GPU (Vulkan) / CPU",
    ),
    "whisper": _EngineMeta(
        short="Whisper",
        rgb="167, 139, 250",
        tagline="99 languages & translation (CTranslate2)",
        device="NVIDIA CUDA / CPU",
    ),
    "local_whisper": _EngineMeta(
        short="Whisper",
        rgb="167, 139, 250",
        tagline="99 languages & translation (CTranslate2)",
        device="NVIDIA CUDA / CPU",
    ),
    "qwen": _EngineMeta(
        short="Qwen3-ASR",
        rgb="251, 191, 36",
        tagline="30 languages, 22 Chinese dialects & compatible GGUFs",
        device="GPU / CPU",
    ),
    "qwen_asr": _EngineMeta(
        short="Qwen3-ASR",
        rgb="251, 191, 36",
        tagline="30 languages, 22 Chinese dialects & compatible GGUFs",
        device="GPU / CPU",
    ),
    "nemotron": _EngineMeta(
        short="Nemotron",
        rgb="244, 114, 182",
        tagline="Low-latency live streaming & meetings",
        device="GPU / CPU",
    ),
    "moonshine": _EngineMeta(
        short="Moonshine",
        rgb="251, 146, 60",
        tagline="Ultra-light CPU streaming & live meetings",
        device="CPU",
    ),
    "parakeet_mlx": _EngineMeta(
        short="Parakeet MLX",
        rgb="45, 212, 191",
        tagline="Apple Silicon GPU dictation & meetings",
        device="Apple GPU / CPU",
    ),
}

_FALLBACK_ENGINE_META = _EngineMeta(
    short="Speech",
    rgb="148, 163, 184",
    tagline="Local speech recognition engine",
    device="CPU",
)


def _engine_meta(backend_or_family: str) -> _EngineMeta:
    return _ENGINE_META.get(backend_or_family, _FALLBACK_ENGINE_META)


def _family_order() -> Tuple[str, ...]:
    """Stable rail ordering of model families supported on this computer."""
    from services.local_asr.catalog import supported_families

    return supported_families()


def _engine_order() -> Tuple[str, ...]:
    """Stable ordering of supported backends, aligned with the family rail."""
    from services.local_asr.catalog import backend_supported_here, backends_in_family

    ordered = []
    for family in _family_order():
        for backend in backends_in_family(family):
            if backend_supported_here(backend):
                ordered.append(backend)
    return tuple(ordered)


def _rail_key_for(key: str) -> str:
    """Map a rail key or backend ID onto the family rail key."""
    if key in ("all", "installed", "components"):
        return key
    from services.local_asr.catalog import family_of

    return family_of(key)


class _EngineRailItem(QPushButton):
    """One selectable row inside the Downloads left Engine Rail."""

    def __init__(
        self,
        key: str,
        label: str,
        *,
        dot_rgb: str = "",
        tooltip: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.key = key
        self.setObjectName("downloadsRailItem")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(25)
        self.setToolTip(tooltip or label)
        self.setAccessibleName(label)

        row = QHBoxLayout(self)
        row.setContentsMargins(7, 0, 7, 0)
        row.setSpacing(6)

        if dot_rgb:
            dot = QLabel()
            dot.setFixedSize(8, 8)
            dot.setStyleSheet(
                f"background-color: rgb({dot_rgb}); border: none; border-radius: 4px;"
            )
            dot.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            row.addWidget(dot, 0, Qt.AlignmentFlag.AlignVCenter)

        self.name_label = ElidingLabel(label)
        self.name_label.setObjectName("downloadsRailItemLabel")
        self.name_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(self.name_label, stretch=1)

        self.count_label = QLabel("")
        self.count_label.setObjectName("downloadsRailItemCount")
        self.count_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.count_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(self.count_label, 0, Qt.AlignmentFlag.AlignVCenter)

    def set_selected(self, selected: bool) -> None:
        set_style_property(self, "selected", selected)
        set_style_property(self.name_label, "selected", selected)
        set_style_property(self.count_label, "selected", selected)

    def set_count(self, text: str, *, attention: bool = False) -> None:
        self.count_label.setText(text)
        set_style_property(self.count_label, "attention", attention)


def _custom_speech(name: Optional[str]):
    """The custom speech model ``name`` is, or None (built-in, Whisper, or nothing)."""
    from services.local_asr.catalog import MODELS
    model = MODELS.get(name) if name else None
    return model if model is not None and model.custom else None


def _user_added(name: str) -> bool:
    """Whether the person added this model: a custom Whisper source or speech model.

    Read from the name, so a row stays recognizable after its model is removed.
    """
    from services.local_asr.custom import CUSTOM_PREFIX
    return name.startswith(CUSTOM_PREFIX) or is_custom_model(name)


def _from_local_files(name: str) -> bool:
    """A model added from files on this computer, which has nothing to download."""
    return _custom_speech(name) is not None or (
        is_custom_model(name) and bool(parse_source(name).local_path))


def _catalog_details(name: str) -> Optional[ModelDetails]:
    """A catalog model's details, or None for a model the catalog doesn't describe."""
    try:
        return get_model_details(name)
    except KeyError:
        return None


def _current_cache_info(
    info: Optional[CachedModelInfo], revision: Optional[str]
) -> Optional[CachedModelInfo]:
    """The cached copy, unless it lacks the revision the catalog pins."""
    if info is None or not revision or revision in info.revision_hashes:
        return info
    return None


@dataclass(frozen=True)
class _RowContext:
    """What each catalog row's refresh needs, gathered once per refresh."""

    loaded_model: Optional[str]
    loaded_repo: Optional[str]
    dictation_model: str
    meeting_model: str
    downloads_blocked: bool
    slot_busy: bool
    selection_locked: bool
    queued_names: Set[str]


class _CompactScrollArea(QScrollArea):
    """Scroll area whose minimumSizeHint yields to short host windows.

    QScrollArea.minimumSizeHint() defaults to 70x70 regardless of
    setMinimumHeight(1), which pushes DownloadsPage past Settings' 520 px
    minimum height when an engine's runtime callout and hardware strip are
    both visible.
    """

    def minimumSizeHint(self) -> QSize:
        return QSize(0, 1)


class BatchDownloadDialog(QDialog):
    """Confirmation for a multi-model download.

    Lists each model with its bundled size estimate, the estimated total,
    and the cache directory the files land in. Accepting here is the consent
    for every listed model; ApplicationController grants each one just
    before its download starts.
    """

    def __init__(self, model_names: List[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Download models")
        self.setWindowIcon(app_icon())
        self.setObjectName("batchDownloadDialog")
        self.setModal(True)
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 16)
        layout.setSpacing(10)

        count = len(model_names)
        noun = "model" if count == 1 else "models"
        title = QLabel(f"Download {count} {noun}?")
        title.setObjectName("downloadsTitle")
        layout.addWidget(title)

        lines = []
        total_bytes = 0
        for name in model_names:
            estimate_mb = MODEL_DOWNLOAD_SIZE_MB.get(name)
            if estimate_mb is not None:
                total_bytes += estimate_mb * 1_000_000
            lines.append(
                f"{name}  \u00b7  {format_download_size(name) or 'size unknown'}"
            )
        list_label = QLabel("\n".join(lines))
        list_label.setObjectName("batchModelList")
        list_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(list_label)
        self.model_list_label = list_label

        notices = [details.notice for details in map(_catalog_details, model_names)
                   if details is not None and details.notice]
        self.notice_label = WrappedLabel("\n\n".join(notices))
        self.notice_label.setObjectName("downloadsLicenseNotice")
        self.notice_label.setVisible(bool(notices))
        layout.addWidget(self.notice_label)

        total_label = QLabel(
            f"Estimated total: ~{format_size_bytes(total_bytes)}"
            if all(name in MODEL_DOWNLOAD_SIZE_MB for name in model_names)
            else "Estimated total: unknown"
        )
        total_label.setObjectName("batchTotalLabel")
        layout.addWidget(total_label)
        self.total_label = total_label

        destination_caption = QLabel("DESTINATION")
        destination_caption.setObjectName("downloadsEyebrow")
        layout.addWidget(destination_caption)
        # Show the real cache folder(s) these models land in: HF hub cache for
        # Whisper models, local_app_dir()/speech-models for optional engines.
        from config import local_app_dir
        from services.local_asr.catalog import MODELS as SPEECH_MODELS
        has_speech = any(name in SPEECH_MODELS for name in model_names)
        has_whisper = any(name not in SPEECH_MODELS for name in model_names)
        speech_dir = str(Path(local_app_dir()) / "speech-models")
        hf_dir = get_hf_cache_dir()
        if has_speech and has_whisper:
            dest_text = f"{hf_dir} · {speech_dir}"
            dest_tip = f"Whisper models: {hf_dir}\nSpeech engine models: {speech_dir}"
        elif has_speech:
            dest_text = speech_dir
            dest_tip = speech_dir
        else:
            dest_text = hf_dir
            dest_tip = hf_dir
        destination_path = ElidingLabel(dest_text)
        destination_path.setObjectName("batchDestPath")
        destination_path.setToolTip(dest_tip)
        destination_path.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(destination_path)
        self.destination_path_label = destination_path

        note = WrappedLabel(
            "Models download one at a time, in the order listed. You can "
            "stop after the current model from Settings → Models & engines while "
            "the queue runs. Models such as Parakeet also need a separate "
            "runtime to work; you will be prompted to install any missing "
            "required runtime."
        )
        note.setObjectName("downloadsSourceNote")
        layout.addWidget(note)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch()
        cancel_button = Button("Cancel")
        fit_compact_button(cancel_button, 100)
        cancel_button.clicked.connect(self.reject)
        buttons.addWidget(cancel_button)
        download_button = PrimaryButton(f"Download {count} {noun}\u2026")
        fit_compact_button(download_button, 0)
        download_button.clicked.connect(self.accept)
        buttons.addWidget(download_button)
        layout.addLayout(buttons)


class _ScanRelay(QObject):
    """Brings cache scans to the Qt thread for whichever Downloads pages exist then.

    It lives for the session, so a scan thread never emits on a page: emitting
    on one raced Settings closing, which can crash the process. Qt drops a
    deleted page's connection, and the scan's generation says which page asked.
    """

    finished = pyqtSignal(int, object)
    discovery_finished = pyqtSignal(int, object)


class _RemoteServiceRelay(QObject):
    """Brings remote pairing changes to the Qt thread without holding a page."""

    service_event = pyqtSignal(int, str)


_relay: Optional[_ScanRelay] = None
_remote_service_relay: Optional[_RemoteServiceRelay] = None
#: Scan generations, unique across pages because they share the relay.
_tokens = itertools.count(1)


def _scan_relay() -> _ScanRelay:
    global _relay
    if _relay is None:
        _relay = _ScanRelay()
    return _relay


def _remote_relay() -> _RemoteServiceRelay:
    global _remote_service_relay
    if _remote_service_relay is None:
        _remote_service_relay = _RemoteServiceRelay()
    return _remote_service_relay


class DownloadsPage(QWidget):
    """The catalog page Settings hosts under Models & storage."""

    #: Width of the left Engine Rail.
    RAIL_WIDTH = 172
    #: Width the on-demand slide-over technical profile takes.
    INSPECTOR_WIDTH = 300
    #: Local Whisper quantization options (shared with Dictation & Meeting Mode).
    COMPUTE_CHOICES = ("auto", "float16", "int8_float16", "int8", "float32")

    component_install_requested = pyqtSignal(str)
    component_cancel_requested = pyqtSignal(str)
    component_remove_requested = pyqtSignal(str)
    #: Emitted when the user changes an engine's Device or Whisper Quantization.
    runtime_settings_changed = pyqtSignal()
    #: Cache totals or download progress changed (rail value, Overview).
    inventory_changed = pyqtSignal()
    #: Only :meth:`rail_value` changed: a running download's progress.
    progress_changed = pyqtSignal()

    #: Assigned by UIController; called with the model name.
    on_download_requested: Optional[Callable[[str], None]] = None
    on_delete_requested: Optional[Callable[[str], None]] = None
    #: Assigned by UIController; called with the confirmed model list.
    on_batch_download_requested: Optional[Callable[[List[str]], None]] = None
    on_batch_cancel_requested: Optional[Callable[[], None]] = None

    def __init__(
        self,
        get_loaded_model: Optional[Callable[[], Optional[str]]] = None,
        parent=None,
        background_cache_scan: bool = True,
        defer_build: bool = False,
        auto_discover: Optional[bool] = None,
    ):
        """Show the catalog with the in-use model protected from deletion.

        Args:
            get_loaded_model: Provider returning the model name currently
                loaded by the engine (or None). Its files are memory-mapped, so
                Delete stays disabled for it.
            background_cache_scan: Scan the cache on a worker thread. Tests
                pass False to scan synchronously through a patched scanner.
            auto_discover: Scan default model caches once for existing reusable
                models to surface the first-time import alert. Defaults to
                ``background_cache_scan`` so unit tests stay isolated from
                developer caches unless opted in.
        """
        super().__init__(parent)
        self.setObjectName("downloadsPage")
        self._get_loaded_model = get_loaded_model
        self._background_cache_scan = bool(background_cache_scan)
        self._auto_discover = (
            bool(background_cache_scan) if auto_discover is None else bool(auto_discover)
        )
        self._discovery_started = False
        self._discovery_generation = 0
        self._discovered_models: list = []
        self._cache_scan_generation = 0
        self._cache_inventory_loading = False
        self._downloading_model: Optional[str] = None
        self._download_fraction: Optional[float] = None
        self._component_rows: Dict[str, ComponentRowWidget] = {}
        self._selected_model: Optional[str] = None
        self._selected_models: Set[str] = set()
        self._batch_queue: List[str] = []
        self._batch_done = 0
        self._batch_failed = 0
        self._downloads_blocked = False
        self._cached_sizes: Dict[str, int] = {}
        self._inspector_open = False
        self._active_rail_key = "all"
        #: Which backend inside a multi-runtime family (e.g. Parakeet) the banner targets.
        self._family_backend: Dict[str, str] = {}
        self._banner_component_id: Optional[str] = None
        self._banner_action_mode = "manage"  # "manage" | "install" | "cancel"
        self._component_progress: Dict[str, Tuple[str, int, int]] = {}
        self._remote_service = None
        self._remote_pairing = None
        self._remote_token = next(_tokens)
        remote_event = _remote_relay().service_event
        token = self._remote_token

        def _remote_listener(kind: str) -> None:
            remote_event.emit(token, kind)

        self._remote_listener = _remote_listener
        _remote_relay().service_event.connect(self._on_remote_service_event)

        self._ui_built = False
        self._inventory = {}
        _scan_relay().finished.connect(self._on_cache_scan_finished)
        _scan_relay().discovery_finished.connect(self._on_discovery_finished)
        if not defer_build:
            self.ensure_ui()

    _UI_ATTRIBUTES = frozenset({
        'target_bar',
        'local_pane',
        'host_pane',
        'engine_rail',
        'engine_rail_scroll',
        '_rail_items',
        '_active_rail_key',
        '_rail_extras_label',
        'right_pane',
        'models_section',
        'components_section',
        'components_scroll',
        'context_banner',
        'banner_dot',
        'banner_title',
        'banner_subtitle',
        'banner_device',
        'banner_runtime_badge',
        'banner_runtime_button',
        'banner_runtime_box',
        'banner_runtime_box_title',
        'banner_runtime_box_note',
        'banner_runtime_progress',
        'banner_runtime_box_button',
        'banner_controls_strip',
        'engine_runtime_label',
        'engine_runtime_combo',
        'engine_device_combo',
        'engine_compute_label',
        'engine_compute_combo',
        'banner_controls_hint',
        'selection_bar',
        'show_all_engines_button',
        'inspector_engine',
        'inspector_best_for_heading',
        'inspector_detail_scroll',
        'inspector_empty',
        '_inspector_open',
        'policy_row',
        'inspector_source_note',
        'inspector_best_for',
        '_cache_scan_generation',
        '_downloading_model',
        '_batch_queue',
        '_toolbar_layout',
        'download_all_button',
        'stats_label',
        'inspector_close_button',
        'inspector_license_button',
        'clear_selection_button',
        'download_selected_button',
        '_selected_model',
        'inspector',
        'message_row',
        'stop_batch_button',
        '_batch_failed',
        'selection_summary',
        '_cached_sizes',
        'inspector_tradeoffs_heading',
        '_download_fraction',
        'library_scroll_area',
        '_batch_done',
        'inspector_tags',
        'catalog_column',
        '_background_cache_scan',
        '_downloads_blocked',
        'select_all_button',
        'custom_models_button',
        'inspector_usage',
        'inspector_description',
        '_get_loaded_model',
        'inspector_tradeoffs',
        'list_layout',
        'inspector_origin_button',
        'inspector_links',
        '_details',
        'env_banner',
        'import_banner',
        'import_banner_title',
        'import_banner_note',
        'import_review_button',
        'import_dismiss_button',
        '_split',
        'message_label',
        'sort_combo',
        '_cache_inventory_loading',
        'status_filter_combo',
        'empty_label',
        'inspector_repo_button',
        'inspector_facts',
        'filter_edit',
        'inspector_name',
        'rows',
        'fact_labels',
    })

    def __getattr__(self, name):
        if (name in self._UI_ATTRIBUTES and "_ui_built" in self.__dict__
                and not self.__dict__["_ui_built"]):
            self.ensure_ui()
            if name in self.__dict__:
                return self.__dict__[name]
        raise AttributeError(name)

    def ensure_ui(self) -> None:
        if self._ui_built:
            return
        self._ui_built = True
        self._setup_ui()
        self._refresh_cached_model_state(self._inventory)

    # ---- construction ----

    def _setup_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        self._outer_layout = outer

        self.target_bar = SegmentedBar(
            (("This computer", ""), ("Remote host", "")),
            compact=True,
        )
        self.target_bar.setAccessibleName("Whose models and engines to manage")
        self.target_bar.currentIndexChanged.connect(self._on_target_changed)
        self.target_bar.hide()
        outer.addWidget(self.target_bar)

        self.local_pane = QWidget()
        self.local_pane.setObjectName("downloadsLocalPane")
        layout = QVBoxLayout(self.local_pane)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        outer.addWidget(self.local_pane, stretch=1)

        self.host_pane = None

        header_row = QHBoxLayout()
        header_row.setSpacing(8)
        # Elides rather than wraps: the cache path is long and would otherwise
        # push the rail and catalog down.
        self.stats_label = ElidingLabel("")
        self.stats_label.setObjectName("downloadsSubtitle")
        header_row.addWidget(self.stats_label, stretch=1)

        self.custom_models_button = Button("Add custom…")
        self.custom_models_button.setObjectName("downloadsToolButton")
        self.custom_models_button.setToolTip(
            "Add a model of your own for Whisper or another speech engine"
        )
        fit_compact_button(self.custom_models_button, 0)
        self.custom_models_button.clicked.connect(self._show_add_custom_menu)
        header_row.addWidget(self.custom_models_button)

        self.download_all_button = Button("Download all…")
        self.download_all_button.setObjectName("downloadsToolButton")
        fit_compact_button(self.download_all_button, 0)
        self.download_all_button.setToolTip(
            "Queue every model that is not downloaded yet"
        )
        self.download_all_button.clicked.connect(self._on_download_all_clicked)
        header_row.addWidget(self.download_all_button)

        open_folder_btn = Button("Open folder")
        open_folder_btn.setObjectName("downloadsToolButton")
        fit_compact_button(open_folder_btn, 110)
        open_folder_btn.setToolTip(
            "Open the folder where downloaded models are stored"
        )
        open_folder_btn.clicked.connect(self._on_open_cache_folder)
        header_row.addWidget(open_folder_btn)
        layout.addLayout(header_row)

        self.env_banner = QLabel(
            "Downloads are disabled by the HF_HUB_OFFLINE environment "
            "variable set outside this application."
        )
        self.env_banner.setObjectName("downloadsEnvBanner")
        self.env_banner.setWordWrap(True)
        self.env_banner.setVisible(False)
        layout.addWidget(self.env_banner)

        self.import_banner = self._build_import_banner()
        self.import_banner.setVisible(False)
        layout.addWidget(self.import_banner)

        # Build components first so the rail knows whether Extras should show.
        self.components_section = self._build_components_section()
        self.components_section.hide()

        self._split = QHBoxLayout()
        self._split.setContentsMargins(0, 0, 0, 0)
        self._split.setSpacing(10)

        self.engine_rail = self._build_engine_rail()
        self._split.addWidget(self.engine_rail)

        self.right_pane = QWidget()
        self.right_pane.setObjectName("downloadsRightPane")
        right_layout = QVBoxLayout(self.right_pane)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self.models_section = QWidget()
        self.models_section.setObjectName("downloadsModelsSection")
        models_layout = QVBoxLayout(self.models_section)
        models_layout.setContentsMargins(0, 0, 0, 0)
        models_layout.setSpacing(0)
        self.catalog_column = self._build_catalog_column()
        models_layout.addWidget(self.catalog_column, stretch=1)
        right_layout.addWidget(self.models_section, stretch=1)
        right_layout.addWidget(self.components_section, stretch=1)

        self._split.addWidget(self.right_pane, stretch=1)
        layout.addLayout(self._split, stretch=1)

        # Settings places the Hugging Face download policy cleanly in the quiet
        # footer area so the engine rail and model list start immediately.
        self.policy_row = QHBoxLayout()
        self.policy_row.setContentsMargins(0, 0, 0, 0)
        self.policy_row.setSpacing(10)
        layout.addLayout(self.policy_row)

        # Shared by models and components: batch progress and install results.
        self.message_row = QHBoxLayout()
        self.message_row.setSpacing(8)
        self.message_label = QLabel("")
        self.message_label.setObjectName("downloadsMessage")
        self.message_row.addWidget(self.message_label, stretch=1)
        self.stop_batch_button = Button("Stop after current")
        self.stop_batch_button.setObjectName("downloadsToolButton")
        fit_compact_button(self.stop_batch_button, 0)
        self.stop_batch_button.setToolTip(
            "Finish the model now downloading, then stop the queue"
        )
        self.stop_batch_button.clicked.connect(self._on_stop_batch_clicked)
        self.stop_batch_button.setVisible(False)
        self.message_row.addWidget(self.stop_batch_button)
        layout.addLayout(self.message_row)

        # On-demand slide-over technical profile: stays hidden until a row is picked.
        self.inspector = self._build_inspector()
        self.inspector.setParent(self)
        set_style_property(self.inspector, "overlay", True)
        shadow = QGraphicsDropShadowEffect(self.inspector)
        shadow.setBlurRadius(28)
        shadow.setOffset(-6, 0)
        shadow.setColor(QColor(0, 0, 0, 110))
        self.inspector.setGraphicsEffect(shadow)
        self.inspector.hide()

        self._active_rail_key = self._default_rail_key()
        self._apply_rail_selection(self._active_rail_key)
        self._refresh_remote_pairing()

    # ---- local / remote host target switcher ----

    def bind_remote_service(self, service) -> None:
        """Attach the remote engine service so paired host models can be managed here."""
        if service is not self._remote_service:
            if self._remote_service is not None:
                self._remote_service.remove_listener(self._remote_listener)
            self._remote_service = service
            if service is not None:
                listener = self._remote_listener
                service.add_listener(listener)
                self.destroyed.connect(lambda *_: service.remove_listener(listener))
        if self._ui_built:
            self._refresh_remote_pairing()

    def _on_remote_service_event(self, token: int, kind: str) -> None:
        if token != self._remote_token:
            return
        if kind in ("client", "host_renamed") and self._ui_built:
            self._refresh_remote_pairing()

    def _ensure_host_pane(self):
        if self.host_pane is None:
            from ui_qt.dialogs.remote_models import RemoteModelsPane

            self.host_pane = RemoteModelsPane(
                self._remote_service,
                getattr(self._remote_pairing, "host_name", ""),
                parent=self,
            )
            self.host_pane.hide()
            self._outer_layout.addWidget(self.host_pane, stretch=1)
        return self.host_pane

    def _refresh_remote_pairing(self) -> None:
        pairing = (
            self._remote_service.client_pairing()
            if self._remote_service is not None
            else None
        )
        self._remote_pairing = pairing
        if pairing is None:
            self.target_bar.hide()
            if self.target_bar.currentIndex() != 0:
                self.target_bar.setCurrentIndex(0)
            self.local_pane.show()
            if self.host_pane is not None:
                self.host_pane.bind(self._remote_service, None)
                self.host_pane.hide()
            return
        host_label = f"{pairing.host_name} (host)"
        self.target_bar.set_title(1, host_label)
        self.target_bar.show()
        if self.host_pane is not None:
            self.host_pane.bind(self._remote_service, pairing)

    def _on_target_changed(self, index: int) -> None:
        show_host = index == 1 and self._remote_pairing is not None
        if show_host:
            self.close_inspector()
            self.local_pane.hide()
            pane = self._ensure_host_pane()
            pane.bind(self._remote_service, self._remote_pairing)
            pane.show()
        else:
            if self.host_pane is not None:
                self.host_pane.hide()
            self.local_pane.show()
            self._sync_toolbar_gutter()
            self._position_inspector()

    def showing_host(self) -> bool:
        return (
            self._ui_built
            and self._remote_pairing is not None
            and self.target_bar.currentIndex() == 1
        )

    def show_local(self) -> None:
        """Switch to this computer's local models and engines."""
        if not self._ui_built:
            return
        if self.target_bar.currentIndex() != 0:
            self.target_bar.setCurrentIndex(0)
        else:
            self._on_target_changed(0)

    def show_host(self) -> bool:
        """Switch to the paired host's models and engines; returns False if unpaired."""
        self.ensure_ui()
        self._refresh_remote_pairing()
        if self._remote_pairing is None:
            self.show_local()
            return False
        if self.target_bar.currentIndex() != 1:
            self.target_bar.setCurrentIndex(1)
        else:
            self._on_target_changed(1)
            if self.host_pane is not None:
                self.host_pane.refresh_host()
        return True

    def on_shown(self) -> None:
        """Refresh the paired host catalog when returning to this page on the host tab."""
        if self.showing_host() and self.host_pane is not None:
            self.host_pane.refresh_host()

    def _default_rail_key(self) -> str:
        """Open focused on the active local speech family when one is selected."""
        settings = self._settings_snapshot()
        selected_backend = SettingsKey.SELECTED_MODEL.read(settings)
        family = _rail_key_for(selected_backend)
        if family in self._rail_items:
            if selected_backend not in ("all", "installed", "components"):
                self._family_backend[family] = selected_backend
            return family
        return "all"

    def _build_engine_rail(self) -> QFrame:
        """Build the left rail grouping local models by model family."""
        from services.local_asr.catalog import MODEL_FAMILIES

        rail = QFrame()
        rail.setObjectName("downloadsEngineRail")
        rail.setFixedWidth(self.RAIL_WIDTH)
        outer = QVBoxLayout(rail)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = _CompactScrollArea()
        scroll.setObjectName("downloadsEngineRailScroll")
        scroll.setWidgetResizable(True)
        scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        scroll.setMinimumHeight(1)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.engine_rail_scroll = scroll

        content = QWidget()
        content.setObjectName("downloadsRailContent")
        rail_layout = QVBoxLayout(content)
        rail_layout.setContentsMargins(6, 6, 6, 6)
        rail_layout.setSpacing(1)

        self._rail_items: Dict[str, _EngineRailItem] = {}

        def add_section(title: str) -> QLabel:
            lbl = QLabel(title)
            lbl.setObjectName("downloadsRailSection")
            rail_layout.addWidget(lbl)
            return lbl

        def add_item(
            key: str, label: str, *, dot_rgb: str = "", tooltip: str = ""
        ) -> _EngineRailItem:
            item = _EngineRailItem(
                key, label, dot_rgb=dot_rgb, tooltip=tooltip, parent=content
            )
            item.clicked.connect(lambda _checked=False, k=key: self.select_rail(k))
            self._rail_items[key] = item
            rail_layout.addWidget(item)
            return item

        add_section("OVERVIEW")
        add_item("all", "All models", tooltip="Show models from every speech family")
        add_item("installed", "Installed", tooltip="Show models already downloaded on this computer")

        add_section("MODEL FAMILIES")
        for family_id in _family_order():
            meta = _engine_meta(family_id)
            rail_name = MODEL_FAMILIES.get(family_id, meta.short)
            if parakeet_mlx_supported() and family_id == "parakeet":
                rail_name = "Parakeet ★"
            tip = f"{rail_name} — {meta.tagline}" if meta.tagline else rail_name
            add_item(family_id, rail_name, dot_rgb=meta.rgb, tooltip=tip)

        self._rail_extras_label = add_section("EXTRAS")
        comp_item = add_item(
            "components",
            "Components",
            tooltip="Speech runtimes and optional meeting add-ons",
        )
        has_components = bool(self._component_rows)
        self._rail_extras_label.setVisible(has_components)
        comp_item.setVisible(has_components)

        rail_layout.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll, stretch=1)
        return rail

    def _build_import_banner(self) -> QFrame:
        """Compact first-time alert when reusable speech models are found on disk."""
        banner = QFrame()
        banner.setObjectName("downloadsImportBanner")
        row = QHBoxLayout(banner)
        row.setContentsMargins(10, 6, 10, 6)
        row.setSpacing(10)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(1)

        self.import_banner_title = ElidingLabel("")
        self.import_banner_title.setObjectName("downloadsImportBannerTitle")
        text_col.addWidget(self.import_banner_title)

        self.import_banner_note = ElidingLabel(
            "Reuse them in place without downloading again."
        )
        self.import_banner_note.setObjectName("downloadsImportBannerNote")
        text_col.addWidget(self.import_banner_note)
        row.addLayout(text_col, stretch=1)

        self.import_review_button = QPushButton("Review & import…")
        self.import_review_button.setObjectName("downloadsBannerAction")
        set_style_property(self.import_review_button, "tone", "primary")
        self.import_review_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.import_review_button.clicked.connect(self._open_import_dialog_from_banner)
        row.addWidget(self.import_review_button, 0, Qt.AlignmentFlag.AlignVCenter)

        self.import_dismiss_button = QPushButton("Dismiss")
        self.import_dismiss_button.setObjectName("downloadsBannerAction")
        self.import_dismiss_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.import_dismiss_button.setToolTip("Hide this alert for these models")
        self.import_dismiss_button.clicked.connect(self._dismiss_import_banner)
        row.addWidget(self.import_dismiss_button, 0, Qt.AlignmentFlag.AlignVCenter)
        return banner

    def _build_context_banner(self) -> QFrame:
        """Build the focused engine banner with inline runtime install & hardware controls."""
        banner = QFrame()
        banner.setObjectName("downloadsContextBanner")
        outer = QVBoxLayout(banner)
        outer.setContentsMargins(12, 6, 12, 6)
        outer.setSpacing(4)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(10)

        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(1)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(7)

        self.banner_dot = QLabel()
        self.banner_dot.setFixedSize(9, 9)
        self.banner_dot.setVisible(False)
        title_row.addWidget(self.banner_dot, 0, Qt.AlignmentFlag.AlignVCenter)

        self.banner_title = QLabel("")
        self.banner_title.setObjectName("downloadsBannerTitle")
        title_row.addWidget(self.banner_title)

        self.banner_device = QLabel("")
        self.banner_device.setObjectName("downloadsBannerDevice")
        self.banner_device.setVisible(False)
        title_row.addWidget(self.banner_device, 0, Qt.AlignmentFlag.AlignVCenter)
        title_row.addStretch()
        left.addLayout(title_row)

        self.banner_subtitle = ElidingLabel("")
        self.banner_subtitle.setObjectName("downloadsBannerSubtitle")
        left.addWidget(self.banner_subtitle)

        header_row.addLayout(left, stretch=1)

        right = QHBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(6)

        self.banner_runtime_badge = QLabel("")
        self.banner_runtime_badge.setObjectName("downloadsRuntimeBadge")
        self.banner_runtime_badge.setVisible(False)
        right.addWidget(self.banner_runtime_badge, 0, Qt.AlignmentFlag.AlignVCenter)

        self.banner_runtime_button = QPushButton("")
        self.banner_runtime_button.setObjectName("downloadsBannerAction")
        self.banner_runtime_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.banner_runtime_button.clicked.connect(self._on_banner_runtime_clicked)
        self.banner_runtime_button.setVisible(False)
        right.addWidget(self.banner_runtime_button, 0, Qt.AlignmentFlag.AlignVCenter)

        header_row.addLayout(right)
        outer.addLayout(header_row)

        # Inline runtime callout / progress box: shown only when a runtime is
        # missing, updating, installing, or optional CUDA is offered.
        self.banner_runtime_box = QFrame()
        self.banner_runtime_box.setObjectName("downloadsRuntimeBox")
        box_row = QHBoxLayout(self.banner_runtime_box)
        box_row.setContentsMargins(10, 6, 10, 6)
        box_row.setSpacing(10)

        box_text = QVBoxLayout()
        box_text.setContentsMargins(0, 0, 0, 0)
        box_text.setSpacing(2)

        self.banner_runtime_box_title = ElidingLabel("")
        self.banner_runtime_box_title.setObjectName("downloadsRuntimeBoxTitle")
        box_text.addWidget(self.banner_runtime_box_title)

        self.banner_runtime_box_note = ElidingLabel("")
        self.banner_runtime_box_note.setObjectName("downloadsRuntimeBoxNote")
        box_text.addWidget(self.banner_runtime_box_note)

        self.banner_runtime_progress = QProgressBar()
        self.banner_runtime_progress.setObjectName("downloadsRuntimeProgress")
        self.banner_runtime_progress.setTextVisible(False)
        self.banner_runtime_progress.setFixedHeight(6)
        self.banner_runtime_progress.setVisible(False)
        box_text.addWidget(self.banner_runtime_progress)

        box_row.addLayout(box_text, stretch=1)

        self.banner_runtime_box_button = QPushButton("")
        self.banner_runtime_box_button.setObjectName("downloadsBannerAction")
        self.banner_runtime_box_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.banner_runtime_box_button.clicked.connect(self._on_banner_runtime_clicked)
        box_row.addWidget(self.banner_runtime_box_button, 0, Qt.AlignmentFlag.AlignVCenter)

        self.banner_runtime_box.setVisible(False)
        outer.addWidget(self.banner_runtime_box)

        # Inline hardware controls strip (optional Runtime switcher + Device + Whisper Quantization).
        self.banner_controls_strip = QFrame()
        self.banner_controls_strip.setObjectName("downloadsControlsStrip")
        ctrl_row = QHBoxLayout(self.banner_controls_strip)
        ctrl_row.setContentsMargins(0, 4, 0, 0)
        ctrl_row.setSpacing(8)

        self.engine_runtime_label = QLabel("Runtime")
        self.engine_runtime_label.setObjectName("downloadsControlLabel")
        self.engine_runtime_label.setVisible(False)
        ctrl_row.addWidget(self.engine_runtime_label)

        self.engine_runtime_combo = ElidingComboBox()
        self.engine_runtime_combo.setObjectName("downloadsRuntimeCombo")
        self.engine_runtime_combo.setMinimumWidth(124)
        self.engine_runtime_combo.setFixedHeight(26)
        self.engine_runtime_combo.setToolTip(
            "Choose which runtime inside this family to configure"
        )
        self.engine_runtime_combo.currentIndexChanged.connect(self._on_banner_runtime_backend_changed)
        self.engine_runtime_combo.setVisible(False)
        ctrl_row.addWidget(self.engine_runtime_combo)

        dev_lbl = QLabel("Device")
        dev_lbl.setObjectName("downloadsControlLabel")
        ctrl_row.addWidget(dev_lbl)

        self.engine_device_combo = ElidingComboBox()
        self.engine_device_combo.setObjectName("downloadsDeviceCombo")
        self.engine_device_combo.setMinimumWidth(80)
        self.engine_device_combo.setFixedHeight(26)
        self.engine_device_combo.setToolTip(
            "Hardware preference for this engine (shared with Dictation and Meeting Mode)"
        )
        self.engine_device_combo.currentIndexChanged.connect(self._on_engine_runtime_changed)
        ctrl_row.addWidget(self.engine_device_combo)

        self.engine_compute_label = QLabel("Quantization")
        self.engine_compute_label.setObjectName("downloadsControlLabel")
        ctrl_row.addWidget(self.engine_compute_label)

        self.engine_compute_combo = ElidingComboBox()
        self.engine_compute_combo.setObjectName("downloadsComputeCombo")
        self.engine_compute_combo.addItems(self.COMPUTE_CHOICES)
        self.engine_compute_combo.setMinimumWidth(84)
        self.engine_compute_combo.setFixedHeight(26)
        self.engine_compute_combo.setToolTip(
            "Local Whisper compute precision (shared with Dictation and Meeting Mode)"
        )
        self.engine_compute_combo.currentTextChanged.connect(self._on_engine_runtime_changed)
        ctrl_row.addWidget(self.engine_compute_combo)

        self.banner_controls_hint = ElidingLabel("Shared with Dictation & Meetings")
        self.banner_controls_hint.setObjectName("downloadsControlHint")
        self.banner_controls_hint.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        ctrl_row.addWidget(self.banner_controls_hint, stretch=1)

        self.banner_controls_strip.setVisible(False)
        outer.addWidget(self.banner_controls_strip)
        return banner

    def add_policy_control(self, label: str, control: QWidget) -> None:
        """Place a labeled control on the quiet footer row."""
        caption = QLabel(label)
        caption.setObjectName("downloadsPolicyLabel")
        self.policy_row.addWidget(caption)
        self.policy_row.addWidget(control, stretch=1)
        self.policy_row.addStretch()

    def _build_catalog_column(self) -> QWidget:
        """Stack the compact filters, batch bar, and scrollable banner + rows."""
        column = QWidget()
        column.setObjectName("downloadsCatalogColumn")
        column_layout = QVBoxLayout(column)
        column_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.setSpacing(5)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(6)
        self._toolbar_layout = toolbar

        self.filter_edit = QLineEdit()
        self.filter_edit.setObjectName("modelManagerSearch")
        self.filter_edit.setPlaceholderText("Search models")
        self.filter_edit.setMinimumWidth(80)
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._apply_filter)
        toolbar.addWidget(self.filter_edit, stretch=1)

        self.status_filter_combo = ElidingComboBox()
        self.status_filter_combo.setObjectName("modelManagerStatusFilter")
        self.status_filter_combo.setMinimumWidth(78)
        self.status_filter_combo.addItem("All", "all")
        self.status_filter_combo.addItem("Downloaded", "downloaded")
        self.status_filter_combo.addItem("Not downloaded", "not_downloaded")
        self.status_filter_combo.setToolTip("Filter by download status")
        self.status_filter_combo.currentIndexChanged.connect(self._apply_filter)
        toolbar.addWidget(self.status_filter_combo)

        self.sort_combo = ElidingComboBox()
        self.sort_combo.setObjectName("modelManagerSort")
        self.sort_combo.setMinimumWidth(96)
        self.sort_combo.addItem("Recommended", "recommended")
        self.sort_combo.addItem("Downloaded first", "downloaded")
        self.sort_combo.addItem("Smallest first", "size")
        self.sort_combo.addItem("Name A-Z", "name")
        self.sort_combo.addItem("By engine", "backend")
        self.sort_combo.setToolTip("Sort model list")
        self.sort_combo.currentIndexChanged.connect(self._apply_filter)
        toolbar.addWidget(self.sort_combo)

        column_layout.addLayout(toolbar)
        self.selection_bar = self._build_selection_bar()
        column_layout.addWidget(self.selection_bar)
        column_layout.addWidget(self._build_list(), stretch=1)
        return column

    def _build_selection_bar(self) -> QWidget:
        """Build the batch-selection bar between the filters and the rows."""
        bar = QWidget()
        bar.setObjectName("downloadsSelectionBar")
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(0, 0, 0, 0)
        bar_layout.setSpacing(10)

        self.select_all_button = Button("Select all")
        self.select_all_button.setObjectName("downloadsFlatButton")
        fit_compact_button(self.select_all_button, 0)
        self.select_all_button.clicked.connect(self._on_select_all_clicked)
        bar_layout.addWidget(self.select_all_button)

        self.clear_selection_button = Button("Clear")
        self.clear_selection_button.setObjectName("downloadsFlatButton")
        fit_compact_button(self.clear_selection_button, 0)
        self.clear_selection_button.clicked.connect(self._on_clear_selection_clicked)
        bar_layout.addWidget(self.clear_selection_button)

        self.selection_summary = ElidingLabel("")
        self.selection_summary.setObjectName("downloadsSelectionSummary")
        bar_layout.addWidget(self.selection_summary, stretch=1)

        self.download_selected_button = PrimaryButton("Download selected")
        fit_compact_button(self.download_selected_button, 0)
        self.download_selected_button.clicked.connect(
            self._on_download_selected_clicked
        )
        bar_layout.addWidget(self.download_selected_button)
        return bar

    def _build_list(self) -> QWidget:
        self.library_scroll_area = _CompactScrollArea()
        self.library_scroll_area.setObjectName("modelManagerLibraryScroll")
        self.library_scroll_area.setWidgetResizable(True)
        # 1 px minimum height keeps the page within Settings' 520 px floor
        # even when a missing-runtime callout is shown, while Expanding keeps a
        # generous default sizeHint when opened standalone.
        self.library_scroll_area.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.library_scroll_area.setMinimumHeight(1)
        self.library_scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.library_scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)

        list_container = QWidget()
        list_container.setObjectName("downloadsListContainer")
        self.list_layout = QVBoxLayout(list_container)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(6)

        self.context_banner = self._build_context_banner()
        self.list_layout.addWidget(self.context_banner)

        self.rows: Dict[str, ModelRowWidget] = {}
        from services.local_asr.catalog import MODELS, model_supported_here
        for model_name in [*config.WHISPER_MODEL_CHOICES, *MODELS]:
            if model_name == "auto":
                continue
            if model_name in MODELS and not model_supported_here(model_name):
                continue
            row = ModelRowWidget(model_name)
            row.download_clicked.connect(self._on_download_clicked)
            row.delete_clicked.connect(self._on_delete_clicked)
            row.details_requested.connect(self._on_row_details_requested)
            row.selection_toggled.connect(self._on_selection_toggled)
            self.rows[model_name] = row
            self.list_layout.addWidget(row)

        self.empty_label = QLabel("No models match")
        self.empty_label.setObjectName("infoLabel")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setVisible(False)
        self.list_layout.addWidget(self.empty_label)

        self.show_all_engines_button = Button("Show all engines")
        self.show_all_engines_button.setObjectName("downloadsFlatButton")
        fit_compact_button(self.show_all_engines_button, 0)
        self.show_all_engines_button.clicked.connect(lambda: self.select_rail("all"))
        self.show_all_engines_button.setVisible(False)
        self.list_layout.addWidget(
            self.show_all_engines_button, 0, Qt.AlignmentFlag.AlignHCenter
        )

        self.list_layout.addStretch()

        self.library_scroll_area.setWidget(list_container)
        # Track the bar's own show/hide so the filters realign the moment a
        # filter result stops needing it.
        self.library_scroll_area.verticalScrollBar().installEventFilter(self)
        return self.library_scroll_area

    def _sync_toolbar_gutter(self) -> None:
        """Keep the filters and the rows they filter on one right edge.

        The gap between the scroll area and its viewport is the scroll bar's
        real width, which is zero while the bar is hidden, so this reads it
        instead of hard-coding the themed width.
        """
        reserved = max(
            0,
            self.library_scroll_area.width()
            - self.library_scroll_area.viewport().width(),
        )
        if self._toolbar_layout.contentsMargins().right() != reserved:
            self._toolbar_layout.setContentsMargins(0, 0, reserved, 0)

    def eventFilter(self, watched, event):
        if watched is self.library_scroll_area.verticalScrollBar() and event.type() in (
            event.Type.Show,
            event.Type.Hide,
            event.Type.Resize,
        ):
            self._sync_toolbar_gutter()
        return super().eventFilter(watched, event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync_toolbar_gutter()
        self._position_inspector()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_toolbar_gutter()
        self._position_inspector()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape and self._inspector_open:
            self.close_inspector()
            event.accept()
            return
        super().keyPressEvent(event)

    # ---- rail navigation & context banner ----

    def current_rail_key(self) -> str:
        return self._active_rail_key

    def select_rail(self, key: str) -> None:
        """Switch the left rail selection and refresh the focused pane."""
        raw = key
        key = _rail_key_for(key)
        if key not in self._rail_items:
            key = "all"
        if key == "components" and not self._component_rows:
            key = "all"
        if raw != key and raw not in ("all", "installed", "components") and key in self._rail_items:
            self._family_backend[key] = raw
        self._active_rail_key = key
        self.close_inspector()
        self._apply_rail_selection(key)

    def _apply_rail_selection(self, key: str) -> None:
        for item_key, item in self._rail_items.items():
            item.set_selected(item_key == key)
        show_components = key == "components"
        self.models_section.setVisible(not show_components)
        self.components_section.setVisible(show_components)
        if not show_components:
            is_installed = key == "installed"
            self.status_filter_combo.setVisible(not is_installed)
            self._update_context_banner()
            self._apply_filter()
            self._update_selection_bar()
            self.library_scroll_area.verticalScrollBar().setValue(0)

    def _supported_family_backends(self, family: str) -> list[str]:
        from services.local_asr.catalog import backend_supported_here, backends_in_family

        return [b for b in backends_in_family(family) if backend_supported_here(b)]

    def _banner_backend_for(self, family: str) -> str:
        from services.local_asr.catalog import WHISPER_BACKEND, WHISPER_FAMILY

        if family == WHISPER_FAMILY:
            return WHISPER_BACKEND
        backends = self._supported_family_backends(family)
        if not backends:
            return family
        chosen = self._family_backend.get(family)
        if chosen in backends:
            return chosen
        settings = self._settings_snapshot()
        active = SettingsKey.SELECTED_MODEL.read(settings)
        if active in backends:
            return active
        return backends[0]

    def _on_banner_runtime_backend_changed(self, _index: int) -> None:
        backend_id = self.engine_runtime_combo.currentData()
        if not backend_id or self._active_rail_key in ("all", "installed", "components"):
            return
        self._family_backend[self._active_rail_key] = backend_id
        self._update_context_banner()

    def _refresh_rail_counts(self) -> None:
        if not self._ui_built:
            return
        from services.local_asr.catalog import family_of

        total_models = len(self.rows)
        installed_models = sum(1 for row in self.rows.values() if row.is_cached)
        if self._cache_inventory_loading:
            self._rail_items["all"].set_count("…")
            self._rail_items["installed"].set_count("…")
        else:
            self._rail_items["all"].set_count(f"{installed_models}/{total_models}")
            self._rail_items["installed"].set_count(str(installed_models))

        for family_id in _family_order():
            item = self._rail_items.get(family_id)
            if item is None:
                continue
            family_rows = [row for row in self.rows.values() if family_of(row.backend) == family_id]
            if self._cache_inventory_loading:
                item.set_count("…")
            else:
                dl = sum(1 for row in family_rows if row.is_cached)
                item.set_count(f"{dl}/{len(family_rows)}")

    def _runtime_for_backend(self, backend_id: str) -> Optional[str]:
        """Return the primary component ID associated with a speech backend."""
        from services.components import component_is_published
        from services.local_asr.catalog import (
            BACKENDS,
            WHISPER_BACKEND,
            resolve_runtime,
            selected_device,
        )

        if backend_id == WHISPER_BACKEND:
            return "gpu-accel" if component_is_published("gpu-accel") else None
        if backend_id not in BACKENDS:
            return None
        settings = self._settings_snapshot()
        try:
            comp_id, _device = resolve_runtime(
                backend_id, selected_device(backend_id, settings)
            )
        except Exception:
            return None
        return comp_id if component_is_published(comp_id) else None

    def _sync_banner_hardware_controls(self, family_id: str, backend_id: str) -> None:
        """Populate the inline Runtime / Device / Quantization combos."""
        from services.local_asr.catalog import (
            WHISPER_BACKEND,
            model_runtime_label,
            selected_device,
        )

        backends = self._supported_family_backends(family_id)
        show_runtime_switcher = len(backends) > 1
        self.engine_runtime_label.setVisible(show_runtime_switcher)
        self.engine_runtime_combo.setVisible(show_runtime_switcher)
        if show_runtime_switcher:
            rt_blocker = self.engine_runtime_combo.blockSignals(True)
            self.engine_runtime_combo.clear()
            for bid in backends:
                self.engine_runtime_combo.addItem(model_runtime_label("", backend=bid), bid)
            rt_idx = self.engine_runtime_combo.findData(backend_id)
            self.engine_runtime_combo.setCurrentIndex(max(0, rt_idx))
            self.engine_runtime_combo.blockSignals(rt_blocker)

        settings = self._settings_snapshot()
        is_whisper = backend_id == WHISPER_BACKEND
        if is_whisper:
            choices = ["auto", "cpu"] if sys.platform == "darwin" else ["auto", "cuda", "cpu"]
            current_device = str(SettingsKey.WHISPER_DEVICE.read(settings) or "auto")
        elif backend_id == "moonshine":
            choices = ["cpu"]
            current_device = "cpu"
        elif backend_id == "parakeet_mlx":
            choices = ["auto", "cpu"]
            current_device = selected_device(backend_id, settings)
        else:
            choices = ["auto", "cuda", "cpu"]
            current_device = selected_device(backend_id, settings)

        dev_blocker = self.engine_device_combo.blockSignals(True)
        set_device_choices(self.engine_device_combo, choices)
        idx = self.engine_device_combo.findData(current_device)
        self.engine_device_combo.setCurrentIndex(max(0, idx))
        self.engine_device_combo.setEnabled(len(choices) > 1)
        self.engine_device_combo.setToolTip(
            "Moonshine runs on CPU only"
            if backend_id == "moonshine"
            else "Hardware preference for this runtime (shared with Dictation and Meeting Mode)"
        )
        self.engine_device_combo.blockSignals(dev_blocker)

        self.engine_compute_label.setVisible(is_whisper)
        self.engine_compute_combo.setVisible(is_whisper)
        if is_whisper:
            compute = str(SettingsKey.WHISPER_COMPUTE_TYPE.read(settings) or "auto")
            comp_blocker = self.engine_compute_combo.blockSignals(True)
            if self.engine_compute_combo.findText(compute) < 0:
                self.engine_compute_combo.addItem(compute)
            self.engine_compute_combo.setCurrentText(compute)
            self.engine_compute_combo.blockSignals(comp_blocker)

        self.banner_controls_strip.setVisible(True)

    def _on_engine_runtime_changed(self, _value=None) -> None:
        """Persist inline Device / Quantization edits and refresh runtime status."""
        from services.local_asr.catalog import BACKENDS, WHISPER_BACKEND

        family_id = self._active_rail_key
        if family_id in ("all", "installed", "components"):
            return
        backend_id = self._banner_backend_for(family_id)
        if backend_id == WHISPER_BACKEND:
            device = self.engine_device_combo.currentData() or "auto"
            compute = self.engine_compute_combo.currentText() or "auto"
            try:
                settings_manager.update_settings({
                    SettingsKey.WHISPER_DEVICE: device,
                    SettingsKey.WHISPER_COMPUTE_TYPE: compute,
                })
            except Exception as exc:
                logger.error("Couldn't save Whisper runtime settings: %s", exc, exc_info=True)
                self.message_label.setText(
                    format_action_error("Couldn't save device or quantization", exc)
                )
                return
        elif backend_id in BACKENDS:
            device = self.engine_device_combo.currentData() or "auto"
            try:
                settings = self._settings_snapshot()
                devices = dict(SettingsKey.LOCAL_ASR_DEVICES.read(settings))
                devices[backend_id] = device
                settings_manager.save_setting(SettingsKey.LOCAL_ASR_DEVICES, devices)
            except Exception as exc:
                logger.error("Couldn't save %s device preference: %s", backend_id, exc, exc_info=True)
                self.message_label.setText(
                    format_action_error("Couldn't save device preference", exc)
                )
                return
        else:
            return
        self._update_context_banner()
        self.runtime_settings_changed.emit()

    def _update_context_banner(self) -> None:
        from services.local_asr.catalog import MODEL_FAMILIES, WHISPER_BACKEND, family_of

        key = self._active_rail_key
        self._banner_component_id = None
        self._banner_action_mode = "manage"
        self.banner_runtime_badge.setVisible(False)
        self.banner_runtime_button.setVisible(False)
        self.banner_runtime_box.setVisible(False)
        self.banner_runtime_progress.setVisible(False)
        self.banner_controls_strip.setVisible(False)
        self.banner_device.setVisible(False)
        self.banner_dot.setVisible(False)

        if key == "all":
            total = len(self.rows)
            downloaded = sum(1 for row in self.rows.values() if row.is_cached)
            total_bytes = sum(self._cached_sizes.values())
            self.banner_title.setText("All speech models")
            if self._cache_inventory_loading:
                self.banner_subtitle.setText("Checking downloaded models…")
            else:
                self.banner_subtitle.setText(
                    f"{downloaded} of {total} downloaded · {format_size_bytes(total_bytes)} used · "
                    "Pick a model family on the left to focus."
                )
            return

        if key == "installed":
            installed_rows = [row for row in self.rows.values() if row.is_cached]
            families_used = len({family_of(row.backend) for row in installed_rows})
            total_bytes = sum(self._cached_sizes.values())
            self.banner_title.setText("Installed on this computer")
            if self._cache_inventory_loading:
                self.banner_subtitle.setText("Checking downloaded models…")
            elif installed_rows:
                noun = "model" if len(installed_rows) == 1 else "models"
                fam_noun = "family" if families_used == 1 else "families"
                self.banner_subtitle.setText(
                    f"{len(installed_rows)} {noun} · {format_size_bytes(total_bytes)} across {families_used} {fam_noun}"
                )
            else:
                self.banner_subtitle.setText(
                    "No speech models are downloaded yet. Pick a family on the left to download one."
                )
            return

        meta = _engine_meta(key)
        backend_id = self._banner_backend_for(key)
        display_name = MODEL_FAMILIES.get(key, meta.short)
        self.banner_dot.setStyleSheet(
            f"background-color: rgb({meta.rgb}); border: none; border-radius: 4px;"
        )
        self.banner_dot.setVisible(True)
        self.banner_title.setText(display_name)
        self.banner_subtitle.setText(meta.tagline)
        self._sync_banner_hardware_controls(key, backend_id)

        comp_id = self._runtime_for_backend(backend_id)
        if not comp_id or comp_id not in self._component_rows:
            if backend_id == WHISPER_BACKEND:
                self.banner_runtime_badge.setText("Built-in engine")
                set_style_property(self.banner_runtime_badge, "tone", "ready")
                self.banner_runtime_badge.setVisible(True)
            return

        info = component_coordinator.describe(comp_id)
        installing = component_coordinator.is_installing(comp_id) or (
            comp_id in self._component_progress
        )
        self._banner_component_id = comp_id
        short_name = info.display_name.replace(" runtime", "")
        size_hint = f" (~{format_size_bytes(info.download_bytes)})" if info.download_bytes else ""

        if installing:
            self._banner_action_mode = "cancel"
            phase, done, total = self._component_progress.get(comp_id, ("Installing", 0, 0))
            if total > 0:
                pct = min(100, int(done * 100 / total))
                self.banner_runtime_box_title.setText(
                    f"{phase} {info.display_name}… {format_size_bytes(done)} of {format_size_bytes(total)} ({pct}%)"
                )
                self.banner_runtime_progress.setRange(0, 100)
                self.banner_runtime_progress.setValue(pct)
            else:
                self.banner_runtime_box_title.setText(f"{phase} {info.display_name}…")
                self.banner_runtime_progress.setRange(0, 0)
            self.banner_runtime_box_note.setText(
                "Verifying and extracting in the background. Recording stays available."
            )
            self.banner_runtime_progress.setVisible(True)
            self.banner_runtime_box_button.setText("Cancel")
            set_style_property(self.banner_runtime_box_button, "tone", "")
            set_style_property(self.banner_runtime_box, "tone", "downloading")
            self.banner_runtime_box.setVisible(True)
            repolish(self.banner_runtime_box_button)
            return

        if info.state == ComponentState.UPDATE_AVAILABLE:
            self._banner_action_mode = "install"
            self.banner_runtime_box_title.setText(
                f"Update available for {info.display_name}{size_hint}"
            )
            self.banner_runtime_box_note.setText(
                "Your installed runtime keeps working until the verified update finishes."
            )
            self.banner_runtime_box_button.setText("Update runtime")
            set_style_property(self.banner_runtime_box_button, "tone", "primary")
            set_style_property(self.banner_runtime_box, "tone", "update")
            self.banner_runtime_box.setVisible(True)
            repolish(self.banner_runtime_box_button)
            return

        if info.is_usable:
            self._banner_action_mode = "manage"
            self.banner_runtime_badge.setText(f"{short_name} · Ready")
            set_style_property(self.banner_runtime_badge, "tone", "ready")
            self.banner_runtime_button.setText("Manage")
            set_style_property(self.banner_runtime_button, "tone", "")
            self.banner_runtime_badge.setVisible(True)
            self.banner_runtime_button.setVisible(True)
            repolish(self.banner_runtime_button)
            return

        if backend_id == WHISPER_BACKEND:
            if self.engine_device_combo.currentData() == "cpu":
                self.banner_runtime_badge.setText("Built-in · CPU")
                set_style_property(self.banner_runtime_badge, "tone", "ready")
                self.banner_runtime_badge.setVisible(True)
                return
            self._banner_action_mode = "install"
            self.banner_runtime_box_title.setText(
                f"Optional NVIDIA CUDA acceleration{size_hint}"
            )
            self.banner_runtime_box_note.setText(
                "Whisper runs on CPU now. Install GPU Acceleration for 2–4× faster NVIDIA transcription."
            )
            self.banner_runtime_box_button.setText("Install CUDA")
            set_style_property(self.banner_runtime_box_button, "tone", "")
            set_style_property(self.banner_runtime_box, "tone", "optional")
            self.banner_runtime_box.setVisible(True)
            repolish(self.banner_runtime_box_button)
            return

        self._banner_action_mode = "install"
        self.banner_runtime_box_title.setText(
            f"{info.display_name} required{size_hint}"
        )
        self.banner_runtime_box_note.setText(
            "Install this runtime so models in this engine can run on your selected device."
        )
        self.banner_runtime_box_button.setText("Install runtime")
        set_style_property(self.banner_runtime_box_button, "tone", "primary")
        set_style_property(self.banner_runtime_box, "tone", "missing")
        self.banner_runtime_box.setVisible(True)
        repolish(self.banner_runtime_box_button)

    def _on_banner_runtime_clicked(self) -> None:
        comp_id = self._banner_component_id
        if not comp_id:
            return
        if self._banner_action_mode == "install":
            self._component_progress[comp_id] = ("Starting", 0, 0)
            self._update_context_banner()
            self.component_install_requested.emit(comp_id)
        elif self._banner_action_mode == "cancel":
            self.component_cancel_requested.emit(comp_id)
        else:
            self.focus_component(comp_id)

    # ---- inspector (on-demand slide-over) ----

    def inspector_docked(self) -> bool:
        """The profile inspector slides over on demand rather than docking empty."""
        return False

    def _position_inspector(self) -> None:
        """Float the slide-over profile over the catalog column's right edge."""
        if not self._inspector_open or self._active_rail_key == "components":
            self.inspector.setVisible(False)
            return
        origin = self.catalog_column.mapTo(self, QPoint(0, 0))
        width = min(self.INSPECTOR_WIDTH, max(240, self.catalog_column.width()))
        self.inspector.setFixedWidth(width)
        self.inspector.setGeometry(
            QRect(
                origin.x() + self.catalog_column.width() - width,
                origin.y(),
                width,
                max(120, self.catalog_column.height()),
            )
        )
        self.inspector.setVisible(True)
        self.inspector.raise_()

    def close_inspector(self) -> None:
        """Hide the slide-over profile and clear the row selection highlight."""
        self._inspector_open = False
        self.inspector.setVisible(False)
        for row in self.rows.values():
            if row.property("selected"):
                set_style_property(row, "selected", False)

    def _on_row_details_requested(self, model_name: str) -> None:
        if self._inspector_open and self._selected_model == model_name:
            self.close_inspector()
            return
        self.select_model(model_name)

    def _build_inspector(self) -> QWidget:
        """Build the slide-over technical profile panel."""
        panel = QFrame()
        panel.setObjectName("downloadsInspector")
        panel.setFixedWidth(self.INSPECTOR_WIDTH)
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(10)

        eyebrow_row = QHBoxLayout()
        eyebrow_row.setContentsMargins(0, 0, 0, 0)
        eyebrow = QLabel("Selected model")
        eyebrow.setObjectName("settingsTileGroupTitle")
        eyebrow_row.addWidget(eyebrow)
        eyebrow_row.addStretch()
        self.inspector_close_button = QPushButton("✕")
        self.inspector_close_button.setObjectName("downloadsInspectorClose")
        self.inspector_close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.inspector_close_button.setToolTip("Close the model profile")
        self.inspector_close_button.setFixedSize(26, 26)
        self.inspector_close_button.clicked.connect(self.close_inspector)
        eyebrow_row.addWidget(self.inspector_close_button)
        outer.addLayout(eyebrow_row)

        self.inspector_engine = QLabel("")
        self.inspector_engine.setObjectName("downloadsInspectorEngine")
        self.inspector_engine.setVisible(False)
        outer.addWidget(self.inspector_engine, alignment=Qt.AlignmentFlag.AlignLeft)

        self.inspector_name = ElidingLabel("")
        self.inspector_name.setObjectName("downloadsInspectorName")
        outer.addWidget(self.inspector_name)

        # Shown instead of the profile until a row is picked.
        self.inspector_empty = EmptyState(
            "No model selected", "Select a model to see its technical profile."
        )
        self.inspector_empty.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        outer.addWidget(self.inspector_empty)

        self.inspector_tags = ElidingLabel("")
        self.inspector_tags.setObjectName("downloadsInspectorTags")
        self.inspector_tags.setVisible(False)
        outer.addWidget(self.inspector_tags, alignment=Qt.AlignmentFlag.AlignLeft)

        # The profile prose is open-ended, so this column scrolls. The catalog
        # list beside it keeps its own scroller; the window chrome does not.
        detail_scroll = QScrollArea()
        detail_scroll.setObjectName("downloadsInspectorScroll")
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        detail_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        detail_scroll.setMinimumHeight(60)
        self.inspector_detail_scroll = detail_scroll

        content = QWidget()
        content.setObjectName("downloadsInspectorContent")
        content.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        detail = QVBoxLayout(content)
        detail.setContentsMargins(0, 0, 6, 0)
        detail.setSpacing(12)

        self.inspector_notice = WrappedLabel("")
        self.inspector_notice.setObjectName("downloadsLicenseNotice")
        self.inspector_notice.setVisible(False)
        detail.addWidget(self.inspector_notice)

        self.inspector_description = WrappedLabel("")
        self.inspector_description.setObjectName("downloadsInspectorBody")
        detail.addWidget(self.inspector_description)

        self.inspector_facts = QGridLayout()
        self.inspector_facts.setContentsMargins(0, 0, 0, 0)
        self.inspector_facts.setHorizontalSpacing(12)
        self.inspector_facts.setVerticalSpacing(5)
        self.inspector_facts.setColumnStretch(1, 1)
        self.fact_labels: Dict[str, QLabel] = {}
        detail.addLayout(self.inspector_facts)

        self.inspector_best_for_heading = QLabel("Best for")
        self.inspector_best_for_heading.setObjectName("downloadsSectionTitle")
        self.inspector_best_for = WrappedLabel("")
        self.inspector_best_for.setObjectName("downloadsInspectorBody")
        detail.addWidget(self.inspector_best_for_heading)
        detail.addWidget(self.inspector_best_for)

        self.inspector_tradeoffs_heading = QLabel("Tradeoffs")
        self.inspector_tradeoffs_heading.setObjectName("downloadsSectionTitle")
        self.inspector_tradeoffs = WrappedLabel("")
        self.inspector_tradeoffs.setObjectName("downloadsInspectorBody")
        detail.addWidget(self.inspector_tradeoffs_heading)
        detail.addWidget(self.inspector_tradeoffs)

        self.inspector_source_note = WrappedLabel(
            "Technical figures are bundled from the linked upstream model "
            "cards. Speed and memory vary with hardware, compute type, audio, "
            "and decoding settings."
        )
        from services.model_catalog import MODEL_SECURITY_NOTICE
        self.inspector_source_note.setText(self.inspector_source_note.text() + "\n\n" + MODEL_SECURITY_NOTICE)
        self.inspector_source_note.setObjectName("downloadsSourceNote")
        detail.addWidget(self.inspector_source_note)
        detail.addStretch()

        detail_scroll.setWidget(content)
        outer.addWidget(detail_scroll, stretch=1)

        self.inspector_usage = QLabel("")
        self.inspector_usage.setObjectName("downloadsInspectorUsage")
        self.inspector_usage.setVisible(False)
        outer.addWidget(self.inspector_usage)

        self.inspector_repo_button = Button("Hugging Face ↗")
        self.inspector_repo_button.setObjectName("downloadsRepoButton")
        fit_compact_button(self.inspector_repo_button, 0)
        self.inspector_repo_button.clicked.connect(self._open_repository)

        self.inspector_origin_button = Button("Original ↗")
        self.inspector_origin_button.setObjectName("downloadsOriginButton")
        fit_compact_button(self.inspector_origin_button, 0)
        self.inspector_origin_button.clicked.connect(self._open_origin)

        self.inspector_license_button = Button("License ↗")
        self.inspector_license_button.setObjectName("downloadsLicenseButton")
        fit_compact_button(self.inspector_license_button, 0)
        self.inspector_license_button.clicked.connect(self._open_license)
        # The 300 px column cannot hold three labels side by side at larger font
        # scales; the row wraps rather than letting the buttons overlap.
        self.inspector_links = ButtonRow(
            (
                self.inspector_repo_button,
                self.inspector_origin_button,
                self.inspector_license_button,
            )
        )
        outer.addWidget(self.inspector_links)
        # Takes the spare height only while the profile (stretch 1) is hidden,
        # so the empty state stays at the top.
        outer.addStretch(0)

        self._details: Optional[ModelDetails] = None
        self._set_inspector_enabled(False)
        return panel

    def _build_components_section(self) -> QScrollArea:
        """The optional components, on their own rail view with room to breathe."""
        scroll = _CompactScrollArea()
        scroll.setObjectName("downloadsComponentsScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        container = QWidget()
        container.setObjectName("downloadsComponentsContainer")
        column = QVBoxLayout(container)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(self._build_component_strip())
        column.addStretch()
        scroll.setWidget(container)
        self.components_scroll = scroll
        return scroll

    def _build_component_strip(self) -> QWidget:
        """Build the optional-components strip.

        Components live here rather than in Settings because this window is
        deliberately non-modal — a multi-gigabyte download must not lock the
        user out of the app — and because Settings commits on accept, which
        does not compose with an in-flight install.

        Returns a hidden widget on platforms with no installable components, so
        no heading advertises a section with nothing in it.
        """
        strip = QFrame()
        strip.setObjectName("downloadsComponentStrip")
        layout = QVBoxLayout(strip)
        layout.setContentsMargins(0, 0, 0, 12)
        layout.setSpacing(8)

        infos = component_coordinator.list_components()
        if not infos:
            strip.setVisible(False)
            return strip

        banner = QFrame()
        banner.setObjectName("downloadsContextBanner")
        banner_layout = QVBoxLayout(banner)
        banner_layout.setContentsMargins(12, 10, 12, 10)
        banner_layout.setSpacing(2)
        title = QLabel("Runtimes & add-ons")
        title.setObjectName("downloadsBannerTitle")
        banner_layout.addWidget(title)
        caption = WrappedLabel(
            "Speech runtimes and optional add-ons, downloaded on demand so the installer stays small."
        )
        caption.setObjectName("downloadsBannerSubtitle")
        banner_layout.addWidget(caption)
        layout.addWidget(banner)

        for info in infos:
            row = ComponentRowWidget(info.component_id)
            row.install_clicked.connect(self.component_install_requested)
            row.cancel_clicked.connect(self.component_cancel_requested)
            row.remove_clicked.connect(self._confirm_component_removal)
            row.details_requested.connect(self._show_component_details)
            self._component_rows[info.component_id] = row
            layout.addWidget(row)

        return strip

    # ---- inspector ----

    def select_model(self, model_name: str) -> None:
        """Show one catalog model's bundled profile in the slide-over inspector."""
        if model_name == "auto" or model_name not in self.rows:
            return
        from services.local_asr.catalog import family_of

        row_backend = self.rows[model_name].backend
        row_family = family_of(row_backend)
        if self._active_rail_key == row_family and self._family_backend.get(row_family) != row_backend:
            self._family_backend[row_family] = row_backend
            self._update_context_banner()
        self._selected_model = model_name
        self._details = (custom_model_details(model_name) if is_custom_model(model_name)
                         else get_model_details(model_name))
        self._render_inspector()
        dictation_resolved, meeting_model, _loaded = self._models_in_use(
            self._settings_snapshot()
        )
        self.inspector_usage.setText(
            self._usage_for(model_name, dictation_resolved, meeting_model)
        )
        self.inspector_usage.setVisible(bool(self.inspector_usage.text()))
        for name, row in self.rows.items():
            set_style_property(row, "selected", name == model_name)
        self._inspector_open = True
        self._position_inspector()

    def reveal_model(self, model_name: str) -> None:
        """Switch to the model's family, clear blocking filters, select it, and scroll to it."""
        self.show_local()
        row = self.rows.get(model_name)
        if row is None:
            return
        family = _rail_key_for(row.backend)
        if self._active_rail_key not in ("all", family):
            self.select_rail(row.backend)
        if row.isHidden():
            self.filter_edit.clear()
            self.status_filter_combo.setCurrentIndex(0)
            if row.isHidden():
                self.select_rail(row.backend)
        self.select_model(model_name)
        self.library_scroll_area.ensureWidgetVisible(row, 0, 12)

    def show_backend(self, backend: str) -> None:
        """Select one backend in the Engine Rail; an empty value shows all models."""
        self.show_local()
        self.select_rail(backend or "all")

    def _render_inspector(self) -> None:
        details = self._details
        if details is None:
            self._set_inspector_enabled(False)
            return
        self._set_inspector_enabled(True)
        from services.local_asr.catalog import BACKENDS, MODELS, backend_of
        if details.model_name in MODELS:
            name = MODELS[details.model_name].label
        elif is_custom_model(details.model_name):
            name = custom_model_label(details.model_name)
        else:
            name = details.model_name
        backend_id = backend_of(details.model_name)
        meta = _engine_meta(backend_id)
        engine_label = BACKENDS.get(backend_id, "Whisper")
        self.inspector_engine.setText(engine_label)
        self.inspector_engine.setStyleSheet(
            f"QLabel#downloadsInspectorEngine {{"
            f" background-color: rgba({meta.rgb}, 0.14);"
            f" color: rgb({meta.rgb});"
            f" border: 1px solid rgba({meta.rgb}, 0.30);"
            f" border-radius: 6px; padding: 2px 8px;"
            f" font-size: 10px; font-weight: 600;"
            f"}}"
        )
        self.inspector_engine.setVisible(True)
        self.inspector_name.setText(name)
        self.inspector_name.setToolTip(details.model_name)
        self.inspector_tags.setText(details.compact_tags)
        self.inspector_tags.setVisible(bool(details.compact_tags))
        self.inspector_notice.setText(details.notice)
        self.inspector_notice.setVisible(bool(details.notice))
        self.inspector_description.setText(details.description)
        self.inspector_best_for.setText(details.best_for)
        self.inspector_tradeoffs.setText(
            "\n".join(f"\u2022 {item}" for item in details.limitations)
        )
        on_hugging_face = "huggingface.co/" in details.repository_url
        self.inspector_repo_button.setText("Hugging Face ↗" if on_hugging_face else "Repository ↗")
        self.inspector_repo_button.setToolTip(details.repository_url)
        self.inspector_origin_button.setToolTip(details.origin_url)
        self.inspector_license_button.setToolTip(details.license_url or details.origin_url)
        self.inspector_links.refresh()
        memory = details.memory_guidance
        if details.model_name not in MODELS:
            # For this computer's card: the compute type it will really run.
            from services import gpu_info

            memory = gpu_info.memory_guidance(details.model_name, gpu_info.nvidia_gpu()) or memory

        rows = (
            ("Origin", details.origin_name),
            ("Repository", details.repository_id),
            ("Maintainer", details.maintainer),
            ("Family", details.family),
            ("Languages", details.language_support),
            ("Tasks", details.task_support),
            ("Parameters", details.parameter_count),
            ("Published speed", details.relative_performance),
            ("Memory guidance", memory),
            ("Download size", details.download_size),
            ("Local format", details.runtime_format),
            ("License", details.license),
            ("Download host", ", ".join(details.download_hosts) or "Local folder"),
            ("Version", details.revision or "Selected by model source"),
            ("Download checks", details.verification),
        )
        while self.inspector_facts.count():
            item = self.inspector_facts.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        self.fact_labels.clear()
        for index, (caption, value) in enumerate(rows):
            caption_label = QLabel(caption)
            caption_label.setObjectName("downloadsFactLabel")
            caption_label.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
            )
            # Values include commit hashes and local model folders, which have
            # nowhere to wrap unless told they may.
            value_label = WrappedLabel(value, break_long_words=True)
            value_label.setObjectName("downloadsFactValue")
            value_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            self.inspector_facts.addWidget(caption_label, index, 0)
            self.inspector_facts.addWidget(value_label, index, 1)
            self.fact_labels[caption] = value_label

    def _set_inspector_enabled(self, enabled: bool) -> None:
        if not enabled:
            self.inspector_engine.setVisible(False)
            self.inspector_name.setText("")
            self.inspector_description.setText("")
            self.inspector_tags.setVisible(False)
            self.inspector_best_for.setText("")
            self.inspector_tradeoffs.setText("")
        self.inspector_empty.setVisible(not enabled)
        self.inspector_engine.setVisible(enabled)
        self.inspector_name.setVisible(enabled)
        self.inspector_detail_scroll.setVisible(enabled)
        for widget in (
            self.inspector_best_for_heading,
            self.inspector_best_for,
            self.inspector_tradeoffs_heading,
            self.inspector_tradeoffs,
            self.inspector_source_note,
            self.inspector_repo_button,
            self.inspector_origin_button,
            self.inspector_license_button,
        ):
            widget.setVisible(enabled)
        self.inspector_links.refresh()

    def _open_repository(self) -> None:
        if self._details is not None:
            QDesktopServices.openUrl(QUrl(self._details.repository_url))

    def _open_license(self) -> None:
        if self._details is not None:
            QDesktopServices.openUrl(QUrl(self._details.license_url or self._details.origin_url))

    def _open_origin(self) -> None:
        if self._details is not None:
            QDesktopServices.openUrl(QUrl(self._details.origin_url))

    def _on_open_cache_folder(self) -> None:
        from services.local_asr.catalog import MODELS
        from services.local_asr.cache import active_model_dir, is_linked, model_dir
        speech = _custom_speech(self._selected_model)
        if speech is not None:
            source = Path(speech.path)
            path = str(source if source.is_dir() else source.parent)
        elif self._selected_model in MODELS:
            if is_linked(self._selected_model):
                path = str(active_model_dir(self._selected_model))
            else:
                path = str(model_dir(self._selected_model).parent)
        else:
            path = get_hf_cache_dir()
        if self._selected_model and is_custom_model(self._selected_model):
            info = custom_model_cache_info(self._selected_model)
            if info is not None:
                path = info.path
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    # ---- catalog actions ----

    def _show_add_custom_menu(self) -> None:
        """Import found models first, then Whisper (CTranslate2) or a local speech model."""
        from PyQt6.QtWidgets import QMenu
        from services.local_asr.custom import runtime_format_options

        menu = QMenu(self)
        menu.addAction("Import existing models…", self._open_import_dialog)
        menu.addSeparator()
        menu.addAction("Whisper model (CTranslate2)…", self._add_custom_models)
        if runtime_format_options():
            menu.addAction(
                "Local speech model (GGUF / ORT / Safetensors)…",
                lambda: self._add_custom_speech_model(None),
            )
        button = self.custom_models_button
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))
        menu.deleteLater()

    def _add_custom_models(self) -> None:
        cached = peek_cached_models() or {}
        added = add_custom_models(self, settings_manager, cached)
        if added:
            self.refresh()
            self.message_label.setText(
                f"Added {len(added)} custom models. Choose one in Voice model or Voice & speakers to load it."
            )

    def _add_custom_speech_model(self, backend: Optional[str] = None) -> None:
        from services.local_asr.catalog import BACKENDS, BUILTIN_MODELS, MODELS
        key = add_custom_speech_model(self, backend)
        if key:
            invalidate_cached_models_snapshot()
            self.refresh()
            model = MODELS[key]
            owner = BACKENDS.get(model.backend, "Local")
            if key in BUILTIN_MODELS:
                self.message_label.setText(
                    f"Linked {model.label} for {owner} in place (no duplicate download)."
                )
            else:
                self.message_label.setText(
                    f"Added {model.label}. Choose it under {owner} or the Local model picker to load it."
                )

    def _open_import_dialog(self) -> None:
        """Open the import screen scanning default model folders."""
        dialog = ImportModelsDialog(discovered=None, parent=self, run_async=self._background_cache_scan)
        try:
            self._run_import_dialog(dialog)
        finally:
            dialog.deleteLater()

    def _open_import_dialog_from_banner(self) -> None:
        """Open the import screen pre-populated with the banner's discovered models."""
        dialog = ImportModelsDialog(
            discovered=list(self._discovered_models),
            parent=self,
            run_async=self._background_cache_scan,
        )
        try:
            self._run_import_dialog(dialog)
        finally:
            dialog.deleteLater()

    def _run_import_dialog(self, dialog: ImportModelsDialog) -> None:
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        result = dialog.import_result
        if accepted and result is not None and result.imported:
            imported_ids = {item.id for item in result.imported}
            skipped_ids = [item.id for item in dialog.discovered_items if item.id not in imported_ids]
            if skipped_ids:
                dismiss_discoveries(skipped_ids)
            self._discovered_models = [
                item for item in self._discovered_models
                if item.id not in imported_ids and item.id not in skipped_ids
            ]
            self._update_import_banner()
            invalidate_cached_models_snapshot()
            self.refresh()
            count = len(result.imported)
            noun = "model" if count == 1 else "models"
            reused = (
                f" ({format_size_bytes(result.reused_bytes)} reused without downloading)"
                if result.reused_bytes else ""
            )
            if result.failed:
                self.message_label.setText(
                    f"Imported {count} {noun}{reused}; {len(result.failed)} failed."
                )
            else:
                self.message_label.setText(f"Imported {count} {noun}{reused}.")
        elif not accepted and dialog.discovered_items:
            # User clicked "Skip for now": remember these discoveries so the
            # first-time alert does not nag on every visit.
            dismiss_discoveries([item.id for item in dialog.discovered_items])
            self._discovered_models = []
            self._update_import_banner()

    def _dismiss_import_banner(self) -> None:
        if self._discovered_models:
            dismiss_discoveries([item.id for item in self._discovered_models])
        self._discovered_models = []
        self._update_import_banner()

    def _update_import_banner(self) -> None:
        if not self._ui_built:
            return
        items = self._discovered_models
        if not items:
            self.import_banner.setVisible(False)
            return
        count = len(items)
        noun = "model" if count == 1 else "models"
        total_bytes = sum(item.size_bytes for item in items)
        size_part = f" ({format_size_bytes(total_bytes)})" if total_bytes else ""
        self.import_banner_title.setText(
            f"Found {count} existing speech {noun} on this computer{size_part}"
        )
        sources = sorted({item.source_label.split(" (", 1)[0] for item in items})
        if sources:
            src_text = ", ".join(sources[:2]) + (" and more" if len(sources) > 2 else "")
            self.import_banner_note.setText(
                f"From {src_text} · Reuse in place without downloading again."
            )
        else:
            self.import_banner_note.setText("Reuse them in place without downloading again.")
        self.import_banner.setVisible(True)

    def run_import_discovery(self, *, force: bool = False) -> None:
        """Check default folders for reusable speech models to populate the alert banner."""
        if self._discovery_started and not force:
            return
        self._discovery_started = True
        generation = self._discovery_generation = next(_tokens)
        if not self._background_cache_scan:
            try:
                found = discover_existing_models()
            except Exception:
                logger.debug("Local model discovery failed", exc_info=True)
                found = []
            self._on_discovery_finished(generation, found)
            return

        finished = _scan_relay().discovery_finished
        discover = discover_existing_models

        def scan() -> None:
            try:
                found = discover()
            except Exception:
                logger.debug("Local model discovery failed", exc_info=True)
                found = []
            finished.emit(generation, found)

        threading.Thread(target=scan, name="downloads-import-discovery", daemon=True).start()

    def _on_discovery_finished(self, generation: int, discovered) -> None:
        if generation != self._discovery_generation:
            return
        self._discovered_models = unseen_discoveries(discovered or (), self._settings_snapshot())
        self._update_import_banner()

    def _sync_custom_rows(self, settings: dict) -> None:
        from services.local_asr.custom import registered
        names = [*custom_models(settings), *registered()]
        for name in list(self.rows):
            if _user_added(name) and name not in names:
                row = self.rows.pop(name)
                self.list_layout.removeWidget(row)
                row.deleteLater()
                self._selected_models.discard(name)
                if self._selected_model == name:
                    self._selected_model = None
                    self._details = None
                    self._set_inspector_enabled(False)
        for name in names:
            if name in self.rows:
                continue
            row = ModelRowWidget(name)
            row.download_clicked.connect(self._on_download_clicked)
            row.delete_clicked.connect(self._on_delete_clicked)
            row.details_requested.connect(self._on_row_details_requested)
            row.selection_toggled.connect(self._on_selection_toggled)
            self.rows[name] = row
            self.list_layout.insertWidget(len(self.rows) - 1, row)

    def _on_download_clicked(self, model_name: str) -> None:
        if self.on_download_requested:
            self.on_download_requested(model_name)

    def _on_delete_clicked(self, model_name: str) -> None:
        if _custom_speech(model_name) is not None:
            from services.local_asr.custom import unregister
            try:
                unregister(model_name)
            except ValueError as exc:
                self.message_label.setText(friendly_error_message(exc))
                return
            invalidate_cached_models_snapshot()
            self.refresh()
            self.message_label.setText("Removed the custom model from the list. Its files are kept.")
            return
        if is_custom_model(model_name):
            settings = self._settings_snapshot()
            if model_name in (SettingsKey.WHISPER_MODEL.read(settings),
                              resolve_meeting_whisper_model(settings)):
                self.message_label.setText("Choose another model before removing this assignment.")
                return
            def remove(values):
                values[SettingsKey.CUSTOM_WHISPER_MODELS] = [
                    name for name in custom_models(values) if name != model_name]
            settings_manager.mutate_settings(remove)
            self.refresh()
            self.message_label.setText("Removed the custom model from the list. Its files are retained.")
            return
        from services.local_asr.cache import is_linked
        if is_linked(model_name):
            reply = QMessageBox.question(
                self,
                "Unlink Model",
                f'Unlink "{model_name}" from OpenWhisper?\n\n'
                "Your original model files on disk will be kept.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes and self.on_delete_requested:
                self.on_delete_requested(model_name)
            return
        reply = QMessageBox.question(
            self,
            "Delete Model",
            f'Delete the downloaded files for "{model_name}"?\n\n'
            "You can download the model again later.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes and self.on_delete_requested:
            self.on_delete_requested(model_name)

    # ---- batch selection ----

    def _on_selection_toggled(self, model_name: str, checked: bool) -> None:
        if checked:
            self._selected_models.add(model_name)
        else:
            self._selected_models.discard(model_name)
        self._update_selection_bar()

    def _on_select_all_clicked(self) -> None:
        """Check every missing row in the active rail scope (cached excluded)."""
        missing = set(self._scoped_missing_names())
        for row in self.rows.values():
            if row.model_name in missing:
                blocked = row.select_checkbox.blockSignals(True)
                try:
                    row.select_checkbox.setChecked(True)
                finally:
                    row.select_checkbox.blockSignals(blocked)
                self._selected_models.add(row.model_name)
        self._update_selection_bar()

    def _on_clear_selection_clicked(self) -> None:
        for row in self.rows.values():
            if row.select_checkbox.isChecked():
                blocked = row.select_checkbox.blockSignals(True)
                try:
                    row.select_checkbox.setChecked(False)
                finally:
                    row.select_checkbox.blockSignals(blocked)
        self._selected_models.clear()
        self._update_selection_bar()

    def _selected_names(self) -> List[str]:
        """Selection in catalog order — the order the batch downloads in."""
        return [name for name in self.rows if name in self._selected_models]

    def _missing_names(self) -> List[str]:
        """Every catalog model without local files, in catalog order."""
        return [name for name, row in self.rows.items() if not row.is_cached
                and not _from_local_files(name)]

    def _scoped_missing_names(self) -> List[str]:
        """Missing models belonging to the active rail scope."""
        from services.local_asr.catalog import family_of

        key = self._active_rail_key
        missing = self._missing_names()
        if key in ("all", "components"):
            return missing
        if key == "installed":
            return []
        return [name for name in missing if family_of(self.rows[name].backend) == key]

    def _download_slot_busy(self) -> bool:
        return self._downloading_model is not None or bool(self._batch_queue)

    def _update_selection_bar(self) -> None:
        names = self._selected_names()
        count = len(names)
        sizes = [self.rows[name].sort_size_bytes for name in names]
        total_bytes = sum(size for size in sizes if math.isfinite(size))
        estimate = (f"~{format_size_bytes(total_bytes)} to download"
                    if all(math.isfinite(size) for size in sizes) else "download size unknown")
        busy = self._download_slot_busy()
        any_missing = bool(self._missing_names())
        any_scoped = bool(self._scoped_missing_names())
        any_selectable = any_missing and not busy and not self._downloads_blocked
        scoped_selectable = any_scoped and not busy and not self._downloads_blocked
        self.selection_bar.setVisible(
            self._active_rail_key != "installed" or bool(names)
        )
        # Nothing ticked: say what the bar is for, or the disabled button
        # reads as broken.
        self.selection_summary.setText(
            f"{count} selected · {estimate}" if count
            else "Tick models to download several at once" if scoped_selectable
            else ""
        )
        noun = "model" if count == 1 else "models"
        self.download_selected_button.setText(
            f"Download {count} {noun}…" if count else "Download selected"
        )
        selectable = bool(names) and not busy and not self._downloads_blocked
        self.download_selected_button.setEnabled(selectable)
        self.download_selected_button.setToolTip(
            "" if selectable
            else "Downloads are disabled by HF_HUB_OFFLINE" if self._downloads_blocked
            else "Wait for the current download to finish" if busy
            else "Tick the models to download first"
        )
        self.clear_selection_button.setEnabled(bool(names) and not busy)
        self.select_all_button.setEnabled(scoped_selectable)
        self.download_all_button.setEnabled(any_selectable)
        self.download_all_button.setVisible(any_missing)

    def _request_batch(self, model_names: List[str]) -> None:
        """Confirm sizes and destination, then hand the plan to the app."""
        dialog = BatchDownloadDialog(model_names, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            if self.on_batch_download_requested:
                self.on_batch_download_requested(list(model_names))

    def _on_download_selected_clicked(self) -> None:
        names = self._selected_names()
        if names:
            self._request_batch(names)

    def _on_download_all_clicked(self) -> None:
        names = self._missing_names()
        if names:
            self._request_batch(names)

    def _on_stop_batch_clicked(self) -> None:
        if self.on_batch_cancel_requested:
            self.on_batch_cancel_requested()

    def focus_component(self, component_id: str) -> None:
        """Bring a required runtime and its install action into view."""
        self.show_local()
        if component_id not in self._component_rows:
            return
        self.select_rail("components")
        for cid, row in self._component_rows.items():
            set_style_property(row, "selected", cid == component_id)
        row = self._component_rows[component_id]
        # The section was just shown: lay it out so the row has a position to scroll to.
        self.components_scroll.widget().layout().activate()
        self.components_scroll.ensureWidgetVisible(row, 0, 12)
        row.setFocus(Qt.FocusReason.OtherFocusReason)

    def _show_component_details(self, component_id: str) -> None:
        """Open the bundled profile popup for one component row."""
        if component_id not in self._component_rows:
            return
        self.focus_component(component_id)
        dialog = ComponentDetailsDialog(component_id, self)
        dialog.exec()
        selected = self._component_rows.get(component_id)
        if selected is not None:
            set_style_property(selected, "selected", False)

    def _confirm_component_removal(self, component_id: str) -> None:
        """Ask before deleting a multi-gigabyte component."""
        info = component_coordinator.describe(component_id)
        confirmed = QMessageBox.question(
            self,
            "Remove component",
            f"Remove {info.display_name} "
            f"({format_size_bytes(info.install_bytes)})?\n\n"
            "You can install it again later.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmed == QMessageBox.StandardButton.Yes:
            self.component_remove_requested.emit(component_id)

    # ---- public state API (driven by UIController) ----

    def refresh(self, scan: bool = True) -> None:
        """Re-read download state.

        Args:
            scan: Rescan the model cache. Without it, the last shared scan is
                reused, so loading a hidden window never touches the disk.
        """
        if not self._background_cache_scan:
            self._cache_inventory_loading = False
            self._refresh_cached_model_state(
                scan_cached_models(max_age_seconds=30.0)
            )
            if scan and self._auto_discover:
                self.run_import_discovery()
            return

        cached = peek_cached_models()
        self._cache_inventory_loading = cached is None and scan
        self._refresh_cached_model_state(cached or {})
        if not scan:
            return
        if self._auto_discover:
            self.run_import_discovery()
        generation = self._cache_scan_generation = next(_tokens)
        # Bound here: the worker must not reach back into the page.
        scan_cache = scan_cached_models
        finished = _scan_relay().finished

        def load() -> None:
            finished.emit(generation, scan_cache(max_age_seconds=30.0))

        threading.Thread(
            target=load,
            name="downloads-cache-scan",
            daemon=True,
        ).start()

    def _on_cache_scan_finished(self, generation: int, cached) -> None:
        if generation != self._cache_scan_generation:
            return
        self._cache_inventory_loading = False
        self._refresh_cached_model_state(dict(cached or {}))

    def _refresh_cached_model_state(
        self,
        cached: Dict[str, CachedModelInfo],
    ) -> None:
        from services.local_asr.cache import inventory
        cached = {**cached, **inventory()}
        self._inventory = dict(cached)
        settings = self._settings_snapshot()
        if not self._ui_built:
            self._record_cached_sizes(cached, settings)
            self.inventory_changed.emit()
            return
        self._sync_custom_rows(settings)
        dictation_resolved, meeting_model, loaded_model = self._models_in_use(settings)
        loaded_repo = resolve_model_repo(loaded_model) if loaded_model else None
        downloads_blocked = is_hf_hub_offline_env_set()
        self._downloads_blocked = downloads_blocked
        self.env_banner.setVisible(downloads_blocked)

        queued_names = set(self._batch_queue)
        slot_busy = self._download_slot_busy()
        context = _RowContext(
            loaded_model=loaded_model,
            loaded_repo=loaded_repo,
            dictation_model=dictation_resolved,
            meeting_model=meeting_model,
            downloads_blocked=downloads_blocked,
            slot_busy=slot_busy,
            selection_locked=(
                slot_busy or downloads_blocked or self._cache_inventory_loading
            ),
            queued_names=queued_names,
        )
        seen_repos: Dict[str, CachedModelInfo] = {}
        self._cached_sizes = {}
        for model_name, row in self.rows.items():
            info = (custom_model_cache_info(model_name) if is_custom_model(model_name)
                    else cached.get(row.repo_id))
            if info is not None:
                seen_repos[info.repo_id] = info
                self._cached_sizes[model_name] = info.size_bytes
            self._refresh_row(model_name, row, info, context)

        self._show_cache_totals(seen_repos)
        self._refresh_rail_counts()
        self.refresh_components()
        self._update_context_banner()
        self._apply_filter(self.filter_edit.text())
        if self._selected_model:
            self.inspector_usage.setText(
                self._usage_for(
                    self._selected_model, dictation_resolved, meeting_model
                )
            )
            self.inspector_usage.setVisible(bool(self.inspector_usage.text()))
        self.stop_batch_button.setVisible(bool(self._batch_queue))
        self._update_selection_bar()
        if self._cache_inventory_loading:
            self.select_all_button.setEnabled(False)
            self.download_all_button.setEnabled(False)
            self.download_selected_button.setEnabled(False)
        self.inventory_changed.emit()

    def _record_cached_sizes(
        self, cached: Dict[str, CachedModelInfo], settings: dict
    ) -> None:
        """Size every downloaded model without building the catalog rows."""
        from services.local_asr.catalog import MODELS, model_supported_here
        self._cached_sizes = {}
        for name in [*config.WHISPER_MODEL_CHOICES, *MODELS, *custom_models(settings)]:
            if name == "auto":
                continue
            if name in MODELS and not model_supported_here(name):
                continue
            info = custom_model_cache_info(name) if is_custom_model(name) else cached.get(resolve_model_repo(name))
            if info is not None:
                self._cached_sizes[name] = info.size_bytes

    def _models_in_use(self, settings: dict):
        """Return ``(dictation model, meeting model, loaded model)``.

        The dictation model is the loaded one when dictation is set to auto;
        the meeting model is "" when meetings use remote speech.
        """
        active_model = SettingsKey.WHISPER_MODEL.read(settings_manager.load_all_settings())
        if active_model not in [*config.WHISPER_MODEL_CHOICES, *custom_models(settings)]:
            active_model = config.DEFAULT_WHISPER_MODEL
        from services.local_asr.catalog import BACKENDS, selected_model
        selected_backend = SettingsKey.SELECTED_MODEL.read(settings)
        if selected_backend in BACKENDS:
            active_model = selected_model(selected_backend, settings)
        meeting_model = resolve_meeting_whisper_model(settings)
        from services.settings import resolve_meeting_asr_source
        if resolve_meeting_asr_source(settings) == "remote":
            meeting_model = ""  # Local downloads are not serving meeting speech.
        loaded_model = self._get_loaded_model() if self._get_loaded_model else None
        dictation_resolved = active_model
        if active_model == "auto" and loaded_model:
            dictation_resolved = loaded_model
        return dictation_resolved, meeting_model, loaded_model

    def _refresh_row(
        self,
        model_name: str,
        row: ModelRowWidget,
        info: Optional[CachedModelInfo],
        context: _RowContext,
    ) -> None:
        from services.local_asr.cache import needs_qwen_redownload
        from services.whisper_sources import model_revision
        revision = model_revision(model_name)
        row.update_state(
            _current_cache_info(info, revision),
            is_active=False,
            is_loaded=(row.repo_id == context.loaded_repo),
            downloading=(model_name == self._downloading_model),
            downloads_blocked=context.downloads_blocked,
            download_slot_busy=context.slot_busy or self._cache_inventory_loading,
            queued=(model_name in context.queued_names
                    and model_name != self._downloading_model),
            selection_enabled=not context.selection_locked,
        )
        # Assignment lives on each feature's page; this one only downloads.
        row.set_active_button.setVisible(False)
        if _user_added(model_name):
            self._show_custom_row_actions(model_name, row, info, context)
        usage = self._usage_for(model_name, context.dictation_model, context.meeting_model)
        if info is None and needs_qwen_redownload(model_name):
            usage = "New GGUF download required" + (f" · {usage}" if usage else "")
        row.set_usage(usage)
        if info is not None or _from_local_files(model_name):
            # A model that gained files elsewhere must not stay checked.
            row.select_checkbox.setChecked(False)
            self._selected_models.discard(model_name)

    @staticmethod
    def _show_custom_row_actions(
        model_name: str,
        row: ModelRowWidget,
        info: Optional[CachedModelInfo],
        context: _RowContext,
    ) -> None:
        local = _from_local_files(model_name)
        row.delete_button.setText("Remove")
        row.delete_button.setVisible(True)
        row.delete_button.setToolTip("Remove from the model list; keep source files")
        in_use = (context.dictation_model, context.meeting_model, context.loaded_model)
        row.delete_button.setEnabled(not context.slot_busy and model_name not in in_use)
        if local:
            row.download_button.setVisible(False)
            row.select_checkbox.setVisible(False)
            if info is None:
                speech = _custom_speech(model_name)
                gone = "File" if speech is not None and Path(speech.path).suffix else "Folder"
                row._set_badge(f"{gone} unavailable", "idle")

    def _show_cache_totals(self, seen_repos: Dict[str, CachedModelInfo]) -> None:
        total_bytes = sum(info.size_bytes for info in seen_repos.values())
        if self._cache_inventory_loading:
            self.stats_label.setText(
                f"Checking downloaded models… · {get_hf_cache_dir()}"
            )
        else:
            self.stats_label.setText(
                f"{len(seen_repos)} of {len(self.rows)} speech models · "
                f"{format_size_bytes(total_bytes)} used · {get_hf_cache_dir()}"
            )

    # ---- summaries read by Settings (rail, Overview) ----

    def is_checking(self) -> bool:
        return self._cache_inventory_loading

    def storage_summary(self) -> dict:
        """Downloaded count, catalog size, and bytes per backend label."""
        from services.local_asr.catalog import BACKENDS, MODELS
        by_backend: Dict[str, int] = {}
        for model_name, size in self._cached_sizes.items():
            if self._ui_built:
                backend = self.rows[model_name].backend
            elif model_name in MODELS:
                backend = MODELS[model_name].backend
            else:
                backend = "local_whisper"
            label = BACKENDS.get(backend, "Whisper")
            by_backend[label] = by_backend.get(label, 0) + size
        if self._ui_built:
            total = len(self.rows)
        else:
            from services.local_asr.catalog import model_supported_here
            whisper_models = [name for name in config.WHISPER_MODEL_CHOICES if name != "auto"]
            speech_models = [key for key in MODELS if model_supported_here(key)]
            total = len(whisper_models) + len(speech_models) + len(custom_models(self._settings_snapshot()))
        return {
            "downloaded": len(self._cached_sizes),
            "total": total,
            "bytes": sum(self._cached_sizes.values()),
            "by_backend": by_backend,
        }

    def rail_value(self) -> str:
        """Download progress while one runs, otherwise the cache totals."""
        if self._downloading_model:
            name = self._display_name(self._downloading_model)
            if self._download_fraction is not None:
                return f"Downloading {name} · {round(self._download_fraction * 100)}%"
            return f"Downloading {name}…"
        if self._cache_inventory_loading:
            return "Checking downloaded models…"
        summary = self.storage_summary()
        return (
            f"{summary['downloaded']} of {summary['total']} · "
            f"{format_size_bytes(summary['bytes'])}"
        )

    @staticmethod
    def _display_name(model_name: str) -> str:
        from services.local_asr.catalog import MODELS
        return MODELS[model_name].label if model_name in MODELS else model_name

    def refresh_components(self) -> None:
        infos = []
        for component_id, row in self._component_rows.items():
            info = component_coordinator.describe(component_id)
            infos.append(info)
            row.update_state(info, component_coordinator.is_installing(component_id))
        comp_item = self._rail_items.get("components")
        if comp_item is not None and infos:
            installed = sum(info.is_usable for info in infos)
            updates = sum(info.state == ComponentState.UPDATE_AVAILABLE for info in infos)
            count_text = f"{installed}/{len(infos)}"
            if updates:
                count_text += f" · {updates}!"
            comp_item.set_count(count_text, attention=bool(updates))
            tip = f"Runtimes & add-ons · {installed} of {len(infos)} installed"
            if updates:
                tip += f" · {updates} update" + ("s" if updates > 1 else "") + " available"
            comp_item.setToolTip(tip)
        if self._ui_built and self._active_rail_key not in ("all", "installed", "components"):
            self._update_context_banner()

    def set_component_progress(
        self, component_id: str, phase: str, done: int, total: int
    ) -> None:
        self._component_progress[component_id] = (phase, done, total)
        row = self._component_rows.get(component_id)
        if row is not None:
            row.set_progress(phase, done, total)
        if self._ui_built and component_id == self._banner_component_id:
            self._update_context_banner()

    def finish_component_install(
        self, component_id: str, success: bool, message: str
    ) -> None:
        self._component_progress.pop(component_id, None)
        self.refresh_components()
        if message:
            self.message_label.setText(message)

    def set_downloading(self, model_name: str) -> None:
        self._downloading_model = model_name
        self._download_fraction = None
        if self._batch_queue:
            position = self._batch_done + 1
            total = self._batch_done + len(self._batch_queue)
            self.message_label.setText(
                f"Downloading model {position} of {total}: {model_name}…"
            )
        else:
            self.message_label.setText(f'Downloading "{model_name}"…')
        self.refresh()

    def set_download_progress(self, model_name: str, done: int, total: int) -> None:
        row = self.rows.get(model_name)
        if row is not None:
            row.set_progress(done, total)
        if total > 0:
            self.message_label.setText(
                f'Downloading "{model_name}"… '
                f"{format_size_bytes(done)} of {format_size_bytes(total)}"
            )
            previous = self._download_fraction
            self._download_fraction = min(1.0, max(0.0, done / total))
            if previous is None or round(previous * 100) != round(
                self._download_fraction * 100
            ):
                self.progress_changed.emit()

    def finish_download(self, model_name: str, success: bool) -> None:
        if self._downloading_model == model_name:
            self._downloading_model = None
            self._download_fraction = None
        if model_name in self._batch_queue:
            self._batch_queue.remove(model_name)
            self._batch_done += 1
            if not success:
                self._batch_failed += 1
            self.refresh()
            if not self._batch_queue:
                # finish_batch reports the closing summary for the queue.
                return
            self.message_label.setText("")
            return
        elif success:
            self.message_label.setText("")
        else:
            self.message_label.setText(f'Download of "{model_name}" failed')
        self.refresh()

    def begin_batch(self, model_names: List[str]) -> None:
        """Enter batch mode with the controller-approved download plan."""
        self._batch_queue = [name for name in model_names if name in self.rows]
        self._batch_done = 0
        self._batch_failed = 0
        self.message_label.setText(
            f"Downloading {len(self._batch_queue)} models…"
        )
        self.refresh()

    def finish_batch(self, completed: int, planned: int) -> None:
        """Close the batch with a summary of what actually downloaded."""
        failed = self._batch_failed
        self._batch_queue = []
        self._batch_failed = 0
        if completed < planned and not failed:
            self.message_label.setText(
                f"Stopped — downloaded {completed} of {planned} models"
            )
        elif failed or completed < planned:
            self.message_label.setText(
                f"Downloaded {completed} of {planned} models — {failed} failed"
            )
        else:
            noun = "model" if planned == 1 else "models"
            self.message_label.setText(f"Downloaded {planned} {noun}")
        self.refresh()

    def show_delete_result(self, model_name: str, success: bool, error: str) -> None:
        if success:
            self.message_label.setText(f'Deleted "{model_name}"')
        else:
            self.message_label.setText(f"Could not delete: {error}")

    # ---- filtering ----

    def _apply_filter(self, _value=None) -> None:
        from services.local_asr.catalog import family_of, model_runtime_label

        needle = self.filter_edit.text().strip().lower()
        key = self._active_rail_key
        if key == "components":
            return
        status = "downloaded" if key == "installed" else self.status_filter_combo.currentData()
        show_family_badges = key in ("all", "installed")
        multi_runtime_family = (
            not show_family_badges and len(self._supported_family_backends(key)) > 1
        )
        any_visible = False
        rows = sorted(self.rows.values(), key=self._sort_key)
        for index, row in enumerate(rows):
            if self.list_layout.indexOf(row) != index + 1:
                self.list_layout.insertWidget(index + 1, row)
            row_family = family_of(row.backend)
            meta = _engine_meta(row_family)
            if show_family_badges:
                row.set_engine_badge(meta.short, meta.rgb, visible=True)
            elif multi_runtime_family and row_family == key:
                runtime_label = model_runtime_label(row.model_name, backend=row.backend)
                row.set_engine_badge(runtime_label, meta.rgb, visible=True)
            else:
                row.set_engine_badge(meta.short, meta.rgb, visible=False)
            visible = row.matches_filter(needle) if needle else True
            if not show_family_badges:
                visible = visible and row_family == key
            if status == "downloaded":
                visible = visible and row.is_cached
            elif status == "not_downloaded":
                visible = visible and not row.is_cached
            row.setVisible(visible)
            any_visible = any_visible or visible
        self.empty_label.setVisible(not any_visible)
        if not any_visible:
            if key == "installed" and not needle:
                self.empty_label.setText("No models downloaded yet")
            elif key not in ("all", "installed") and needle:
                self.empty_label.setText(
                    f"No matches in {_engine_meta(key).short}"
                )
            else:
                self.empty_label.setText("No models match")
        self.show_all_engines_button.setVisible(not any_visible and key != "all")

    def _sort_key(self, row: ModelRowWidget):
        """Return a stable sort key for the selected built-in ordering."""
        mode = self.sort_combo.currentData()
        name = row.model_name.casefold()
        if mode == "downloaded":
            return (not row.is_cached, name)
        if mode == "size":
            return (row.sort_size_bytes, name)
        if mode == "name":
            return (name,)
        # Recommended: prefer MLX on a supported Mac, then downloaded and size.
        recommended = (not row.is_cached, row.sort_size_bytes, name)
        if mode == "backend":
            order = _engine_order()
            rank = order.index(row.backend) if row.backend in order else len(order)
            return (rank, *recommended)
        return (parakeet_mlx_supported() and row.backend != "parakeet_mlx", *recommended)

    @staticmethod
    def _usage_for(
        model_name: str, dictation_model: str, meeting_model: str
    ) -> str:
        uses = []
        if model_name == dictation_model:
            uses.append("Dictation")
        if model_name == meeting_model:
            uses.append("Meetings")
        return " · ".join(uses)

    @staticmethod
    def _settings_snapshot() -> dict:
        """Load settings, or an empty dict when the store is unavailable."""
        try:
            return settings_manager.load_all_settings()
        except Exception:
            logger.warning("Could not load settings for the Downloads page", exc_info=True)
            return {}

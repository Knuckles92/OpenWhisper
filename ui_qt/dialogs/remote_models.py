"""Model and runtime management for a paired host, embedded in Settings → Models & engines."""

from __future__ import annotations

import itertools
import logging
import threading

from PyQt6.QtCore import QObject, QPoint, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
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

from ui_qt.utils.restyle import repolish, set_style_property
from ui_qt.widgets import (
    Button,
    ElidingComboBox,
    ElidingLabel,
    PrimaryButton,
    WrappedLabel,
)
from ui_qt.widgets.buttons import fit_compact_button, neutral_button
from ui_qt.widgets.download_row import DownloadRow, row_style
from ui_qt.widgets.engine_field import DEVICE_LABELS as _DEVICE_LABELS
from ui_qt.widgets.speech_backend_picker import backend_picker_label

logger = logging.getLogger(__name__)


class _RequestRelay(QObject):
    """Brings host replies to the Qt thread for whichever panes exist then.

    It lives for the session, so a request thread never holds or emits on a
    pane. Holding one let the thread drop the last reference and delete the
    widget off the Qt thread, and emitting on one raced it closing; both crash
    the process. Qt drops a deleted widget's connection.
    """

    #: Token, operation, result, error.
    finished = pyqtSignal(int, str, object, str)


_relay: _RequestRelay | None = None
#: Request tokens, unique across panes because they share the relay.
_tokens = itertools.count(1)


def _request_relay() -> _RequestRelay:
    global _relay
    if _relay is None:
        _relay = _RequestRelay()
    return _relay


_GPU_ENGINE_DEVICES = frozenset({"cuda", "vulkan", "mps", "metal", "gpu"})
_EXTRA_DEVICE_LABELS = {"vulkan": "Vulkan", "mps": "Apple GPU", "metal": "Apple GPU"}


def _device_matches_engine(option_device: str | None, engine_device: str | None) -> bool:
    """Whether a host dependency choice matches the host engine's active device."""
    if not option_device or not engine_device:
        return True
    opt = option_device.lower()
    eng = engine_device.lower()
    if opt == eng:
        return True
    return opt in ("cuda", "auto") and eng in _GPU_ENGINE_DEVICES


def _engine_summary(engine, runtime) -> str:
    """One line naming the host's engine, its device, and any GPU."""
    gpu = runtime.get("gpu") or {}
    hardware = (
        f" · {gpu['name']} ({gpu.get('total_mib', 0) / 1024:g} GB VRAM)"
        if gpu
        else ""
    )
    raw_device = engine.get("device")
    device = _DEVICE_LABELS.get(
        raw_device,
        _EXTRA_DEVICE_LABELS.get(str(raw_device or "").lower(), raw_device or "device not reported"),
    )
    return (
        f"{'Running' if engine.get('available') else 'Not ready'}: "
        f"{engine.get('label') or 'No engine'} · {device}{hardware}"
    )


_HOST_ROW_STYLE = row_style(
    "hostModelRow",
    primary="hostModelActionButton",
    secondary="hostModelSecondaryButton",
    remove="hostModelRemoveButton",
    progress="hostModelRowProgress",
    state_rules="""
    QFrame#hostModelRow[active="true"] {
        background-color: @accent-tint;
        border: 1px solid @accent-tint-border;
    }
    QFrame#hostModelRow[active="true"]:hover {
        background-color: @accent-tint-strong;
        border: 1px solid @accent-tint-border-strong;
    }""",
    extra="""
    QLabel#hostModelRowSize[muted="true"] {
        color: @slate-text-4;
    }
    QLabel#hostModelRowBadge[tone="active"] {
        background-color: rgba(@accent-rgb, 0.14);
        color: @accent-soft;
        border: 1px solid rgba(@accent-rgb, 0.28);
    }
    QLabel#hostModelRowBadge[tone="downloaded"] {
        background-color: rgba(@success-rgb, 0.12);
        color: @success-text-strong;
        border: 1px solid rgba(@success-rgb, 0.28);
    }
    QLabel#hostModelRowBadge[tone="missing"] {
        background-color: rgba(@warning-rgb, 0.14);
        color: @warning-text-strong;
        border: 1px solid rgba(@warning-rgb, 0.32);
    }
    QLabel#hostModelRowEngine {
        border-radius: 5px;
        padding: 1px 6px;
        font-size: 10px;
        font-weight: 600;
    }
    QPushButton#hostModelSecondaryButton {
        background-color: @slate-raised;
        color: @slate-text;
        border: 1px solid @slate-border-strong;
    }
    QPushButton#hostModelSecondaryButton:hover {
        background-color: @slate-raised;
        border: 1px solid @slate-border-hover;
    }
    QPushButton#hostModelSecondaryButton:disabled {
        color: @slate-text-disabled;
        border: 1px solid @slate-border-subtle;
    }
""",
)


class _HostModelRow(DownloadRow):
    """One model card in the paired-host catalog, matching ModelRowWidget."""

    download_clicked = pyqtSignal(str)
    select_clicked = pyqtSignal(str)
    setup_clicked = pyqtSignal(str)

    def __init__(self, key: str, model_entry: dict, parent=None):
        label = model_entry.get("label") or model_entry.get("model") or key
        super().__init__(
            key,
            object_name="hostModelRow",
            style=_HOST_ROW_STYLE,
            tooltip="Click to view details and device options",
            accessible_name=f"{label} host model",
            accessible_description=(
                "Open host model details and device options. Quick actions are separate."
            ),
            parent=parent,
        )
        self.key = key
        self.family = str(model_entry.get("family") or "")
        self.model_name = str(model_entry.get("model") or "")
        self.display_label = label
        self.is_cached = bool(model_entry.get("cached"))
        self.is_active = False
        self._search_blob = ""
        self._setup_ui()
        self._populate_static(model_entry)

    def _setup_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(10)

        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(2)

        top_line = QHBoxLayout()
        top_line.setContentsMargins(0, 0, 0, 0)
        top_line.setSpacing(6)

        self.name_label = ElidingLabel(self.display_label)
        self.name_label.setObjectName("hostModelRowName")
        name_font = QFont()
        name_font.setPointSize(10)
        name_font.setWeight(QFont.Weight.DemiBold)
        self.name_label.setFont(name_font)
        top_line.addWidget(self.name_label)

        self.engine_badge = QLabel("")
        self.engine_badge.setObjectName("hostModelRowEngine")
        self.engine_badge.setVisible(False)
        top_line.addWidget(self.engine_badge, 0, Qt.AlignmentFlag.AlignVCenter)

        self.badge_label = QLabel("")
        self.badge_label.setObjectName("hostModelRowBadge")
        top_line.addWidget(self.badge_label, 0, Qt.AlignmentFlag.AlignVCenter)

        self.size_label = QLabel("")
        self.size_label.setObjectName("hostModelRowSize")
        size_font = QFont()
        size_font.setPointSize(9)
        self.size_label.setFont(size_font)
        top_line.addWidget(self.size_label, 0, Qt.AlignmentFlag.AlignVCenter)
        top_line.addStretch()
        left.addLayout(top_line)

        self.summary_label = ElidingLabel("")
        self.summary_label.setObjectName("hostModelRowSummary")
        summary_font = QFont()
        summary_font.setPointSize(9)
        self.summary_label.setFont(summary_font)
        left.addWidget(self.summary_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("hostModelRowProgress")
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setVisible(False)
        left.addWidget(self.progress_bar)

        layout.addLayout(left, stretch=1)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(6)

        self.action_button = Button("Download")
        self.action_button.setObjectName("hostModelActionButton")
        self.action_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.action_button.clicked.connect(self._on_action_clicked)
        actions.addWidget(self.action_button)

        self.secondary_button = Button("Setup runtime")
        self.secondary_button.setObjectName("hostModelSecondaryButton")
        self.secondary_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.secondary_button.clicked.connect(lambda: self.setup_clicked.emit(self.key))
        self.secondary_button.setVisible(False)
        actions.addWidget(self.secondary_button)

        layout.addLayout(actions)
        self._action_mode = "download"

    def _populate_static(self, entry: dict) -> None:
        summary = ""
        try:
            from services.model_catalog import get_model_details

            profile = get_model_details(self.model_name)
            summary = f"{profile.description} · {profile.language_support}"
        except KeyError:
            pass
        self.summary_label.setText(summary)
        self.summary_label.setVisible(bool(summary))
        self._search_blob = " ".join(
            part for part in (self.display_label, self.model_name, self.family, summary) if part
        ).casefold()

    def set_engine_badge(self, label: str, rgb: str, *, visible: bool) -> None:
        if self.engine_badge.text() != label:
            self.engine_badge.setText(label)
        if visible and getattr(self, "_engine_badge_rgb", None) != rgb:
            self._engine_badge_rgb = rgb
            self.engine_badge.setStyleSheet(
                f"QLabel#hostModelRowEngine {{"
                f" background-color: rgba({rgb}, 0.14);"
                f" color: rgb({rgb});"
                f" border: 1px solid rgba({rgb}, 0.28);"
                f" border-radius: 5px; padding: 1px 6px;"
                f" font-size: 10px; font-weight: 600;"
                f"}}"
            )
        if self.engine_badge.isVisible() != visible:
            self.engine_badge.setVisible(visible)

    def matches_filter(self, needle: str) -> bool:
        return not needle or needle in self._search_blob

    def _on_action_clicked(self) -> None:
        if self._action_mode == "select":
            self.select_clicked.emit(self.key)
        else:
            self.download_clicked.emit(self.key)

    def update_state(
        self,
        entry: dict,
        *,
        is_active: bool,
        ready: bool,
        can_select: bool,
        downloading_this: bool,
        download_job: dict,
        any_downloading: bool,
        busy: bool,
    ) -> None:
        cached = bool(entry.get("cached"))
        self.is_cached = cached
        self.is_active = is_active
        set_style_property(self, "active", is_active)

        size_text = entry.get("download_size") or ""
        if is_active:
            badge_text, badge_tone = "Active on host", "active"
            self.size_label.setText(size_text)
            set_style_property(self.size_label, "muted", False)
        elif downloading_this:
            done, total = download_job.get("done", 0), download_job.get("total", 0)
            pct = f" {min(100, int(100 * done / total))}%" if total > 0 else "…"
            badge_text, badge_tone = f"Downloading{pct}", "downloading"
            self.size_label.setText(size_text)
            set_style_property(self.size_label, "muted", False)
        elif cached and not ready:
            badge_text, badge_tone = "Downloaded · Setup needed", "missing"
            self.size_label.setText(size_text)
            set_style_property(self.size_label, "muted", False)
        elif cached:
            badge_text, badge_tone = "Downloaded", "downloaded"
            self.size_label.setText(size_text)
            set_style_property(self.size_label, "muted", False)
        else:
            badge_text, badge_tone = "Not downloaded", "idle"
            self.size_label.setText(size_text or "Size unknown")
            set_style_property(self.size_label, "muted", True)

        self.badge_label.setText(badge_text)
        set_style_property(self.badge_label, "tone", badge_tone)
        self.size_label.setVisible(bool(self.size_label.text()))

        self.progress_bar.setVisible(downloading_this)
        if downloading_this:
            done, total = download_job.get("done", 0), download_job.get("total", 0)
            self.progress_bar.setRange(0, 100 if total > 0 else 0)
            if total > 0:
                self.progress_bar.setValue(min(100, int(100 * done / total)))

        if is_active:
            self.action_button.setVisible(False)
            self.secondary_button.setVisible(False)
        elif not cached:
            self._action_mode = "download"
            self.action_button.setText("Downloading…" if downloading_this else "Download")
            self.action_button.setEnabled(not busy and not any_downloading)
            self.action_button.setVisible(True)
            self.secondary_button.setVisible(False)
            fit_compact_button(self.action_button, 0)
        elif ready:
            self._action_mode = "select"
            self.action_button.setText("Use on host")
            self.action_button.setEnabled(not busy and can_select)
            self.action_button.setVisible(True)
            self.secondary_button.setVisible(False)
            fit_compact_button(self.action_button, 0)
        else:
            self.action_button.setVisible(False)
            self.secondary_button.setText("Setup runtime")
            self.secondary_button.setEnabled(not busy)
            self.secondary_button.setVisible(True)
            fit_compact_button(self.secondary_button, 0)


class RemoteModelsPane(QWidget):
    """Unified host model & engine manager embedded inside Settings → Models & engines."""

    RAIL_WIDTH = 172
    INSPECTOR_WIDTH = 300

    def __init__(self, service=None, host_name: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("remoteModelsPane")
        self._service = service
        self._host_name = host_name
        self._pairing = service.client_pairing() if service is not None else None
        if not self._host_name and self._pairing is not None:
            self._host_name = self._pairing.host_name
        self._busy = False
        self._valid = False
        self._request_token = 0
        self._catalog: dict = {}
        self._models_by_key: dict[str, dict] = {}
        self._rows: dict[str, _HostModelRow] = {}
        self._rail_items: dict[str, object] = {}
        self._rail_engine_keys: list[str] = []
        self._active_rail_key = "all"
        self._selected_key: str | None = None
        self._inspector_open = False
        self._device_model = None
        self._engine_devices: dict[str, str] = {}
        self._banner_install_dep: dict | None = None
        self._banner_install_model: dict | None = None

        _request_relay().finished.connect(self._on_finished)
        self._poll = QTimer(self)
        self._poll.setInterval(2000)
        self._poll.timeout.connect(lambda: self._request("model_catalog"))

        self._setup_ui()
        self._update_actions()

    # ---- construction ----

    def _setup_ui(self) -> None:
        from ui_qt.dialogs.settings_downloads import _CompactScrollArea

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(8)
        self.engine_label = ElidingLabel("Connecting to host…")
        self.engine_label.setObjectName("downloadsSubtitle")
        header_row.addWidget(self.engine_label, stretch=1)

        self.refresh_button = Button("Refresh")
        self.refresh_button.setObjectName("downloadsToolButton")
        self.refresh_button.setToolTip("Refresh the host's models and runtimes")
        fit_compact_button(self.refresh_button, 0)
        self.refresh_button.clicked.connect(lambda: self._request("model_catalog"))
        header_row.addWidget(self.refresh_button)
        layout.addLayout(header_row)

        split = QHBoxLayout()
        split.setContentsMargins(0, 0, 0, 0)
        split.setSpacing(10)

        self.engine_rail = self._build_engine_rail(_CompactScrollArea)
        split.addWidget(self.engine_rail)

        self.catalog_column = self._build_catalog_column(_CompactScrollArea)
        split.addWidget(self.catalog_column, stretch=1)
        layout.addLayout(split, stretch=1)

        # Footer status & progress (matches local Models & engines footer).
        self.status = WrappedLabel("Reading host models…")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setObjectName("downloadsMessage")
        layout.addWidget(self.status)

        self.download_progress = QProgressBar()
        self.download_progress.setObjectName("downloadsRuntimeProgress")
        self.download_progress.setTextVisible(False)
        self.download_progress.setFixedHeight(6)
        self.download_progress.setAccessibleName("Host model download progress")
        self.download_progress.hide()
        layout.addWidget(self.download_progress)

        self.install_status = WrappedLabel("")
        self.install_status.setTextFormat(Qt.TextFormat.PlainText)
        self.install_status.setObjectName("downloadsMessage")
        self.install_status.hide()
        layout.addWidget(self.install_status)

        self.install_progress = QProgressBar()
        self.install_progress.setObjectName("downloadsRuntimeProgress")
        self.install_progress.setTextVisible(False)
        self.install_progress.setFixedHeight(6)
        self.install_progress.setAccessibleName("Host runtime installation progress")
        self.install_progress.hide()
        layout.addWidget(self.install_progress)

        # On-demand slide-over inspector (same pattern as local Models & engines).
        self.inspector = self._build_inspector()
        self.inspector.setParent(self)
        set_style_property(self.inspector, "overlay", True)
        shadow = QGraphicsDropShadowEffect(self.inspector)
        shadow.setBlurRadius(28)
        shadow.setOffset(-6, 0)
        shadow.setColor(QColor(0, 0, 0, 110))
        self.inspector.setGraphicsEffect(shadow)
        self.inspector.hide()

    def _build_engine_rail(self, scroll_cls) -> QFrame:
        from ui_qt.dialogs.settings_downloads import _EngineRailItem

        rail = QFrame()
        rail.setObjectName("downloadsEngineRail")
        rail.setFixedWidth(self.RAIL_WIDTH)
        outer = QVBoxLayout(rail)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = scroll_cls()
        scroll.setObjectName("downloadsEngineRailScroll")
        scroll.setWidgetResizable(True)
        scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        scroll.setMinimumHeight(1)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        content = QWidget()
        content.setObjectName("downloadsRailContent")
        self._rail_layout = QVBoxLayout(content)
        self._rail_layout.setContentsMargins(6, 6, 6, 6)
        self._rail_layout.setSpacing(1)

        def add_section(title: str) -> QLabel:
            lbl = QLabel(title)
            lbl.setObjectName("downloadsRailSection")
            self._rail_layout.addWidget(lbl)
            return lbl

        def add_item(key: str, label: str, tooltip: str = ""):
            item = _EngineRailItem(key, label, tooltip=tooltip, parent=content)
            item.clicked.connect(lambda _checked=False, k=key: self.select_rail(k))
            self._rail_items[key] = item
            self._rail_layout.addWidget(item)
            return item

        add_section("OVERVIEW")
        add_item("all", "All models", tooltip="Show models available on the host")
        add_item("installed", "Installed", tooltip="Show models already downloaded on the host")

        self._rail_engines_header = add_section("SPEECH ENGINES")
        self._rail_engines_header.setVisible(False)
        self._rail_engines_container = QVBoxLayout()
        self._rail_engines_container.setContentsMargins(0, 0, 0, 0)
        self._rail_engines_container.setSpacing(1)
        self._rail_layout.addLayout(self._rail_engines_container)
        self._rail_layout.addStretch()

        scroll.setWidget(content)
        outer.addWidget(scroll, stretch=1)
        self._rail_items["all"].set_selected(True)
        return rail

    def _build_catalog_column(self, scroll_cls) -> QWidget:
        column = QWidget()
        column.setObjectName("downloadsCatalogColumn")
        column_layout = QVBoxLayout(column)
        column_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.setSpacing(5)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(6)
        self._toolbar_layout = toolbar

        self.search = QLineEdit()
        self.search.setObjectName("modelManagerSearch")
        self.search.setPlaceholderText("Search host models")
        self.search.setMinimumWidth(80)
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Search host models")
        self.search.textChanged.connect(self._filter_models)
        toolbar.addWidget(self.search, stretch=1)

        self.status_filter_combo = ElidingComboBox()
        self.status_filter_combo.setObjectName("modelManagerStatusFilter")
        self.status_filter_combo.setMinimumWidth(78)
        self.status_filter_combo.addItem("All", "all")
        self.status_filter_combo.addItem("Downloaded", "downloaded")
        self.status_filter_combo.addItem("Not downloaded", "not_downloaded")
        self.status_filter_combo.setToolTip("Filter host models by download status")
        self.status_filter_combo.currentIndexChanged.connect(self._filter_models)
        toolbar.addWidget(self.status_filter_combo)
        column_layout.addLayout(toolbar)

        self.library_scroll_area = scroll_cls()
        self.library_scroll_area.setObjectName("modelManagerLibraryScroll")
        self.library_scroll_area.setWidgetResizable(True)
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

        self.rows_layout = QVBoxLayout()
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(6)
        self.list_layout.addLayout(self.rows_layout)

        self.empty_label = WrappedLabel("No matching models")
        self.empty_label.setTextFormat(Qt.TextFormat.PlainText)
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
        self.library_scroll_area.verticalScrollBar().installEventFilter(self)
        column_layout.addWidget(self.library_scroll_area, stretch=1)
        return column

    def _build_context_banner(self) -> QFrame:
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
        title_row.addStretch()
        left.addLayout(title_row)

        self.banner_subtitle = ElidingLabel("")
        self.banner_subtitle.setObjectName("downloadsBannerSubtitle")
        left.addWidget(self.banner_subtitle)
        header_row.addLayout(left, stretch=1)

        self.banner_runtime_badge = QLabel("")
        self.banner_runtime_badge.setObjectName("downloadsRuntimeBadge")
        self.banner_runtime_badge.setVisible(False)
        header_row.addWidget(self.banner_runtime_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addLayout(header_row)

        # Inline runtime callout / install box for the focused engine.
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

        self.banner_runtime_box_button = QPushButton("Install runtime")
        self.banner_runtime_box_button.setObjectName("downloadsBannerAction")
        self.banner_runtime_box_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.banner_runtime_box_button.clicked.connect(self._on_banner_install_clicked)
        box_row.addWidget(self.banner_runtime_box_button, 0, Qt.AlignmentFlag.AlignVCenter)

        self.banner_runtime_box.setVisible(False)
        outer.addWidget(self.banner_runtime_box)

        # Inline host device selector for the focused engine.
        self.banner_controls_strip = QFrame()
        self.banner_controls_strip.setObjectName("downloadsControlsStrip")
        ctrl_row = QHBoxLayout(self.banner_controls_strip)
        ctrl_row.setContentsMargins(0, 4, 0, 0)
        ctrl_row.setSpacing(8)

        dev_lbl = QLabel("Run on host")
        dev_lbl.setObjectName("downloadsControlLabel")
        ctrl_row.addWidget(dev_lbl)

        self.banner_device_combo = ElidingComboBox()
        self.banner_device_combo.setObjectName("downloadsDeviceCombo")
        self.banner_device_combo.setMinimumWidth(130)
        self.banner_device_combo.setFixedHeight(26)
        self.banner_device_combo.setAccessibleName("Device for focused host engine")
        self.banner_device_combo.currentIndexChanged.connect(self._on_banner_device_changed)
        ctrl_row.addWidget(self.banner_device_combo)

        self.banner_controls_hint = ElidingLabel("Applies to all connected clients")
        self.banner_controls_hint.setObjectName("downloadsControlHint")
        self.banner_controls_hint.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        ctrl_row.addWidget(self.banner_controls_hint, stretch=1)

        self.banner_controls_strip.setVisible(False)
        outer.addWidget(self.banner_controls_strip)
        return banner

    def _build_inspector(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("downloadsInspector")
        panel.setFixedWidth(self.INSPECTOR_WIDTH)
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(10)

        eyebrow_row = QHBoxLayout()
        eyebrow_row.setContentsMargins(0, 0, 0, 0)
        eyebrow = QLabel("Host model")
        eyebrow.setObjectName("settingsTileGroupTitle")
        eyebrow_row.addWidget(eyebrow)
        eyebrow_row.addStretch()
        self.inspector_close_button = QPushButton("✕")
        self.inspector_close_button.setObjectName("downloadsInspectorClose")
        self.inspector_close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.inspector_close_button.setToolTip("Close the model panel")
        self.inspector_close_button.setFixedSize(26, 26)
        self.inspector_close_button.clicked.connect(self.close_inspector)
        eyebrow_row.addWidget(self.inspector_close_button)
        outer.addLayout(eyebrow_row)

        self.inspector_engine = QLabel("")
        self.inspector_engine.setObjectName("downloadsInspectorEngine")
        self.inspector_engine.setVisible(False)
        outer.addWidget(self.inspector_engine, alignment=Qt.AlignmentFlag.AlignLeft)

        self.model_title = ElidingLabel("Choose a model")
        self.model_title.setObjectName("downloadsInspectorName")
        outer.addWidget(self.model_title)

        detail_scroll = QScrollArea()
        detail_scroll.setObjectName("downloadsInspectorScroll")
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        detail_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        detail_scroll.setMinimumHeight(60)

        content = QWidget()
        content.setObjectName("downloadsInspectorContent")
        content.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        details = QVBoxLayout(content)
        details.setContentsMargins(0, 0, 6, 0)
        details.setSpacing(10)

        self.description = WrappedLabel("")
        self.description.setTextFormat(Qt.TextFormat.PlainText)
        self.description.setObjectName("downloadsInspectorBody")
        details.addWidget(self.description)

        self.detail = WrappedLabel("")
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        self.detail.setObjectName("downloadsFactValue")
        details.addWidget(self.detail)

        self.download_button = neutral_button(Button("Download model on host"))
        self.download_button.setObjectName("remoteModelDownloadButton")
        self.download_button.clicked.connect(self._download)
        details.addWidget(self.download_button)

        device_label = QLabel("Run on host")
        device_label.setObjectName("downloadsSectionTitle")
        self.device_combo = ElidingComboBox()
        self.device_combo.setAccessibleName("Device for selected host model")
        device_label.setBuddy(self.device_combo)
        self._device_label = device_label
        details.addWidget(device_label)
        details.addWidget(self.device_combo)
        self.device_combo.currentIndexChanged.connect(self._on_inspector_device_changed)

        self.runtime_detail = WrappedLabel("")
        self.runtime_detail.setTextFormat(Qt.TextFormat.PlainText)
        self.runtime_detail.setObjectName("downloadsInspectorBody")
        details.addWidget(self.runtime_detail)

        self.install_button = neutral_button(Button("Install runtime on host"))
        self.install_button.setObjectName("remoteRuntimeInstallButton")
        self.install_button.clicked.connect(self._install)
        details.addWidget(self.install_button)

        note = WrappedLabel(
            "Downloads use the host's network and storage and continue if you leave this page. "
            "Engine changes apply to every connected client."
        )
        note.setTextFormat(Qt.TextFormat.PlainText)
        note.setObjectName("downloadsSourceNote")
        details.addWidget(note)
        details.addStretch()

        detail_scroll.setWidget(content)
        outer.addWidget(detail_scroll, stretch=1)

        self.select_button = PrimaryButton("Use on host")
        self.select_button.setProperty("buttonId", "remoteModelSelectButton")
        self.select_button.clicked.connect(self._select)
        outer.addWidget(self.select_button)
        return panel

    # ---- lifecycle & binding ----

    def bind(self, service, pairing=None) -> None:
        """Attach the remote engine service and active pairing."""
        if pairing is None and service is not None:
            pairing = service.client_pairing()
        host_name = getattr(pairing, "host_name", "") or ""
        same_pairing = pairing == self._pairing and service is self._service
        self._service = service
        self._pairing = pairing
        self._host_name = host_name
        if not same_pairing:
            self._poll.stop()
            self._valid = False
            self._busy = False
            self._request_token = next(_tokens)
            self._catalog = {}
            self._models_by_key = {}
            self._selected_key = None
            self._engine_devices.clear()
            self.close_inspector()
            self._clear_rows()
            self._rebuild_engine_rail([])
            self.download_progress.hide()
            self.install_progress.hide()
            self.install_status.hide()
            self.engine_label.setText(
                f"Connecting to {host_name}…" if host_name else "No remote host paired"
            )
            self.status.setText(
                "Reading host models…" if pairing is not None else "Pair a host in Dictation → Remote engine."
            )
            self._update_context_banner()
            self._update_actions()
        elif host_name:
            self._update_context_banner()
        if self.isVisible() and pairing is not None and not self._busy:
            self._request("model_catalog")

    def refresh_host(self) -> None:
        if self._service is not None and self._pairing is not None:
            self._request("model_catalog")

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync_toolbar_gutter()
        self._position_inspector()
        if self._service is not None and self._pairing is not None:
            self._request("model_catalog")

    def hideEvent(self, event) -> None:
        self._poll.stop()
        self.close_inspector()
        super().hideEvent(event)

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

    def eventFilter(self, watched, event):
        if watched is self.library_scroll_area.verticalScrollBar() and event.type() in (
            event.Type.Show,
            event.Type.Hide,
            event.Type.Resize,
        ):
            self._sync_toolbar_gutter()
        return super().eventFilter(watched, event)

    def _sync_toolbar_gutter(self) -> None:
        reserved = max(
            0,
            self.library_scroll_area.width()
            - self.library_scroll_area.viewport().width(),
        )
        if self._toolbar_layout.contentsMargins().right() != reserved:
            self._toolbar_layout.setContentsMargins(0, 0, reserved, 0)

    # ---- rail & inspector positioning ----

    def select_rail(self, key: str) -> None:
        if key not in self._rail_items:
            key = "all"
        self._active_rail_key = key
        self.close_inspector()
        for item_key, item in self._rail_items.items():
            item.set_selected(item_key == key)
        self.status_filter_combo.setVisible(key != "installed")
        self._update_context_banner()
        self._filter_models()
        self.library_scroll_area.verticalScrollBar().setValue(0)

    def _position_inspector(self) -> None:
        if not self._inspector_open:
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
        self._inspector_open = False
        self.inspector.setVisible(False)
        for row in self._rows.values():
            if row.property("selected"):
                set_style_property(row, "selected", False)

    # ---- selection & device helpers ----

    def _choice(self) -> dict:
        if not self._selected_key:
            return {}
        return self._models_by_key.get(self._selected_key, {})

    def select_model(self, key: str, *, open_inspector: bool = True) -> None:
        if key not in self._models_by_key:
            return
        self._selected_key = key
        for row_key, row in self._rows.items():
            set_style_property(row, "selected", row_key == key)
        self._update_actions()
        if open_inspector:
            self._inspector_open = True
            self._position_inspector()

    def _on_row_details_requested(self, key: str) -> None:
        if self._inspector_open and self._selected_key == key:
            self.close_inspector()
            return
        self.select_model(key, open_inspector=True)

    def _preferred_device_for_model(self, model: dict) -> str | None:
        options = model.get("dependencies") or []
        if not options:
            return None
        family = model.get("family") or ""
        saved = self._engine_devices.get(family)
        valid_devices = [opt["device"] for opt in options]
        if saved in valid_devices:
            return saved
        selected = model.get("selected_device")
        if selected in valid_devices:
            return selected
        return next(
            (opt["device"] for opt in reversed(options) if opt.get("ready")),
            options[0]["device"],
        )

    def _dependency_for_model(self, model: dict, device: str | None = None) -> dict:
        options = model.get("dependencies") or []
        if not options:
            return {}
        target = device or self._preferred_device_for_model(model)
        return next(
            (opt for opt in options if opt.get("device") == target),
            options[0],
        )

    def _dependency(self) -> dict:
        choice = self._choice()
        if not choice:
            return {}
        current = self.device_combo.currentData()
        return next(
            (
                item
                for item in choice.get("dependencies", [])
                if item["device"] == current
            ),
            {},
        )

    def _on_inspector_device_changed(self, _index: int = 0) -> None:
        choice = self._choice()
        device = self.device_combo.currentData()
        if choice and device:
            self._engine_devices[choice["family"]] = device
            self._update_context_banner()
            self._refresh_row_states()
        self._update_actions()

    def _on_banner_device_changed(self, _index: int = 0) -> None:
        family = self._active_rail_key
        device = self.banner_device_combo.currentData()
        if family not in ("all", "installed") and device:
            self._engine_devices[family] = device
            self._update_context_banner()
            self._refresh_row_states()
            choice = self._choice()
            if choice and choice.get("family") == family:
                self._update_actions()

    # ---- filtering & catalog building ----

    def _clear_rows(self) -> None:
        while self.rows_layout.count():
            item = self.rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        self._rows.clear()

    def _rebuild_engine_rail(self, families: list[str]) -> None:
        from services.local_asr.catalog import BACKENDS, WHISPER_BACKEND
        from ui_qt.dialogs.settings_downloads import (
            _engine_meta,
            _engine_order,
            _EngineRailItem,
        )

        preferred = [f for f in _engine_order() if f in families]
        extras = sorted(set(families) - set(preferred))
        ordered = preferred + extras

        if ordered == self._rail_engine_keys:
            self._refresh_rail_counts()
            return

        for old_key in self._rail_engine_keys:
            item = self._rail_items.pop(old_key, None)
            if item is not None:
                self._rail_engines_container.removeWidget(item)
                item.hide()
                item.deleteLater()

        self._rail_engine_keys = ordered
        self._rail_engines_header.setVisible(bool(ordered))
        backend_names = {WHISPER_BACKEND: "Whisper", **BACKENDS}

        for backend_id in ordered:
            meta = _engine_meta(backend_id)
            label = meta.short if backend_id in backend_names else backend_id
            tip = f"{backend_names.get(backend_id, label)} — {meta.tagline}" if meta.tagline else label
            item = _EngineRailItem(
                backend_id,
                label,
                dot_rgb=meta.rgb,
                tooltip=tip,
            )
            item.clicked.connect(lambda _checked=False, k=backend_id: self.select_rail(k))
            self._rail_items[backend_id] = item
            self._rail_engines_container.addWidget(item)

        if self._active_rail_key not in self._rail_items:
            self._active_rail_key = "all"
        for key, item in self._rail_items.items():
            item.set_selected(key == self._active_rail_key)
        self._refresh_rail_counts()

    def _refresh_rail_counts(self) -> None:
        models = list(self._models_by_key.values())
        total = len(models)
        cached = sum(1 for m in models if m.get("cached"))
        if not self._valid and not models:
            self._rail_items["all"].set_count("…" if self._busy else "—")
            self._rail_items["installed"].set_count("…" if self._busy else "—")
        else:
            self._rail_items["all"].set_count(f"{cached}/{total}")
            self._rail_items["installed"].set_count(str(cached))
        for backend_id in self._rail_engine_keys:
            item = self._rail_items.get(backend_id)
            if item is None:
                continue
            fam_models = [m for m in models if m.get("family") == backend_id]
            fam_cached = sum(1 for m in fam_models if m.get("cached"))
            item.set_count(f"{fam_cached}/{len(fam_models)}")

    def _populate_models(self, result: dict, engine: dict) -> None:
        models = result.get("models", [])
        new_keys = [f"{m['family']}:{m['model']}" for m in models]
        self._models_by_key = {f"{m['family']}:{m['model']}": m for m in models}

        families = list(dict.fromkeys(m["family"] for m in models))
        self._rebuild_engine_rail(families)

        if list(self._rows.keys()) != new_keys:
            scroll = self.library_scroll_area.verticalScrollBar().value()
            self._clear_rows()
            for model in models:
                key = f"{model['family']}:{model['model']}"
                row = _HostModelRow(key, model)
                row.details_requested.connect(self._on_row_details_requested)
                row.download_clicked.connect(self._download_key)
                row.select_clicked.connect(self._select_key)
                row.setup_clicked.connect(self._setup_key)
                self._rows[key] = row
                self.rows_layout.addWidget(row)
            self.library_scroll_area.verticalScrollBar().setValue(scroll)

        # Keep current selection if still present, otherwise pick the active host model.
        if self._selected_key not in self._models_by_key:
            active_key = (
                f"{engine.get('family')}:{engine.get('model')}"
                if engine.get("available")
                else None
            )
            if active_key in self._models_by_key:
                self._selected_key = active_key
            elif new_keys:
                self._selected_key = new_keys[0]
            else:
                self._selected_key = None

        self._refresh_row_states()
        self._update_context_banner()
        self._filter_models()

    def _refresh_row_states(self) -> None:
        engine = self._catalog.get("engine") or {}
        job = self._catalog.get("download") or {}
        any_downloading = job.get("state") == "downloading"
        can_select = bool(self._catalog.get("can_select"))
        for key, row in self._rows.items():
            entry = self._models_by_key.get(key)
            if entry is None:
                continue
            options = entry.get("dependencies") or []
            dep = self._dependency_for_model(entry)
            ready = bool(dep.get("ready")) if options else bool(entry.get("runtime_ready"))
            active = (
                bool(engine.get("available"))
                and (entry.get("family"), entry.get("model"))
                == (engine.get("family"), engine.get("model"))
                and (not dep or _device_matches_engine(dep.get("device"), engine.get("device")))
            )
            downloading_this = any_downloading and (
                job.get("family"),
                job.get("model"),
            ) == (entry.get("family"), entry.get("model"))
            row.update_state(
                entry,
                is_active=active,
                ready=ready,
                can_select=can_select,
                downloading_this=downloading_this,
                download_job=job,
                any_downloading=any_downloading,
                busy=self._busy or not self._valid,
            )
            set_style_property(row, "selected", self._inspector_open and key == self._selected_key)

    def _filter_models(self, *_args) -> None:
        from ui_qt.dialogs.settings_downloads import _engine_meta

        needle = self.search.text().strip().casefold()
        rail_key = self._active_rail_key
        status = (
            "downloaded"
            if rail_key == "installed"
            else self.status_filter_combo.currentData()
        )
        show_engine_badges = rail_key in ("all", "installed")
        any_visible = False

        # Sort active model first, then downloaded, preserving host catalog order.
        ordered_rows = sorted(
            self._rows.values(),
            key=lambda r: (not r.is_active, not r.is_cached),
        )
        for index, row in enumerate(ordered_rows):
            if self.rows_layout.indexOf(row) != index:
                self.rows_layout.insertWidget(index, row)
            meta = _engine_meta(row.family)
            row.set_engine_badge(meta.short, meta.rgb, visible=show_engine_badges)
            visible = row.matches_filter(needle)
            if rail_key not in ("all", "installed"):
                visible = visible and row.family == rail_key
            if status == "downloaded":
                visible = visible and row.is_cached
            elif status == "not_downloaded":
                visible = visible and not row.is_cached
            row.setVisible(visible)
            any_visible = any_visible or visible

        show_empty = (not any_visible) and (self._valid or not self._rows)
        self.empty_label.setVisible(show_empty)
        if show_empty:
            if not self._valid:
                host = self._host_name or "the host"
                if self._busy:
                    self.empty_label.setText(f"Reading models on {host}…")
                else:
                    self.empty_label.setText(self.status.text())
            elif rail_key == "installed" and not needle:
                self.empty_label.setText(f"No models downloaded on {self._host_name or 'host'} yet")
            elif rail_key not in ("all", "installed") and needle:
                self.empty_label.setText(f"No matches in {_engine_meta(rail_key).short}")
            else:
                self.empty_label.setText("No matching models")
        self.show_all_engines_button.setVisible(
            self._valid and not any_visible and rail_key != "all"
        )

    def _update_context_banner(self) -> None:
        from services.hf_access import format_size_bytes
        from services.local_asr.catalog import BACKENDS, WHISPER_BACKEND
        from ui_qt.dialogs.settings_downloads import _engine_meta

        key = self._active_rail_key
        host = self._host_name or "host"
        self._banner_install_dep = None
        self._banner_install_model = None
        self.banner_dot.setVisible(False)
        self.banner_runtime_badge.setVisible(False)
        self.banner_runtime_box.setVisible(False)
        self.banner_runtime_progress.setVisible(False)
        self.banner_controls_strip.setVisible(False)

        models = list(self._models_by_key.values())
        cached_count = sum(1 for m in models if m.get("cached"))

        if key == "all":
            self.banner_title.setText(f"All models on {host}")
            if not self._valid:
                self.banner_subtitle.setText(
                    f"Downloads use {host}'s network and storage. Engine changes apply to every connected client."
                )
            else:
                self.banner_subtitle.setText(
                    f"{cached_count} of {len(models)} downloaded on {host} · "
                    "Downloads use the host's network and storage; engine changes apply to every connected client."
                )
            return

        if key == "installed":
            self.banner_title.setText(f"Installed on {host}")
            fam_count = len({m["family"] for m in models if m.get("cached")})
            if cached_count:
                noun = "model" if cached_count == 1 else "models"
                eng_noun = "engine" if fam_count == 1 else "engines"
                self.banner_subtitle.setText(
                    f"{cached_count} {noun} across {fam_count} {eng_noun} on {host}"
                )
            else:
                self.banner_subtitle.setText(
                    f"No speech models are downloaded on {host} yet."
                )
            return

        meta = _engine_meta(key)
        names = {WHISPER_BACKEND: "Whisper", **BACKENDS}
        display_name = backend_picker_label(names.get(key, meta.short))
        self.banner_dot.setStyleSheet(
            f"background-color: rgb({meta.rgb}); border: none; border-radius: 4px;"
        )
        self.banner_dot.setVisible(True)
        self.banner_title.setText(f"{display_name} on {host}")
        self.banner_subtitle.setText(meta.tagline)

        sample = next((m for m in models if m.get("family") == key), None)
        if sample is None:
            return
        options = sample.get("dependencies") or []
        if not options:
            if sample.get("runtime_ready"):
                self.banner_runtime_badge.setText("Runtime · Ready")
                set_style_property(self.banner_runtime_badge, "tone", "ready")
                self.banner_runtime_badge.setVisible(True)
            return

        selected_dev = self._preferred_device_for_model(sample)
        blocker = self.banner_device_combo.blockSignals(True)
        self.banner_device_combo.clear()
        for opt in options:
            state = (
                "Ready"
                if opt.get("ready")
                else "Setup needed"
                if opt.get("installable")
                else "Unavailable"
            )
            self.banner_device_combo.addItem(
                f"{_DEVICE_LABELS.get(opt['device'], opt['device'])} · {state}",
                opt["device"],
            )
        idx = self.banner_device_combo.findData(selected_dev)
        if idx >= 0:
            self.banner_device_combo.setCurrentIndex(idx)
        self.banner_device_combo.setEnabled(self._valid and not self._busy)
        self.banner_device_combo.blockSignals(blocker)
        self.banner_controls_strip.setVisible(True)

        dep = self._dependency_for_model(sample, selected_dev)
        install = self._catalog.get("installation") or {}
        if install.get("state") == "installing":
            phase = install.get("phase") or "Installing"
            done, total = install.get("done", 0), install.get("total", 0)
            pct_str = f" ({min(100, int(100 * done / total))}%)" if total > 0 else "…"
            self.banner_runtime_box_title.setText(
                f"{phase} {install.get('label', 'runtime')} on {host}{pct_str}"
            )
            self.banner_runtime_box_note.setText(
                "Installing verified runtime files on the host. Continues if you leave this page."
            )
            self.banner_runtime_progress.setRange(0, 100 if total > 0 else 0)
            if total > 0:
                self.banner_runtime_progress.setValue(min(100, int(100 * done / total)))
            self.banner_runtime_progress.setVisible(True)
            self.banner_runtime_box_button.setVisible(False)
            set_style_property(self.banner_runtime_box, "tone", "downloading")
            self.banner_runtime_box.setVisible(True)
            return

        if dep.get("ready"):
            self.banner_runtime_badge.setText(f"{dep['label']} · Ready")
            set_style_property(self.banner_runtime_badge, "tone", "ready")
            self.banner_runtime_badge.setVisible(True)
            return

        size_str = (
            f" (~{format_size_bytes(dep['download_bytes'])})"
            if dep.get("download_bytes")
            else ""
        )
        self._banner_install_dep = dep
        self._banner_install_model = sample
        if dep.get("installable") and self._catalog.get("can_install_runtime"):
            self.banner_runtime_box_title.setText(
                f"{dep['label']} required on {host}{size_str}"
            )
            self.banner_runtime_box_note.setText(
                dep.get("reason")
                or f"Install this runtime on {host} to run {display_name} on "
                f"{_DEVICE_LABELS.get(dep['device'], dep['device'])}."
            )
            self.banner_runtime_box_button.setText("Install runtime")
            self.banner_runtime_box_button.setEnabled(self._valid and not self._busy)
            self.banner_runtime_box_button.setVisible(True)
            set_style_property(self.banner_runtime_box_button, "tone", "primary")
            set_style_property(self.banner_runtime_box, "tone", "missing")
            self.banner_runtime_box.setVisible(True)
            repolish(self.banner_runtime_box_button)
        else:
            self.banner_runtime_box_title.setText(
                f"{dep.get('label', 'Runtime')} unavailable on {host}"
            )
            self.banner_runtime_box_note.setText(
                dep.get("reason")
                or "This device isn't available for this engine on the host."
            )
            self.banner_runtime_box_button.setVisible(False)
            set_style_property(self.banner_runtime_box, "tone", "optional")
            self.banner_runtime_box.setVisible(True)

    # ---- inspector & actions state ----

    def _update_actions(self, *_args) -> None:
        from services.local_asr.catalog import BACKENDS
        from ui_qt.dialogs.settings_downloads import _engine_meta

        choice = self._choice()
        key = (choice.get("family"), choice.get("model"))
        options = choice.get("dependencies", [])
        previous = (
            self.device_combo.currentData() if self._device_model == key else None
        )
        family_saved = self._engine_devices.get(choice.get("family") or "") if choice else None
        selected = family_saved or previous or (choice.get("selected_device") if choice else None)
        valid_devices = [opt["device"] for opt in options]
        if options and selected not in valid_devices:
            selected = next(
                (option["device"] for option in reversed(options) if option.get("ready")),
                options[0]["device"],
            )
        self._device_model = key
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for option in options:
            state = (
                "Ready"
                if option.get("ready")
                else "Setup needed"
                if option.get("installable")
                else "Unavailable"
            )
            self.device_combo.addItem(
                f"{_DEVICE_LABELS.get(option['device'], option['device'])} · {state}",
                option["device"],
            )
        index = self.device_combo.findData(selected)
        if index >= 0:
            self.device_combo.setCurrentIndex(index)
        self.device_combo.blockSignals(False)
        self.device_combo.setVisible(bool(options))
        self._device_label.setVisible(bool(options))

        dependency = self._dependency()
        enabled = self._valid and not self._busy and bool(choice)
        self.device_combo.setEnabled(enabled)
        self.refresh_button.setEnabled(not self._busy and self._pairing is not None)
        job = self._catalog.get("download") or {}
        install = self._catalog.get("installation") or {}

        self.download_button.setEnabled(
            bool(
                enabled
                and not choice.get("cached")
                and job.get("state") != "downloading"
            )
        )
        self.download_button.setText(
            "Model downloaded" if choice.get("cached") else "Download model on host"
        )
        self.download_button.setVisible(bool(choice) and not choice.get("cached", False))

        self.install_button.setEnabled(
            bool(
                enabled
                and self._catalog.get("can_install_runtime")
                and dependency.get("installable")
                and install.get("state") != "installing"
            )
        )
        self.install_button.setVisible(bool(dependency and not dependency.get("ready")))

        engine = self._catalog.get("engine") or {}
        active = (
            bool(choice and engine.get("available"))
            and key == (engine.get("family"), engine.get("model"))
            and (not dependency or _device_matches_engine(dependency["device"], engine.get("device")))
        )
        ready = dependency.get("ready") if options else choice.get("runtime_ready")
        self.select_button.setEnabled(
            bool(
                enabled
                and choice.get("cached")
                and ready
                and self._catalog.get("can_select")
                and not active
            )
        )
        self.select_button.setText("Active on host" if active else "Use on host")

        if choice:
            fam = choice.get("family") or ""
            meta = _engine_meta(fam)
            self.inspector_engine.setText(BACKENDS.get(fam, meta.short))
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
        else:
            self.inspector_engine.setVisible(False)

        self.model_title.setText(choice.get("label") or "Choose a model")
        description = ""
        if choice:
            try:
                from services.model_catalog import get_model_details

                profile = get_model_details(choice["model"])
                description = profile.description + "\n" + profile.language_support
            except KeyError:
                pass  # Older clients can still manage newly added host models.
        self.description.setText(description)
        self.detail.setText(
            (
                "Model downloaded on host"
                if choice.get("cached")
                else f"Model download: {choice.get('download_size') or 'size unknown'}"
            )
            if choice
            else ""
        )
        if dependency:
            from services.hf_access import format_size_bytes

            text = (
                f"{dependency['label']} · Ready"
                if dependency.get("ready")
                else dependency.get("reason")
            )
            if not text:
                size = (
                    format_size_bytes(dependency["download_bytes"])
                    if dependency.get("download_bytes")
                    else "size unknown"
                )
                text = (
                    f"{dependency['label']} runtime is missing. "
                    f"Install it on the host ({size}) to use this device."
                )
            self.runtime_detail.setText(text)
        else:
            self.runtime_detail.setText(
                "Update OpenWhisper on the host to manage its CPU/GPU runtimes here. "
                "Missing runtimes can be installed in Settings → Models & engines on the host."
                if choice
                else ""
            )

    # ---- host requests & polling ----

    def _request(self, op: str, **fields) -> None:
        if self._busy or not self.isVisible() or self._service is None:
            return
        self._busy = True
        self._poll.stop()
        self._refresh_row_states()
        self._update_context_banner()
        self._update_actions()
        if op != "model_catalog" or not self._valid:
            self.status.setText(
                {
                    "model_catalog": "Reading host models…",
                    "download_model": "Starting model download…",
                    "install_runtime": "Starting runtime installation…",
                    "select_model": "Loading model on host…",
                }[op]
            )
        self._request_token = token = next(_tokens)
        # Bound here: the worker must not reach back into the widget.
        service, pairing = self._service, self._pairing
        finished = _request_relay().finished

        def work():
            result, error = None, ""
            try:
                result = service.remote_model_request(
                    op, expected_pairing=pairing, **fields
                )
            except Exception as exc:
                logger.warning("Remote model request %s failed", op, exc_info=True)
                error = str(exc) or type(exc).__name__
            finished.emit(token, op, result, error)

        threading.Thread(target=work, name="remote-model-request", daemon=True).start()

    @staticmethod
    def _progress(bar: QProgressBar, job: dict, busy_state: str) -> bool:
        busy = job.get("state") == busy_state
        bar.setVisible(busy)
        done, total = job.get("done", 0), job.get("total", 0)
        bar.setRange(0, 100 if total > 0 else 0)
        if total > 0:
            bar.setValue(min(100, int(100 * done / total)))
        return busy

    def _on_finished(self, token: int, op: str, result, error: str) -> None:
        if token != self._request_token:
            return
        self._busy = False
        if error:
            self._valid = False
            self.status.setText(f"{error} Refresh to check the host's current state.")
            self.engine_label.setText(f"Couldn't read models on {self._host_name or 'host'}")
            self._refresh_rail_counts()
            self._refresh_row_states()
            self._update_context_banner()
            self._filter_models()
        elif op == "model_catalog":
            self._apply_catalog(result)
        else:
            self._request("model_catalog")
        self._update_actions()

    def _apply_catalog(self, result: dict) -> None:
        self._valid = True
        self._catalog = result or {}
        engine = self._catalog.get("engine") or {}
        self._populate_models(self._catalog, engine)
        summary = _engine_summary(engine, self._catalog.get("runtime") or {})
        self.engine_label.setText(summary)
        self.engine_label.setToolTip(summary)
        job = self._catalog.get("download") or {}
        installation = self._catalog.get("installation") or {}
        self.install_status.setVisible(bool(installation))
        downloading = self._progress(self.download_progress, job, "downloading")
        installing = self._progress(
            self.install_progress, installation, "installing"
        )
        self._show_download_status(job, downloading)
        self._show_install_status(installation, installing)
        if self.isVisible() and (downloading or installing):
            self._poll.start()

    def _show_download_status(self, job: dict, downloading: bool) -> None:
        if downloading:
            percent = (
                f" ({self.download_progress.value()}%)"
                if job.get("total", 0) > 0
                else ""
            )
            self.status.setText(
                f"Downloading {job.get('label', 'model')} on host{percent}…"
            )
        elif job.get("state") == "failed":
            self.status.setText(
                job.get("error") or "Model download failed. Retry the download."
            )
        elif job.get("state") == "complete":
            self.status.setText(
                f"{job.get('label', 'Model')} downloaded. Choose a device, then Use on host."
            )
        else:
            self.status.setText(
                "Download a model, prepare its device, then choose Use on host."
            )

    def _show_install_status(self, installation: dict, installing: bool) -> None:
        if installing:
            phase = installation.get("phase") or "Installing"
            self.install_status.setText(
                f"{installation.get('label', 'Runtime')} · {phase}…"
            )
        elif installation.get("state") in ("failed", "restart_required"):
            self.install_status.setText(
                installation.get("error")
                or "Runtime setup needs attention on the host."
            )
        elif installation.get("state") == "complete":
            self.install_status.setText(
                f"{installation.get('label', 'Runtime')} installed. Ready to use on the host."
            )
        else:
            self.install_status.clear()

    # ---- user actions ----

    def _download_key(self, key: str) -> None:
        self.select_model(key, open_inspector=False)
        self._download()

    def _select_key(self, key: str) -> None:
        self.select_model(key, open_inspector=False)
        self._select()

    def _setup_key(self, key: str) -> None:
        self.select_model(key, open_inspector=True)

    def _on_banner_install_clicked(self) -> None:
        dep = self._banner_install_dep
        sample = self._banner_install_model
        if not dep or not sample or not self._valid or self._busy:
            return
        if not (self._catalog.get("can_install_runtime") and dep.get("installable")):
            return
        if self._confirm(
            "Install runtime on host?",
            f"Install {dep['label']} on {self._host_name}? "
            "This downloads verified runtime files using the host's network and disk space.",
        ):
            self._request(
                "install_runtime",
                family=sample["family"],
                model=sample["model"],
                device=dep["device"],
            )

    def _download(self) -> None:
        choice = self._choice()
        if not self.download_button.isEnabled() or not choice:
            return
        text = (
            f"Download {choice['label']} ({choice.get('download_size') or 'size unknown'}) "
            f"on {self._host_name}? This uses the host's network and disk space."
        )
        try:
            from services.model_catalog import get_model_details

            notice = get_model_details(choice["model"]).notice
        except KeyError:
            notice = ""
        if notice:
            text = f"{text}\n\n{notice}"
        if self._confirm("Download on host?", text):
            self._request(
                "download_model", family=choice["family"], model=choice["model"]
            )

    def _install(self) -> None:
        choice, dependency = self._choice(), self._dependency()
        if not self.install_button.isEnabled() or not dependency:
            return
        if self._confirm(
            "Install runtime on host?",
            f"Install {dependency['label']} on {self._host_name}? "
            "This downloads verified runtime files using the host's network and disk space.",
        ):
            self._request(
                "install_runtime",
                family=choice["family"],
                model=choice["model"],
                device=dependency["device"],
            )

    def _select(self) -> None:
        choice = self._choice()
        if not self.select_button.isEnabled() or not choice:
            return
        device = self.device_combo.currentData()
        where = f" using {_DEVICE_LABELS.get(device, device)}" if device else ""
        if self._confirm(
            "Change the host's engine?",
            f"Use {choice['label']} on {self._host_name}{where}? "
            "This changes the engine for the host and every connected client.",
        ):
            fields = {"family": choice["family"], "model": choice["model"]}
            if device and self._catalog.get("can_install_runtime"):
                fields["device"] = device
            self._request("select_model", **fields)

    def _confirm(self, title: str, text: str) -> bool:
        return (
            QMessageBox.question(
                self,
                title,
                text,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )

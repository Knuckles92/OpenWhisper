"""Whisper catalog, optional components, and per-model technical profile.

Split out of Model Manager so the assignment surface never has to host a
sixteen-row list. This is the one place a scrolling list is correct, and it is
the only scroller here: the header, toolbar, and component strip stay put.

Non-modal, like Model Manager, because a multi-gigabyte download must not lock
the user out of recording.
"""
from typing import Callable, Dict, Optional

from PyQt6.QtCore import QSize, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from config import config
from services.components import component_coordinator
from services.hf_access import (
    CachedModelInfo,
    format_size_bytes,
    get_hf_cache_dir,
    resolve_model_repo,
    scan_cached_models,
)
from services.model_catalog import ModelDetails, get_model_details
from services.settings import (
    SettingsKey,
    is_hf_hub_offline_env_set,
    resolve_meeting_whisper_model,
    settings_manager,
)
from ui_qt.utils.app_icon import app_icon
from ui_qt.widgets import Button, ElidingLabel
from ui_qt.widgets.component_row_widget import ComponentRowWidget
from ui_qt.widgets.model_row_widget import ModelRowWidget
from ui_qt.widgets.wrapped_label import WrappedLabel


class DownloadsDialog(QDialog):
    #: Wide enough for a catalog row and the inspector side by side: the widest
    #: row plus its scroll bar plus the inspector. The floor is well above Qt's
    #: own minimum, because the rows elide rather than clip and would shrink
    #: past the point where a repo id or a size is still readable.
    DEFAULT_SIZE = QSize(1060, 680)
    MINIMUM_SIZE = QSize(980, 560)

    INSPECTOR_WIDTH = 300

    component_install_requested = pyqtSignal(str)
    component_cancel_requested = pyqtSignal(str)
    component_remove_requested = pyqtSignal(str)

    #: Assigned by UIController; called with the model name.
    on_download_requested: Optional[Callable[[str], None]] = None
    on_delete_requested: Optional[Callable[[str], None]] = None

    def __init__(
        self,
        get_loaded_model: Optional[Callable[[], Optional[str]]] = None,
        parent=None,
    ):
        """Show the catalog with the in-use model protected from deletion.

        Args:
            get_loaded_model: Provider returning the model name currently
                loaded by the engine (or None). Its files are memory-mapped, so
                Delete stays disabled for it.
        """
        super().__init__(parent)
        self._get_loaded_model = get_loaded_model
        self._downloading_model: Optional[str] = None
        self._component_rows: Dict[str, ComponentRowWidget] = {}
        self._selected_model: Optional[str] = None

        self.setWindowTitle("Downloads — Model Manager")
        self.setWindowIcon(app_icon())
        self.setObjectName("downloadsDialog")
        self.setModal(False)
        self.setWindowFlag(Qt.WindowType.MSWindowsFixedSizeDialogHint, False)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.setSizeGripEnabled(True)
        self.setMinimumSize(self.MINIMUM_SIZE)

        self._setup_ui()
        self.resize(self.DEFAULT_SIZE)
        self.refresh()

    # ---- construction ----

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 0)
        layout.setSpacing(10)

        head = QVBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)

        header_row = QHBoxLayout()
        header_row.setSpacing(8)
        title_block = QVBoxLayout()
        title_block.setSpacing(2)
        title = QLabel("Downloads")
        title.setObjectName("downloadsTitle")
        # Elides rather than wraps: the cache path is long and would otherwise
        # push the toolbar and the list down by two lines.
        self.stats_label = ElidingLabel("")
        self.stats_label.setObjectName("downloadsSubtitle")
        title_block.addWidget(title)
        title_block.addWidget(self.stats_label)
        header_row.addLayout(title_block)
        header_row.addStretch()

        open_folder_btn = Button("Open folder")
        self._compact_button(open_folder_btn, 110)
        open_folder_btn.setToolTip(
            "Open the folder where downloaded models are stored"
        )
        open_folder_btn.clicked.connect(self._on_open_cache_folder)
        header_row.addWidget(open_folder_btn)

        close_btn = Button("Close")
        close_btn.setObjectName("downloadsCloseButton")
        self._compact_button(close_btn, 100)
        close_btn.clicked.connect(self.close)
        header_row.addWidget(close_btn)
        head.addLayout(header_row)

        self.env_banner = QLabel(
            "Downloads are disabled by the HF_HUB_OFFLINE environment "
            "variable set outside this application."
        )
        self.env_banner.setObjectName("downloadsEnvBanner")
        self.env_banner.setWordWrap(True)
        self.env_banner.setVisible(False)
        head.addWidget(self.env_banner)
        layout.addLayout(head)

        split = QHBoxLayout()
        split.setSpacing(12)
        split.addWidget(self._build_catalog_column(), stretch=1)
        split.addWidget(self._build_inspector())
        layout.addLayout(split, stretch=1)

        self.message_label = QLabel("")
        self.message_label.setObjectName("downloadsMessage")
        layout.addWidget(self.message_label)

        layout.addWidget(self._build_component_strip())

    def _build_catalog_column(self) -> QWidget:
        """Stack the filters directly above the rows they filter."""
        column = QWidget()
        column_layout = QVBoxLayout(column)
        column_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.setSpacing(8)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(8)
        self._toolbar_layout = toolbar
        self.filter_edit = QLineEdit()
        self.filter_edit.setObjectName("modelManagerSearch")
        self.filter_edit.setPlaceholderText("Search models")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._apply_filter)
        toolbar.addWidget(self.filter_edit, stretch=1)

        self.status_filter_combo = QComboBox()
        self.status_filter_combo.setObjectName("modelManagerStatusFilter")
        self.status_filter_combo.addItem("All", "all")
        self.status_filter_combo.addItem("Downloaded", "downloaded")
        self.status_filter_combo.addItem("Not downloaded", "not_downloaded")
        self.status_filter_combo.setToolTip("Filter by download status")
        self.status_filter_combo.currentIndexChanged.connect(self._apply_filter)
        toolbar.addWidget(self.status_filter_combo)

        self.sort_combo = QComboBox()
        self.sort_combo.setObjectName("modelManagerSort")
        self.sort_combo.addItem("Recommended", "recommended")
        self.sort_combo.addItem("Downloaded first", "downloaded")
        self.sort_combo.addItem("Smallest first", "size")
        self.sort_combo.addItem("Name A-Z", "name")
        self.sort_combo.setToolTip("Sort model list")
        self.sort_combo.currentIndexChanged.connect(self._apply_filter)
        toolbar.addWidget(self.sort_combo)
        column_layout.addLayout(toolbar)
        column_layout.addWidget(self._build_list(), stretch=1)
        return column

    def _build_list(self) -> QWidget:
        self.library_scroll_area = QScrollArea()
        self.library_scroll_area.setObjectName("modelManagerLibraryScroll")
        self.library_scroll_area.setWidgetResizable(True)
        self.library_scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.library_scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)

        list_container = QWidget()
        self.list_layout = QVBoxLayout(list_container)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(6)

        self.rows: Dict[str, ModelRowWidget] = {}
        for model_name in config.WHISPER_MODEL_CHOICES:
            if model_name == "auto":
                continue
            row = ModelRowWidget(model_name)
            row.download_clicked.connect(self._on_download_clicked)
            row.delete_clicked.connect(self._on_delete_clicked)
            row.details_requested.connect(self.select_model)
            self.rows[model_name] = row
            self.list_layout.addWidget(row)

        self.empty_label = QLabel("No models match")
        self.empty_label.setObjectName("infoLabel")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setVisible(False)
        self.list_layout.addWidget(self.empty_label)
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

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_toolbar_gutter()

    def _build_inspector(self) -> QWidget:
        """Build the side profile that replaced the Model Details dialog."""
        panel = QFrame()
        panel.setObjectName("downloadsInspector")
        panel.setFixedWidth(self.INSPECTOR_WIDTH)
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(10)

        eyebrow = QLabel("Selected model")
        eyebrow.setObjectName("downloadsEyebrow")
        outer.addWidget(eyebrow)

        self.inspector_name = QLabel("—")
        self.inspector_name.setObjectName("downloadsInspectorName")
        outer.addWidget(self.inspector_name)

        self.inspector_tags = QLabel("")
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
        # A resizable scroll area propagates its content's minimum height, which
        # here is taller than the panel: without a floor of its own it would ask
        # for the whole profile and the column would pay for it by squeezing the
        # name, the facts, and the source buttons.
        detail_scroll.setMinimumHeight(120)
        self.inspector_detail_scroll = detail_scroll

        content = QWidget()
        detail = QVBoxLayout(content)
        detail.setContentsMargins(0, 0, 6, 0)
        detail.setSpacing(12)

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
        self.inspector_source_note.setObjectName("downloadsSourceNote")
        detail.addWidget(self.inspector_source_note)
        detail.addStretch()

        detail_scroll.setWidget(content)
        outer.addWidget(detail_scroll, stretch=1)

        self.inspector_usage = QLabel("")
        self.inspector_usage.setObjectName("downloadsInspectorUsage")
        self.inspector_usage.setVisible(False)
        outer.addWidget(self.inspector_usage)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.inspector_repo_button = Button("Hugging Face ↗")
        self.inspector_repo_button.setObjectName("downloadsRepoButton")
        self._compact_button(self.inspector_repo_button, 0)
        self.inspector_repo_button.clicked.connect(self._open_repository)
        actions.addWidget(self.inspector_repo_button, stretch=1)

        self.inspector_origin_button = Button("Original ↗")
        self.inspector_origin_button.setObjectName("downloadsOriginButton")
        self._compact_button(self.inspector_origin_button, 0)
        self.inspector_origin_button.clicked.connect(self._open_origin)
        actions.addWidget(self.inspector_origin_button, stretch=1)
        outer.addLayout(actions)

        self._details: Optional[ModelDetails] = None
        self._set_inspector_enabled(False)
        return panel

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
        layout.setContentsMargins(0, 10, 0, 12)
        layout.setSpacing(6)

        infos = component_coordinator.list_components()
        if not infos:
            strip.setVisible(False)
            return strip

        heading_row = QHBoxLayout()
        heading_row.setSpacing(10)
        heading = QLabel("Components")
        heading.setObjectName("downloadsEyebrow")
        heading_row.addWidget(heading, alignment=Qt.AlignmentFlag.AlignTop)
        caption = WrappedLabel(
            "Optional add-ons, downloaded on demand so the installer stays small."
        )
        caption.setObjectName("infoLabel")
        heading_row.addWidget(caption, stretch=1)
        layout.addLayout(heading_row)

        for info in infos:
            row = ComponentRowWidget(info.component_id)
            row.install_clicked.connect(self.component_install_requested)
            row.cancel_clicked.connect(self.component_cancel_requested)
            row.remove_clicked.connect(self._confirm_component_removal)
            self._component_rows[info.component_id] = row
            layout.addWidget(row)

        return strip

    @staticmethod
    def _compact_button(button: Button, width: int) -> None:
        """Size a shared button for this window's compact chrome.

        Treats the given size as a floor, never a cap below what the polished
        label needs, so descenders and the link arrow are not clipped.
        """
        button.set_base_minimum_size(width, 34)
        button.ensurePolished()
        height = max(34, button.sizeHint().height())
        button.setMinimumHeight(height)
        button.setMaximumHeight(height)
        if width:
            fitted = max(width, button.minimumWidth(), button.sizeHint().width())
            button.setMinimumWidth(fitted)
            button.setMaximumWidth(fitted)
        else:
            button.setMinimumWidth(0)
            button.setMaximumWidth(16777215)

    # ---- inspector ----

    def select_model(self, model_name: str) -> None:
        """Show one catalog model's bundled profile in the side inspector."""
        if model_name == "auto" or model_name not in self.rows:
            return
        self._selected_model = model_name
        self._details = get_model_details(model_name)
        self._render_inspector()
        for name, row in self.rows.items():
            row.setProperty("selected", name == model_name)
            row.style().unpolish(row)
            row.style().polish(row)
            row.update()

    def _render_inspector(self) -> None:
        details = self._details
        if details is None:
            self._set_inspector_enabled(False)
            return
        self._set_inspector_enabled(True)
        self.inspector_name.setText(details.model_name)
        self.inspector_tags.setText(details.compact_tags)
        self.inspector_tags.setVisible(bool(details.compact_tags))
        self.inspector_description.setText(details.description)
        self.inspector_best_for.setText(details.best_for)
        self.inspector_tradeoffs.setText(
            "\n".join(f"\u2022 {item}" for item in details.limitations)
        )
        self.inspector_repo_button.setToolTip(details.repository_url)
        self.inspector_origin_button.setToolTip(details.origin_url)

        rows = (
            ("Origin", details.origin_name),
            ("Repository", details.repository_id),
            ("Maintainer", details.maintainer),
            ("Family", details.family),
            ("Languages", details.language_support),
            ("Tasks", details.task_support),
            ("Parameters", details.parameter_count),
            ("Published speed", details.relative_performance),
            ("Memory guidance", details.memory_guidance),
            ("Download size", details.download_size),
            ("Local format", details.runtime_format),
            ("License", details.license),
        )
        while self.inspector_facts.count():
            item = self.inspector_facts.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.fact_labels.clear()
        for index, (caption, value) in enumerate(rows):
            caption_label = QLabel(caption)
            caption_label.setObjectName("downloadsFactLabel")
            caption_label.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
            )
            value_label = WrappedLabel(value)
            value_label.setObjectName("downloadsFactValue")
            value_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            self.inspector_facts.addWidget(caption_label, index, 0)
            self.inspector_facts.addWidget(value_label, index, 1)
            self.fact_labels[caption] = value_label

    def _set_inspector_enabled(self, enabled: bool) -> None:
        if not enabled:
            self.inspector_name.setText("—")
            self.inspector_description.setText(
                "Select a model to see its technical profile."
            )
            self.inspector_tags.setVisible(False)
            self.inspector_best_for.setText("")
            self.inspector_tradeoffs.setText("")
        for widget in (
            self.inspector_best_for_heading,
            self.inspector_best_for,
            self.inspector_tradeoffs_heading,
            self.inspector_tradeoffs,
            self.inspector_source_note,
            self.inspector_repo_button,
            self.inspector_origin_button,
        ):
            widget.setVisible(enabled)

    def _open_repository(self) -> None:
        if self._details is not None:
            QDesktopServices.openUrl(QUrl(self._details.repository_url))

    def _open_origin(self) -> None:
        if self._details is not None:
            QDesktopServices.openUrl(QUrl(self._details.origin_url))

    def _on_open_cache_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(get_hf_cache_dir()))

    # ---- catalog actions ----

    def _on_download_clicked(self, model_name: str) -> None:
        if self.on_download_requested:
            self.on_download_requested(model_name)

    def _on_delete_clicked(self, model_name: str) -> None:
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

    def refresh(self) -> None:
        cached = scan_cached_models()
        settings = self._settings_snapshot()
        active_model = settings_manager.get(
            SettingsKey.WHISPER_MODEL, config.DEFAULT_WHISPER_MODEL
        )
        if active_model not in config.WHISPER_MODEL_CHOICES:
            active_model = config.DEFAULT_WHISPER_MODEL
        meeting_model = resolve_meeting_whisper_model(settings)
        loaded_model = self._get_loaded_model() if self._get_loaded_model else None
        dictation_resolved = active_model
        if active_model == "auto" and loaded_model:
            dictation_resolved = loaded_model
        loaded_repo = resolve_model_repo(loaded_model) if loaded_model else None
        downloads_blocked = is_hf_hub_offline_env_set()
        self.env_banner.setVisible(downloads_blocked)

        seen_repos: Dict[str, CachedModelInfo] = {}
        for model_name, row in self.rows.items():
            info = cached.get(row.repo_id)
            if info is not None:
                seen_repos[row.repo_id] = info
            row.update_state(
                info,
                is_active=False,
                is_loaded=(row.repo_id == loaded_repo),
                downloading=(model_name == self._downloading_model),
                downloads_blocked=downloads_blocked,
                download_slot_busy=(self._downloading_model is not None),
            )
            # Assignment lives in Model Manager; this window only downloads.
            row.set_active_button.setVisible(False)
            row.set_usage(
                self._usage_for(model_name, dictation_resolved, meeting_model)
            )

        total_bytes = sum(info.size_bytes for info in seen_repos.values())
        self.stats_label.setText(
            f"{len(seen_repos)} of {len(self.rows)} Whisper models · "
            f"{format_size_bytes(total_bytes)} used · {get_hf_cache_dir()}"
        )
        self.refresh_components()
        self._apply_filter(self.filter_edit.text())
        if self._selected_model:
            self.inspector_usage.setText(
                self._usage_for(
                    self._selected_model, dictation_resolved, meeting_model
                )
            )
            self.inspector_usage.setVisible(bool(self.inspector_usage.text()))

    def refresh_components(self) -> None:
        for component_id, row in self._component_rows.items():
            row.update_state(
                component_coordinator.describe(component_id),
                component_coordinator.is_installing(component_id),
            )

    def set_component_progress(
        self, component_id: str, phase: str, done: int, total: int
    ) -> None:
        row = self._component_rows.get(component_id)
        if row is not None:
            row.set_progress(phase, done, total)

    def finish_component_install(
        self, component_id: str, success: bool, message: str
    ) -> None:
        self.refresh_components()
        if message:
            self.message_label.setText(message)

    def set_downloading(self, model_name: str) -> None:
        self._downloading_model = model_name
        self.message_label.setText(f'Downloading "{model_name}"…')
        self.refresh()

    def finish_download(self, model_name: str, success: bool) -> None:
        if self._downloading_model == model_name:
            self._downloading_model = None
        self.message_label.setText(
            "" if success else f'Download of "{model_name}" failed'
        )
        self.refresh()

    def show_delete_result(self, model_name: str, success: bool, error: str) -> None:
        if success:
            self.message_label.setText(f'Deleted "{model_name}"')
        else:
            self.message_label.setText(f"Could not delete: {error}")

    # ---- filtering ----

    def _apply_filter(self, _value=None) -> None:
        needle = self.filter_edit.text().strip().lower()
        status = self.status_filter_combo.currentData()
        any_visible = False
        rows = sorted(self.rows.values(), key=self._sort_key)
        for index, row in enumerate(rows):
            self.list_layout.insertWidget(index, row)
            visible = row.matches_filter(needle) if needle else True
            if status == "downloaded":
                visible = visible and row.is_cached
            elif status == "not_downloaded":
                visible = visible and not row.is_cached
            row.setVisible(visible)
            any_visible = any_visible or visible
        self.empty_label.setVisible(not any_visible)

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
        # Recommended: downloaded first, then smallest — keep the order stable
        # so a state change does not make a row jump under the pointer.
        return (not row.is_cached, row.sort_size_bytes, name)

    @staticmethod
    def _usage_for(
        model_name: str, dictation_model: str, meeting_model: str
    ) -> str:
        uses = []
        if model_name == dictation_model:
            uses.append("On-demand")
        if model_name == meeting_model:
            uses.append("Meetings")
        return " · ".join(uses)

    @staticmethod
    def _settings_snapshot() -> dict:
        """Load settings, or an empty dict when the store is unavailable."""
        try:
            return settings_manager.load_all_settings()
        except Exception:
            return {}

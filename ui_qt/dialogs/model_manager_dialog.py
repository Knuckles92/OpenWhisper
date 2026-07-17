"""Model Manager dialog: browse, download, and delete local Whisper models.

Lists the app's known model catalog (``config.WHISPER_MODEL_CHOICES`` minus
``"auto"``) with per-model cache status, real on-disk size, and actions.
Downloads route through the existing Hugging Face consent flow; the dialog
itself never contacts the network.

Unlike the app's other dialogs this one is NON-modal (``show()``, not
``exec()``): downloads are long-running and the user should be able to keep
recording and transcribing while the manager is open. ``UIController`` holds
a single instance and re-raises it instead of stacking copies.
"""
import logging
from typing import Callable, Dict, Optional

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from config import config
from services.hf_access import (
    CachedModelInfo,
    format_size_bytes,
    get_hf_cache_dir,
    resolve_model_repo,
    scan_cached_models,
)
from services.model_catalog import get_model_details
from services.settings import (
    SettingsKey,
    is_hf_hub_offline_env_set,
    settings_manager,
)
from ui_qt.widgets import Button
from ui_qt.dialogs.model_details_dialog import ModelDetailsDialog
from ui_qt.widgets.model_row_widget import ModelRowWidget

logger = logging.getLogger(__name__)


class _CompactStat(QWidget):
    """Small inline statistic used in the Model Manager summary."""

    def __init__(self, label: str, value: str, parent=None):
        super().__init__(parent)
        self.setObjectName("modelManagerStat")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        self.value = QLabel(value)
        self.value.setObjectName("modelManagerStatValue")
        caption = QLabel(label)
        caption.setObjectName("modelManagerStatLabel")
        layout.addWidget(self.value)
        layout.addWidget(caption)

    def set_value(self, value: str) -> None:
        """Update the displayed statistic value."""
        self.value.setText(value)


class ModelManagerDialog(QDialog):
    """Non-modal manager for the local Whisper model cache."""

    def __init__(
        self,
        get_loaded_model: Optional[Callable[[], Optional[str]]] = None,
        parent=None,
    ):
        """Initialize the Model Manager.

        Args:
            get_loaded_model: Provider returning the model name currently
                loaded by the engine (or None). Used to disable Delete on the
                in-use model, whose files are memory-mapped.
        """
        super().__init__(parent)
        self._get_loaded_model = get_loaded_model
        self._downloading_model: Optional[str] = None

        self.setWindowTitle("Model Manager")
        self.setModal(False)
        self.setMinimumSize(720, 500)
        self.resize(720, 560)

        self._setup_ui()
        self.refresh()

    # ── Construction ───────────────────────────────────────────────

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        header_row = QHBoxLayout()
        header_row.setSpacing(8)
        title_block = QVBoxLayout()
        title_block.setSpacing(2)
        title = QLabel("Local models")
        title.setObjectName("headerLabel")
        subtitle = QLabel("Download and choose a Whisper model")
        subtitle.setObjectName("infoLabel")
        title_block.addWidget(title)
        title_block.addWidget(subtitle)
        header_row.addLayout(title_block)
        header_row.addStretch()
        open_folder_btn = Button("Open Folder")
        self._compact_button(open_folder_btn, 110)
        open_folder_btn.setToolTip(
            "Open the folder where downloaded models are stored"
        )
        open_folder_btn.clicked.connect(self._on_open_cache_folder)
        header_row.addWidget(open_folder_btn)
        layout.addLayout(header_row)

        cache_path = get_hf_cache_dir()
        cache_path_label = QLabel(f"Cache: {cache_path}")
        cache_path_label.setObjectName("modelManagerCachePath")
        cache_path_label.setToolTip(cache_path)
        layout.addWidget(cache_path_label)

        self.env_banner = QLabel(
            "Downloads are disabled by the HF_HUB_OFFLINE environment "
            "variable set outside this application."
        )
        self.env_banner.setObjectName("modelManagerEnvBanner")
        self.env_banner.setWordWrap(True)
        self.env_banner.setVisible(False)
        layout.addWidget(self.env_banner)

        stats_row = QHBoxLayout()
        stats_row.setSpacing(8)
        self.downloaded_stat = _CompactStat("downloaded", "0")
        self.disk_stat = _CompactStat("used", "0 B")
        stats_row.addWidget(self.downloaded_stat)
        divider = QLabel("•")
        divider.setObjectName("modelManagerStatLabel")
        stats_row.addWidget(divider)
        stats_row.addWidget(self.disk_stat)
        stats_row.addStretch()
        layout.addLayout(stats_row)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

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
        layout.addLayout(toolbar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
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
            row.set_active_clicked.connect(self._on_set_active_clicked)
            row.details_requested.connect(self._on_details_requested)
            self.rows[model_name] = row
            self.list_layout.addWidget(row)

        self.empty_label = QLabel("No models match")
        self.empty_label.setObjectName("infoLabel")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setVisible(False)
        self.list_layout.addWidget(self.empty_label)

        self.list_layout.addStretch()
        scroll.setWidget(list_container)
        layout.addWidget(scroll, stretch=1)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.message_label = QLabel("")
        self.message_label.setObjectName("infoLabel")
        footer.addWidget(self.message_label, stretch=1)
        close_btn = Button("Close")
        self._compact_button(close_btn, 82)
        close_btn.clicked.connect(self.close)
        footer.addWidget(close_btn)
        layout.addLayout(footer)

    @staticmethod
    def _compact_button(button: Button, width: int) -> None:
        """Size a shared button for the dialog's compact toolbar/footer.

        Uses ``width`` as a preferred size floor, but never caps maxWidth below
        the label's natural width so text like "Open Folder" is not clipped.
        """
        button.set_base_minimum_size(width, 34)
        button.setMinimumHeight(34)
        button.setMaximumHeight(34)
        fitted = button.minimumWidth()
        button.setMaximumWidth(max(width, fitted))

    # ── Callback plumbing (dialog signals) ─────────────────────────

    #: Assigned by UIController; called with the model name.
    on_download_requested: Optional[Callable[[str], None]] = None
    on_delete_requested: Optional[Callable[[str], None]] = None
    on_set_active_requested: Optional[Callable[[str], None]] = None

    def _on_download_clicked(self, model_name: str):
        if self.on_download_requested:
            self.on_download_requested(model_name)

    def _on_delete_clicked(self, model_name: str):
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

    def _on_set_active_clicked(self, model_name: str):
        if self.on_set_active_requested:
            self.on_set_active_requested(model_name)
        self.refresh()

    def _on_details_requested(self, model_name: str) -> None:
        """Open the bundled technical profile for a selected model."""
        details = get_model_details(model_name)
        dialog = ModelDetailsDialog(details, parent=self)
        dialog.exec()

    def _on_open_cache_folder(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(get_hf_cache_dir()))

    # ── State updates ──────────────────────────────────────────────

    def refresh(self) -> None:
        """Re-scan the cache and re-render every row and the header stats."""
        cached = scan_cached_models()
        active_model = settings_manager.get(
            SettingsKey.WHISPER_MODEL, config.DEFAULT_WHISPER_MODEL
        )
        loaded_model = self._get_loaded_model() if self._get_loaded_model else None
        if active_model == "auto" and loaded_model:
            # "auto" resolved to whatever is loaded — badge that row instead.
            active_model = loaded_model
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
                is_active=(model_name == active_model),
                is_loaded=(row.repo_id == loaded_repo),
                downloading=(model_name == self._downloading_model),
                downloads_blocked=downloads_blocked,
                download_slot_busy=(self._downloading_model is not None),
            )

        self.downloaded_stat.set_value(str(len(seen_repos)))
        total_bytes = sum(info.size_bytes for info in seen_repos.values())
        self.disk_stat.set_value(format_size_bytes(total_bytes))
        self._apply_filter(self.filter_edit.text())

    def set_downloading(self, model_name: str) -> None:
        """Mark a model as downloading (badge + disabled buttons)."""
        self._downloading_model = model_name
        self.message_label.setText(f'Downloading "{model_name}"…')
        self.refresh()

    def finish_download(self, model_name: str, success: bool) -> None:
        """Clear the downloading state once a download ends."""
        if self._downloading_model == model_name:
            self._downloading_model = None
        self.message_label.setText(
            "" if success else f'Download of "{model_name}" failed'
        )
        self.refresh()

    def show_delete_result(self, model_name: str, success: bool, error: str) -> None:
        """Report a delete outcome (row refresh arrives via cache-changed)."""
        if success:
            self.message_label.setText(f'Deleted "{model_name}"')
        else:
            self.message_label.setText(f"Could not delete: {error}")

    # ── Filter ─────────────────────────────────────────────────────

    def _apply_filter(self, _value=None):
        """Filter and sort rows using the current toolbar selections."""
        text = self.filter_edit.text()
        needle = text.strip().lower()
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
        # Recommended: downloaded first, then smallest — keep order stable when
        # the active model changes so Set Active does not jump the row.
        return (not row.is_cached, row.sort_size_bytes, name)

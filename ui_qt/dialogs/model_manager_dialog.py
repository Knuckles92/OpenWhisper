"""Model Manager dialog for speech-recognition and text-processing models.

The Voice tab lists the app's known Whisper catalog with per-model cache
status, real on-disk size, and actions. Voice downloads route through the
existing Hugging Face consent flow; the dialog itself never downloads model
files. The Text tab owns the provider/catalog/model selection used by AI
transcript cleanup and loads provider catalogs in a background thread.

Unlike the app's other dialogs this one is NON-modal (``show()``, not
``exec()``): downloads are long-running and the user should be able to keep
recording and transcribing while the manager is open. ``UIController`` holds
a single instance and re-raises it instead of stacking copies.
"""
import logging
import threading
from typing import Callable, Dict, Optional

from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QScrollArea,
    QTabWidget,
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
from services.model_catalog import get_model_details
from services.settings import (
    SettingsKey,
    TranscriptCleanupModelSort,
    TranscriptCleanupProvider,
    default_transcript_cleanup_model,
    is_hf_hub_offline_env_set,
    settings_manager,
)
from services.transcript_cleanup import find_api_key
from ui_qt.widgets import Button, NoWheelComboBox, SearchableComboBox
from ui_qt.dialogs.model_details_dialog import ModelDetailsDialog
from ui_qt.widgets.component_row_widget import ComponentRowWidget
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


class _TextModelPicker(QWidget):
    """Single-flow provider and text-model selection controls."""

    provider_changed = pyqtSignal(str)
    refresh_requested = pyqtSignal(str)
    activation_requested = pyqtSignal(str)
    sort_changed = pyqtSignal(str)

    _PROVIDERS = (
        (
            TranscriptCleanupProvider.OPENAI,
            "OpenAI",
            "Direct access to OpenAI chat and reasoning models.",
            "Requires OPENAI_API_KEY",
            "OAI",
        ),
        (
            TranscriptCleanupProvider.OPENROUTER,
            "OpenRouter",
            "One catalog with models from OpenAI, Anthropic, Google, and more.",
            "Requires OPENROUTER_API_KEY",
            "OR",
        ),
    )

    _SORT_OPTIONS = (
        ("A → Z", TranscriptCleanupModelSort.ALPHABETICAL),
        ("Most popular", TranscriptCleanupModelSort.MOST_POPULAR),
        ("Top this week", TranscriptCleanupModelSort.TOP_WEEKLY),
        ("Newest", TranscriptCleanupModelSort.NEWEST),
        ("Cheapest first", TranscriptCleanupModelSort.PRICING_LOW_TO_HIGH),
        ("Priciest first", TranscriptCleanupModelSort.PRICING_HIGH_TO_LOW),
        ("Largest context", TranscriptCleanupModelSort.CONTEXT_HIGH_TO_LOW),
        (
            "Highest throughput",
            TranscriptCleanupModelSort.THROUGHPUT_HIGH_TO_LOW,
        ),
        ("Lowest latency", TranscriptCleanupModelSort.LATENCY_LOW_TO_HIGH),
    )

    def __init__(self, parent=None):
        """Build the text-model picker.

        Args:
            parent: Optional owning widget.
        """
        super().__init__(parent)
        self.provider = TranscriptCleanupProvider.OPENAI
        self._active_provider = ""
        self._active_model = ""
        self._draft_models = {
            provider: default_transcript_cleanup_model(provider)
            for provider in TranscriptCleanupProvider.ALL
        }
        self._setup_ui()

    def _setup_ui(self) -> None:
        """Construct the single provider-to-model selection flow."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(12)

        card = QFrame()
        card.setObjectName("textModelPickerCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 18, 20, 20)
        card_layout.setSpacing(10)

        provider_label = QLabel("1  Choose a provider")
        provider_label.setObjectName("textModelSectionLabel")
        card_layout.addWidget(provider_label)

        self.provider_combo = NoWheelComboBox()
        self.provider_combo.setObjectName("textModelProviderCombo")
        self.provider_combo.setMinimumHeight(40)
        for provider, name, _description, _requirement, _mark in self._PROVIDERS:
            self.provider_combo.addItem(name, provider)
        self.provider_combo.currentIndexChanged.connect(
            self._on_provider_combo_changed
        )
        card_layout.addWidget(self.provider_combo)

        identity_row = QHBoxLayout()
        identity_row.setSpacing(12)
        self.provider_mark = QLabel("OAI")
        self.provider_mark.setObjectName("textProviderMark")
        self.provider_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.provider_mark.setFixedSize(44, 44)
        identity_row.addWidget(self.provider_mark)

        copy = QVBoxLayout()
        copy.setSpacing(2)
        self.provider_title = QLabel("OpenAI")
        self.provider_title.setObjectName("textProviderTitle")
        self.provider_description = QLabel("")
        self.provider_description.setObjectName("textProviderDescription")
        self.provider_description.setWordWrap(True)
        self.provider_requirement = QLabel("")
        self.provider_requirement.setObjectName("textProviderRequirement")
        copy.addWidget(self.provider_title)
        copy.addWidget(self.provider_description)
        copy.addWidget(self.provider_requirement)
        identity_row.addLayout(copy, stretch=1)
        card_layout.addLayout(identity_row)

        self.active_summary = QLabel("")
        self.active_summary.setObjectName("textModelActiveSummary")
        self.active_summary.setWordWrap(True)
        card_layout.addWidget(self.active_summary)

        model_label = QLabel("2  Choose a model")
        model_label.setObjectName("textModelSectionLabel")
        card_layout.addWidget(model_label)

        sort_row = QHBoxLayout()
        sort_row.setSpacing(8)
        self.sort_label = QLabel("Sort catalog")
        self.sort_label.setObjectName("textModelFieldLabel")
        sort_row.addWidget(self.sort_label)
        self.sort_combo = NoWheelComboBox()
        self.sort_combo.setObjectName("textModelSort")
        for label, value in self._SORT_OPTIONS:
            self.sort_combo.addItem(label, value)
        self.sort_combo.setMinimumHeight(36)
        self.sort_combo.currentIndexChanged.connect(
            lambda: self.sort_changed.emit(self.provider)
        )
        sort_row.addWidget(self.sort_combo, stretch=1)
        card_layout.addLayout(sort_row)

        model_row = QHBoxLayout()
        model_row.setSpacing(8)
        self.model_combo = SearchableComboBox()
        self.model_combo.setObjectName("textModelCombo")
        self.model_combo.setMinimumHeight(38)
        self.model_combo.setToolTip(
            "Choose from the provider catalog or enter a model id directly"
        )
        self.model_combo.currentTextChanged.connect(self._on_model_changed)
        model_row.addWidget(self.model_combo, stretch=1)

        self.refresh_button = Button("Refresh")
        self.refresh_button.setObjectName("textModelRefreshButton")
        self.refresh_button.setToolTip("Reload the selected provider's catalog")
        self.refresh_button.clicked.connect(
            lambda: self.refresh_requested.emit(self.provider)
        )
        model_row.addWidget(self.refresh_button)
        card_layout.addLayout(model_row)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.status_label = QLabel("Open Text to load the model catalog.")
        self.status_label.setObjectName("textModelStatus")
        self.status_label.setWordWrap(True)
        action_row.addWidget(self.status_label, stretch=1)
        self.activate_button = Button("Use This Model")
        self.activate_button.setObjectName("textModelActivateButton")
        self.activate_button.clicked.connect(
            lambda: self.activation_requested.emit(self.provider)
        )
        action_row.addWidget(self.activate_button)
        card_layout.addLayout(action_row)

        layout.addWidget(card)
        layout.addStretch()
        self._render_provider()

    def set_provider(self, provider: str, model: Optional[str] = None) -> None:
        """Show one provider and optionally stage a model for it.

        Args:
            provider: A ``TranscriptCleanupProvider`` value.
            model: Optional model id to stage for the provider.
        """
        if provider not in TranscriptCleanupProvider.ALL:
            return
        self._save_current_draft()
        if model:
            self._draft_models[provider] = model
        index = self.provider_combo.findData(provider)
        self.provider_combo.blockSignals(True)
        self.provider_combo.setCurrentIndex(max(0, index))
        self.provider_combo.blockSignals(False)
        self.provider = provider
        self._render_provider()

    def _on_provider_combo_changed(self, _index: int) -> None:
        """Swap the in-place catalog when the provider selector changes."""
        self._save_current_draft()
        provider = self.provider_combo.currentData()
        if provider not in TranscriptCleanupProvider.ALL:
            return
        self.provider = provider
        self._render_provider()
        self.provider_changed.emit(provider)

    def _provider_details(self) -> tuple:
        """Return display metadata for the selected provider."""
        for details in self._PROVIDERS:
            if details[0] == self.provider:
                return details
        return self._PROVIDERS[0]

    def _render_provider(self) -> None:
        """Update provider copy and the staged model without changing pages."""
        _provider, name, description, requirement, mark = self._provider_details()
        self.provider_mark.setText(mark)
        self.provider_title.setText(name)
        self.provider_description.setText(description)
        self._update_credential_status(requirement)
        show_sort = self.provider == TranscriptCleanupProvider.OPENROUTER
        self.sort_label.setVisible(show_sort)
        self.sort_combo.setVisible(show_sort)

        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.setCurrentText(
            self._draft_models.get(
                self.provider, default_transcript_cleanup_model(self.provider)
            )
        )
        self.model_combo.blockSignals(False)
        self.status_label.setText("Loading models…")
        self._update_active_summary()

    def _update_credential_status(self, requirement: Optional[str] = None) -> None:
        """Show whether the selected provider's API key is available.

        Args:
            requirement: Missing-key copy from the selected provider metadata.
                When omitted, the copy is looked up from that metadata.
        """
        if requirement is None:
            requirement = self._provider_details()[3]
        credential_name = requirement.removeprefix("Requires ")
        available = bool(find_api_key(self.provider))
        self.provider_requirement.setText(
            f"✓ {credential_name} found" if available else requirement
        )
        self.provider_requirement.setProperty("available", available)
        self.provider_requirement.style().unpolish(self.provider_requirement)
        self.provider_requirement.style().polish(self.provider_requirement)
        self.provider_requirement.update()

    def _save_current_draft(self) -> None:
        """Remember typed model ids independently for each provider."""
        model = self.model_combo.currentText().strip()
        if model:
            self._draft_models[self.provider] = model

    def _on_model_changed(self, text: str) -> None:
        """Track the staged model and refresh activation affordance."""
        if text.strip():
            self._draft_models[self.provider] = text.strip()
        self._update_activation_button()

    def current_sort(self) -> str:
        """Return the catalog order applicable to this provider.

        Returns:
            A ``TranscriptCleanupModelSort`` value.
        """
        if self.provider != TranscriptCleanupProvider.OPENROUTER:
            return TranscriptCleanupModelSort.ALPHABETICAL
        return (
            self.sort_combo.currentData()
            or TranscriptCleanupModelSort.ALPHABETICAL
        )

    def set_sort(self, sort: str) -> None:
        """Select a saved catalog order without triggering a refresh.

        Args:
            sort: A ``TranscriptCleanupModelSort`` value.
        """
        index = self.sort_combo.findData(sort)
        self.sort_combo.blockSignals(True)
        self.sort_combo.setCurrentIndex(max(0, index))
        self.sort_combo.blockSignals(False)

    def set_models(self, models: list) -> None:
        """Populate catalog choices while preserving the staged model id.

        Args:
            models: Model ids returned by the selected provider.
        """
        current = self._draft_models.get(self.provider, "").strip()
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.addItems(models)
        self.model_combo.setCurrentText(
            current or default_transcript_cleanup_model(self.provider)
        )
        self.model_combo.blockSignals(False)
        self._update_activation_button()

    def set_loading(self, loading: bool) -> None:
        """Render catalog loading state.

        Args:
            loading: Whether a catalog request is in flight.
        """
        self.refresh_button.setEnabled(not loading)
        if loading:
            self.status_label.setText("Loading models…")

    def set_active_selection(self, provider: str, model: str) -> None:
        """Update the always-visible summary of the active selection.

        Args:
            provider: Active ``TranscriptCleanupProvider`` value.
            model: Active model id.
        """
        self._active_provider = provider
        self._active_model = model
        self._update_active_summary()

    def _update_active_summary(self) -> None:
        """Render the active provider/model independently of staged choices."""
        provider_name = (
            "OpenAI"
            if self._active_provider == TranscriptCleanupProvider.OPENAI
            else "OpenRouter"
        )
        self.active_summary.setText(
            f"Active now: {provider_name} · {self._active_model}"
        )
        self._update_activation_button()

    def _update_activation_button(self, _text: str = "") -> None:
        """Disable activation only when the staged selection is already active."""
        selected = self.model_combo.currentText().strip()
        is_active = (
            self.provider == self._active_provider
            and selected == self._active_model
        )
        self.activate_button.setText("Active" if is_active else "Use This Model")
        self.activate_button.setEnabled(bool(selected) and not is_active)


class ModelManagerDialog(QDialog):
    """Non-modal home for voice and text model selection."""

    # Re-emitted for the controller; the dialog never installs anything itself.
    component_install_requested = pyqtSignal(str)
    component_cancel_requested = pyqtSignal(str)
    component_remove_requested = pyqtSignal(str)
    _text_models_loaded = pyqtSignal(str, str, list, str)

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
        self._component_rows: Dict[str, ComponentRowWidget] = {}
        self._text_models_cache: Dict[tuple, list] = {}
        self._text_models_loading = set()
        self._active_text_provider = TranscriptCleanupProvider.OPENAI
        self._active_text_model = default_transcript_cleanup_model(
            self._active_text_provider
        )

        self.setWindowTitle("Model Manager")
        self.setModal(False)
        self.setMinimumSize(760, 540)

        self._setup_ui()
        self.resize(820, 650)
        self._text_models_loaded.connect(self._on_text_models_loaded)
        self.refresh()

    # ── Construction ───────────────────────────────────────────────

    def _build_components_section(self) -> QVBoxLayout:
        """Build the optional-components group shown above the model list.

        Components live here rather than in Settings because this dialog is
        deliberately non-modal — a multi-gigabyte download must not lock the
        user out of the app — and because Settings commits on accept, which
        does not compose with an in-flight install.

        Returns an empty layout on platforms with no installable components, so
        no heading advertises a section with nothing in it.
        """
        section = QVBoxLayout()
        section.setSpacing(6)

        infos = component_coordinator.list_components()
        if not infos:
            return section

        heading = QLabel("Components")
        heading.setObjectName("headerLabel")
        section.addWidget(heading)

        caption = QLabel(
            "Optional add-ons. These are downloaded on demand so the "
            "installer stays small."
        )
        caption.setObjectName("infoLabel")
        caption.setWordWrap(True)
        section.addWidget(caption)

        for info in infos:
            row = ComponentRowWidget(info.component_id)
            row.install_clicked.connect(self.component_install_requested)
            row.cancel_clicked.connect(self.component_cancel_requested)
            row.remove_clicked.connect(self._confirm_component_removal)
            self._component_rows[info.component_id] = row
            section.addWidget(row)

        return section

    def refresh_components(self) -> None:
        """Re-read component state from disk and re-render every row."""
        for component_id, row in self._component_rows.items():
            row.update_state(
                component_coordinator.describe(component_id),
                component_coordinator.is_installing(component_id),
            )

    def set_component_progress(
        self, component_id: str, phase: str, done: int, total: int
    ) -> None:
        """Forward install progress to the matching row."""
        row = self._component_rows.get(component_id)
        if row is not None:
            row.set_progress(phase, done, total)

    def finish_component_install(
        self, component_id: str, success: bool, message: str
    ) -> None:
        """Render the outcome of an install attempt."""
        self.refresh_components()
        if message:
            self.message_label.setText(message)

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

    def _setup_ui(self) -> None:
        """Build the shared shell and the Voice/Text tabs."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        title = QLabel("Model Manager")
        title.setObjectName("modelManagerTitle")
        subtitle = QLabel(
            "Choose the models that turn speech into text and refine the result."
        )
        subtitle.setObjectName("modelManagerSubtitle")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("modelManagerTabs")
        self.tabs.tabBar().setCursor(Qt.CursorShape.PointingHandCursor)
        self.voice_tab = self._build_voice_tab()
        self.text_tab = self._build_text_tab()
        self.tabs.addTab(self.voice_tab, "Voice")
        self.tabs.addTab(self.text_tab, "Text")
        self.tabs.currentChanged.connect(self._on_manager_tab_changed)
        layout.addWidget(self.tabs, stretch=1)

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

    def _build_voice_tab(self) -> QWidget:
        """Build the local Whisper catalog tab."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 14, 4, 4)
        layout.setSpacing(10)

        header_row = QHBoxLayout()
        header_row.setSpacing(8)
        title_block = QVBoxLayout()
        title_block.setSpacing(2)
        title = QLabel("Speech recognition")
        title.setObjectName("headerLabel")
        subtitle = QLabel("Download and choose a local Whisper model")
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

        layout.addLayout(self._build_components_section())

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
        return tab

    def _build_text_tab(self) -> QWidget:
        """Build one linear provider-to-model cleanup flow."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 14, 4, 4)
        layout.setSpacing(10)

        title = QLabel("Text processing")
        title.setObjectName("headerLabel")
        subtitle = QLabel(
            "Choose the provider and chat model used by AI transcript cleanup."
        )
        subtitle.setObjectName("infoLabel")
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        self.text_model_picker = _TextModelPicker()
        self.text_model_picker.provider_changed.connect(
            self._on_text_provider_changed
        )
        self.text_model_picker.refresh_requested.connect(
            lambda provider: self._fetch_text_models(provider, force=True)
        )
        self.text_model_picker.activation_requested.connect(
            self._activate_text_model
        )
        self.text_model_picker.sort_changed.connect(self._on_text_sort_changed)
        layout.addWidget(self.text_model_picker, stretch=1)

        note = QLabel(
            "Text models are called only when AI cleanup is enabled. Cleanup "
            "behavior, prompts, and learned rules remain in Settings → Cleanup."
        )
        note.setObjectName("textModelFootnote")
        note.setWordWrap(True)
        layout.addWidget(note)
        return tab

    @staticmethod
    def _compact_button(button: Button, width: int) -> None:
        """Size a shared button for the dialog's compact toolbar/footer.

        Uses ``width`` as a preferred size floor, but never caps maxWidth below
        the polished sizeHint so text like "Open Folder" is not clipped on
        macOS (where theme font metrics differ from the Button constructor font).
        """
        button.set_base_minimum_size(width, 34)
        button.setMinimumHeight(34)
        button.setMaximumHeight(34)
        button.ensurePolished()
        fitted = max(width, button.minimumWidth(), button.sizeHint().width())
        button.setMinimumWidth(fitted)
        button.setMaximumWidth(fitted)

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

    def _load_text_settings(self) -> None:
        """Load the active cleanup choice into the single text-model picker."""
        provider = settings_manager.get(
            SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER,
            config.TRANSCRIPT_CLEANUP_PROVIDER,
        )
        if provider not in TranscriptCleanupProvider.ALL:
            provider = config.TRANSCRIPT_CLEANUP_PROVIDER

        model = settings_manager.get(
            SettingsKey.TRANSCRIPT_CLEANUP_MODEL,
            default_transcript_cleanup_model(provider),
        )
        if not isinstance(model, str) or not model.strip():
            model = default_transcript_cleanup_model(provider)
        model = model.strip()

        sort = settings_manager.get(
            SettingsKey.TRANSCRIPT_CLEANUP_MODEL_SORT,
            config.TRANSCRIPT_CLEANUP_MODEL_SORT,
        )
        if sort not in TranscriptCleanupModelSort.ALL:
            sort = config.TRANSCRIPT_CLEANUP_MODEL_SORT

        self._active_text_provider = provider
        self._active_text_model = model
        self.text_model_picker.set_sort(sort)
        self.text_model_picker.set_provider(provider, model)
        self.text_model_picker.set_active_selection(provider, model)

    def _on_manager_tab_changed(self, index: int) -> None:
        """Load the selected provider catalog when Text is first opened."""
        if self.tabs.widget(index) is self.text_tab:
            self._fetch_text_models(self.text_model_picker.provider)

    def _on_text_provider_changed(self, provider: str) -> None:
        """Load a newly selected provider in the same model picker."""
        if self.tabs.currentWidget() is self.text_tab:
            self._fetch_text_models(provider)

    def _on_text_sort_changed(self, provider: str) -> None:
        """Persist OpenRouter's catalog order and reload that catalog."""
        try:
            settings_manager.save_setting(
                SettingsKey.TRANSCRIPT_CLEANUP_MODEL_SORT,
                self.text_model_picker.current_sort(),
            )
        except Exception as exc:
            logger.warning("Couldn't save text model sort: %s", exc)
        self._fetch_text_models(provider)

    def _fetch_text_models(self, provider: str, force: bool = False) -> None:
        """Load one provider's text-model catalog on a worker thread.

        Args:
            provider: A ``TranscriptCleanupProvider`` value.
            force: Bypass the in-dialog cache when true.
        """
        if provider == self.text_model_picker.provider:
            self.text_model_picker._update_credential_status()
        sort = self.text_model_picker.current_sort()
        key = (provider, sort)
        if not force and key in self._text_models_cache:
            models = self._text_models_cache[key]
            if provider == self.text_model_picker.provider:
                self.text_model_picker.set_models(models)
                self.text_model_picker.set_loading(False)
                self.text_model_picker.status_label.setText(
                    f"{len(models)} models available"
                )
            return
        if key in self._text_models_loading:
            if provider == self.text_model_picker.provider:
                self.text_model_picker.set_loading(True)
            return

        self._text_models_loading.add(key)
        if provider == self.text_model_picker.provider:
            self.text_model_picker.set_loading(True)

        def worker():
            try:
                from services.transcript_cleanup import list_cleanup_models

                models = list_cleanup_models(provider, sort=sort)
                error = ""
            except Exception as exc:
                models = []
                error = str(exc)
            try:
                self._text_models_loaded.emit(provider, sort, models, error)
            except RuntimeError:
                pass  # Dialog was destroyed before the catalog finished.

        threading.Thread(
            target=worker,
            name=f"text-models-{provider}",
            daemon=True,
        ).start()

    def _on_text_models_loaded(
        self, provider: str, sort: str, models: list, error: str
    ) -> None:
        """Apply a provider catalog result on the Qt thread."""
        key = (provider, sort)
        self._text_models_loading.discard(key)
        provider_loading = any(
            loading_provider == provider
            for loading_provider, _loading_sort in self._text_models_loading
        )
        if not error:
            self._text_models_cache[key] = models
        if provider != self.text_model_picker.provider:
            return
        self.text_model_picker.set_loading(provider_loading)
        if sort != self.text_model_picker.current_sort():
            return
        if error:
            self.text_model_picker.status_label.setText(
                f"Couldn't load models: {error}"
            )
            return
        self.text_model_picker.set_models(models)
        self.text_model_picker.status_label.setText(
            f"{len(models)} models available"
        )

    def _activate_text_model(self, provider: str) -> None:
        """Persist one provider/model pair as the cleanup model."""
        if provider != self.text_model_picker.provider:
            return
        model = self.text_model_picker.model_combo.currentText().strip()
        if not model:
            return
        try:
            settings = settings_manager.load_all_settings()
            settings[SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER] = provider
            settings[SettingsKey.TRANSCRIPT_CLEANUP_MODEL] = model
            settings_manager.save_all_settings(settings)
        except Exception as exc:
            logger.error("Couldn't activate text model: %s", exc)
            self.message_label.setText(f"Couldn't set text model: {exc}")
            return

        self._active_text_provider = provider
        self._active_text_model = model
        self.text_model_picker.set_active_selection(provider, model)
        display_provider = "OpenAI" if provider == "openai" else "OpenRouter"
        self.message_label.setText(
            f"Text model set to {display_provider} · {model}"
        )

    # ── State updates ──────────────────────────────────────────────

    def refresh(self) -> None:
        """Refresh voice cache state and the active text-model selection."""
        self._load_text_settings()
        self.refresh_components()
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

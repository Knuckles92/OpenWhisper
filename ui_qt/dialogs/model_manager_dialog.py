"""Model Manager dialog for speech, cleanup, and meeting models.

The Voice tab lists the app's known Whisper catalog with per-model cache
status, real on-disk size, and actions. Voice downloads route through the
existing Hugging Face consent flow; the dialog itself never downloads model
files. The Text tab owns the provider/catalog/model selection used by AI
transcript cleanup. The Meeting tab owns meeting ASR and intelligence model
selection and loads LLM catalogs in a background thread.

Unlike the app's other dialogs this one is NON-modal (``show()``, not
``exec()``): downloads are long-running and the user should be able to keep
recording and transcribing while the manager is open. ``UIController`` holds
a single instance and re-raises it instead of stacking copies.
"""
import logging
import threading
from pathlib import Path
from typing import Callable, Dict, Optional

from PyQt6.QtCore import QSize, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QIcon
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

from config import bundle_root, config
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
    resolve_meeting_llm_model,
    resolve_meeting_llm_provider,
    settings_manager,
)
from services.transcript_cleanup import find_api_key
from ui_qt.dialogs.model_details_dialog import ModelDetailsDialog
from ui_qt.utils.app_icon import app_icon
from ui_qt.widgets import Button, NoWheelComboBox, SearchableComboBox
from ui_qt.widgets.component_row_widget import ComponentRowWidget
from ui_qt.widgets.model_row_widget import ModelRowWidget

logger = logging.getLogger(__name__)


def _design_icon(filename: str) -> QIcon:
    """Load a bundled Tabler icon used by the Model Manager."""
    path = Path(bundle_root()) / "ui_qt" / "assets" / "tabler" / filename
    icon = QIcon(str(path))
    # Preserve the semantic icon color for disabled current-state buttons.
    icon.addPixmap(icon.pixmap(24, 24), QIcon.Mode.Disabled, QIcon.State.Off)
    return icon


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

    def __init__(
        self,
        parent=None,
        idle_status: str = "Open Text to load the model catalog.",
    ):
        """Build the text-model picker.

        Args:
            parent: Optional owning widget.
            idle_status: Placeholder shown before a catalog is fetched.
        """
        super().__init__(parent)
        self.provider = TranscriptCleanupProvider.OPENAI
        self._active_provider = ""
        self._active_model = ""
        self._idle_status = idle_status
        self._draft_models = {
            provider: default_transcript_cleanup_model(provider)
            for provider in TranscriptCleanupProvider.ALL
        }
        self._setup_ui()

    @staticmethod
    def _step_heading(number: str, title: str) -> QHBoxLayout:
        """Build the numbered heading row shared by both section cards."""
        heading = QHBoxLayout()
        heading.setSpacing(10)
        step = QLabel(number)
        step.setObjectName("textModelStepNumber")
        step.setAlignment(Qt.AlignmentFlag.AlignCenter)
        step.setFixedSize(28, 28)
        title_label = QLabel(title)
        title_label.setObjectName("textModelStepTitle")
        heading.addWidget(step)
        heading.addWidget(title_label)
        heading.addStretch()
        return heading

    def _setup_ui(self) -> None:
        """Construct the side-by-side provider and model selection cards.

        The two numbered steps sit in equal-width columns so the whole flow
        fits the dialog's compact default height without scrolling; the active
        banner and primary action pin to the bottom edge of their cards.
        """
        self.setObjectName("textModelPicker")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(12)

        self.provider_card = QFrame()
        self.provider_card.setObjectName("textModelSectionCard")
        provider_layout = QVBoxLayout(self.provider_card)
        provider_layout.setContentsMargins(16, 14, 16, 14)
        provider_layout.setSpacing(10)
        provider_layout.addLayout(self._step_heading("1", "Choose a provider"))

        self.provider_combo = NoWheelComboBox()
        self.provider_combo.setObjectName("textModelProviderCombo")
        self.provider_combo.setMinimumHeight(44)
        provider_icons = {
            TranscriptCleanupProvider.OPENAI: _design_icon("box-blue.svg"),
            TranscriptCleanupProvider.OPENROUTER: _design_icon(
                "stack-purple.svg"
            ),
        }
        for provider, name, _description, _requirement, _mark in self._PROVIDERS:
            self.provider_combo.addItem(provider_icons[provider], name, provider)
        self.provider_combo.setIconSize(QSize(22, 22))
        self.provider_combo.currentIndexChanged.connect(
            self._on_provider_combo_changed
        )
        provider_layout.addWidget(self.provider_combo)

        self.provider_identity_card = QFrame()
        self.provider_identity_card.setObjectName("textProviderIdentityCard")
        identity_card_layout = QVBoxLayout(self.provider_identity_card)
        identity_card_layout.setContentsMargins(12, 10, 12, 10)
        identity_card_layout.setSpacing(10)

        identity_row = QHBoxLayout()
        identity_row.setSpacing(12)
        self.provider_mark = QLabel("OAI")
        self.provider_mark.setObjectName("textProviderMark")
        self.provider_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.provider_mark.setFixedSize(44, 44)
        identity_row.addWidget(
            self.provider_mark, alignment=Qt.AlignmentFlag.AlignTop
        )

        copy = QVBoxLayout()
        copy.setSpacing(3)
        self.provider_title = QLabel("OpenAI")
        self.provider_title.setObjectName("textProviderTitle")
        self.provider_description = QLabel("")
        self.provider_description.setObjectName("textProviderDescription")
        self.provider_description.setWordWrap(True)
        self.provider_requirement = QLabel("")
        self.provider_requirement.setObjectName("textProviderRequirement")
        credential_row = QHBoxLayout()
        credential_row.setSpacing(6)
        self.provider_credential_icon = QLabel()
        self.provider_credential_icon.setObjectName("textProviderCredentialIcon")
        self.provider_credential_icon.setFixedSize(16, 16)
        credential_row.addWidget(self.provider_credential_icon)
        credential_row.addWidget(self.provider_requirement)
        credential_row.addStretch()
        copy.addWidget(self.provider_title)
        copy.addWidget(self.provider_description)
        copy.addLayout(credential_row)
        identity_row.addLayout(copy, stretch=1)
        identity_card_layout.addLayout(identity_row)
        provider_layout.addWidget(self.provider_identity_card)
        provider_layout.addStretch()

        self.active_summary_card = QFrame()
        self.active_summary_card.setObjectName("textModelActiveCard")
        self.active_summary_card.setMinimumHeight(56)
        active_layout = QHBoxLayout(self.active_summary_card)
        active_layout.setContentsMargins(14, 8, 14, 8)
        active_layout.setSpacing(12)
        self.active_summary_icon = QLabel()
        self.active_summary_icon.setObjectName("textModelActiveIcon")
        self.active_summary_icon.setFixedSize(24, 24)
        self.active_summary_icon.setPixmap(
            _design_icon("bolt-green.svg").pixmap(24, 24)
        )
        self.active_summary = QLabel("")
        self.active_summary.setObjectName("textModelActiveSummary")
        self.active_summary.setWordWrap(True)
        active_layout.addWidget(self.active_summary_icon)
        active_layout.addWidget(self.active_summary, stretch=1)
        provider_layout.addWidget(self.active_summary_card)
        layout.addWidget(self.provider_card, stretch=1)

        self.model_card = QFrame()
        self.model_card.setObjectName("textModelSectionCard")
        model_card_layout = QVBoxLayout(self.model_card)
        model_card_layout.setContentsMargins(16, 14, 16, 14)
        model_card_layout.setSpacing(10)
        model_card_layout.addLayout(self._step_heading("2", "Choose a model"))

        sort_row = QHBoxLayout()
        sort_row.setSpacing(10)
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
        model_card_layout.addLayout(sort_row)

        model_row = QHBoxLayout()
        model_row.setSpacing(10)
        self.model_combo = SearchableComboBox()
        self.model_combo.setObjectName("textModelCombo")
        self.model_combo.setMinimumHeight(44)
        self._model_icon = _design_icon("box-blue.svg")
        self.model_combo.setIconSize(QSize(20, 20))
        self.model_combo.setToolTip(
            "Choose from the provider catalog or enter a model id directly"
        )
        self.model_combo.currentTextChanged.connect(self._on_model_changed)
        model_row.addWidget(self.model_combo, stretch=1)

        self.refresh_button = Button("Refresh")
        self.refresh_button.setObjectName("textModelRefreshButton")
        self.refresh_button.setIcon(_design_icon("refresh-blue.svg"))
        self.refresh_button.setIconSize(QSize(16, 16))
        self.refresh_button.set_base_minimum_size(104, 44)
        self.refresh_button.setToolTip("Reload the selected provider's catalog")
        self.refresh_button.clicked.connect(
            lambda: self.refresh_requested.emit(self.provider)
        )
        model_row.addWidget(self.refresh_button)
        model_card_layout.addLayout(model_row)

        self.catalog_summary = QFrame()
        self.catalog_summary.setObjectName("textModelCatalogSummary")
        summary_layout = QHBoxLayout(self.catalog_summary)
        summary_layout.setContentsMargins(10, 6, 10, 6)
        summary_layout.setSpacing(10)
        self.catalog_status_icon = QLabel()
        self.catalog_status_icon.setObjectName("textModelCatalogIcon")
        self.catalog_status_icon.setFixedSize(20, 20)
        self.catalog_status_icon.setPixmap(
            _design_icon("stack-slate.svg").pixmap(20, 20)
        )
        summary_layout.addWidget(self.catalog_status_icon)

        self.status_label = QLabel(self._idle_status)
        self.status_label.setObjectName("textModelStatus")
        self.status_label.setWordWrap(True)
        summary_layout.addWidget(self.status_label)
        catalog_separator = QFrame()
        catalog_separator.setObjectName("textModelCatalogSeparator")
        catalog_separator.setFixedHeight(1)
        summary_layout.addWidget(catalog_separator, stretch=1)
        model_card_layout.addWidget(self.catalog_summary)
        model_card_layout.addStretch()

        action_row = QHBoxLayout()
        action_row.setSpacing(10)
        self.current_model_card = QFrame()
        self.current_model_card.setObjectName("textCurrentModelCard")
        current_layout = QHBoxLayout(self.current_model_card)
        current_layout.setContentsMargins(10, 6, 10, 6)
        current_layout.setSpacing(8)
        self.current_model_icon = QLabel()
        self.current_model_icon.setObjectName("textCurrentModelIcon")
        self.current_model_icon.setFixedSize(24, 24)
        self.current_model_icon.setPixmap(
            _design_icon("box-blue.svg").pixmap(22, 22)
        )
        current_layout.addWidget(self.current_model_icon)
        current_copy = QVBoxLayout()
        current_copy.setSpacing(0)
        current_eyebrow = QLabel("Current model")
        current_eyebrow.setObjectName("textCurrentModelEyebrow")
        self.current_model_value = QLabel("")
        self.current_model_value.setObjectName("textCurrentModelValue")
        current_copy.addWidget(current_eyebrow)
        current_copy.addWidget(self.current_model_value)
        current_layout.addLayout(current_copy)
        action_row.addWidget(self.current_model_card)
        action_row.addStretch()

        self.activate_button = Button("Use This Model")
        self.activate_button.setObjectName("textModelActivateButton")
        self.activate_button.setIcon(_design_icon("check-green.svg"))
        self.activate_button.setIconSize(QSize(18, 18))
        self.activate_button.set_base_minimum_size(150, 44)
        self.activate_button.clicked.connect(
            lambda: self.activation_requested.emit(self.provider)
        )
        action_row.addWidget(
            self.activate_button, alignment=Qt.AlignmentFlag.AlignBottom
        )
        model_card_layout.addLayout(action_row)

        layout.addWidget(self.model_card, stretch=1)
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
            f"{credential_name} found" if available else requirement
        )
        credential_icon = _design_icon(
            "check-green.svg" if available else "info-warning.svg"
        )
        self.provider_credential_icon.setPixmap(credential_icon.pixmap(16, 16))
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
        for model in models:
            self.model_combo.addItem(self._model_icon, model)
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
        """Store the active selection and refresh related status UI.

        Args:
            provider: Active ``TranscriptCleanupProvider`` value.
            model: Active model id.
        """
        self._active_provider = provider
        self._active_model = model
        self._update_active_summary()

    def _update_active_summary(self) -> None:
        """Show the Active now badge only for the currently selected provider."""
        provider_name = (
            "OpenAI"
            if self._active_provider == TranscriptCleanupProvider.OPENAI
            else "OpenRouter"
        )
        self.active_summary.setText(
            f"Active now: {provider_name} · {self._active_model}"
        )
        self.active_summary_card.setVisible(
            bool(self._active_provider)
            and self.provider == self._active_provider
        )
        # Long provider/model ids must not widen the chip past its row.
        value = self._active_model or "Not selected"
        metrics = self.current_model_value.fontMetrics()
        self.current_model_value.setText(
            metrics.elidedText(value, Qt.TextElideMode.ElideMiddle, 240)
        )
        self.current_model_value.setToolTip(self._active_model)
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
        self.activate_button.setProperty("current", is_active)
        self.activate_button.style().unpolish(self.activate_button)
        self.activate_button.style().polish(self.activate_button)
        self.activate_button.update()


class ModelManagerDialog(QDialog):
    """Non-modal home for voice, text, and meeting model selection."""

    DEFAULT_SIZE = QSize(900, 620)
    MINIMUM_SIZE = QSize(720, 480)
    _AUTO_CACHE_INFO = CachedModelInfo(
        repo_id="auto",
        size_bytes=0,
        path="",
        revision_hashes=(),
    )

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
        self._active_meeting_provider = TranscriptCleanupProvider.OPENROUTER
        self._active_meeting_llm_model = config.MEETING_LLM_MODEL

        self.setWindowTitle("Model Manager")
        self.setWindowIcon(app_icon())
        self.setObjectName("modelManagerDialog")
        self.setModal(False)
        self.setWindowFlag(Qt.WindowType.MSWindowsFixedSizeDialogHint, False)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.setSizeGripEnabled(True)
        self.setMinimumSize(self.MINIMUM_SIZE)

        self._setup_ui()
        self.resize(self.DEFAULT_SIZE)
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
        """Build the shared shell and the Voice/Text/Meeting tabs."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 16)
        layout.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(14)
        brand_icon = QLabel()
        brand_icon.setObjectName("modelManagerHeaderIcon")
        brand_icon.setPixmap(app_icon().pixmap(44, 44))
        brand_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_icon.setFixedSize(52, 52)
        header.addWidget(brand_icon)

        title_block = QVBoxLayout()
        title_block.setSpacing(3)
        title = QLabel("Model Manager")
        title.setObjectName("modelManagerTitle")
        subtitle = QLabel(
            "Choose models for dictation, transcript cleanup, and meetings."
        )
        subtitle.setObjectName("modelManagerSubtitle")
        title_block.addStretch()
        title_block.addWidget(title)
        title_block.addWidget(subtitle)
        title_block.addStretch()
        header.addLayout(title_block, stretch=1)
        layout.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("modelManagerTabs")
        self.tabs.tabBar().setCursor(Qt.CursorShape.PointingHandCursor)
        self.tabs.tabBar().setExpanding(True)
        self.tabs.tabBar().setFixedWidth(750)
        self.tabs.setIconSize(QSize(20, 20))
        self.voice_tab = self._build_voice_tab()
        self.text_tab = self._build_text_tab()
        self.meeting_tab = self._build_meeting_tab()
        self.tabs.addTab(
            self.voice_tab,
            _design_icon("microphone-blue.svg"),
            "Voice",
        )
        self.tabs.addTab(
            self.text_tab,
            _design_icon("file-text-blue.svg"),
            "Text",
        )
        self.tabs.addTab(
            self.meeting_tab,
            _design_icon("stack-slate.svg"),
            "Meeting",
        )
        self.tabs.currentChanged.connect(self._on_manager_tab_changed)
        layout.addWidget(self.tabs, stretch=1)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.message_label = QLabel("")
        self.message_label.setObjectName("modelManagerMessage")
        footer.addWidget(self.message_label, stretch=1)
        close_btn = Button("Close")
        close_btn.setObjectName("modelManagerCloseButton")
        self._compact_button(close_btn, 110)
        close_btn.clicked.connect(self.close)
        footer.addWidget(close_btn)
        layout.addLayout(footer)

    def _build_voice_tab(self) -> QWidget:
        """Build the local Whisper catalog tab.

        The header, stats, and search toolbar stay pinned; only the model
        list scrolls, so the filter controls remain reachable while browsing.
        """
        tab = QWidget()
        tab.setObjectName("modelManagerVoiceTab")
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 12, 4, 4)
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

        self.voice_scroll_area = QScrollArea()
        self.voice_scroll_area.setObjectName("modelManagerVoiceScroll")
        self.voice_scroll_area.setWidgetResizable(True)
        self.voice_scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.voice_scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)

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

        self.voice_scroll_area.setWidget(list_container)
        layout.addWidget(self.voice_scroll_area, stretch=1)
        return tab

    def _build_text_tab(self) -> QWidget:
        """Build one linear provider-to-model cleanup flow."""
        tab = QWidget()
        tab.setObjectName("modelManagerTextTab")
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(0, 0, 0, 0)
        tab_layout.setSpacing(0)

        self.text_scroll_area = QScrollArea()
        self.text_scroll_area.setObjectName("modelManagerTextScroll")
        self.text_scroll_area.setWidgetResizable(True)
        # AsNeeded: near the dialog's minimum width a slim scrollbar beats
        # clipping the action buttons off the right edge.
        self.text_scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.text_scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)

        content = QWidget()
        content.setObjectName("modelManagerTextContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 12, 8, 4)
        layout.setSpacing(12)

        heading = QHBoxLayout()
        heading.setSpacing(14)
        accent = QFrame()
        accent.setObjectName("textTabAccent")
        accent.setFixedSize(3, 38)
        heading.addWidget(accent)
        heading_copy = QVBoxLayout()
        heading_copy.setSpacing(2)
        title = QLabel("Text processing")
        title.setObjectName("textTabTitle")
        subtitle = QLabel(
            "Choose the provider and chat model used by AI transcript cleanup."
        )
        subtitle.setObjectName("textTabSubtitle")
        subtitle.setWordWrap(True)
        heading_copy.addWidget(title)
        heading_copy.addWidget(subtitle)
        heading.addLayout(heading_copy, stretch=1)
        layout.addLayout(heading)

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

        footnote_card = QFrame()
        footnote_card.setObjectName("textModelFootnoteCard")
        footnote_layout = QHBoxLayout(footnote_card)
        footnote_layout.setContentsMargins(14, 8, 14, 8)
        footnote_layout.setSpacing(12)
        footnote_icon = QLabel()
        footnote_icon.setObjectName("textModelFootnoteIcon")
        footnote_icon.setFixedSize(20, 20)
        footnote_icon.setPixmap(
            _design_icon("info-blue.svg").pixmap(18, 18)
        )
        footnote_layout.addWidget(footnote_icon)
        note = QLabel(
            "Text models are called only when AI cleanup is enabled. Cleanup "
            "behavior, prompts, and learned rules remain in Settings → Cleanup."
        )
        note.setObjectName("textModelFootnote")
        note.setWordWrap(True)
        footnote_layout.addWidget(note, stretch=1)
        layout.addWidget(footnote_card)

        self.text_scroll_area.setWidget(content)
        tab_layout.addWidget(self.text_scroll_area)
        return tab

    def _build_meeting_tab(self) -> QWidget:
        """Build meeting ASR and intelligence model selection."""
        tab = QWidget()
        tab.setObjectName("modelManagerMeetingTab")
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(0, 0, 0, 0)
        tab_layout.setSpacing(0)

        self.meeting_scroll_area = QScrollArea()
        self.meeting_scroll_area.setObjectName("modelManagerMeetingScroll")
        self.meeting_scroll_area.setWidgetResizable(True)
        self.meeting_scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.meeting_scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)

        content = QWidget()
        content.setObjectName("modelManagerMeetingContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 12, 8, 4)
        layout.setSpacing(14)

        heading = QHBoxLayout()
        heading.setSpacing(14)
        accent = QFrame()
        accent.setObjectName("meetingTabAccent")
        accent.setFixedSize(3, 38)
        heading.addWidget(accent)
        heading_copy = QVBoxLayout()
        heading_copy.setSpacing(2)
        title = QLabel("Meeting Mode")
        title.setObjectName("meetingTabTitle")
        subtitle = QLabel(
            "Choose the Whisper model and chat model used by meetings."
        )
        subtitle.setObjectName("meetingTabSubtitle")
        subtitle.setWordWrap(True)
        heading_copy.addWidget(title)
        heading_copy.addWidget(subtitle)
        heading.addLayout(heading_copy, stretch=1)
        layout.addLayout(heading)

        asr_title = QLabel("Transcription")
        asr_title.setObjectName("sectionLabel")
        layout.addWidget(asr_title)
        asr_caption = QLabel(
            "Meetings load their own Whisper instance, separate from "
            "dictation. Download or delete models on the Voice tab."
        )
        asr_caption.setObjectName("infoLabel")
        asr_caption.setWordWrap(True)
        layout.addWidget(asr_caption)

        self.meeting_rows: Dict[str, ModelRowWidget] = {}
        for model_name in config.WHISPER_MODEL_CHOICES:
            row = ModelRowWidget(model_name)
            if model_name == "auto":
                row.repo_label.setText("Turbo on GPU · base on CPU")
                row.setToolTip(
                    "Automatic model selection for meetings "
                    "(no downloadable files)"
                )
            row.download_clicked.connect(self._on_download_clicked)
            row.delete_clicked.connect(self._on_delete_clicked)
            row.set_active_clicked.connect(self._on_meeting_set_active_clicked)
            row.details_requested.connect(self._on_details_requested)
            self.meeting_rows[model_name] = row
            layout.addWidget(row)

        intelligence_title = QLabel("Intelligence")
        intelligence_title.setObjectName("sectionLabel")
        layout.addWidget(intelligence_title)
        intelligence_caption = QLabel(
            "Chat model used for meeting insights when cloud intelligence "
            "is enabled for a session."
        )
        intelligence_caption.setObjectName("infoLabel")
        intelligence_caption.setWordWrap(True)
        layout.addWidget(intelligence_caption)

        self.meeting_model_picker = _TextModelPicker(
            idle_status="Open Meeting to load the model catalog."
        )
        self.meeting_model_picker.provider_changed.connect(
            self._on_meeting_provider_changed
        )
        self.meeting_model_picker.refresh_requested.connect(
            lambda provider: self._fetch_catalog_models(
                provider,
                picker=self.meeting_model_picker,
                force=True,
            )
        )
        self.meeting_model_picker.activation_requested.connect(
            self._activate_meeting_llm_model
        )
        self.meeting_model_picker.sort_changed.connect(
            self._on_meeting_sort_changed
        )
        layout.addWidget(self.meeting_model_picker)

        footnote_card = QFrame()
        footnote_card.setObjectName("textModelFootnoteCard")
        footnote_layout = QHBoxLayout(footnote_card)
        footnote_layout.setContentsMargins(14, 8, 14, 8)
        footnote_layout.setSpacing(12)
        footnote_icon = QLabel()
        footnote_icon.setObjectName("textModelFootnoteIcon")
        footnote_icon.setFixedSize(20, 20)
        footnote_icon.setPixmap(
            _design_icon("info-blue.svg").pixmap(18, 18)
        )
        footnote_layout.addWidget(footnote_icon)
        note = QLabel(
            "A large meeting model alongside your dictation model can "
            "exhaust GPU memory. Agent core and dashboard access stay in "
            "Settings → Meeting."
        )
        note.setObjectName("textModelFootnote")
        note.setWordWrap(True)
        footnote_layout.addWidget(note, stretch=1)
        layout.addWidget(footnote_card)
        layout.addStretch()

        self.meeting_scroll_area.setWidget(content)
        tab_layout.addWidget(self.meeting_scroll_area)
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

    def _on_meeting_set_active_clicked(self, model_name: str) -> None:
        """Persist the Meeting Mode Whisper ASR selection."""
        try:
            settings_manager.save_setting(
                SettingsKey.MEETING_WHISPER_MODEL, model_name
            )
        except Exception as exc:
            logger.error("Couldn't set meeting Whisper model: %s", exc)
            self.message_label.setText(
                f"Couldn't set meeting transcription model: {exc}"
            )
            return
        self.message_label.setText(
            f'Meeting transcription model set to "{model_name}"'
        )
        self.refresh()

    def _on_details_requested(self, model_name: str) -> None:
        """Open the bundled technical profile for a selected model."""
        if model_name == "auto":
            return
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

    def _load_meeting_settings(self) -> None:
        """Load meeting Whisper/LLM choices into the Meeting tab controls."""
        try:
            settings = settings_manager.load_all_settings()
        except Exception:
            settings = {}
        provider = resolve_meeting_llm_provider(settings)
        model = resolve_meeting_llm_model(settings)
        self._active_meeting_provider = provider
        self._active_meeting_llm_model = model
        # Sort order is in-session only for Meeting (never overwrites cleanup).
        self.meeting_model_picker.set_provider(provider, model)
        self.meeting_model_picker.set_active_selection(provider, model)

    def show_text_tab(self) -> None:
        """Select the Text tab (cleanup provider/model picker)."""
        self.tabs.setCurrentWidget(self.text_tab)

    def show_meeting_tab(self) -> None:
        """Select the Meeting tab (ASR + intelligence pickers)."""
        self.tabs.setCurrentWidget(self.meeting_tab)

    def _on_manager_tab_changed(self, index: int) -> None:
        """Load the selected provider catalog when Text/Meeting opens."""
        widget = self.tabs.widget(index)
        if widget is self.text_tab:
            self._fetch_catalog_models(
                self.text_model_picker.provider,
                picker=self.text_model_picker,
            )
        elif widget is self.meeting_tab:
            self._fetch_catalog_models(
                self.meeting_model_picker.provider,
                picker=self.meeting_model_picker,
            )

    def _on_text_provider_changed(self, provider: str) -> None:
        """Load a newly selected provider in the cleanup model picker."""
        if self.tabs.currentWidget() is self.text_tab:
            self._fetch_catalog_models(
                provider, picker=self.text_model_picker
            )

    def _on_meeting_provider_changed(self, provider: str) -> None:
        """Load a newly selected provider in the meeting LLM picker."""
        if self.tabs.currentWidget() is self.meeting_tab:
            self._fetch_catalog_models(
                provider, picker=self.meeting_model_picker
            )

    def _on_text_sort_changed(self, provider: str) -> None:
        """Persist OpenRouter's catalog order and reload that catalog."""
        try:
            settings_manager.save_setting(
                SettingsKey.TRANSCRIPT_CLEANUP_MODEL_SORT,
                self.text_model_picker.current_sort(),
            )
        except Exception as exc:
            logger.warning("Couldn't save text model sort: %s", exc)
        self._fetch_catalog_models(provider, picker=self.text_model_picker)

    def _on_meeting_sort_changed(self, provider: str) -> None:
        """Reload the meeting catalog order without touching cleanup sort."""
        self._fetch_catalog_models(
            provider, picker=self.meeting_model_picker
        )

    def _fetch_text_models(self, provider: str, force: bool = False) -> None:
        """Compatibility wrapper for cleanup catalog loads."""
        self._fetch_catalog_models(
            provider, picker=self.text_model_picker, force=force
        )

    def _fetch_catalog_models(
        self,
        provider: str,
        picker: _TextModelPicker,
        force: bool = False,
    ) -> None:
        """Load one provider's chat-model catalog on a worker thread.

        Args:
            provider: A ``TranscriptCleanupProvider`` value.
            picker: Text or Meeting picker that requested the catalog.
            force: Bypass the in-dialog cache when true.
        """
        if provider == picker.provider:
            picker._update_credential_status()
        sort = picker.current_sort()
        key = (provider, sort)
        if not force and key in self._text_models_cache:
            models = self._text_models_cache[key]
            self._apply_catalog_to_picker(picker, provider, sort, models, "")
            return
        if key in self._text_models_loading:
            if provider == picker.provider:
                picker.set_loading(True)
            return

        self._text_models_loading.add(key)
        if provider == picker.provider:
            picker.set_loading(True)

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

    def _apply_catalog_to_picker(
        self,
        picker: _TextModelPicker,
        provider: str,
        sort: str,
        models: list,
        error: str,
    ) -> None:
        """Apply a catalog result to one picker when it still matches."""
        if provider != picker.provider:
            return
        provider_loading = any(
            loading_provider == provider
            for loading_provider, _loading_sort in self._text_models_loading
        )
        picker.set_loading(provider_loading)
        if sort != picker.current_sort():
            return
        if error:
            picker.status_label.setText(f"Couldn't load models: {error}")
            return
        picker.set_models(models)
        picker.status_label.setText(f"{len(models)} models available")

    def _on_text_models_loaded(
        self, provider: str, sort: str, models: list, error: str
    ) -> None:
        """Apply a provider catalog result on the Qt thread."""
        key = (provider, sort)
        self._text_models_loading.discard(key)
        if not error:
            self._text_models_cache[key] = models
        self._apply_catalog_to_picker(
            self.text_model_picker, provider, sort, models, error
        )
        self._apply_catalog_to_picker(
            self.meeting_model_picker, provider, sort, models, error
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

    def _activate_meeting_llm_model(self, provider: str) -> None:
        """Persist one provider/model pair as the meeting intelligence model."""
        if provider != self.meeting_model_picker.provider:
            return
        model = self.meeting_model_picker.model_combo.currentText().strip()
        if not model:
            return
        try:
            settings = settings_manager.load_all_settings()
            settings[SettingsKey.MEETING_LLM_PROVIDER] = provider
            settings[SettingsKey.MEETING_LLM_MODEL] = model
            settings_manager.save_all_settings(settings)
        except Exception as exc:
            logger.error("Couldn't activate meeting LLM model: %s", exc)
            self.message_label.setText(f"Couldn't set meeting model: {exc}")
            return

        self._active_meeting_provider = provider
        self._active_meeting_llm_model = model
        self.meeting_model_picker.set_active_selection(provider, model)
        display_provider = "OpenAI" if provider == "openai" else "OpenRouter"
        self.message_label.setText(
            f"Meeting intelligence model set to {display_provider} · {model}"
        )

    def _refresh_meeting_whisper_rows(
        self,
        cached: Dict[str, CachedModelInfo],
        downloads_blocked: bool,
        loaded_repo: Optional[str],
    ) -> None:
        """Update Meeting-tab Whisper rows from cache and settings."""
        active_model = settings_manager.get(
            SettingsKey.MEETING_WHISPER_MODEL, config.MEETING_WHISPER_MODEL
        )
        if active_model not in config.WHISPER_MODEL_CHOICES:
            active_model = config.MEETING_WHISPER_MODEL
        for model_name, row in self.meeting_rows.items():
            if model_name == "auto":
                row.update_state(
                    self._AUTO_CACHE_INFO,
                    is_active=(active_model == "auto"),
                    is_loaded=False,
                    downloading=False,
                    downloads_blocked=downloads_blocked,
                    download_slot_busy=(self._downloading_model is not None),
                )
                row.download_button.setVisible(False)
                row.delete_button.setVisible(False)
                row.size_label.setText("auto")
                row.size_label.setProperty("muted", True)
                row.size_label.style().unpolish(row.size_label)
                row.size_label.style().polish(row.size_label)
                continue

            info = cached.get(row.repo_id)
            row.update_state(
                info,
                is_active=(model_name == active_model),
                is_loaded=(row.repo_id == loaded_repo),
                downloading=(model_name == self._downloading_model),
                downloads_blocked=downloads_blocked,
                download_slot_busy=(self._downloading_model is not None),
            )

    # ── State updates ──────────────────────────────────────────────

    def refresh(self) -> None:
        """Refresh voice/meeting cache state and active text selections."""
        self._load_text_settings()
        self._load_meeting_settings()
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

        self._refresh_meeting_whisper_rows(
            cached, downloads_blocked, loaded_repo
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

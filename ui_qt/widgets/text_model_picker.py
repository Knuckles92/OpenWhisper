"""Profile-to-model picker used by the Model Manager text cards."""
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from config import bundle_root
from services.settings import (
    TranscriptCleanupModelSort,
    TranscriptCleanupProvider,
    default_transcript_cleanup_model,
)
from services.text_llm import (
    PROFILE_KIND_OPENROUTER,
    TextLLMProfile,
    builtin_profiles,
    credential_status,
    get_profile,
    list_profiles,
)
from services.transcript_cleanup import find_api_key
from ui_qt.widgets.buttons import Button
from ui_qt.widgets.no_wheel import NoWheelComboBox
from ui_qt.widgets.searchable_combo import SearchableComboBox


def _design_icon(filename: str) -> QIcon:
    """Load a bundled Tabler icon used by the text-model picker."""
    path = Path(bundle_root()) / "ui_qt" / "assets" / "tabler" / filename
    icon = QIcon(str(path))
    icon.addPixmap(icon.pixmap(24, 24), QIcon.Mode.Disabled, QIcon.State.Off)
    return icon


class TextModelPicker(QWidget):
    """Single-flow profile and text-model selection controls."""

    provider_changed = pyqtSignal(str)
    refresh_requested = pyqtSignal(str)
    activation_requested = pyqtSignal(str)
    sort_changed = pyqtSignal(str)
    add_endpoint_requested = pyqtSignal()
    edit_endpoint_requested = pyqtSignal(str)
    delete_endpoint_requested = pyqtSignal(str)

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
        idle_status: str = "Open this tab to load the model catalog.",
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
        self._profiles: List[TextLLMProfile] = list(builtin_profiles())
        self._draft_models = {
            profile.id: default_transcript_cleanup_model(profile.id)
            for profile in self._profiles
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
        """Construct the side-by-side provider and model selection cards."""
        self.setObjectName("textModelPicker")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(12)

        self.provider_card = QFrame()
        self.provider_card.setObjectName("textModelSectionCard")
        provider_layout = QVBoxLayout(self.provider_card)
        provider_layout.setContentsMargins(16, 14, 16, 14)
        provider_layout.setSpacing(10)
        provider_layout.addLayout(self._step_heading("1", "Choose an endpoint"))

        self.provider_combo = NoWheelComboBox()
        self.provider_combo.setObjectName("textModelProviderCombo")
        self.provider_combo.setMinimumHeight(44)
        self.provider_combo.setIconSize(QSize(22, 22))
        self.provider_combo.currentIndexChanged.connect(
            self._on_provider_combo_changed
        )
        provider_layout.addWidget(self.provider_combo)

        manage_row = QHBoxLayout()
        manage_row.setSpacing(8)
        self.add_endpoint_button = Button("Add endpoint")
        self.add_endpoint_button.setObjectName("textEndpointAddButton")
        self.add_endpoint_button.setToolTip(
            "Add an OpenAI-compatible server such as LM Studio or vLLM"
        )
        self.add_endpoint_button.clicked.connect(self.add_endpoint_requested.emit)
        self.edit_endpoint_button = Button("Edit")
        self.edit_endpoint_button.setObjectName("textEndpointEditButton")
        self.edit_endpoint_button.clicked.connect(self._emit_edit)
        self.delete_endpoint_button = Button("Delete")
        self.delete_endpoint_button.setObjectName("textEndpointDeleteButton")
        self.delete_endpoint_button.clicked.connect(self._emit_delete)
        manage_row.addWidget(self.add_endpoint_button)
        manage_row.addWidget(self.edit_endpoint_button)
        manage_row.addWidget(self.delete_endpoint_button)
        manage_row.addStretch()
        provider_layout.addLayout(manage_row)

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
        self.provider_url = QLabel("")
        self.provider_url.setObjectName("textProviderUrl")
        self.provider_url.setWordWrap(True)
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
        copy.addWidget(self.provider_url)
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
        self._reload_provider_combo()
        self._render_provider()

    def set_profiles(self, profiles: List[TextLLMProfile]) -> None:
        """Replace the selectable profile list without changing the catalog."""
        if not profiles:
            profiles = list(builtin_profiles())
        current = self.provider
        self._profiles = list(profiles)
        for profile in self._profiles:
            self._draft_models.setdefault(
                profile.id, default_transcript_cleanup_model(profile.id)
            )
        self._reload_provider_combo()
        if get_profile(current) is None and current not in {
            p.id for p in self._profiles
        }:
            current = self._profiles[0].id
        self.set_provider(current)

    def current_profile(self) -> Optional[TextLLMProfile]:
        """Return the selected profile, if any."""
        for profile in self._profiles:
            if profile.id == self.provider:
                return profile
        return get_profile(self.provider)

    def set_provider(self, provider: str, model: Optional[str] = None) -> None:
        """Show one profile and optionally stage a model for it.

        Args:
            provider: A text-LLM profile id.
            model: Optional model id to stage for the provider.
        """
        known = {profile.id for profile in self._profiles}
        if provider not in known and get_profile(provider) is None:
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

    def _reload_provider_combo(self) -> None:
        """Rebuild the profile combo from the current profile list."""
        icons = {
            TranscriptCleanupProvider.OPENAI: _design_icon("box-blue.svg"),
            TranscriptCleanupProvider.OPENROUTER: _design_icon(
                "stack-purple.svg"
            ),
        }
        custom_icon = _design_icon("stack-slate.svg")
        self.provider_combo.blockSignals(True)
        self.provider_combo.clear()
        for profile in self._profiles:
            icon = icons.get(profile.id, custom_icon)
            self.provider_combo.addItem(icon, profile.name, profile.id)
        self.provider_combo.blockSignals(False)

    def _on_provider_combo_changed(self, _index: int) -> None:
        """Swap the in-place catalog when the provider selector changes."""
        self._save_current_draft()
        provider = self.provider_combo.currentData()
        if not provider:
            return
        self.provider = provider
        self._render_provider()
        self.provider_changed.emit(provider)

    def _profile_copy(self, profile: Optional[TextLLMProfile]) -> tuple:
        """Return mark, description, and default requirement copy."""
        if profile is None:
            return ("API", "Unknown endpoint.", "Requires an API key")
        if profile.id == TranscriptCleanupProvider.OPENAI:
            return (
                "OAI",
                "Direct access to OpenAI chat and reasoning models.",
                "Requires OPENAI_API_KEY",
            )
        if profile.id == TranscriptCleanupProvider.OPENROUTER:
            return (
                "OR",
                "One catalog with models from OpenAI, Anthropic, Google, and more.",
                "Requires OPENROUTER_API_KEY",
            )
        return (
            "API",
            "Any server that speaks the OpenAI Chat Completions API.",
            (
                f"Requires {profile.api_key_env}"
                if profile.api_key_env else "No API key required"
            ),
        )

    def _render_provider(self) -> None:
        """Update provider copy and the staged model without changing pages."""
        profile = self.current_profile()
        mark, description, requirement = self._profile_copy(profile)
        self.provider_mark.setText(mark)
        self.provider_title.setText(profile.name if profile else "Endpoint")
        self.provider_description.setText(description)
        if profile is not None and profile.base_url:
            self.provider_url.setText(profile.base_url)
            self.provider_url.setVisible(True)
        else:
            self.provider_url.setText("")
            self.provider_url.setVisible(False)
        self._update_credential_status(requirement)
        show_sort = bool(profile and profile.kind == PROFILE_KIND_OPENROUTER)
        self.sort_label.setVisible(show_sort)
        self.sort_combo.setVisible(show_sort)
        can_edit = bool(profile and not profile.builtin)
        self.edit_endpoint_button.setEnabled(can_edit)
        self.delete_endpoint_button.setEnabled(can_edit)

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
        profile = self.current_profile()
        if profile is not None and not profile.builtin:
            available, text = credential_status(profile)
        else:
            if requirement is None:
                requirement = self._profile_copy(profile)[2]
            credential_name = requirement.removeprefix("Requires ")
            available = bool(find_api_key(self.provider))
            text = (
                f"{credential_name} found" if available else requirement
            )
        self.provider_requirement.setText(text)
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
        profile = self.current_profile()
        if profile is None or profile.kind != PROFILE_KIND_OPENROUTER:
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
            provider: Active text-LLM profile id.
            model: Active model id.
        """
        self._active_provider = provider
        self._active_model = model
        self._update_active_summary()

    def _active_profile_name(self) -> str:
        """Display name for the currently active profile."""
        for profile in self._profiles:
            if profile.id == self._active_provider:
                return profile.name
        profile = get_profile(self._active_provider)
        if profile is not None:
            return profile.name
        if self._active_provider == TranscriptCleanupProvider.OPENAI:
            return "OpenAI"
        if self._active_provider == TranscriptCleanupProvider.OPENROUTER:
            return "OpenRouter"
        return self._active_provider or "Endpoint"

    def _update_active_summary(self) -> None:
        """Show the Active now badge only for the currently selected provider."""
        provider_name = self._active_profile_name()
        self.active_summary.setText(
            f"Active now: {provider_name} · {self._active_model}"
        )
        self.active_summary_card.setVisible(
            bool(self._active_provider)
            and self.provider == self._active_provider
        )
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

    def _emit_edit(self) -> None:
        """Ask the owner to edit the selected custom endpoint."""
        profile = self.current_profile()
        if profile is not None and not profile.builtin:
            self.edit_endpoint_requested.emit(profile.id)

    def _emit_delete(self) -> None:
        """Ask the owner to delete the selected custom endpoint."""
        profile = self.current_profile()
        if profile is not None and not profile.builtin:
            self.delete_endpoint_requested.emit(profile.id)

"""Single-column endpoint + model picker for Model Manager destinations.

The picker is stacked vertically because it lives inside a rail destination
that must fit the window without scrolling. Provider prose that used to sit in
its own identity card is now carried by the combo's tooltip.

Choosing a model assigns it: there is no separate confirm step. An Active badge
inside the model combo marks the assignment, so the only place that reports what
is in use is the value itself. A search fragment left in the editor is not a
choice — it reverts when focus leaves without Enter or a pick from the list.
"""
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
)
from services.transcript_cleanup import find_api_key
from ui_qt.widgets.buttons import Button
from ui_qt.widgets.no_wheel import ElidingComboBox
from ui_qt.widgets.searchable_combo import SearchableComboBox


def _design_icon(filename: str) -> QIcon:
    path = Path(bundle_root()) / "ui_qt" / "assets" / "tabler" / filename
    icon = QIcon(str(path))
    icon.addPixmap(icon.pixmap(24, 24), QIcon.Mode.Disabled, QIcon.State.Off)
    return icon


class TextModelPicker(QWidget):
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
        idle_status: str = "Open this destination to load the model catalog.",
    ):
        super().__init__(parent)
        self.provider = TranscriptCleanupProvider.OPENAI
        self._active_provider = ""
        self._active_model = ""
        self._idle_status = idle_status
        self._profiles: List[TextLLMProfile] = list(builtin_profiles())
        # Model shown per endpoint. Only a committed pick or a caller updates
        # it, so switching endpoints never carries a search fragment along.
        self._staged_models = {
            profile.id: default_transcript_cleanup_model(profile.id)
            for profile in self._profiles
        }
        self._model_edited = False
        self._setup_ui()

    def _setup_ui(self) -> None:
        self.setObjectName("textModelPicker")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        endpoint_label = QLabel("Endpoint")
        endpoint_label.setObjectName("textModelFieldLabel")
        layout.addWidget(endpoint_label)

        self.provider_combo = ElidingComboBox()
        self.provider_combo.setObjectName("textModelProviderCombo")
        self.provider_combo.setMinimumHeight(40)
        self.provider_combo.setIconSize(QSize(20, 20))
        self.provider_combo.currentIndexChanged.connect(
            self._on_provider_combo_changed
        )
        layout.addWidget(self.provider_combo)

        credential_row = QHBoxLayout()
        credential_row.setSpacing(8)
        self.provider_credential_icon = QLabel()
        self.provider_credential_icon.setObjectName("textProviderCredentialIcon")
        self.provider_credential_icon.setFixedSize(16, 16)
        self.provider_requirement = QLabel("")
        self.provider_requirement.setObjectName("textProviderRequirement")
        # Wrapped so the credential copy cannot set the window's width floor.
        self.provider_requirement.setWordWrap(True)
        credential_row.addWidget(
            self.provider_credential_icon, alignment=Qt.AlignmentFlag.AlignTop
        )
        credential_row.addWidget(self.provider_requirement, stretch=1)
        self.provider_url = QLabel("")
        self.provider_url.setObjectName("textProviderUrl")
        credential_row.addWidget(self.provider_url)
        layout.addLayout(credential_row)

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
        for button in (
            self.add_endpoint_button,
            self.edit_endpoint_button,
            self.delete_endpoint_button,
        ):
            button.set_base_minimum_size(0, 28)
            button.setMaximumHeight(28)
            manage_row.addWidget(button)
        manage_row.addStretch()
        layout.addLayout(manage_row)

        separator = QFrame()
        separator.setObjectName("textModelGroupSeparator")
        separator.setFixedHeight(1)
        layout.addWidget(separator)

        model_label_row = QHBoxLayout()
        model_label_row.setSpacing(10)
        model_label = QLabel("Model")
        model_label.setObjectName("textModelFieldLabel")
        model_label_row.addWidget(model_label)
        model_label_row.addStretch()
        self.sort_label = QLabel("Sort catalog")
        self.sort_label.setObjectName("textModelFieldLabel")
        model_label_row.addWidget(self.sort_label)
        self.sort_combo = ElidingComboBox()
        self.sort_combo.setObjectName("textModelSort")
        for label, value in self._SORT_OPTIONS:
            self.sort_combo.addItem(label, value)
        self.sort_combo.setMinimumHeight(26)
        self.sort_combo.setMaximumHeight(26)
        self.sort_combo.currentIndexChanged.connect(
            lambda: self.sort_changed.emit(self.provider)
        )
        model_label_row.addWidget(self.sort_combo)
        layout.addLayout(model_label_row)

        model_row = QHBoxLayout()
        model_row.setSpacing(10)
        self.model_combo = SearchableComboBox()
        self.model_combo.setObjectName("textModelCombo")
        self.model_combo.setMinimumHeight(40)
        self._model_icon = _design_icon("box-blue.svg")
        self.model_combo.setIconSize(QSize(20, 20))
        self.model_combo.currentTextChanged.connect(self._on_model_changed)
        self.model_combo.textActivated.connect(self._on_model_activated)
        model_line_edit = self.model_combo.lineEdit()
        model_line_edit.textEdited.connect(self._on_model_text_edited)
        model_line_edit.returnPressed.connect(self._commit_model_selection)
        model_line_edit.editingFinished.connect(self._on_model_editing_finished)
        model_row.addWidget(self.model_combo, stretch=1)

        self.refresh_button = Button("Refresh")
        self.refresh_button.setObjectName("textModelRefreshButton")
        self.refresh_button.setIcon(_design_icon("refresh-blue.svg"))
        self.refresh_button.setIconSize(QSize(16, 16))
        self.refresh_button.set_base_minimum_size(100, 40)
        self.refresh_button.setMaximumHeight(40)
        self.refresh_button.setToolTip("Reload the selected provider's catalog")
        self.refresh_button.clicked.connect(
            lambda: self.refresh_requested.emit(self.provider)
        )
        model_row.addWidget(self.refresh_button)
        layout.addLayout(model_row)

        self.status_label = QLabel(self._idle_status)
        self.status_label.setObjectName("textModelStatus")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self._reload_provider_combo()
        self._render_provider()

    def set_profiles(self, profiles: List[TextLLMProfile]) -> None:
        """Replace the selectable profile list without changing the catalog."""
        if not profiles:
            profiles = list(builtin_profiles())
        current = self.provider
        self._profiles = list(profiles)
        for profile in self._profiles:
            self._staged_models.setdefault(
                profile.id, default_transcript_cleanup_model(profile.id)
            )
        self._reload_provider_combo()
        if get_profile(current) is None and current not in {
            p.id for p in self._profiles
        }:
            current = self._profiles[0].id
        self.set_provider(current)

    def current_profile(self) -> Optional[TextLLMProfile]:
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
        if model:
            self._staged_models[provider] = model
        index = self.provider_combo.findData(provider)
        self.provider_combo.blockSignals(True)
        self.provider_combo.setCurrentIndex(max(0, index))
        self.provider_combo.blockSignals(False)
        self.provider = provider
        self._render_provider()

    def _reload_provider_combo(self) -> None:
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
            description = self._profile_copy(profile)[1]
            self.provider_combo.setItemData(
                self.provider_combo.count() - 1,
                f"{profile.name} — {description}",
                Qt.ItemDataRole.ToolTipRole,
            )
        self.provider_combo.blockSignals(False)

    def _on_provider_combo_changed(self, _index: int) -> None:
        provider = self.provider_combo.currentData()
        if not provider:
            return
        self.provider = provider
        self._render_provider()
        self.provider_changed.emit(provider)

    def _profile_copy(self, profile: Optional[TextLLMProfile]) -> tuple:
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
        profile = self.current_profile()
        _mark, description, requirement = self._profile_copy(profile)
        name = profile.name if profile else "Endpoint"
        self.provider_combo.setToolTip(f"{name} — {description}")
        if profile is not None and profile.base_url:
            metrics = self.provider_url.fontMetrics()
            self.provider_url.setText(
                metrics.elidedText(
                    profile.base_url, Qt.TextElideMode.ElideMiddle, 220
                )
            )
            self.provider_url.setToolTip(profile.base_url)
            self.provider_url.setVisible(True)
        else:
            self.provider_url.setText("")
            self.provider_url.setToolTip("")
            self.provider_url.setVisible(False)
        self._update_credential_status(requirement)
        show_sort = bool(profile and profile.kind == PROFILE_KIND_OPENROUTER)
        self.sort_label.setVisible(show_sort)
        self.sort_combo.setVisible(show_sort)
        can_edit = bool(profile and not profile.builtin)
        self.edit_endpoint_button.setEnabled(can_edit)
        self.delete_endpoint_button.setEnabled(can_edit)

        self._model_edited = False
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.setCurrentText(self._staged_model())
        self.model_combo.blockSignals(False)
        self.status_label.setText("Loading models…")
        self._update_active_badge()

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

    def _staged_model(self) -> str:
        """Return the model this picker shows for the selected endpoint."""
        return self._staged_models.get(
            self.provider, default_transcript_cleanup_model(self.provider)
        )

    def _on_model_changed(self, _text: str) -> None:
        self._update_active_badge()

    def _on_model_text_edited(self, _text: str) -> None:
        self._model_edited = True

    def _on_model_activated(self, _text: str) -> None:
        """Assign a model the moment it is picked from the catalog."""
        self._commit_model_selection()

    def _on_model_editing_finished(self) -> None:
        """Drop an uncommitted edit when focus leaves the model editor.

        Typing filters the dropdown, so the editor can hold a fragment such as
        ``clau`` that the user never chose. Only Enter or a pick from the list
        assigns; anything else rewinds to the model already staged.
        """
        if self._model_edited:
            self._restore_staged_model()

    def _commit_model_selection(self) -> None:
        """Stage and request activation for the model now in the editor."""
        self._model_edited = False
        model = self.model_combo.currentText().strip()
        if not model:
            self._restore_staged_model()
            return
        self._staged_models[self.provider] = model
        self._update_active_badge()
        already_active = (
            self.provider == self._active_provider
            and model == self._active_model
        )
        if not already_active:
            self.activation_requested.emit(self.provider)

    def _restore_staged_model(self) -> None:
        self._model_edited = False
        staged = self._staged_model()
        if self.model_combo.currentText() != staged:
            self.model_combo.setCurrentText(staged)
        self._update_active_badge()

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
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        for model in models:
            self.model_combo.addItem(self._model_icon, model)
        self.model_combo.setCurrentText(self._staged_model())
        self.model_combo.blockSignals(False)
        self._model_edited = False
        self._update_active_badge()

    def set_loading(self, loading: bool) -> None:
        self.refresh_button.setEnabled(not loading)
        if loading:
            self.status_label.setText("Loading models…")

    def set_active_selection(self, provider: str, model: str) -> None:
        """Record the saved assignment the badge reports."""
        self._active_provider = provider
        self._active_model = model
        if model:
            self._staged_models[provider] = model
        self._update_active_badge()

    def _active_profile_name(self) -> str:
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

    def _update_active_badge(self) -> None:
        """Badge the model combo when it shows the saved assignment.

        The badge is the only report of what is in use, so an endpoint being
        browsed shows no badge and the combo tooltip names the real one.
        """
        selected = self.model_combo.currentText().strip()
        is_active = bool(self._active_provider) and (
            self.provider == self._active_provider
            and selected == self._active_model
        )
        self.model_combo.set_badge("Active" if is_active else "")
        if is_active:
            self.model_combo.setToolTip(
                f"{self._active_profile_name()} · {self._active_model} is the "
                "model in use."
            )
        else:
            self.model_combo.setToolTip(
                "Choose from the catalog or type a model id — the choice is "
                "saved immediately. In use now: "
                f"{self._active_profile_name()} · {self._active_model}."
            )

    def _emit_edit(self) -> None:
        profile = self.current_profile()
        if profile is not None and not profile.builtin:
            self.edit_endpoint_requested.emit(profile.id)

    def _emit_delete(self) -> None:
        profile = self.current_profile()
        if profile is not None and not profile.builtin:
            self.delete_endpoint_requested.emit(profile.id)

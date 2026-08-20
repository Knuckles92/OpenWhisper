"""Editor for a custom OpenAI-compatible text-LLM endpoint."""
from typing import Optional

from PyQt6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from services.text_llm import (
    TextLLMProfile,
    normalize_base_url,
    validate_api_key_env,
    validate_profile_name,
)
from ui_qt.utils.app_icon import app_icon
from ui_qt.widgets import Button, PrimaryButton


class TextEndpointDialog(QDialog):
    """Modal dialog for creating or editing a custom chat endpoint."""

    def __init__(
        self,
        profile: Optional[TextLLMProfile] = None,
        parent=None,
    ):
        """Build the editor.

        Args:
            profile: Existing custom profile to edit. None creates a new one.
            parent: Optional owning widget.
        """
        super().__init__(parent)
        self._profile = profile
        self._payload = None
        self.setWindowTitle(
            "Edit endpoint" if profile is not None else "Add endpoint"
        )
        self.setWindowIcon(app_icon())
        self.setObjectName("textEndpointDialog")
        self.setModal(True)
        self.setMinimumWidth(460)
        self._setup_ui()

    def _setup_ui(self) -> None:
        """Construct name, URL, and optional API-key fields."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        title = QLabel(
            "Edit OpenAI-compatible endpoint"
            if self._profile is not None
            else "Add an OpenAI-compatible endpoint"
        )
        title.setObjectName("headerLabel")
        layout.addWidget(title)

        intro = QLabel(
            "Any server that speaks the OpenAI Chat Completions API works "
            "here — LM Studio, vLLM, Ollama's /v1 route, LiteLLM, or a "
            "private gateway. The API key stays in an environment variable "
            "or .env file; leave that field blank for a local server that "
            "does not authenticate."
        )
        intro.setObjectName("infoLabel")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        form.setSpacing(8)
        self.name_edit = QLineEdit()
        self.name_edit.setObjectName("textEndpointNameEdit")
        self.name_edit.setPlaceholderText("LM Studio")
        self.url_edit = QLineEdit()
        self.url_edit.setObjectName("textEndpointUrlEdit")
        self.url_edit.setPlaceholderText("http://127.0.0.1:1234/v1")
        self.env_edit = QLineEdit()
        self.env_edit.setObjectName("textEndpointEnvEdit")
        self.env_edit.setPlaceholderText("Optional, e.g. LMSTUDIO_API_KEY")
        if self._profile is not None:
            self.name_edit.setText(self._profile.name)
            self.url_edit.setText(self._profile.base_url or "")
            self.env_edit.setText(self._profile.api_key_env)
        form.addRow("Name", self.name_edit)
        form.addRow("Base URL", self.url_edit)
        form.addRow("API key variable", self.env_edit)
        layout.addLayout(form)

        self.error_label = QLabel("")
        self.error_label.setObjectName("textEndpointError")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = Button("Cancel")
        cancel.setObjectName("textEndpointCancelButton")
        cancel.clicked.connect(self.reject)
        save = PrimaryButton("Save endpoint")
        save.setObjectName("textEndpointSaveButton")
        save.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def result_payload(self) -> Optional[dict]:
        """Return the validated field dict after a successful save."""
        return self._payload

    def _save(self) -> None:
        """Validate fields and accept the dialog."""
        try:
            name = validate_profile_name(self.name_edit.text())
            base_url = normalize_base_url(self.url_edit.text())
            api_key_env = validate_api_key_env(self.env_edit.text())
        except ValueError as exc:
            self.error_label.setText(str(exc))
            return
        self._payload = {
            "name": name,
            "base_url": base_url,
            "api_key_env": api_key_env,
        }
        if self._profile is not None:
            self._payload["id"] = self._profile.id
        self.accept()

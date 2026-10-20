"""Editor for an OpenAI-compatible text-LLM endpoint.

It adds and edits custom endpoints, and edits a built-in local server preset,
whose name and API are fixed so only its address and key variable change.
"""
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
    normalize_local_server_url,
    validate_api_key_env,
    validate_profile_name,
    validate_text_protocol,
)
from ui_qt.utils.app_icon import app_icon
from ui_qt.widgets import Button, NoWheelComboBox, PrimaryButton


class TextEndpointDialog(QDialog):
    """Modal dialog for creating or editing a chat endpoint."""

    def __init__(
        self,
        profile: Optional[TextLLMProfile] = None,
        parent=None,
    ):
        """Build the editor.

        Args:
            profile: Existing custom profile or local server preset to edit.
                None creates a new custom endpoint.
            parent: Optional owning widget.
        """
        super().__init__(parent)
        self._profile = profile
        self._preset = profile is not None and profile.builtin
        self._payload = None
        self.setWindowTitle(
            f"{profile.name} server" if self._preset
            else "Edit endpoint" if profile is not None
            else "Add endpoint"
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

        if self._preset:
            heading = f"{self._profile.name} server"
            copy = (
                "Cleanup and meetings share this server. Change the address "
                "when it runs on another port or computer. If you started it "
                "with an API key, name the variable that holds the key, then "
                "enter the key under Settings → App → API keys (or export the "
                "variable). Leave it blank when the server does not "
                "authenticate."
            )
        else:
            heading = (
                "Edit OpenAI-compatible endpoint"
                if self._profile is not None
                else "Add an OpenAI-compatible endpoint"
            )
            copy = (
                "Servers that support OpenAI Chat Completions or Responses "
                "work here — LiteLLM, a second LM Studio or vLLM server, or a "
                "private gateway. Name the variable that holds its API key, "
                "then enter the key under Settings → App → API keys (or export the "
                "variable). Leave it blank for a local server that does not "
                "authenticate."
            )
        title = QLabel(heading)
        title.setObjectName("headerLabel")
        layout.addWidget(title)

        intro = QLabel(copy)
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
        self.env_edit.setPlaceholderText(
            "Optional, e.g. "
            + (f"{self._profile.id.upper()}_API_KEY" if self._preset else "LMSTUDIO_API_KEY")
        )
        self.protocol_combo = NoWheelComboBox()
        self.protocol_combo.setObjectName("textEndpointProtocolCombo")
        self.protocol_combo.addItem("Chat Completions", "chat")
        self.protocol_combo.addItem("Responses", "responses")
        self.protocol_combo.setToolTip("Choose the API supported by your server.")
        if self._profile is not None:
            self.name_edit.setText(self._profile.name)
            self.url_edit.setText(self._profile.base_url or "")
            self.env_edit.setText(self._profile.api_key_env)
            self.protocol_combo.setCurrentIndex(
                self.protocol_combo.findData(self._profile.protocol)
            )
        if not self._preset:
            form.addRow("Name", self.name_edit)
        form.addRow("Base URL", self.url_edit)
        if not self._preset:
            form.addRow("API", self.protocol_combo)
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
        save = PrimaryButton("Save server" if self._preset else "Save endpoint")
        save.setProperty("buttonId", "textEndpointSaveButton")
        save.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def result_payload(self) -> Optional[dict]:
        """Return the validated field dict after a successful save."""
        return self._payload

    def _save(self) -> None:
        """Validate fields and accept the dialog."""
        normalize_url = normalize_local_server_url if self._preset else normalize_base_url
        try:
            name = validate_profile_name(self.name_edit.text())
            base_url = normalize_url(self.url_edit.text())
            api_key_env = validate_api_key_env(self.env_edit.text())
            protocol = validate_text_protocol(self.protocol_combo.currentData())
        except ValueError as exc:
            self.error_label.setText(str(exc))
            return
        self._payload = {
            "name": name,
            "base_url": base_url,
            "api_key_env": api_key_env,
            "protocol": protocol,
        }
        if self._profile is not None:
            self._payload["id"] = self._profile.id
        self.accept()

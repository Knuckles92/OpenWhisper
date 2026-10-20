"""Editor for a self-hosted speech server's address, model and key variable."""
from PyQt6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from services import speech_endpoints
from services.speech_endpoints import SpeechEndpoint
from ui_qt.utils.app_icon import app_icon
from ui_qt.widgets import Button, PrimaryButton


class SpeechServerDialog(QDialog):
    """Modal dialog that saves one configurable endpoint when accepted."""

    def __init__(self, endpoint: SpeechEndpoint, parent=None):
        super().__init__(parent)
        self._endpoint = endpoint
        self.setWindowTitle(f"{endpoint.name} server")
        self.setWindowIcon(app_icon())
        self.setObjectName("speechServerDialog")
        self.setModal(True)
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        title = QLabel(f"{endpoint.name} server")
        title.setObjectName("headerLabel")
        layout.addWidget(title)
        intro = QLabel(
            f"{endpoint.description} OpenWhisper sends recordings to its "
            "OpenAI-compatible /audio/transcriptions route. A server on this "
            "computer keeps audio here. Name the variable that holds its API "
            "key, then enter the key under Settings → App → API keys, or leave it "
            "blank for a server that does not authenticate."
        )
        intro.setObjectName("infoLabel")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        form.setSpacing(8)
        self.url_edit = QLineEdit(endpoint.base_url or "")
        self.url_edit.setObjectName("speechServerUrlEdit")
        self.url_edit.setPlaceholderText("http://127.0.0.1:8000/v1")
        self.model_edit = QLineEdit(speech_endpoints.endpoint_model(endpoint))
        self.model_edit.setObjectName("speechServerModelEdit")
        self.env_edit = QLineEdit(endpoint.connection.api_key_env)
        self.env_edit.setObjectName("speechServerEnvEdit")
        self.env_edit.setPlaceholderText("Optional, e.g. SPEACHES_API_KEY")
        form.addRow("Base URL", self.url_edit)
        form.addRow("Model", self.model_edit)
        form.addRow("API key variable", self.env_edit)
        layout.addLayout(form)

        self.error_label = QLabel("")
        self.error_label.setObjectName("speechServerError")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = Button("Cancel")
        cancel.clicked.connect(self.reject)
        save = PrimaryButton("Save server")
        save.setProperty("buttonId", "speechServerSaveButton")
        save.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def _save(self) -> None:
        try:
            speech_endpoints.save_server(
                self._endpoint.id,
                base_url=self.url_edit.text(),
                api_key_env=self.env_edit.text(),
                model=self.model_edit.text(),
            )
        except ValueError as exc:
            self.error_label.setText(str(exc))
            return
        self.accept()

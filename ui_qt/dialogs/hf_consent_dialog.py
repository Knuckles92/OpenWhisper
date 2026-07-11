"""Consent dialog for downloading Whisper models from Hugging Face.

Shown when a requested local model is missing from the cache and the access
policy requires user consent (``ask``) or an explicit override (``never``).
Also explains the read-only state when an external ``HF_HUB_OFFLINE=1``
environment override disables downloads entirely.
"""
import logging
from typing import Final

from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QVBoxLayout

from services.hf_access import (
    ConsentAction,
    format_download_size,
    resolve_model_repo,
)
from services.settings import HuggingFaceAccessPolicy
from ui_qt.widgets import Button, PrimaryButton

logger = logging.getLogger(__name__)


class HuggingFaceConsentDialog(QDialog):
    """Modal dialog asking the user to approve a Hugging Face model download."""

    RESULT_CANCEL: Final[str] = ConsentAction.CANCEL
    RESULT_DOWNLOAD_ONCE: Final[str] = ConsentAction.DOWNLOAD_ONCE
    RESULT_ALWAYS_ALLOW: Final[str] = ConsentAction.ALWAYS_ALLOW
    RESULT_OPEN_SETTINGS: Final[str] = ConsentAction.OPEN_SETTINGS

    def __init__(self, model_name: str, policy: str, env_blocked: bool = False,
                 parent=None):
        """Initialize the consent dialog.

        Args:
            model_name: Resolved faster-whisper model name to download.
            policy: Current ``HuggingFaceAccessPolicy`` value; selects which
                action buttons are offered.
            env_blocked: True when an external ``HF_HUB_OFFLINE=1`` disables
                downloads regardless of policy (informational state, no
                download actions offered).
        """
        super().__init__(parent)
        self.model_name = model_name
        self.policy = policy
        self.env_blocked = env_blocked
        self.result_action = self.RESULT_CANCEL

        self.setWindowTitle("Download Whisper Model")
        self.setMinimumWidth(460)
        self.setModal(True)

        self._setup_ui()

    def _setup_ui(self):
        """Build the dialog copy and the policy-dependent action buttons."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        title = QLabel(f'Download "{self.model_name}" model?')
        title.setObjectName("headerLabel")
        layout.addWidget(title)

        body = QLabel(self._body_text())
        body.setObjectName("consentBodyLabel")
        body.setWordWrap(True)
        layout.addWidget(body)

        storage_note = QLabel(
            "Model files are stored locally on this computer. Once downloaded, "
            "this model works fully offline."
        )
        storage_note.setObjectName("infoLabel")
        storage_note.setWordWrap(True)
        layout.addWidget(storage_note)

        layout.addSpacing(8)
        button_layout = QHBoxLayout()
        button_layout.setSpacing(8)
        button_layout.addStretch()

        if self.env_blocked:
            close_btn = Button("Close")
            close_btn.setObjectName("consentCloseButton")
            close_btn.clicked.connect(self.reject)
            button_layout.addWidget(close_btn)
        else:
            cancel_btn = Button("Cancel")
            cancel_btn.setObjectName("consentCancelButton")
            cancel_btn.clicked.connect(self.reject)
            button_layout.addWidget(cancel_btn)

            if self.policy == HuggingFaceAccessPolicy.NEVER:
                settings_btn = Button("Open Settings")
                settings_btn.setObjectName("consentOpenSettingsButton")
                settings_btn.clicked.connect(
                    lambda: self._finish(self.RESULT_OPEN_SETTINGS)
                )
                button_layout.addWidget(settings_btn)
            else:
                always_btn = Button("Always allow")
                always_btn.setObjectName("consentAlwaysAllowButton")
                always_btn.clicked.connect(
                    lambda: self._finish(self.RESULT_ALWAYS_ALLOW)
                )
                button_layout.addWidget(always_btn)

            download_btn = PrimaryButton("Download once")
            download_btn.setObjectName("consentDownloadOnceButton")
            download_btn.clicked.connect(
                lambda: self._finish(self.RESULT_DOWNLOAD_ONCE)
            )
            button_layout.addWidget(download_btn)
            download_btn.setDefault(True)

        layout.addLayout(button_layout)

    def _body_text(self) -> str:
        """Compose the explanatory copy for the current state."""
        repo = resolve_model_repo(self.model_name)
        lines = [
            f'The Whisper model "{self.model_name}" is not on this computer.',
            f"It can be downloaded from Hugging Face (huggingface.co), "
            f"repository {repo}.",
        ]

        size = format_download_size(self.model_name)
        if size:
            lines.append(f"Approximate download size: {size}.")

        if self.env_blocked:
            lines.append(
                "Downloads are currently disabled by the HF_HUB_OFFLINE "
                "environment variable set outside this application. Unset it "
                "and restart to allow downloads."
            )
        elif self.policy == HuggingFaceAccessPolicy.NEVER:
            lines.append(
                'Your settings are set to "Never connect" to Hugging Face. '
                "You can allow this one download, or change the policy in "
                "Settings."
            )

        return "\n\n".join(lines)

    def _finish(self, action: str):
        """Record the chosen action and accept the dialog."""
        self.result_action = action
        self.accept()

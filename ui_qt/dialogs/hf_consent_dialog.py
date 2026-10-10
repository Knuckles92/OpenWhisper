"""Consent dialog for downloading Whisper models from Hugging Face.

Shown when a requested local model is missing from the cache and the access
policy requires user consent (``ask``) or an explicit override (``never``).
Also explains the read-only state when an external ``HF_HUB_OFFLINE=1``
environment override disables downloads entirely.

A download can be gigabytes, so nothing here starts one by default: Cancel
is the default and focused button, and the size is in the title, which stays
in view with the buttons however short the screen is; the long notice scrolls.
"""
import logging
from typing import Final

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from services.hf_access import (
    ConsentAction,
    format_download_size,
    resolve_model_repo,
)
from services.settings import HuggingFaceAccessPolicy
from ui_qt.widgets import Button, PrimaryButton

logger = logging.getLogger(__name__)


class HuggingFaceConsentDialog(QDialog):
    RESULT_CANCEL: Final[str] = ConsentAction.CANCEL
    RESULT_DOWNLOAD_ONCE: Final[str] = ConsentAction.DOWNLOAD_ONCE
    RESULT_ALWAYS_ALLOW: Final[str] = ConsentAction.ALWAYS_ALLOW
    RESULT_OPEN_SETTINGS: Final[str] = ConsentAction.OPEN_SETTINGS

    #: The dialog's width; the notice wraps to it.
    WIDTH = 520
    #: Kept free of the screen's height for the window frame and panels.
    SCREEN_MARGIN = 64
    #: The notice never shrinks below this, however short the screen.
    MIN_BODY_HEIGHT = 96

    def __init__(self, model_name: str, policy: str, env_blocked: bool = False,
                 parent=None):
        """Configure the actions allowed by policy and environment state.

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

        self.setWindowTitle("Download Speech Model")
        self.setMinimumWidth(460)
        self.setModal(True)

        self._setup_ui()

    def _title_text(self) -> str:
        size = format_download_size(self.model_name)
        if size:
            return f'Download "{self.model_name}" (about {size.lstrip("~")})?'
        return f'Download "{self.model_name}" model?'

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        self.title_label = QLabel(self._title_text())
        self.title_label.setObjectName("headerLabel")
        self.title_label.setWordWrap(True)
        layout.addWidget(self.title_label)

        # Publisher, licence, version and the security notice run long: they
        # scroll, so the title and buttons stay in view on a 640 px screen.
        self._body_host = QWidget()
        self._body_host.setObjectName("consentBody")
        body_layout = QVBoxLayout(self._body_host)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(12)

        body = QLabel(self._body_text())
        body.setObjectName("consentBodyLabel")
        body.setWordWrap(True)
        body_layout.addWidget(body)

        storage_note = QLabel(
            "Model files are stored locally on this computer. Once downloaded, "
            "and with its required runtime installed, this model works fully offline."
        )
        storage_note.setObjectName("infoLabel")
        storage_note.setWordWrap(True)
        body_layout.addWidget(storage_note)

        self.body_scroll = QScrollArea()
        self.body_scroll.setObjectName("consentBodyScroll")
        self.body_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.body_scroll.setWidget(self._body_host)
        layout.addWidget(self.body_scroll, stretch=1)

        layout.addSpacing(8)
        button_layout = QHBoxLayout()
        button_layout.setSpacing(8)
        button_layout.addStretch()

        if self.env_blocked:
            close_btn = Button("Close")
            close_btn.setObjectName("consentCloseButton")
            close_btn.clicked.connect(self.reject)
            button_layout.addWidget(close_btn)
            close_btn.setDefault(True)
            self._safe_button = close_btn
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
                always_btn.setAutoDefault(False)
                always_btn.clicked.connect(
                    lambda: self._finish(self.RESULT_ALWAYS_ALLOW)
                )
                button_layout.addWidget(always_btn)

            download_btn = PrimaryButton("Download once")
            download_btn.setObjectName("consentDownloadOnceButton")
            download_btn.setAutoDefault(False)
            download_btn.clicked.connect(
                lambda: self._finish(self.RESULT_DOWNLOAD_ONCE)
            )
            button_layout.addWidget(download_btn)
            # Enter or Space on a dialog that just popped up must not start
            # a multi-gigabyte download.
            cancel_btn.setDefault(True)
            self._safe_button = cancel_btn

        layout.addLayout(button_layout)
        self._safe_button.setFocus()
        self.resize(self.WIDTH, self.sizeHint().height())

    def _available_height(self) -> int:
        """The height of the screen the dialog opens on, less its panels."""
        parent = self.parentWidget()
        screen = (parent.screen() if parent is not None else None) or self.screen()
        if screen is None:
            return 10_000
        return screen.availableGeometry().height()

    def _fit_body(self) -> None:
        """Show the whole notice when it fits on screen; scroll it when not."""
        layout = self.layout()
        margins = layout.contentsMargins()
        width = max(self.width(), self.minimumWidth())
        inner = width - margins.left() - margins.right()
        content = self._body_host.heightForWidth(inner)
        if content < 0:
            content = self._body_host.sizeHint().height()
        # Everything but the notice: title, spacing, buttons and margins.
        self.body_scroll.setFixedHeight(0)
        layout.activate()
        outside = layout.totalHeightForWidth(width) if layout.hasHeightForWidth() else (
            layout.totalSizeHint().height()
        )
        room = self._available_height() - self.SCREEN_MARGIN - outside
        height = max(self.MIN_BODY_HEIGHT, min(content, room))
        self.body_scroll.setMinimumHeight(height)
        self.body_scroll.setMaximumHeight(max(height, content))
        layout.activate()
        self.resize(width, outside + height)

    def showEvent(self, event):
        if not event.spontaneous():
            # Before QDialog's own showEvent, which places it by its size.
            self._fit_body()
        super().showEvent(event)
        if not event.spontaneous():
            self.body_scroll.verticalScrollBar().setValue(0)
            self._safe_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def _body_text(self) -> str:
        from services.component_catalog import get_component_details
        from services.local_asr.catalog import MODELS, gpu_runtime_offer, missing_runtime
        from services.settings import settings_manager

        from services.model_catalog import (
            CUSTOM_MODEL_NOTICE, MODEL_SECURITY_NOTICE, custom_model_details, get_model_details,
        )
        from services.whisper_sources import is_custom_model

        custom = is_custom_model(self.model_name)
        try:
            details = custom_model_details(self.model_name) if custom else get_model_details(self.model_name)
        except KeyError:
            details = None
        repo = resolve_model_repo(self.model_name)
        label = MODELS[self.model_name].label if self.model_name in MODELS else self.model_name
        lines = [
            f'The speech model "{label}" is not on this computer.',
            (f"Downloading connects to {', '.join(details.download_hosts)}. Source: {repo}."
             if details and details.download_hosts != ("huggingface.co",) else
             f"It can be downloaded from Hugging Face (huggingface.co), repository {repo}."),
        ]

        if details:
            lines.append(f"Publisher / maintainer: {details.maintainer}.")
            lines.append(f"License: {details.license}. Terms: {details.license_url or details.origin_url}")
            lines.append(details.verification + ".")
            if details.revision:
                lines.append(f"Selected version: {details.revision}.")
        lines.append(MODEL_SECURITY_NOTICE)
        if custom:
            lines.append(CUSTOM_MODEL_NOTICE)
        if details and "SHA-256" in details.verification:
            lines.append("Integrity checks do not guarantee security or accuracy.")
        # The download size is in the title, where it stays in view.

        settings = settings_manager.load_all_settings()
        runtime = missing_runtime(self.model_name, settings)
        gpu_runtime = gpu_runtime_offer(self.model_name, settings) if runtime else None
        if gpu_runtime:
            lines.append(
                f"A speech runtime is also required to use this model: "
                f"{get_component_details(gpu_runtime).display_name} for this computer's "
                f"NVIDIA GPU, or {get_component_details(runtime).display_name}. The model "
                "download alone will not enable transcription. After you approve the model "
                "download, you will be asked which runtime to install."
            )
        elif runtime:
            name = get_component_details(runtime).display_name
            lines.append(
                f"{name} is also required to use this model. The model download alone "
                "will not enable transcription. After you approve the model download, "
                "you will also be prompted to install the required runtime."
            )

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
        self.result_action = action
        self.accept()

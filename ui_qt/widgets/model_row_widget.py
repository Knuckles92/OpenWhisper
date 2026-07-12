"""Single-model row for the Model Manager dialog.

Shows one catalog model's identity (name, Hugging Face repo), its cache
status (downloaded / active / downloading / not downloaded), its size
(actual on-disk when cached, bundled estimate otherwise), and the actions
that apply in the current state (Download / Set Active / Delete).
"""
import logging
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout

from services.hf_access import (
    CachedModelInfo,
    format_download_size,
    format_size_bytes,
    resolve_model_repo,
)
from ui_qt.widgets.buttons import Button, DangerButton, PrimaryButton

logger = logging.getLogger(__name__)

# Theme palette (matches ui_qt/utils/theme_manager.py)
_PRIMARY = "#0a84ff"
_SUCCESS = "#30d158"
_SECONDARY = "#8e8e93"

_ROW_STYLE = (
    "QFrame#modelRow { background-color: #2c2c2e; "
    "border: 1px solid #3a3a3c; border-radius: 8px; }"
)
_BADGE_STYLE = (
    "QLabel {{ color: {color}; border: 1px solid {color}; "
    "border-radius: 9px; padding: 1px 8px; font-size: 10px; }}"
)


class ModelRowWidget(QFrame):
    """One row in the Model Manager's model list.

    The row is "dumb": it renders the state handed to :meth:`update_state`
    and re-emits button clicks with its model name; all cache scanning and
    download/delete logic stays with the dialog and controller.
    """

    download_clicked = pyqtSignal(str)
    delete_clicked = pyqtSignal(str)
    set_active_clicked = pyqtSignal(str)

    def __init__(self, model_name: str, parent=None):
        """Initialize the row for one catalog model.

        Args:
            model_name: Concrete faster-whisper model name (e.g. ``"base"``).
        """
        super().__init__(parent)
        self.model_name = model_name
        self.repo_id = resolve_model_repo(model_name)

        self.setObjectName("modelRow")
        self.setStyleSheet(_ROW_STYLE)
        self._setup_ui()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(12)

        # Identity column: model name over repo ID.
        identity = QVBoxLayout()
        identity.setSpacing(2)

        name_label = QLabel(self.model_name)
        name_font = QFont("Segoe UI", 11)
        name_font.setBold(True)
        name_label.setFont(name_font)
        identity.addWidget(name_label)

        repo_label = QLabel(self.repo_id)
        repo_label.setFont(QFont("Segoe UI", 9))
        repo_label.setStyleSheet(f"color: {_SECONDARY};")
        identity.addWidget(repo_label)

        layout.addLayout(identity, stretch=1)

        self.size_label = QLabel("")
        self.size_label.setFont(QFont("Segoe UI", 10))
        layout.addWidget(self.size_label)

        self.badge = QLabel("")
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.badge)

        self.download_button = PrimaryButton("Download")
        self.download_button.clicked.connect(
            lambda: self.download_clicked.emit(self.model_name)
        )
        layout.addWidget(self.download_button)

        self.set_active_button = Button("Set Active")
        self.set_active_button.clicked.connect(
            lambda: self.set_active_clicked.emit(self.model_name)
        )
        layout.addWidget(self.set_active_button)

        self.delete_button = DangerButton("Delete")
        self.delete_button.clicked.connect(
            lambda: self.delete_clicked.emit(self.model_name)
        )
        layout.addWidget(self.delete_button)

    def _set_badge(self, text: str, color: str):
        self.badge.setText(text)
        self.badge.setStyleSheet(_BADGE_STYLE.format(color=color))

    def update_state(
        self,
        info: Optional[CachedModelInfo],
        is_active: bool,
        is_loaded: bool,
        downloading: bool,
        downloads_blocked: bool = False,
        download_slot_busy: bool = False,
    ) -> None:
        """Render the row for the current cache/engine state.

        Args:
            info: Cache entry for this model's repo, or None when not
                downloaded.
            is_active: True when this model is the persisted selection.
            is_loaded: True when the engine currently has this model loaded
                (Delete is disabled — the files are memory-mapped).
            downloading: True while a download for this model is in flight.
            downloads_blocked: True when ``HF_HUB_OFFLINE`` disables all
                downloads.
            download_slot_busy: True while any model is downloading (only one
                download runs at a time).
        """
        cached = info is not None

        if cached:
            self.size_label.setText(format_size_bytes(info.size_bytes))
            self.size_label.setStyleSheet("")
        else:
            estimate = format_download_size(self.model_name)
            self.size_label.setText(estimate or "size unknown")
            self.size_label.setStyleSheet(f"color: {_SECONDARY};")

        if downloading:
            self._set_badge("Downloading…", _PRIMARY)
        elif is_active and cached:
            self._set_badge("Active", _PRIMARY)
        elif cached:
            self._set_badge("Downloaded", _SUCCESS)
        else:
            self._set_badge("Not downloaded", _SECONDARY)

        self.download_button.setVisible(not cached)
        self.download_button.setEnabled(
            not downloading and not downloads_blocked and not download_slot_busy
        )
        if downloads_blocked:
            self.download_button.setToolTip(
                "Downloads are disabled by HF_HUB_OFFLINE"
            )
        elif download_slot_busy and not downloading:
            self.download_button.setToolTip("Another download is in progress")
        else:
            self.download_button.setToolTip("")

        self.set_active_button.setVisible(cached and not is_active)
        self.set_active_button.setEnabled(not downloading)

        self.delete_button.setVisible(cached)
        self.delete_button.setEnabled(not is_loaded and not downloading)
        self.delete_button.setToolTip(
            "In use — switch models first" if is_loaded else ""
        )

    def matches_filter(self, text: str) -> bool:
        """Return True when the row matches a filter string.

        Args:
            text: Lowercased substring to match against name and repo ID.
        """
        return text in self.model_name.lower() or text in self.repo_id.lower()

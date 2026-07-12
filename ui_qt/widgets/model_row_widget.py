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
    MODEL_DOWNLOAD_SIZE_MB,
    format_download_size,
    format_size_bytes,
    resolve_model_repo,
)
from ui_qt.widgets.buttons import Button, DangerButton, PrimaryButton

logger = logging.getLogger(__name__)

# Cohesive row stylesheet. Child labels must set an explicit transparent
# background — the global ``QWidget { background-color: #1c1c1e }`` rule
# otherwise paints dark rectangles on top of the lighter row fill.
_ROW_STYLE = """
    QFrame#modelRow {
        background-color: rgba(44, 44, 46, 0.55);
        border: 1px solid rgba(255, 255, 255, 0.06);
        border-radius: 10px;
    }
    QFrame#modelRow:hover {
        background-color: rgba(58, 58, 60, 0.65);
        border: 1px solid rgba(10, 132, 255, 0.28);
    }
    QFrame#modelRow[active="true"] {
        background-color: rgba(10, 132, 255, 0.12);
        border: 1px solid rgba(10, 132, 255, 0.22);
    }
    QFrame#modelRow[active="true"]:hover {
        background-color: rgba(10, 132, 255, 0.18);
        border: 1px solid rgba(10, 132, 255, 0.35);
    }
    QLabel#modelRowName {
        color: #f5f5f7;
        background-color: transparent;
        border: none;
        font-weight: 600;
    }
    QLabel#modelRowSummary {
        color: #8e8e93;
        background-color: transparent;
        border: none;
    }
    QLabel#modelRowSize {
        color: #aeaeb2;
        background-color: transparent;
        border: none;
    }
    QLabel#modelRowSize[muted="true"] {
        color: #636366;
    }
    QLabel#modelRowBadge {
        background-color: rgba(142, 142, 147, 0.14);
        color: #aeaeb2;
        border: 1px solid rgba(142, 142, 147, 0.28);
        border-radius: 6px;
        padding: 2px 8px;
        font-size: 10px;
        font-weight: 600;
    }
    QLabel#modelRowBadge[tone="active"],
    QLabel#modelRowBadge[tone="downloading"] {
        background-color: rgba(10, 132, 255, 0.14);
        color: #6fb1ff;
        border: 1px solid rgba(10, 132, 255, 0.28);
    }
    QLabel#modelRowBadge[tone="downloaded"] {
        background-color: rgba(48, 209, 88, 0.12);
        color: #32d74b;
        border: 1px solid rgba(48, 209, 88, 0.28);
    }
    QPushButton#modelDownloadButton,
    QPushButton#modelSetActiveButton,
    QPushButton#modelDeleteButton {
        border-radius: 7px;
        padding: 4px 10px;
        font-size: 11px;
        font-weight: 600;
        min-height: 28px;
        max-height: 28px;
    }
    QPushButton#modelDownloadButton {
        background-color: rgba(10, 132, 255, 0.18);
        color: #6fb1ff;
        border: 1px solid rgba(10, 132, 255, 0.32);
    }
    QPushButton#modelDownloadButton:hover {
        background-color: rgba(10, 132, 255, 0.28);
        border: 1px solid rgba(10, 132, 255, 0.5);
    }
    QPushButton#modelDownloadButton:disabled {
        background-color: rgba(58, 58, 60, 0.4);
        color: #636366;
        border: 1px solid rgba(255, 255, 255, 0.06);
    }
    QPushButton#modelSetActiveButton {
        background-color: rgba(255, 255, 255, 0.06);
        color: #f5f5f7;
        border: 1px solid rgba(255, 255, 255, 0.1);
    }
    QPushButton#modelSetActiveButton:hover {
        background-color: rgba(255, 255, 255, 0.1);
        border: 1px solid rgba(10, 132, 255, 0.35);
    }
    QPushButton#modelDeleteButton {
        background-color: transparent;
        color: #ff6961;
        border: 1px solid rgba(255, 255, 255, 0.1);
    }
    QPushButton#modelDeleteButton:hover {
        background-color: rgba(255, 69, 58, 0.14);
        border: 1px solid rgba(255, 69, 58, 0.45);
    }
    QPushButton#modelDeleteButton:disabled {
        color: #636366;
        border: 1px solid rgba(255, 255, 255, 0.06);
    }
"""


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
        self.is_cached = False
        self.is_active = False
        self.sort_size_bytes = 0

        self.setObjectName("modelRow")
        self.setStyleSheet(_ROW_STYLE)
        self._setup_ui()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 12, 10)
        layout.setSpacing(12)

        # Identity column: model name over summary.
        identity = QVBoxLayout()
        identity.setSpacing(2)

        name_label = QLabel(self.model_name)
        name_label.setObjectName("modelRowName")
        name_font = QFont("Segoe UI", 10)
        name_font.setBold(True)
        name_label.setFont(name_font)
        identity.addWidget(name_label)

        self.repo_label = QLabel(self._model_summary())
        self.repo_label.setObjectName("modelRowSummary")
        self.repo_label.setFont(QFont("Segoe UI", 8))
        self.repo_label.setToolTip(self.repo_id)
        identity.addWidget(self.repo_label)

        layout.addLayout(identity, stretch=1)

        self.size_label = QLabel("")
        self.size_label.setObjectName("modelRowSize")
        self.size_label.setFont(QFont("Segoe UI", 9))
        self.size_label.setMinimumWidth(72)
        self.size_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        layout.addWidget(self.size_label)

        self.badge = QLabel("")
        self.badge.setObjectName("modelRowBadge")
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.setFixedHeight(22)
        layout.addWidget(self.badge)

        self.download_button = PrimaryButton("Download")
        self.download_button.setObjectName("modelDownloadButton")
        self._compact_button(self.download_button, 90)
        self.download_button.clicked.connect(
            lambda: self.download_clicked.emit(self.model_name)
        )
        layout.addWidget(self.download_button)

        self.set_active_button = Button("Set Active")
        self.set_active_button.setObjectName("modelSetActiveButton")
        self._compact_button(self.set_active_button, 90)
        self.set_active_button.clicked.connect(
            lambda: self.set_active_clicked.emit(self.model_name)
        )
        layout.addWidget(self.set_active_button)

        self.delete_button = DangerButton("Delete")
        self.delete_button.setObjectName("modelDeleteButton")
        self._compact_button(self.delete_button, 72)
        self.delete_button.clicked.connect(
            lambda: self.delete_clicked.emit(self.model_name)
        )
        layout.addWidget(self.delete_button)

    def _model_summary(self) -> str:
        """Return a compact, user-facing description of this model."""
        language = "English only" if self.model_name.endswith(".en") else "Multilingual"
        family = "Distilled" if self.model_name.startswith("distil-") else ""
        return " / ".join(part for part in (language, family) if part)

    @staticmethod
    def _compact_button(button, width: int) -> None:
        """Apply dialog-sized dimensions to a shared application button."""
        button.set_base_minimum_size(width, 28)
        button.setMinimumWidth(width)
        button.setMaximumWidth(width)
        button.setMinimumHeight(28)
        button.setMaximumHeight(28)
        button.setFont(QFont("Segoe UI", 10))

    def _set_badge(self, text: str, tone: str):
        """Update badge text and dynamic tone property for QSS styling."""
        self.badge.setText(text)
        self.badge.setProperty("tone", tone)
        # Re-polish so the dynamic property selector takes effect.
        self.badge.style().unpolish(self.badge)
        self.badge.style().polish(self.badge)
        self.badge.update()

    def _set_active_style(self, active: bool) -> None:
        """Tint the row when this model is the active selection."""
        self.setProperty("active", active)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

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
        self.is_cached = cached
        self.is_active = is_active
        self._set_active_style(is_active and cached)

        if cached:
            self.size_label.setText(format_size_bytes(info.size_bytes))
            self.size_label.setProperty("muted", False)
            self.sort_size_bytes = info.size_bytes
        else:
            estimate = format_download_size(self.model_name)
            self.size_label.setText(estimate or "size unknown")
            self.size_label.setProperty("muted", True)
            self.sort_size_bytes = (
                MODEL_DOWNLOAD_SIZE_MB.get(self.model_name, float("inf"))
                * 1_000_000
            )
        self.size_label.style().unpolish(self.size_label)
        self.size_label.style().polish(self.size_label)

        if downloading:
            self._set_badge("Downloading…", "downloading")
        elif is_active and cached:
            self._set_badge("Active", "active")
        elif cached:
            self._set_badge("Downloaded", "downloaded")
        else:
            self._set_badge("Not downloaded", "idle")

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

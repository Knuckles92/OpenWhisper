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
from PyQt6.QtWidgets import (
    QAbstractButton,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QVBoxLayout,
)

from services.hf_access import (
    CachedModelInfo,
    MODEL_DOWNLOAD_SIZE_MB,
    format_download_size,
    format_size_bytes,
    resolve_model_repo,
)
from ui_qt.utils.fuzzy_match import fuzzy_match
from ui_qt.widgets.buttons import Button, DangerButton, PrimaryButton
from ui_qt.widgets.eliding_label import ElidingLabel

logger = logging.getLogger(__name__)

# Cohesive row stylesheet. Child labels must set an explicit transparent
# background — the global ``QWidget { background-color: #1c1c1e }`` rule
# otherwise paints dark rectangles on top of the lighter row fill.
_ROW_STYLE = """
    QFrame#modelRow {
        background-color: #141b22;
        border: 1px solid #303b45;
        border-radius: 12px;
    }
    QFrame#modelRow:hover {
        background-color: #182028;
        border: 1px solid #3d4a57;
    }
    QFrame#modelRow:focus {
        border: 1px solid #2a5382;
        outline: none;
    }
    QFrame#modelRow[active="true"] {
        background-color: #12222f;
        border: 1px solid #245079;
    }
    QFrame#modelRow[active="true"]:hover {
        background-color: #15283a;
        border: 1px solid #2f6396;
    }
    QFrame#modelRow[selected="true"],
    QFrame#modelRow[selected="true"]:hover {
        background-color: #17263a;
        border: 1px solid #3a6aa3;
    }
    QLabel#modelRowName {
        color: #e8edf2;
        background-color: transparent;
        border: none;
        font-weight: 600;
    }
    QLabel#modelRowSummary {
        color: #98a3b0;
        background-color: transparent;
        border: none;
    }
    QLabel#modelRowSize {
        color: #c7d0d9;
        background-color: transparent;
        border: none;
    }
    QLabel#modelRowSize[muted="true"] {
        color: #6f7b87;
    }
    QLabel#modelRowBadge {
        background-color: rgba(141, 154, 167, 0.12);
        color: #aeb8c3;
        border: 1px solid rgba(141, 154, 167, 0.28);
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
    QLabel#modelRowBadge[tone="queued"] {
        background-color: rgba(10, 132, 255, 0.08);
        color: #8e99a6;
        border: 1px solid rgba(10, 132, 255, 0.18);
    }
    QCheckBox#modelSelectCheckbox {
        background-color: transparent;
        border: none;
    }
    QLabel#modelRowUsage {
        background-color: rgba(10, 132, 255, 0.10);
        color: #8eb8ff;
        border: 1px solid rgba(10, 132, 255, 0.22);
        border-radius: 6px;
        padding: 2px 8px;
        font-size: 10px;
        font-weight: 600;
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
        background-color: #1b252e;
        color: #5d6873;
        border: 1px solid #263038;
    }
    QPushButton#modelSetActiveButton {
        background-color: #1b252e;
        color: #e8edf2;
        border: 1px solid #35404a;
    }
    QPushButton#modelSetActiveButton:hover {
        background-color: #22303b;
        border: 1px solid #4b5966;
    }
    QPushButton#modelDeleteButton {
        background-color: transparent;
        color: #ff6961;
        border: 1px solid #3b4752;
    }
    QPushButton#modelDeleteButton:hover {
        background-color: rgba(255, 69, 58, 0.14);
        border: 1px solid rgba(255, 69, 58, 0.45);
    }
    QPushButton#modelDeleteButton:disabled {
        color: #5d6873;
        border: 1px solid #263038;
    }
    QProgressBar#modelRowProgress {
        background-color: #243039;
        border: none;
        border-radius: 3px;
        min-height: 6px;
        max-height: 6px;
    }
    QProgressBar#modelRowProgress::chunk {
        background-color: #0a84ff;
        border-radius: 3px;
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
    details_requested = pyqtSignal(str)
    selection_toggled = pyqtSignal(str, bool)

    def __init__(self, model_name: str, parent=None):
        """Represent one concrete faster-whisper catalog model.

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
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip("Click to view technical details")
        self.setAccessibleName(f"{model_name} model")
        self.setAccessibleDescription(
            "Open technical details. Model management actions are separate."
        )
        self._setup_ui()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 12, 10)
        layout.setSpacing(12)

        self.select_checkbox = QCheckBox()
        self.select_checkbox.setObjectName("modelSelectCheckbox")
        self.select_checkbox.setToolTip("Select for batch download")
        self.select_checkbox.setAccessibleName(f"Select {self.model_name} for download")
        self.select_checkbox.toggled.connect(
            lambda checked: self.selection_toggled.emit(self.model_name, checked)
        )
        layout.addWidget(self.select_checkbox)

        identity = QVBoxLayout()
        identity.setSpacing(2)

        from services.local_asr.catalog import MODELS
        name_label = ElidingLabel(MODELS[self.model_name].label if self.model_name in MODELS else self.model_name)
        name_label.setObjectName("modelRowName")
        name_font = QFont("Segoe UI", 10)
        name_font.setBold(True)
        name_label.setFont(name_font)
        identity.addWidget(name_label)

        # Elides: this secondary line is long enough that a plain QLabel would
        # raise the whole window's minimum width past the list column.
        self.repo_label = ElidingLabel(self._model_summary())
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

        self.usage_label = QLabel("")
        self.usage_label.setObjectName("modelRowUsage")
        self.usage_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.usage_label.setFixedHeight(22)
        self.usage_label.setVisible(False)
        layout.addWidget(self.usage_label)

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

        # Overlay, not a layout child: a hidden QProgressBar still inflates
        # the catalog column's minimum width past the Downloads viewport.
        self.progress = QProgressBar(self)
        self.progress.setObjectName("modelRowProgress")
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 0)
        self.progress.hide()

    def _model_summary(self) -> str:
        from services.local_asr.catalog import MODELS
        if self.model_name in MODELS:
            model = MODELS[self.model_name]
            return model.languages + (" / Live meetings" if model.streaming else "")
        language = "English only" if self.model_name.endswith(".en") else "Multilingual"
        family = "Distilled" if self.model_name.startswith("distil-") else ""
        return " / ".join(part for part in (language, family) if part)

    @staticmethod
    def _compact_button(button, width: int) -> None:
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
        queued: bool = False,
        selection_enabled: bool = True,
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
            queued: True while this model waits in a batch download queue.
            selection_enabled: False while downloads are busy or blocked; the
                batch checkbox then cannot be toggled.
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
            self._show_progress()
        else:
            self._hide_progress()
            if queued:
                self._set_badge("Queued", "queued")
            elif is_active and cached:
                self._set_badge("Active", "active")
            elif cached:
                self._set_badge("Downloaded", "downloaded")
            else:
                self._set_badge("Not downloaded", "idle")

        self.select_checkbox.setVisible(not cached)
        self.select_checkbox.setEnabled(selection_enabled and not downloading)
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

    def _show_progress(self) -> None:
        self.progress.show()
        self._place_progress()

    def _hide_progress(self) -> None:
        self.progress.hide()
        self.progress.setValue(0)

    def _place_progress(self) -> None:
        if not self.progress.isVisible():
            return
        margins = self.layout().contentsMargins()
        height = 6
        self.progress.setGeometry(
            margins.left(),
            self.height() - margins.bottom() - height,
            max(0, self.width() - margins.left() - margins.right()),
            height,
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_progress()

    def set_progress(self, done: int, total: int) -> None:
        """Update the in-row download bar with bytes or an indeterminate state."""
        self._show_progress()
        if total <= 0:
            self.progress.setRange(0, 0)
            return
        self.progress.setRange(0, 100)
        self.progress.setValue(int(done * 100 / total))
        self.size_label.setText(
            f"{format_size_bytes(done)} of {format_size_bytes(total)}"
        )

    def set_usage(self, text: str) -> None:
        """Show which mode pages currently assign this model.

        Args:
            text: Usage chip copy such as ``"On-demand"`` or
                ``"On-demand · Meetings"``. Empty hides the chip.
        """
        self.usage_label.setText(text)
        self.usage_label.setVisible(bool(text))

    def matches_filter(self, text: str) -> bool:
        return fuzzy_match(text, self.model_name) or fuzzy_match(text, self.repo_id)

    @staticmethod
    def _is_action_child(widget) -> bool:
        while widget is not None:
            if isinstance(widget, QAbstractButton):
                return True
            widget = widget.parentWidget()
        return False

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            child = self.childAt(event.position().toPoint())
            if not self._is_action_child(child):
                self.details_requested.emit(self.model_name)
                event.accept()
                return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Space,
        ):
            self.details_requested.emit(self.model_name)
            event.accept()
            return
        super().keyPressEvent(event)

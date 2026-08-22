"""Compact local-Whisper assignment control for Model Manager mode pages."""
from typing import Dict, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from config import config
from services.hf_access import CachedModelInfo, resolve_model_repo
from ui_qt.widgets.no_wheel import ElidingComboBox


class LocalModelPicker(QWidget):
    """Assign a local Whisper model from ``auto`` plus downloaded sizes.

    The Downloads window owns download and delete. Each Model Manager
    destination uses this picker to choose which cached model it should load.
    """

    model_changed = pyqtSignal(str)
    manage_downloads_requested = pyqtSignal()

    def __init__(self, parent=None):
        """Build the compact assignment combo and manage-downloads link."""
        super().__init__(parent)
        self.setObjectName("localModelPicker")
        self._selected = config.DEFAULT_WHISPER_MODEL
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)

        self.model_combo = ElidingComboBox()
        self.model_combo.setObjectName("localModelPickerCombo")
        self.model_combo.setMinimumHeight(40)
        self.model_combo.currentIndexChanged.connect(self._on_combo_changed)
        row.addWidget(self.model_combo, stretch=1)

        self.manage_button = QPushButton("Manage downloads")
        self.manage_button.setObjectName("localModelPickerManage")
        self.manage_button.setFlat(True)
        self.manage_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.manage_button.setToolTip(
            "Open Downloads to add or delete Whisper models"
        )
        self.manage_button.clicked.connect(self.manage_downloads_requested)
        row.addWidget(self.manage_button)
        layout.addLayout(row)

        self.caption_label = QLabel("")
        self.caption_label.setObjectName("localModelPickerCaption")
        self.caption_label.setWordWrap(True)
        layout.addWidget(self.caption_label)

    def current_model(self) -> str:
        """Return the staged Whisper model name."""
        data = self.model_combo.currentData()
        if isinstance(data, str) and data:
            return data
        return self.model_combo.currentText().strip() or self._selected

    def set_caption(self, text: str) -> None:
        """Update the resolved-state or explanatory caption.

        Args:
            text: Caption shown under the combo. Empty hides the line.
        """
        self.caption_label.setText(text)
        self.caption_label.setVisible(bool(text))

    def set_options(
        self,
        cached: Dict[str, CachedModelInfo],
        selected: str,
        resolved: Optional[str] = None,
    ) -> None:
        """Rebuild the combo from cache state without emitting ``model_changed``.

        Args:
            cached: Repo-id keyed cache scan from ``scan_cached_models``.
            selected: Persisted model name, including ``auto``.
            resolved: Concrete model ``auto`` currently maps to, if known.
        """
        if selected not in config.WHISPER_MODEL_CHOICES:
            selected = config.DEFAULT_WHISPER_MODEL
        self._selected = selected

        names = ["auto"]
        for name in config.WHISPER_MODEL_CHOICES:
            if name == "auto":
                continue
            repo_id = resolve_model_repo(name)
            if repo_id in cached or name == selected:
                names.append(name)
        if selected not in names:
            names.append(selected)

        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        for name in names:
            if name == "auto":
                label = "auto — turbo on GPU · base on CPU"
            else:
                label = name
            self.model_combo.addItem(label, name)
        index = self.model_combo.findData(selected)
        self.model_combo.setCurrentIndex(max(0, index))
        self.model_combo.blockSignals(False)

        if selected == "auto" and resolved:
            self.set_caption(f"Automatic selection currently resolves to {resolved}.")
        elif selected == "auto":
            self.set_caption("Automatic selection: turbo on GPU, base on CPU.")
        else:
            self.set_caption("")

    def _on_combo_changed(self, _index: int) -> None:
        """Emit the newly assigned model name."""
        model = self.current_model()
        if not model or model == self._selected:
            return
        self._selected = model
        self.model_changed.emit(model)

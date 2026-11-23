"""Review and import existing local speech models found on this computer.

Shows models discovered in default caches (Hugging Face Hub, Handy/transcribe.cpp,
Moonshine Voice, NeMo-Speech) or in any folder the user chooses to scan. Lets the
user import all of them or a selected subset in place without copying or
re-downloading multi-gigabyte model files.
"""
from __future__ import annotations

import itertools
import logging
import threading
from dataclasses import replace
from typing import Dict, List, Optional, Sequence

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from services.format_utils import format_size_bytes
from services.local_asr import custom
from services.local_asr.catalog import BACKENDS
from services.local_asr.discovery import (
    DiscoveredModel,
    ImportResult,
    discover_existing_models,
    discover_models_in_folder,
    import_discovered_models,
)
from ui_qt.utils.app_icon import app_icon
from ui_qt.widgets import Button, ElidingLabel, PrimaryButton
from ui_qt.widgets.buttons import fit_compact_button
from ui_qt.widgets.no_wheel import NoWheelComboBox
from ui_qt.widgets.wrapped_label import WrappedLabel

logger = logging.getLogger(__name__)

_ENGINE_COLORS: Dict[str, tuple[str, str]] = {
    "parakeet_unified": ("Parakeet Unified", "56, 189, 248"),
    "local_whisper": ("Whisper", "167, 139, 250"),
    "parakeet": ("Parakeet", "52, 211, 153"),
    "qwen_asr": ("Qwen3-ASR", "251, 191, 36"),
    "nemotron": ("Nemotron", "244, 114, 182"),
    "moonshine": ("Moonshine", "251, 146, 60"),
    "parakeet_mlx": ("Parakeet MLX", "45, 212, 191"),
}


class _ImportRelay(QObject):
    """Marshals background folder scans and imports back onto the Qt thread."""

    scan_finished = pyqtSignal(int, object, str)
    import_finished = pyqtSignal(int, object)


_relay: Optional[_ImportRelay] = None
_tokens = itertools.count(1)


def _import_relay() -> _ImportRelay:
    global _relay
    if _relay is None:
        _relay = _ImportRelay()
    return _relay


class _ImportModelRow(QFrame):
    """One selectable discovered model row inside :class:`ImportModelsDialog`."""

    toggled = pyqtSignal()

    def __init__(self, item: DiscoveredModel, gguf_backends: Sequence[str], parent=None):
        super().__init__(parent)
        self.item = item
        self.setObjectName("importModelRow")

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 9, 12, 9)
        row.setSpacing(10)

        self.checkbox = QCheckBox()
        self.checkbox.setChecked(True)
        self.checkbox.setAccessibleName(f"Import {item.label}")
        self.checkbox.toggled.connect(lambda _checked: self.toggled.emit())
        row.addWidget(self.checkbox, 0, Qt.AlignmentFlag.AlignVCenter)

        info_col = QVBoxLayout()
        info_col.setContentsMargins(0, 0, 0, 0)
        info_col.setSpacing(2)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(6)

        self.name_label = ElidingLabel(item.label)
        self.name_label.setObjectName("importModelRowName")
        self.name_label.setToolTip(item.path)
        title_row.addWidget(self.name_label, stretch=1)

        engine_name, rgb = _ENGINE_COLORS.get(item.backend, (BACKENDS.get(item.backend, item.backend), "148, 163, 184"))
        self.engine_badge = QLabel(engine_name)
        self.engine_badge.setObjectName("importModelEngineBadge")
        self.engine_badge.setStyleSheet(
            f"QLabel#importModelEngineBadge {{"
            f" background-color: rgba({rgb}, 0.14);"
            f" color: rgb({rgb});"
            f" border: 1px solid rgba({rgb}, 0.30);"
            f" border-radius: 5px; padding: 1px 6px;"
            f" font-size: 10px; font-weight: 600;"
            f"}}"
        )
        title_row.addWidget(self.engine_badge, 0, Qt.AlignmentFlag.AlignVCenter)

        kind_text = "Built-in match" if item.kind == "builtin_speech" else "Custom model"
        self.kind_badge = QLabel(kind_text)
        self.kind_badge.setObjectName("importModelKindBadge")
        self.kind_badge.setProperty("kind", "builtin" if item.kind == "builtin_speech" else "custom")
        title_row.addWidget(self.kind_badge, 0, Qt.AlignmentFlag.AlignVCenter)

        info_col.addLayout(title_row)

        action_note = (
            "Links directly to built-in catalog entry"
            if item.kind == "builtin_speech"
            else f"{item.source_label} · {item.path}"
        )
        subtitle_text = (
            f"{item.source_label} · {action_note}"
            if item.kind == "builtin_speech"
            else action_note
        )
        self.subtitle_label = ElidingLabel(subtitle_text)
        self.subtitle_label.setObjectName("importModelRowSubtitle")
        self.subtitle_label.setToolTip(item.path)
        info_col.addWidget(self.subtitle_label)

        row.addLayout(info_col, stretch=1)

        self.engine_combo: Optional[NoWheelComboBox] = None
        if item.needs_engine_choice and item.kind == "custom_speech" and gguf_backends:
            self.engine_combo = NoWheelComboBox()
            self.engine_combo.setMinimumWidth(130)
            for backend_id in gguf_backends:
                self.engine_combo.addItem(BACKENDS[backend_id], backend_id)
            idx = self.engine_combo.findData(item.backend)
            if idx >= 0:
                self.engine_combo.setCurrentIndex(idx)
            self.engine_combo.currentIndexChanged.connect(self._on_backend_changed)
            row.addWidget(self.engine_combo, 0, Qt.AlignmentFlag.AlignVCenter)

        self.size_label = QLabel(format_size_bytes(item.size_bytes) if item.size_bytes else "")
        self.size_label.setObjectName("importModelRowSize")
        self.size_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.size_label.setMinimumWidth(56)
        row.addWidget(self.size_label, 0, Qt.AlignmentFlag.AlignVCenter)

    def _on_backend_changed(self, _index: int) -> None:
        if self.engine_combo is None:
            return
        backend = self.engine_combo.currentData()
        if not isinstance(backend, str) or not backend:
            return
        self.item = replace(self.item, backend=backend)
        engine_name, rgb = _ENGINE_COLORS.get(backend, (BACKENDS.get(backend, backend), "148, 163, 184"))
        self.engine_badge.setText(engine_name)
        self.engine_badge.setStyleSheet(
            f"QLabel#importModelEngineBadge {{"
            f" background-color: rgba({rgb}, 0.14);"
            f" color: rgb({rgb});"
            f" border: 1px solid rgba({rgb}, 0.30);"
            f" border-radius: 5px; padding: 1px 6px;"
            f" font-size: 10px; font-weight: 600;"
            f"}}"
        )

    def is_checked(self) -> bool:
        return self.checkbox.isChecked()

    def set_checked(self, checked: bool) -> None:
        self.checkbox.setChecked(checked)


class ImportModelsDialog(QDialog):
    """Choose which discovered local speech models to import in place."""

    def __init__(
        self,
        discovered: Optional[Sequence[DiscoveredModel]] = None,
        parent=None,
        *,
        run_async: bool = True,
    ):
        super().__init__(parent)
        self.setWindowTitle("Import existing speech models")
        self.setWindowIcon(app_icon())
        self.setObjectName("importModelsDialog")
        self.setModal(True)
        self.setMinimumSize(640, 480)

        self._run_async = run_async
        self._gguf_backends = custom.addable_gguf_backends()
        self._rows: List[_ImportModelRow] = []
        self._seen_ids: set[str] = set()
        self._busy = False
        self._token = 0
        self.import_result: Optional[ImportResult] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        title = QLabel("Import existing speech models")
        title.setObjectName("downloadsTitle")
        layout.addWidget(title)

        intro = WrappedLabel(
            "OpenWhisper can reuse compatible models already stored on this computer "
            "(for example in your Hugging Face cache or another local app). Imported "
            "models stay where they are — nothing is copied or downloaded again."
        )
        intro.setObjectName("downloadsSourceNote")
        layout.addWidget(intro)

        # Selection toolbar
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(8)

        self.select_all_button = Button("Select all")
        self.select_all_button.setObjectName("downloadsFlatButton")
        fit_compact_button(self.select_all_button, 0)
        self.select_all_button.clicked.connect(lambda: self._set_all_checked(True))
        bar.addWidget(self.select_all_button)

        self.select_none_button = Button("Select none")
        self.select_none_button.setObjectName("downloadsFlatButton")
        fit_compact_button(self.select_none_button, 0)
        self.select_none_button.clicked.connect(lambda: self._set_all_checked(False))
        bar.addWidget(self.select_none_button)

        self.summary_label = ElidingLabel("")
        self.summary_label.setObjectName("downloadsSelectionSummary")
        bar.addWidget(self.summary_label, stretch=1)

        self.scan_folder_button = Button("Scan folder…")
        self.scan_folder_button.setObjectName("downloadsToolButton")
        fit_compact_button(self.scan_folder_button, 0)
        self.scan_folder_button.setToolTip("Look for compatible speech models in another folder")
        self.scan_folder_button.clicked.connect(self._on_scan_folder_clicked)
        bar.addWidget(self.scan_folder_button)
        layout.addLayout(bar)

        # Scrollable rows list
        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("importModelsScroll")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)

        list_container = QWidget()
        list_container.setObjectName("importModelsContainer")
        self.list_layout = QVBoxLayout(list_container)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(6)

        self.empty_label = QLabel(
            "No additional reusable speech models were found in default folders.\n"
            "Click “Scan folder…” to search another directory on this computer."
        )
        self.empty_label.setObjectName("infoLabel")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.list_layout.addWidget(self.empty_label)
        self.list_layout.addStretch()
        self.scroll_area.setWidget(list_container)
        layout.addWidget(self.scroll_area, stretch=1)

        self.status_label = QLabel("")
        self.status_label.setObjectName("downloadsMessage")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch()
        self.cancel_button = Button("Skip for now")
        fit_compact_button(self.cancel_button, 0)
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)

        self.import_button = PrimaryButton("Import selected")
        fit_compact_button(self.import_button, 0)
        self.import_button.clicked.connect(self._on_import_clicked)
        buttons.addWidget(self.import_button)
        layout.addLayout(buttons)

        relay = _import_relay()
        relay.scan_finished.connect(self._on_scan_finished)
        relay.import_finished.connect(self._on_import_finished)

        if discovered is not None:
            self._add_items(discovered)
            self._refresh_controls()
        else:
            self._start_scan(discover_existing_models, "Checking default model folders…")

    @property
    def discovered_items(self) -> list[DiscoveredModel]:
        return [row.item for row in self._rows]

    def selected_items(self) -> list[DiscoveredModel]:
        return [row.item for row in self._rows if row.is_checked()]

    def _add_items(self, items: Sequence[DiscoveredModel]) -> int:
        added = 0
        for item in items:
            if item.id in self._seen_ids:
                continue
            self._seen_ids.add(item.id)
            row = _ImportModelRow(item, self._gguf_backends)
            row.toggled.connect(self._refresh_controls)
            # Insert above empty_label and stretch.
            self.list_layout.insertWidget(len(self._rows), row)
            self._rows.append(row)
            added += 1
        self.empty_label.setVisible(not self._rows)
        return added

    def _set_all_checked(self, checked: bool) -> None:
        for row in self._rows:
            row.set_checked(checked)
        self._refresh_controls()

    def _set_busy(self, busy: bool, status: str = "") -> None:
        self._busy = busy
        self.status_label.setText(status)
        self.scan_folder_button.setEnabled(not busy)
        self.select_all_button.setEnabled(not busy and bool(self._rows))
        self.select_none_button.setEnabled(not busy and bool(self._rows))
        self.cancel_button.setEnabled(not busy)
        for row in self._rows:
            row.setEnabled(not busy)
        self._refresh_controls()

    def _refresh_controls(self) -> None:
        total = len(self._rows)
        selected = self.selected_items()
        count = len(selected)
        total_bytes = sum(item.size_bytes for item in selected)

        if total == 0:
            self.summary_label.setText("")
            self.import_button.setText("Import selected")
        elif count == 0:
            self.summary_label.setText(f"0 of {total} selected")
            self.import_button.setText("Import selected")
        else:
            size_text = f" · {format_size_bytes(total_bytes)} reused" if total_bytes else ""
            self.summary_label.setText(f"{count} of {total} selected{size_text}")
            if count == total and total > 1:
                self.import_button.setText(f"Import all ({count})")
            else:
                noun = "model" if count == 1 else "models"
                self.import_button.setText(f"Import {count} {noun}")

        self.select_all_button.setEnabled(not self._busy and total > 0 and count < total)
        self.select_none_button.setEnabled(not self._busy and count > 0)
        self.import_button.setEnabled(not self._busy and count > 0)
        self.cancel_button.setText("Close" if total == 0 else "Skip for now")

    def _start_scan(self, fn, message: str) -> None:
        self._token = token = next(_tokens)
        self._set_busy(True, message)
        if not self._run_async:
            try:
                items, error = fn(), ""
            except Exception as exc:
                items, error = [], str(exc)
            self._on_scan_finished(token, items, error)
            return

        finished = _import_relay().scan_finished

        def run():
            try:
                items, error = fn(), ""
            except Exception as exc:
                logger.warning("Model discovery scan failed", exc_info=True)
                items, error = [], str(exc)
            finished.emit(token, items, error)

        threading.Thread(target=run, name="local-model-import-scan", daemon=True).start()

    def _on_scan_finished(self, token: int, items, error: str) -> None:
        if token != self._token:
            return
        added = self._add_items(items or ())
        if error:
            status = f"Scan error: {error}"
        elif added:
            noun = "model" if added == 1 else "models"
            status = f"Found {added} reusable {noun}."
        elif self._rows:
            status = "No additional reusable models found in that folder."
        else:
            status = ""
        self._set_busy(False, status)

    def _on_scan_folder_clicked(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder to scan for speech models")
        if folder:
            self._start_scan(
                lambda f=folder: discover_models_in_folder(f),
                f"Scanning {folder}…",
            )

    def _on_import_clicked(self) -> None:
        chosen = self.selected_items()
        if not chosen or self._busy:
            return
        self._token = token = next(_tokens)
        noun = "model" if len(chosen) == 1 else "models"
        self._set_busy(True, f"Verifying and importing {len(chosen)} {noun}…")
        if not self._run_async:
            result = import_discovered_models(chosen)
            self._on_import_finished(token, result)
            return

        finished = _import_relay().import_finished

        def run():
            result = import_discovered_models(chosen)
            finished.emit(token, result)

        threading.Thread(target=run, name="local-model-import-apply", daemon=True).start()

    def _on_import_finished(self, token: int, result: ImportResult) -> None:
        if token != self._token:
            return
        self.import_result = result
        if result.imported and not result.failed:
            self.accept()
            return
        if result.failed and not result.imported:
            first_item, first_err = result.failed[0]
            self._set_busy(False, f"Couldn't import {first_item.label}: {first_err}")
            return
        # Partial success: still accept so the caller refreshes imported rows.
        self.accept()

    def done(self, result: int) -> None:
        self._token = next(_tokens)
        super().done(result)

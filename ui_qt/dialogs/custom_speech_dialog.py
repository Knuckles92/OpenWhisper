"""Add a model file or folder of your own to one of the local speech runtimes."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from services.local_asr import custom
from services.local_asr.catalog import BACKENDS
from ui_qt.widgets import Button, PrimaryButton
from ui_qt.widgets.no_wheel import NoWheelComboBox

logger = logging.getLogger(__name__)


class CustomSpeechModelDialog(QDialog):
    """Choose the runtime format, model file or folder, name, and options.

    Adding only remembers where the model is: nothing is copied, downloaded
    or loaded, and a host never offers it to paired computers.
    """

    def __init__(self, backend: Optional[str] = None, parent=None):
        super().__init__(parent)
        self._explicit_backend = backend
        self.added_key = ""
        self._options = custom.runtime_format_options()
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        self.intro_label = QLabel("")
        self.intro_label.setWordWrap(True)
        layout.addWidget(self.intro_label)

        self.format_label = QLabel("Runtime format")
        self.format_combo = NoWheelComboBox()
        for option in self._options:
            self.format_combo.addItem(option.label, option)
        self.format_hint_label = QLabel("")
        self.format_hint_label.setObjectName("infoLabel")
        self.format_hint_label.setWordWrap(True)
        layout.addWidget(self.format_label)
        layout.addWidget(self.format_combo)
        layout.addWidget(self.format_hint_label)

        self.mode_checkbox = QCheckBox("")
        self.mode_hint_label = QLabel("")
        self.mode_hint_label.setObjectName("infoLabel")
        self.mode_hint_label.setWordWrap(True)
        layout.addWidget(self.mode_checkbox)
        layout.addWidget(self.mode_hint_label)

        self.path_title_label = QLabel("Model file")
        layout.addWidget(self.path_title_label)
        path_row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.textChanged.connect(self._changed)
        path_row.addWidget(self.path_edit, stretch=1)
        self.browse_button = Button("Choose…")
        self.browse_button.clicked.connect(self._browse)
        path_row.addWidget(self.browse_button)
        layout.addLayout(path_row)

        layout.addWidget(QLabel("Name"))
        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText("How it reads in model pickers")
        self.label_edit.textEdited.connect(self._label_edited)
        layout.addWidget(self.label_edit)
        self._label_touched = False

        self.arch_label = QLabel("Model size")
        self.arch_combo = NoWheelComboBox()
        self.arch_combo.addItem("Choose…", "")
        self.arch_combo.addItem("Small", "small")
        self.arch_combo.addItem("Medium", "medium")
        self.arch_combo.currentIndexChanged.connect(self._changed)
        layout.addWidget(self.arch_label)
        layout.addWidget(self.arch_combo)

        from services.model_catalog import CUSTOM_MODEL_NOTICE
        notice = QLabel(CUSTOM_MODEL_NOTICE)
        notice.setObjectName("infoLabel")
        notice.setWordWrap(True)
        layout.addWidget(notice)

        self.status_label = QLabel("")
        self.status_label.setObjectName("customSpeechError")
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = Button("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.add_button = PrimaryButton("Add model")
        self.add_button.setEnabled(False)
        self.add_button.clicked.connect(self._add)
        buttons.addWidget(self.add_button)
        layout.addLayout(buttons)

        self._initialize_selection(backend)
        self.format_combo.currentIndexChanged.connect(self._on_format_changed)
        self.mode_checkbox.toggled.connect(self._on_mode_toggled)

    def _initialize_selection(self, backend: Optional[str]) -> None:
        if backend:
            option, checked = custom.runtime_format_for_backend(backend)
            if option is not None:
                for index in range(self.format_combo.count()):
                    if self.format_combo.itemData(index).id == option.id:
                        self.format_combo.setCurrentIndex(index)
                        break
                self._apply_option(option, checked=checked)
                return
            # Fallback when invoked directly for a backend without a runtime here (e.g. tests).
            self.backend = backend
            self._format = custom.model_format(backend)
            self.format_label.hide()
            self.format_combo.hide()
            self.format_hint_label.hide()
            self.mode_checkbox.hide()
            self.mode_hint_label.hide()
            self._sync_backend_copy()
            return

        if self._options:
            self._apply_option(self._options[0], checked=self._options[0].toggle_default)
        else:
            self.backend = "parakeet"
            self._format = custom.model_format(self.backend)
            self._sync_backend_copy()

    def _current_option(self) -> Optional[custom.RuntimeFormatOption]:
        if self.format_combo.isHidden() or self.format_combo.currentIndex() < 0:
            return None
        return self.format_combo.currentData()

    def _apply_option(self, option: custom.RuntimeFormatOption, *, checked: bool) -> None:
        blocker = self.mode_checkbox.blockSignals(True)
        has_toggle = bool(option.toggle_label)
        self.mode_checkbox.setVisible(has_toggle)
        self.mode_hint_label.setVisible(has_toggle and bool(option.toggle_hint))
        if has_toggle:
            self.mode_checkbox.setText(option.toggle_label)
            self.mode_checkbox.setChecked(checked)
            self.mode_hint_label.setText(option.toggle_hint)
        self.mode_checkbox.blockSignals(blocker)
        self.format_hint_label.setText(option.hint)
        self._resolve_backend_from_option(option)
        self._sync_backend_copy()

    def _resolve_backend_from_option(self, option: custom.RuntimeFormatOption) -> None:
        if option.toggle_label and option.alt_backend:
            use_default = (
                self.mode_checkbox.isChecked()
                if option.toggle_selects_default
                else not self.mode_checkbox.isChecked()
            )
            self.backend = option.default_backend if use_default else option.alt_backend
        else:
            self.backend = option.default_backend
        self._format = custom.model_format(self.backend)

    def _sync_backend_copy(self) -> None:
        engine = BACKENDS[self.backend]
        option = self._current_option()
        if self._explicit_backend:
            self.setWindowTitle(f"Add a {engine} model")
        else:
            self.setWindowTitle("Add a custom speech model")
        what = "file" if self._format.gguf else "folder"
        runtime_name = option.label if option is not None else engine
        self.intro_label.setText(
            f"{runtime_name} loads {self._format.describe()}. OpenWhisper uses the model "
            "from where it is, without copying it. Paired computers never see it."
        )
        self.path_title_label.setText(f"Model {what}")
        self.path_edit.setPlaceholderText(
            "…\\model.gguf" if self._format.gguf else "Folder holding the model's files"
        )
        is_moonshine = self.backend == "moonshine"
        self.arch_label.setVisible(is_moonshine)
        self.arch_combo.setVisible(is_moonshine)
        self._changed()

    def _on_format_changed(self, _index: int) -> None:
        option = self._current_option()
        if option is not None:
            self._apply_option(option, checked=option.toggle_default)

    def _on_mode_toggled(self, _checked: bool) -> None:
        option = self._current_option()
        if option is not None:
            self._resolve_backend_from_option(option)
            self._sync_backend_copy()

    def _arch(self) -> str:
        return self.arch_combo.currentData() if self.backend == "moonshine" else ""

    def _vad_gate(self) -> Optional[bool]:
        option = self._current_option()
        if option is not None and option.id == "transcribe_gguf" and option.toggle_label:
            return self.mode_checkbox.isChecked()
        return None

    def _browse(self) -> None:
        engine = BACKENDS[self.backend]
        if self._format.gguf:
            path, _filter = QFileDialog.getOpenFileName(
                self, f"Choose a {engine} model", "", "GGUF models (*.gguf);;All files (*)")
        else:
            path = QFileDialog.getExistingDirectory(self, f"Choose a {engine} model folder")
        if path:
            self.path_edit.setText(str(Path(path)))

    def _label_edited(self, _text: str) -> None:
        self._label_touched = True

    def _changed(self, *_args) -> None:
        path = self.path_edit.text().strip()
        if path and not self._label_touched:
            self.label_edit.setText(Path(path).stem)
        self.status_label.hide()
        needs_arch = self.backend == "moonshine"
        self.add_button.setEnabled(bool(path) and (not needs_arch or bool(self._arch())))

    def _add(self) -> None:
        try:
            self.added_key = custom.register(
                self.backend,
                self.path_edit.text().strip(),
                self.label_edit.text(),
                self._arch(),
                vad_gate=self._vad_gate(),
            )
        except (ValueError, OSError) as exc:
            self.status_label.setText(str(exc))
            self.status_label.show()
            return
        self.accept()


def add_custom_speech_model(parent, backend: Optional[str] = None) -> str:
    """Ask for a custom speech model and register it; its key, or "" if none was added."""
    dialog = CustomSpeechModelDialog(backend, parent)
    try:
        return dialog.added_key if dialog.exec() == QDialog.DialogCode.Accepted else ""
    finally:
        dialog.deleteLater()

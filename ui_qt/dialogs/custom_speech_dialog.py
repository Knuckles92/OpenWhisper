"""Add a model file or folder of your own to one of the local speech engines."""
from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtWidgets import (
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
    """Choose the file or folder, name it, and (for Moonshine) give its size.

    Adding only remembers where the model is: nothing is copied, downloaded
    or loaded, and a host never offers it to paired computers.
    """

    def __init__(self, backend: str, parent=None):
        super().__init__(parent)
        self.backend = backend
        self.added_key = ""
        self._format = custom.model_format(backend)
        engine = BACKENDS[backend]
        self.setWindowTitle(f"Add a {engine} model")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        what = "file" if self._format.gguf else "folder"
        intro = QLabel(
            f"{engine} loads {self._format.describe()}. Choose a model built for {engine}; "
            "OpenWhisper uses it from where it is, without copying it. "
            "Paired computers never see it."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addWidget(QLabel(f"Model {what}"))
        path_row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText(
            "…\\model.gguf" if self._format.gguf else "Folder holding the model's files")
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

        self.arch_combo = None
        if backend == "moonshine":
            layout.addWidget(QLabel("Model size"))
            self.arch_combo = NoWheelComboBox()
            self.arch_combo.addItem("Choose…", "")
            self.arch_combo.addItem("Small", "small")
            self.arch_combo.addItem("Medium", "medium")
            self.arch_combo.currentIndexChanged.connect(self._changed)
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

    def _arch(self) -> str:
        return self.arch_combo.currentData() if self.arch_combo is not None else ""

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
        self.add_button.setEnabled(bool(path) and (self.arch_combo is None or bool(self._arch())))

    def _add(self) -> None:
        try:
            self.added_key = custom.register(
                self.backend, self.path_edit.text().strip(), self.label_edit.text(), self._arch())
        except (ValueError, OSError) as exc:
            self.status_label.setText(str(exc))
            self.status_label.show()
            return
        self.accept()


def add_custom_speech_model(parent, backend: str) -> str:
    """Ask for a model for ``backend`` and register it; its key, or "" if none was added."""
    dialog = CustomSpeechModelDialog(backend, parent)
    try:
        return dialog.added_key if dialog.exec() == QDialog.DialogCode.Accepted else ""
    finally:
        dialog.deleteLater()

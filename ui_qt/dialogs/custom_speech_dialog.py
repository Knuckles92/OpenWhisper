"""Add a model file or folder of your own to one of the local speech engines.

Supports both a pre-selected engine (e.g. ``"parakeet"``) and ``"auto"`` mode
for GGUF files, where GGUF metadata inspection automatically identifies the
compatible runtime/engine, flags non-speech LLMs, detects exact matches to
built-in models so they can be linked without duplicating files, and lets the
user configure capabilities (streaming, meetings, multilingual) independently
of the engine's default model.
"""
from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from services.local_asr import cache, custom
from services.local_asr.catalog import BACKENDS, BUILTIN_MODELS, DEFAULT_MODELS
from ui_qt.widgets import Button, PrimaryButton
from ui_qt.widgets.no_wheel import NoWheelComboBox

logger = logging.getLogger(__name__)

_GGUF_CAPABILITY_BACKENDS = frozenset({"parakeet", "nemotron", "parakeet_unified"})


class CustomSpeechModelDialog(QDialog):
    """Choose the file or folder, inspect compatibility, name it, and add/link it.

    Adding only remembers where the model is: nothing is copied, downloaded
    or loaded, and a host never offers custom models to paired computers.
    """

    def __init__(self, backend: str, parent=None):
        super().__init__(parent)
        self._auto_mode = backend == "auto"
        self._gguf_backends = custom.addable_gguf_backends()
        if self._auto_mode:
            self.backend = self._gguf_backends[0] if self._gguf_backends else "parakeet_unified"
            self._format = custom.ModelFormat(gguf=True)
        else:
            self.backend = backend
            self._format = custom.model_format(backend)

        self.added_key = ""
        self.linked_builtin_key = ""
        self._inspection: custom.SourceInspection | None = None
        self._engine_touched = not self._auto_mode
        self._caps_touched = False

        engine = BACKENDS[self.backend]
        self.setWindowTitle(
            "Add a GGUF speech model" if self._auto_mode else f"Add a {engine} model"
        )
        self.setMinimumWidth(540)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        what = "file" if self._format.gguf else "folder"
        if self._auto_mode:
            intro_text = (
                "Choose a speech recognition .gguf file on this computer. OpenWhisper "
                "reads its GGUF header to match the right engine and uses it in place "
                "without copying it."
            )
        else:
            intro_text = (
                f"{engine} loads {self._format.describe()}. Choose a model built for {engine}; "
                "OpenWhisper uses it from where it is, without copying it. "
                "Paired computers never see it."
            )
        intro = QLabel(intro_text)
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

        self.detected_label = QLabel("")
        self.detected_label.setObjectName("infoLabel")
        self.detected_label.setWordWrap(True)
        self.detected_label.hide()
        layout.addWidget(self.detected_label)

        self.engine_combo = None
        if self._auto_mode:
            layout.addWidget(QLabel("Speech engine"))
            self.engine_combo = NoWheelComboBox()
            for backend_id in self._gguf_backends:
                self.engine_combo.addItem(BACKENDS[backend_id], backend_id)
            self.engine_combo.currentIndexChanged.connect(self._on_engine_picked)
            layout.addWidget(self.engine_combo)

        self.link_builtin_checkbox = QCheckBox("")
        self.link_builtin_checkbox.setChecked(True)
        self.link_builtin_checkbox.toggled.connect(self._on_link_builtin_toggled)
        self.link_builtin_checkbox.hide()
        layout.addWidget(self.link_builtin_checkbox)

        self.name_heading = QLabel("Name")
        layout.addWidget(self.name_heading)
        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText("How it reads in model pickers")
        self.label_edit.textEdited.connect(self._label_edited)
        layout.addWidget(self.label_edit)
        self._label_touched = False

        self.arch_combo = None
        if not self._auto_mode and backend == "moonshine":
            layout.addWidget(QLabel("Model size"))
            self.arch_combo = NoWheelComboBox()
            self.arch_combo.addItem("Choose…", "")
            self.arch_combo.addItem("Small", "small")
            self.arch_combo.addItem("Medium", "medium")
            self.arch_combo.currentIndexChanged.connect(self._changed)
            layout.addWidget(self.arch_combo)

        # Optional per-model capability overrides for multi-type GGUF engines.
        self.capabilities_heading = QLabel("Capabilities")
        self.capabilities_row = QHBoxLayout()
        self.capabilities_row.setSpacing(14)
        self.streaming_checkbox = QCheckBox("Native live streaming")
        self.meeting_checkbox = QCheckBox("Allow in Meeting Mode")
        self.multilingual_checkbox = QCheckBox("Multilingual")
        for box in (self.streaming_checkbox, self.meeting_checkbox, self.multilingual_checkbox):
            box.toggled.connect(self._on_cap_toggled)
            self.capabilities_row.addWidget(box)
        self.capabilities_row.addStretch()
        layout.addWidget(self.capabilities_heading)
        layout.addLayout(self.capabilities_row)
        self._sync_capability_visibility()

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

    def _active_backend(self) -> str:
        if self.engine_combo is not None:
            data = self.engine_combo.currentData()
            if isinstance(data, str) and data:
                return data
        return self.backend

    def _arch(self) -> str:
        return self.arch_combo.currentData() if self.arch_combo is not None else ""

    def _linking_builtin(self) -> bool:
        return (
            self._inspection is not None
            and bool(self._inspection.builtin_match)
            and not self._inspection.builtin_already_cached
            and self.link_builtin_checkbox.isVisible()
            and self.link_builtin_checkbox.isChecked()
        )

    def _sync_capability_visibility(self) -> None:
        backend = self._active_backend()
        show_caps = (
            self._format.gguf
            and backend in _GGUF_CAPABILITY_BACKENDS
            and not self._linking_builtin()
        )
        self.capabilities_heading.setVisible(show_caps)
        self.streaming_checkbox.setVisible(show_caps)
        self.meeting_checkbox.setVisible(show_caps)
        self.multilingual_checkbox.setVisible(show_caps and backend == "parakeet_unified")
        if show_caps and not self._caps_touched:
            base = BUILTIN_MODELS[DEFAULT_MODELS[backend]]
            self._set_caps(
                streaming=base.streaming,
                meeting=base.meeting,
                multilingual=(base.languages != "English"),
            )

    def _set_caps(self, *, streaming: bool, meeting: bool, multilingual: bool) -> None:
        for box, value in (
            (self.streaming_checkbox, streaming),
            (self.meeting_checkbox, meeting),
            (self.multilingual_checkbox, multilingual),
        ):
            blocked = box.blockSignals(True)
            box.setChecked(bool(value))
            box.blockSignals(blocked)

    def _on_cap_toggled(self, _checked: bool) -> None:
        self._caps_touched = True

    def _on_engine_picked(self, _index: int) -> None:
        self._engine_touched = True
        self.backend = self._active_backend()
        self._caps_touched = False
        self._sync_capability_visibility()
        self._inspect_current_path()

    def _on_link_builtin_toggled(self, _checked: bool) -> None:
        linking = self._linking_builtin()
        self.name_heading.setVisible(not linking)
        self.label_edit.setVisible(not linking)
        if self.engine_combo is not None:
            self.engine_combo.setEnabled(not linking)
        self._sync_capability_visibility()
        self.add_button.setText("Link built-in model" if linking else "Add model")

    def _browse(self) -> None:
        engine = "GGUF speech" if self._auto_mode else BACKENDS[self._active_backend()]
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
        self._inspect_current_path()
        self.add_button.setEnabled(
            bool(path)
            and (self.arch_combo is None or bool(self._arch()))
        )

    def _inspect_current_path(self) -> None:
        path_text = self.path_edit.text().strip()
        self._inspection = None
        self.detected_label.hide()
        self.link_builtin_checkbox.hide()
        self.name_heading.setVisible(True)
        self.label_edit.setVisible(True)
        if self.engine_combo is not None:
            self.engine_combo.setEnabled(True)
        self.add_button.setText("Add model")

        if not path_text or not self._format.gguf:
            self._sync_capability_visibility()
            return
        candidate = Path(path_text).expanduser()
        if not candidate.is_file() or candidate.suffix.lower() != ".gguf":
            self._sync_capability_visibility()
            return

        target_backend = "" if (self._auto_mode and not self._engine_touched) else self._active_backend()
        inspection = custom.inspect_source(candidate, target_backend, verify_builtin_hash=False)
        self._inspection = inspection

        if self._auto_mode and not self._engine_touched and inspection.suggested_backend:
            idx = self.engine_combo.findData(inspection.suggested_backend)
            if idx >= 0:
                blocked = self.engine_combo.blockSignals(True)
                self.engine_combo.setCurrentIndex(idx)
                self.engine_combo.blockSignals(blocked)
                self.backend = inspection.suggested_backend

        if not self._label_touched and inspection.suggested_label:
            self.label_edit.setText(inspection.suggested_label)

        notes: list[str] = []
        if inspection.gguf_info is not None:
            summary = inspection.gguf_info.describe_detected()
            if summary:
                notes.append(f"Detected: {summary}")
        if inspection.warning:
            notes.append(inspection.warning)
        if notes:
            self.detected_label.setText("\n".join(notes))
            self.detected_label.show()

        if inspection.builtin_match and not inspection.builtin_already_cached:
            builtin_label = BUILTIN_MODELS[inspection.builtin_match].label
            self.link_builtin_checkbox.setText(
                f"Link as built-in {builtin_label} (reuses this file without duplicating)"
            )
            self.link_builtin_checkbox.setChecked(True)
            self.link_builtin_checkbox.show()
            self._on_link_builtin_toggled(True)
        else:
            self._sync_capability_visibility()
            if not self._caps_touched and inspection.suggested_streaming is not None:
                self._set_caps(
                    streaming=bool(inspection.suggested_streaming),
                    meeting=bool(inspection.suggested_meeting),
                    multilingual=bool(inspection.suggested_multilingual),
                )

    def _add(self) -> None:
        path_text = self.path_edit.text().strip()
        if self._linking_builtin():
            key = self._inspection.builtin_match
            try:
                cache.link_external(key, path_text, verify_hash=True)
            except (ValueError, OSError) as exc:
                self.status_label.setText(str(exc))
                self.status_label.show()
                return
            self.linked_builtin_key = key
            self.added_key = key
            self.accept()
            return

        backend = self._active_backend()
        kwargs: dict = {}
        if self._format.gguf and backend in _GGUF_CAPABILITY_BACKENDS:
            base = BUILTIN_MODELS[DEFAULT_MODELS[backend]]
            if self._caps_touched or self.streaming_checkbox.isChecked() != base.streaming:
                kwargs["streaming"] = self.streaming_checkbox.isChecked()
            if self._caps_touched or self.meeting_checkbox.isChecked() != base.meeting:
                kwargs["meeting"] = self.meeting_checkbox.isChecked()
            if backend == "parakeet_unified" and (
                self._caps_touched or self.multilingual_checkbox.isChecked()
            ):
                kwargs["multilingual"] = self.multilingual_checkbox.isChecked()

        try:
            self.added_key = custom.register(
                backend, path_text, self.label_edit.text(), self._arch(), **kwargs
            )
            self.backend = backend
        except (ValueError, OSError) as exc:
            self.status_label.setText(str(exc))
            self.status_label.show()
            return
        self.accept()


def add_custom_speech_model(parent, backend: str) -> str:
    """Ask for a model for ``backend`` (or ``"auto"``) and register/link it."""
    dialog = CustomSpeechModelDialog(backend, parent)
    try:
        return dialog.added_key if dialog.exec() == QDialog.DialogCode.Accepted else ""
    finally:
        dialog.deleteLater()

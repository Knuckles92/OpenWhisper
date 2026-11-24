"""Add a model file or folder of your own to one of the local speech runtimes.

The person picks a runtime format (or the dialog opens on one engine's
format). For a ``.gguf`` file the dialog reads its header: it switches the
format to the engine the file was built for until the person picks one
themselves, flags non-speech LLMs, offers to link an exact copy of a
built-in model in place, and suggests capabilities (streaming, meetings,
multilingual) that can be changed independently of the engine's default.
"""
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

from services.local_asr import cache, custom
from services.local_asr.catalog import BACKENDS, BUILTIN_MODELS, DEFAULT_MODELS
from ui_qt.widgets import Button, PrimaryButton
from ui_qt.widgets.no_wheel import NoWheelComboBox

logger = logging.getLogger(__name__)

_GGUF_CAPABILITY_BACKENDS = frozenset({"parakeet", "nemotron", "parakeet_unified"})


class CustomSpeechModelDialog(QDialog):
    """Choose the runtime format, model file or folder, name, and options.

    Adding only remembers where the model is: nothing is copied, downloaded
    or loaded, and a host never offers it to paired computers.
    """

    def __init__(self, backend: Optional[str] = None, parent=None):
        super().__init__(parent)
        self._explicit_backend = backend
        self.added_key = ""
        self.linked_builtin_key = ""
        self._inspection: custom.SourceInspection | None = None
        #: The person chose the format or toggle, so GGUF detection stops steering it.
        self._format_touched = bool(backend)
        self._caps_touched = False
        self._options = custom.runtime_format_options()
        self.setMinimumWidth(540)

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

        self.detected_label = QLabel("")
        self.detected_label.setObjectName("infoLabel")
        self.detected_label.setWordWrap(True)
        self.detected_label.hide()
        layout.addWidget(self.detected_label)

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

        self.arch_label = QLabel("Model size")
        self.arch_combo = NoWheelComboBox()
        self.arch_combo.addItem("Choose…", "")
        self.arch_combo.addItem("Small", "small")
        self.arch_combo.addItem("Medium", "medium")
        self.arch_combo.currentIndexChanged.connect(self._refresh_add_button)
        layout.addWidget(self.arch_label)
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

    # ---- runtime format ----

    def _initialize_selection(self, backend: Optional[str]) -> None:
        if backend:
            if self._select_backend(backend):
                return
            # Fallback when invoked directly for a backend without a runtime here (e.g. tests).
            self.backend = backend
            self._format = custom.model_format(backend)
            for widget in (self.format_label, self.format_combo, self.format_hint_label,
                           self.mode_checkbox, self.mode_hint_label):
                widget.hide()
            self._sync_backend_copy()
            return

        if self._options:
            self._apply_option(self._options[0], checked=self._options[0].toggle_default)
        else:
            self.backend = "parakeet"
            self._format = custom.model_format(self.backend)
            self._sync_backend_copy()

    def _select_backend(self, backend: str) -> bool:
        """Point the format combo and toggle at ``backend``; False when no format offers it."""
        option, checked = custom.runtime_format_for_backend(backend)
        if option is None:
            return False
        blocker = self.format_combo.blockSignals(True)
        for index in range(self.format_combo.count()):
            if self.format_combo.itemData(index).id == option.id:
                self.format_combo.setCurrentIndex(index)
                break
        self.format_combo.blockSignals(blocker)
        self._apply_option(option, checked=checked)
        return True

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
        previous = getattr(self, "backend", "")
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
        if self.backend != previous:
            self._caps_touched = False

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
        self._sync_capability_visibility()
        self._refresh_add_button()

    def _on_format_changed(self, _index: int) -> None:
        option = self._current_option()
        if option is not None:
            self._format_touched = True
            self._apply_option(option, checked=option.toggle_default)
            self._inspect_current_path()

    def _on_mode_toggled(self, _checked: bool) -> None:
        option = self._current_option()
        if option is not None:
            self._format_touched = True
            self._resolve_backend_from_option(option)
            self._sync_backend_copy()
            self._inspect_current_path()

    def _arch(self) -> str:
        return self.arch_combo.currentData() if self.backend == "moonshine" else ""

    def _vad_gate(self) -> Optional[bool]:
        option = self._current_option()
        if option is not None and option.id == "transcribe_gguf" and option.toggle_label:
            return self.mode_checkbox.isChecked()
        return None

    # ---- capabilities and built-in linking ----

    def _linking_builtin(self) -> bool:
        return (
            self._inspection is not None
            and bool(self._inspection.builtin_match)
            and not self._inspection.builtin_already_cached
            and not self.link_builtin_checkbox.isHidden()
            and self.link_builtin_checkbox.isChecked()
        )

    def _sync_capability_visibility(self) -> None:
        show_caps = (
            self._format.gguf
            and self.backend in _GGUF_CAPABILITY_BACKENDS
            and not self._linking_builtin()
        )
        self.capabilities_heading.setVisible(show_caps)
        self.streaming_checkbox.setVisible(show_caps)
        self.meeting_checkbox.setVisible(show_caps)
        self.multilingual_checkbox.setVisible(show_caps and self.backend == "parakeet_unified")
        if show_caps and not self._caps_touched:
            base = BUILTIN_MODELS[DEFAULT_MODELS[self.backend]]
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

    def _on_link_builtin_toggled(self, _checked: bool) -> None:
        linking = self._linking_builtin()
        self.name_heading.setVisible(not linking)
        self.label_edit.setVisible(not linking)
        self.format_combo.setEnabled(not linking)
        self.mode_checkbox.setEnabled(not linking)
        self._sync_capability_visibility()
        self.add_button.setText("Link built-in model" if linking else "Add model")

    # ---- path ----

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

    def _refresh_add_button(self, *_args) -> None:
        path = self.path_edit.text().strip()
        needs_arch = self.backend == "moonshine"
        self.add_button.setEnabled(bool(path) and (not needs_arch or bool(self._arch())))

    def _changed(self, *_args) -> None:
        path = self.path_edit.text().strip()
        if path and not self._label_touched:
            self.label_edit.setText(Path(path).stem)
        self.status_label.hide()
        self._inspect_current_path()
        self._refresh_add_button()

    def _inspect_current_path(self) -> None:
        path_text = self.path_edit.text().strip()
        self._inspection = None
        self.detected_label.hide()
        self.link_builtin_checkbox.hide()
        self.name_heading.setVisible(True)
        self.label_edit.setVisible(True)
        self.format_combo.setEnabled(True)
        self.mode_checkbox.setEnabled(True)
        self.add_button.setText("Add model")

        if not path_text or not self._format.gguf:
            self._sync_capability_visibility()
            return
        candidate = Path(path_text).expanduser()
        if not candidate.is_file() or candidate.suffix.lower() != ".gguf":
            self._sync_capability_visibility()
            return

        # Against the format on screen, so the suggestion is the header's (or a
        # built-in match's) engine when it names one, and the current one otherwise.
        inspection = custom.inspect_source(candidate, self.backend, verify_builtin_hash=False)
        if (not self._format_touched and inspection.suggested_backend
                and inspection.suggested_backend != self.backend
                and self._select_backend(inspection.suggested_backend)):
            # Capability suggestions depend on the engine, so read them for the new one.
            inspection = custom.inspect_source(candidate, self.backend, verify_builtin_hash=False)
        self._inspection = inspection

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

    # ---- add ----

    def _capability_overrides(self) -> dict:
        if not (self._format.gguf and self.backend in _GGUF_CAPABILITY_BACKENDS):
            return {}
        base = BUILTIN_MODELS[DEFAULT_MODELS[self.backend]]
        overrides: dict = {}
        if self._caps_touched or self.streaming_checkbox.isChecked() != base.streaming:
            overrides["streaming"] = self.streaming_checkbox.isChecked()
        if self._caps_touched or self.meeting_checkbox.isChecked() != base.meeting:
            overrides["meeting"] = self.meeting_checkbox.isChecked()
        if self.backend == "parakeet_unified" and (
            self._caps_touched or self.multilingual_checkbox.isChecked()
        ):
            overrides["multilingual"] = self.multilingual_checkbox.isChecked()
        return overrides

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

        try:
            self.added_key = custom.register(
                self.backend,
                path_text,
                self.label_edit.text(),
                self._arch(),
                vad_gate=self._vad_gate(),
                **self._capability_overrides(),
            )
        except (ValueError, OSError) as exc:
            self.status_label.setText(str(exc))
            self.status_label.show()
            return
        self.accept()


def add_custom_speech_model(parent, backend: Optional[str] = None) -> str:
    """Ask for a custom speech model and register or link it; its key, or "" if none."""
    dialog = CustomSpeechModelDialog(backend, parent)
    try:
        return dialog.added_key if dialog.exec() == QDialog.DialogCode.Accepted else ""
    finally:
        dialog.deleteLater()

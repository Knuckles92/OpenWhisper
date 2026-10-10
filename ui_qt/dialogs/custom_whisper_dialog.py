"""Review discovered local or Hugging Face Whisper models before adding them."""
from __future__ import annotations

import itertools
import threading
from typing import Optional

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QVBoxLayout,
)

from services.settings import is_hf_hub_offline_env_set
from services.whisper_sources import (
    custom_model_label,
    custom_models,
    discover_cached_models,
    discover_hub_models,
    discover_local_models,
    hub_source,
)
from ui_qt.widgets import Button, PrimaryButton


class _DiscoveryRelay(QObject):
    """Brings discovery results to the Qt thread for whichever dialogs exist then.

    It lives for the session, so a discovery thread never holds or emits on a
    dialog. Holding one let the thread drop the last reference and delete the
    dialog off the Qt thread, and emitting on one raced it closing; both crash
    the process. Qt drops a deleted dialog's connection.
    """

    #: Token, model names, error.
    found = pyqtSignal(int, object, str)


_relay: Optional[_DiscoveryRelay] = None
#: Discovery tokens, unique across dialogs because they share the relay.
_tokens = itertools.count(1)


def _discovery_relay() -> _DiscoveryRelay:
    global _relay
    if _relay is None:
        _relay = _DiscoveryRelay()
    return _relay


class CustomWhisperDialog(QDialog):
    """Discovery reads filenames; adding sources neither downloads nor activates."""

    def __init__(self, settings: dict, cached: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add custom Whisper models")
        self.setMinimumSize(560, 420)
        self.selected_models: list[str] = []
        self._registered = set(custom_models(settings))
        self._cached = dict(cached)
        self._generation = 0
        self._busy = False
        layout = QVBoxLayout(self)
        note = QLabel(
            "Choose a model folder, or find models inside a Hugging Face repository. "
            "Review the results and select the models to add. "
            "Choose an added model in Voice model or Voice & speakers to load it."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        local_row = QHBoxLayout()
        self.local_button = Button("Choose local folder…")
        self.local_button.clicked.connect(self._choose_local_folder)
        local_row.addWidget(self.local_button)
        self.cached_button = Button("Find cached models")
        self.cached_button.clicked.connect(self._find_cached)
        local_row.addWidget(self.cached_button)
        local_row.addStretch()
        layout.addLayout(local_row)

        layout.addWidget(QLabel("Hugging Face repository"))
        self.repo_edit = QLineEdit()
        self.repo_edit.setPlaceholderText("owner/model")
        layout.addWidget(self.repo_edit)
        self.subfolder_edit = QLineEdit()
        self.subfolder_edit.setPlaceholderText("Optional subfolder, e.g. ct2_int8_float16")
        layout.addWidget(self.subfolder_edit)
        self.hub_button = Button("Find on Hugging Face")
        self.hub_button.clicked.connect(self._find_hub)
        self.hub_button.setEnabled(not is_hf_hub_offline_env_set())
        layout.addWidget(self.hub_button)
        lookup_note = QLabel(
            "Find reads the repository's file list. Model files download only when "
            "you choose a model to load or download. Models must include model.bin, "
            "config.json, and tokenizer.json."
        )
        from services.model_catalog import CUSTOM_MODEL_NOTICE
        lookup_note.setText(lookup_note.text() + "\n\n" + CUSTOM_MODEL_NOTICE)
        lookup_note.setWordWrap(True)
        layout.addWidget(lookup_note)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.results = QListWidget()
        self.results.itemChanged.connect(self._selection_changed)
        layout.addWidget(self.results, stretch=1)
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = Button("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.add_button = PrimaryButton("Add selected")
        self.add_button.setEnabled(False)
        self.add_button.clicked.connect(self._add_selected)
        buttons.addWidget(self.add_button)
        layout.addLayout(buttons)
        _discovery_relay().found.connect(self._show_found)
        QTimer.singleShot(0, self._find_cached)

    def _scan(self, operation, message: str) -> None:
        """Run ``operation`` off the Qt thread; it must not reach back into the dialog."""
        self._generation = generation = next(_tokens)
        self._busy = True
        self.results.clear()
        self.add_button.setEnabled(False)
        self.status_label.setText(message)
        for button in (self.local_button, self.cached_button, self.hub_button):
            button.setEnabled(False)
        found = _discovery_relay().found

        def run():
            try:
                models, error = operation(), ""
            except Exception as exc:
                models, error = [], str(exc)
            found.emit(generation, models, error)

        threading.Thread(target=run, name="custom-whisper-discovery", daemon=True).start()

    def _find_cached(self) -> None:
        if not self._busy:
            cached = self._cached
            self._scan(lambda: discover_cached_models(cached), "Checking cached model folders…")

    def _choose_local_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose a Whisper model or parent folder")
        if folder:
            self._scan(lambda: discover_local_models(folder), "Checking local model folders…")

    def _find_hub(self) -> None:
        try:
            source = hub_source(self.repo_edit.text(), self.subfolder_edit.text())
            if is_hf_hub_offline_env_set():
                raise ValueError("Hugging Face lookup is disabled by HF_HUB_OFFLINE.")
        except ValueError as exc:
            self.status_label.setText(str(exc))
            return
        self._scan(lambda: discover_hub_models(source.repo_id, source.subfolder),
                   "Finding model folders on Hugging Face…")

    def _show_found(self, generation: int, models: list, error: str) -> None:
        if generation != self._generation:
            return
        self._busy = False
        self.local_button.setEnabled(True)
        self.cached_button.setEnabled(True)
        self.hub_button.setEnabled(not is_hf_hub_offline_env_set())
        self.results.clear()
        from services.model_catalog import MODEL_REPOSITORIES
        known = self._registered | set(MODEL_REPOSITORIES.values())
        models = sorted(set(models) - known)
        for name in models:
            item = QListWidgetItem(custom_model_label(name))
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setToolTip(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.results.addItem(item)
        self.status_label.setText(error or (
            f"Found {len(models)} compatible models. Select the ones to add."
            if models else "No additional compatible models found. Existing models are already in the model list."
        ))
        self._selection_changed()

    def _checked_models(self) -> list[str]:
        return [self.results.item(index).data(Qt.ItemDataRole.UserRole)
                for index in range(self.results.count())
                if self.results.item(index).checkState() == Qt.CheckState.Checked]

    def _selection_changed(self, *_args) -> None:
        self.add_button.setEnabled(not self._busy and bool(self._checked_models()))

    def _add_selected(self) -> None:
        self.selected_models = self._checked_models()
        if self.selected_models:
            self.accept()

    def done(self, result: int) -> None:
        self._generation = next(_tokens)
        super().done(result)


def add_custom_models(parent, settings_manager, cached: dict) -> list[str]:
    """Persist only the sources the user chose, leaving active assignments intact."""
    from services.settings import SettingsKey

    dialog = CustomWhisperDialog(settings_manager.load_all_settings(), cached, parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return []
    def register(settings):
        settings[SettingsKey.CUSTOM_WHISPER_MODELS] = list(dict.fromkeys(
            [*custom_models(settings), *dialog.selected_models]
        ))
    try:
        settings_manager.mutate_settings(register)
    except Exception as exc:
        QMessageBox.warning(parent, "Couldn't add custom models", str(exc))
        return []
    return dialog.selected_models

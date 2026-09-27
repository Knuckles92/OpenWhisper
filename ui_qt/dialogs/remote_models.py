"""A paired host's speech model catalog, never the client's local storage."""
from __future__ import annotations

import threading

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QMessageBox, QVBoxLayout

from ui_qt.widgets import Button, ElidingComboBox, PrimaryButton, WrappedLabel


class RemoteModelsDialog(QDialog):
    _finished = pyqtSignal(str, object, str)

    def __init__(self, service, host_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Models on {host_name}")
        self.setMinimumWidth(560)
        self._service = service
        self._host_name = host_name
        self._busy = False
        self._catalog: dict = {}
        self._valid = False
        self._finished.connect(self._on_finished)
        self._poll = QTimer(self)
        self._poll.setInterval(3000)
        self._poll.timeout.connect(lambda: self._request("model_catalog"))

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        note = WrappedLabel(
            f"These models are stored on {host_name}, not this computer. "
            "Downloads use the host's network and disk space and continue if you close this window. "
            "Runtime installation and device settings must still be configured on the host."
        )
        note.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(note)
        self.engine_label = WrappedLabel("")
        self.engine_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.engine_label)
        self.model_combo = ElidingComboBox()
        self.model_combo.setObjectName("remoteManagedModelCombo")
        self.model_combo.currentIndexChanged.connect(self._update_actions)
        layout.addWidget(self.model_combo)
        self.detail = WrappedLabel("")
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.detail)
        self.status = WrappedLabel("")
        self.status.setObjectName("remoteModelStatus")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.download_button = PrimaryButton("Download on host")
        self.download_button.setObjectName("remoteModelDownloadButton")
        self.download_button.clicked.connect(self._download)
        self.select_button = Button("Use on host")
        self.select_button.setObjectName("remoteModelSelectButton")
        self.select_button.clicked.connect(self._select)
        self.refresh_button = Button("Refresh")
        self.refresh_button.clicked.connect(lambda: self._request("model_catalog"))
        close = Button("Close")
        close.clicked.connect(self.reject)
        for button in (self.download_button, self.select_button, self.refresh_button, close):
            actions.addWidget(button)
        layout.addLayout(actions)
        self._update_actions()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._request("model_catalog")

    def hideEvent(self, event) -> None:
        self._poll.stop()
        super().hideEvent(event)

    def _choice(self) -> dict:
        key = self.model_combo.currentData()
        return next((model for model in self._catalog.get("models", [])
                     if f"{model['family']}:{model['model']}" == key), {})

    def _update_actions(self, *_args) -> None:
        choice = self._choice()
        job = self._catalog.get("download") or {}
        engine = self._catalog.get("engine") or {}
        active = bool(choice and engine.get("available") and
                      (choice["family"], choice["model"]) == (engine.get("family"), engine.get("model")))
        enabled = self._valid and not self._busy and bool(choice)
        self.model_combo.setEnabled(not self._busy)
        self.refresh_button.setEnabled(not self._busy)
        self.download_button.setEnabled(bool(enabled and not choice.get("cached") and
                                             job.get("state") != "downloading"))
        self.select_button.setEnabled(bool(enabled and choice.get("cached") and
                                           choice.get("runtime_ready") and
                                           self._catalog.get("can_select") and not active))
        self.select_button.setText("Active on host" if active else "Use on host")
        if choice:
            parts = ["Downloaded" if choice.get("cached") else f"Download: {choice.get('download_size') or 'size unknown'}"]
            if not choice.get("runtime_ready"):
                parts.append("Install this engine's runtime on the host before using it")
            self.detail.setText(" · ".join(parts))
        else:
            self.detail.clear()

    def _request(self, op: str, **fields) -> None:
        if self._busy or not self.isVisible():
            return
        self._busy = True
        self._poll.stop()
        self._update_actions()
        if op != "model_catalog" or not self._valid:
            self.status.setText({"model_catalog": "Reading host models…", "download_model": "Starting download…",
                                 "select_model": "Switching the host's engine…"}[op])
        service = self._service

        def work():
            result, error = None, ""
            try:
                result = service.remote_model_request(op, **fields)
            except Exception as exc:
                error = str(exc) or type(exc).__name__
            try:
                self._finished.emit(op, result, error)
            except RuntimeError:
                pass  # The parent window was destroyed while a request finished.

        threading.Thread(target=work, name="remote-model-request", daemon=True).start()

    def _on_finished(self, op: str, result, error: str) -> None:
        self._busy = False
        if error:
            self._valid = False
            self.status.setText(f"{error} Refresh to check the host's current state.")
        elif op == "model_catalog":
            self._valid = True
            self._catalog = result
            key = self.model_combo.currentData()
            blocked = self.model_combo.blockSignals(True)
            self.model_combo.clear()
            for model in result.get("models", []):
                state = "downloaded" if model.get("cached") else "not downloaded"
                self.model_combo.addItem(f"{model['label']} — {state}", f"{model['family']}:{model['model']}")
            index = self.model_combo.findData(key)
            if index >= 0:
                self.model_combo.setCurrentIndex(index)
            self.model_combo.blockSignals(blocked)
            engine = result.get("engine") or {}
            self.engine_label.setText(f"Host engine: {engine.get('label') or 'None'}")
            job = result.get("download") or {}
            state = job.get("state")
            label = job.get("label") or "Model"
            if state == "downloading":
                done, total = job.get("done", 0), job.get("total", 0)
                progress = f" ({min(100, int(100 * done / total))}%)" if total > 0 else ""
                self.status.setText(f"Downloading {label} on the host{progress}…")
                if self.isVisible():
                    self._poll.start()
            elif state == "failed":
                self.status.setText(f"{label}: {job.get('error') or 'Download failed.'}")
            elif state == "complete":
                self.status.setText(f"{label} is downloaded. Choose ‘Use on host’ to activate it.")
            else:
                self.status.setText("Choose a model to download or use on the host.")
        else:
            # Downloads are acknowledged immediately. Read authoritative state;
            # do not assume that downloading also selected or loaded the model.
            self._request("model_catalog")
        self._update_actions()

    def _download(self) -> None:
        choice = self._choice()
        if not self.download_button.isEnabled() or not choice:
            return
        answer = QMessageBox.question(
            self, "Download on host?",
            f"Download {choice['label']} ({choice.get('download_size') or 'size unknown'}) "
            f"on {self._host_name}? This uses the host's network and disk space.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._request("download_model", family=choice["family"], model=choice["model"])

    def _select(self) -> None:
        choice = self._choice()
        if not self.select_button.isEnabled() or not choice:
            return
        answer = QMessageBox.question(
            self, "Change the host's engine?",
            f"Use {choice['label']} on {self._host_name}? This changes the engine for the host "
            "and every connected client.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._request("select_model", family=choice["family"], model=choice["model"])

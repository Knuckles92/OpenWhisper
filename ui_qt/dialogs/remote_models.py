"""Model and runtime management for a paired host, independent of local storage."""

from __future__ import annotations

import threading

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ui_qt.widgets import Button, ElidingComboBox, PrimaryButton, WrappedLabel

_DEVICE_LABELS = {"cpu": "CPU", "cuda": "NVIDIA GPU", "auto": "Auto"}


class RemoteModelsDialog(QDialog):
    _finished = pyqtSignal(str, object, str)

    def __init__(self, service, host_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Models on {host_name}")
        self.setObjectName("remoteModelsDialog")
        self.setMinimumSize(760, 570)
        self.resize(960, 700)
        self._service, self._host_name = service, host_name
        self._pairing = service.client_pairing() if service is not None else None
        self._busy = self._valid = False
        self._catalog = {}
        self._device_model = None
        self._finished.connect(self._on_finished)
        self._poll = QTimer(self)
        self._poll.setInterval(2000)
        self._poll.timeout.connect(lambda: self._request("model_catalog"))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(14)
        title = self._label(f"Models on {host_name}")
        title.setObjectName("downloadsTitle")
        layout.addWidget(title)
        layout.addWidget(
            self._label(
                "Manage the host's models and speech runtimes here. Downloads use its network and storage "
                "and continue when this window closes. Engine changes apply to every connected client."
            )
        )
        self.engine_label = self._label("")
        self.engine_label.setObjectName("remoteHostSummary")
        layout.addWidget(self.engine_label)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        browser = QWidget()
        browse_layout = QVBoxLayout(browser)
        browse_layout.setContentsMargins(0, 0, 8, 0)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search host models…")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Search host models")
        self.search.textChanged.connect(self._filter_models)
        browse_layout.addWidget(self.search)
        self.model_list = QListWidget()
        self.model_list.setObjectName("remoteModelList")
        self.model_list.setAccessibleName("Models available on the host")
        self.model_list.currentItemChanged.connect(self._update_actions)
        browse_layout.addWidget(self.model_list)
        self.empty_label = self._label("No matching models")
        self.empty_label.hide()
        browse_layout.addWidget(self.empty_label)
        splitter.addWidget(browser)

        inspector = QFrame()
        inspector.setObjectName("remoteModelInspector")
        inspector.setMinimumWidth(320)
        details = QVBoxLayout(inspector)
        details.setContentsMargins(18, 16, 18, 16)
        details.setSpacing(12)
        self.model_title = self._label("Choose a model")
        self.model_title.setObjectName("remoteModelTitle")
        details.addWidget(self.model_title)
        self.description = self._label("")
        details.addWidget(self.description)
        self.detail = self._label("")
        details.addWidget(self.detail)
        self.download_button = Button("Download model on host")
        self.download_button.setObjectName("remoteModelDownloadButton")
        self.download_button.clicked.connect(self._download)
        details.addWidget(self.download_button)
        device_label = QLabel("Run on host")
        self.device_combo = ElidingComboBox()
        self.device_combo.setAccessibleName("Device for selected host model")
        device_label.setBuddy(self.device_combo)
        details.addWidget(device_label)
        details.addWidget(self.device_combo)
        self.device_combo.currentIndexChanged.connect(self._update_actions)
        self.runtime_detail = self._label("")
        details.addWidget(self.runtime_detail)
        self.install_button = Button("Install runtime on host")
        self.install_button.setObjectName("remoteRuntimeInstallButton")
        self.install_button.clicked.connect(self._install)
        details.addWidget(self.install_button)
        details.addStretch()
        self.select_button = PrimaryButton("Use on host")
        self.select_button.setObjectName("remoteModelSelectButton")
        self.select_button.clicked.connect(self._select)
        inspector_scroll = QScrollArea()
        inspector_scroll.setWidgetResizable(True)
        inspector_scroll.setFrameShape(QFrame.Shape.NoFrame)
        inspector_scroll.setWidget(inspector)
        inspector_scroll.setMinimumWidth(340)
        inspector_column = QWidget()
        inspector_layout = QVBoxLayout(inspector_column)
        inspector_layout.setContentsMargins(0, 0, 0, 0)
        inspector_layout.addWidget(inspector_scroll, stretch=1)
        inspector_layout.addWidget(self.select_button)
        splitter.addWidget(inspector_column)
        splitter.setSizes([360, 520])
        layout.addWidget(splitter, stretch=1)

        self.status = self._label("Reading host models…")
        self.status.setObjectName("remoteModelStatus")
        layout.addWidget(self.status)
        self.download_progress = QProgressBar()
        self.download_progress.setAccessibleName("Host model download progress")
        layout.addWidget(self.download_progress)
        self.install_status = self._label("")
        self.install_status.hide()
        layout.addWidget(self.install_status)
        self.install_progress = QProgressBar()
        self.install_progress.setAccessibleName("Host runtime installation progress")
        layout.addWidget(self.install_progress)
        self.download_progress.hide()
        self.install_progress.hide()
        footer = QHBoxLayout()
        self.refresh_button = Button("Refresh host")
        self.refresh_button.clicked.connect(lambda: self._request("model_catalog"))
        footer.addWidget(self.refresh_button)
        footer.addStretch()
        close = Button("Close")
        close.clicked.connect(self.close)
        footer.addWidget(close)
        layout.addLayout(footer)
        self._update_actions()

    @staticmethod
    def _label(text):
        label = WrappedLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        return label

    def showEvent(self, event):
        super().showEvent(event)
        self._request("model_catalog")

    def hideEvent(self, event):
        self._poll.stop()
        super().hideEvent(event)

    def _choice(self):
        item = self.model_list.currentItem()
        key = item.data(Qt.ItemDataRole.UserRole) if item else None
        return next(
            (
                model
                for model in self._catalog.get("models", [])
                if f"{model['family']}:{model['model']}" == key
            ),
            {},
        )

    def select_model(self, key):
        for row in range(self.model_list.count()):
            item = self.model_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == key:
                self.model_list.setCurrentItem(item)
                break

    def _dependency(self):
        return next(
            (
                item
                for item in self._choice().get("dependencies", [])
                if item["device"] == self.device_combo.currentData()
            ),
            {},
        )

    def _filter_models(self, *_args):
        query = self.search.text().strip().casefold()
        first_visible = None
        for row in range(self.model_list.count()):
            item = self.model_list.item(row)
            item.setHidden(query not in item.text().casefold())
            if not item.isHidden() and first_visible is None:
                first_visible = item
        self.empty_label.setVisible(first_visible is None and self._valid)
        current = self.model_list.currentItem()
        if current is None or current.isHidden():
            if first_visible is not None:
                self.model_list.setCurrentItem(first_visible)
            else:
                self.model_list.setCurrentRow(-1)

    def _update_actions(self, *_args):
        choice = self._choice()
        key = (choice.get("family"), choice.get("model"))
        options = choice.get("dependencies", [])
        previous = (
            self.device_combo.currentData() if self._device_model == key else None
        )
        selected = previous or choice.get("selected_device")
        if selected not in ("cpu", "cuda"):
            selected = next(
                (
                    option["device"]
                    for option in reversed(options)
                    if option.get("ready")
                ),
                "cpu",
            )
        self._device_model = key
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for option in options:
            state = (
                "Ready"
                if option.get("ready")
                else "Setup needed"
                if option.get("installable")
                else "Unavailable"
            )
            self.device_combo.addItem(
                f"{_DEVICE_LABELS[option['device']]} · {state}", option["device"]
            )
        index = self.device_combo.findData(selected)
        if index >= 0:
            self.device_combo.setCurrentIndex(index)
        self.device_combo.blockSignals(False)
        self.device_combo.setVisible(bool(options))
        dependency = self._dependency()
        enabled = self._valid and not self._busy and bool(choice)
        self.model_list.setEnabled(not self._busy)
        self.device_combo.setEnabled(enabled)
        self.refresh_button.setEnabled(not self._busy)
        job = self._catalog.get("download") or {}
        install = self._catalog.get("installation") or {}
        self.download_button.setEnabled(
            bool(
                enabled
                and not choice.get("cached")
                and job.get("state") != "downloading"
            )
        )
        self.download_button.setText(
            "Model downloaded" if choice.get("cached") else "Download model on host"
        )
        self.download_button.setVisible(not choice.get("cached", False))
        self.install_button.setEnabled(
            bool(
                enabled
                and self._catalog.get("can_install_runtime")
                and dependency.get("installable")
                and install.get("state") != "installing"
            )
        )
        self.install_button.setVisible(bool(dependency and not dependency.get("ready")))
        engine = self._catalog.get("engine") or {}
        active = (
            bool(choice and engine.get("available"))
            and key == (engine.get("family"), engine.get("model"))
            and (not dependency or dependency["device"] == engine.get("device"))
        )
        ready = dependency.get("ready") if options else choice.get("runtime_ready")
        self.select_button.setEnabled(
            bool(
                enabled
                and choice.get("cached")
                and ready
                and self._catalog.get("can_select")
                and not active
            )
        )
        self.select_button.setText("Active on host" if active else "Use on host")
        self.model_title.setText(choice.get("label") or "Choose a model")
        description = ""
        if choice:
            try:
                from services.model_catalog import get_model_details

                profile = get_model_details(choice["model"])
                description = profile.description + "\n" + profile.language_support
            except KeyError:
                pass  # Older clients can still manage newly added host models.
        self.description.setText(description)
        self.detail.setText(
            (
                "Model downloaded on host"
                if choice.get("cached")
                else f"Model download: {choice.get('download_size') or 'size unknown'}"
            )
            if choice
            else ""
        )
        if dependency:
            from services.hf_access import format_size_bytes

            text = (
                f"{dependency['label']} · Ready"
                if dependency.get("ready")
                else dependency.get("reason")
            )
            if not text:
                size = (
                    format_size_bytes(dependency["download_bytes"])
                    if dependency.get("download_bytes")
                    else "size unknown"
                )
                text = f"{dependency['label']} runtime is missing. Install it on the host ({size}) to use this device."
            self.runtime_detail.setText(text)
        else:
            self.runtime_detail.setText(
                "Update OpenWhisper on the host to manage its CPU/GPU runtimes here. "
                "Missing runtimes can be installed in Settings → Downloads on the host."
                if choice
                else ""
            )

    def _request(self, op, **fields):
        if self._busy or not self.isVisible():
            return
        self._busy = True
        self._poll.stop()
        self._update_actions()
        if op != "model_catalog" or not self._valid:
            self.status.setText(
                {
                    "model_catalog": "Reading host models…",
                    "download_model": "Starting model download…",
                    "install_runtime": "Starting runtime installation…",
                    "select_model": "Loading model on host…",
                }[op]
            )

        def work():
            result, error = None, ""
            try:
                result = self._service.remote_model_request(
                    op, expected_pairing=self._pairing, **fields
                )
            except Exception as exc:
                error = str(exc) or type(exc).__name__
            try:
                self._finished.emit(op, result, error)
            except RuntimeError:
                pass

        threading.Thread(target=work, name="remote-model-request", daemon=True).start()

    @staticmethod
    def _progress(bar, job, busy_state):
        busy = job.get("state") == busy_state
        bar.setVisible(busy)
        done, total = job.get("done", 0), job.get("total", 0)
        bar.setRange(0, 100 if total > 0 else 0)
        if total > 0:
            bar.setValue(min(100, int(100 * done / total)))
        return busy

    def _on_finished(self, op, result, error):
        self._busy = False
        if error:
            self._valid = False
            self.status.setText(f"{error} Refresh to check the host's current state.")
        elif op == "model_catalog":
            self._valid = True
            choice = self._choice()
            key = f"{choice.get('family')}:{choice.get('model')}"
            self._catalog = result
            engine = result.get("engine") or {}
            self.model_list.blockSignals(True)
            scroll = self.model_list.verticalScrollBar().value()
            self.model_list.clear()
            selected = None
            for model in result.get("models", []):
                active = engine.get("available") and (
                    model["family"],
                    model["model"],
                ) == (engine.get("family"), engine.get("model"))
                state = (
                    "Active"
                    if active
                    else "Downloaded"
                    if model.get("cached")
                    else model.get("download_size") or "Not downloaded"
                )
                item = QListWidgetItem(f"{model['label']}\n{state}")
                item.setData(
                    Qt.ItemDataRole.UserRole, f"{model['family']}:{model['model']}"
                )
                self.model_list.addItem(item)
                if item.data(Qt.ItemDataRole.UserRole) == key:
                    selected = item
                elif not choice and active:
                    selected = item
            if selected:
                self.model_list.setCurrentItem(selected)
            elif self.model_list.count():
                self.model_list.setCurrentRow(0)
            self.model_list.verticalScrollBar().setValue(scroll)
            self.model_list.blockSignals(False)
            self._filter_models()
            runtime = result.get("runtime") or {}
            gpu = runtime.get("gpu") or {}
            hardware = (
                f" · {gpu['name']} ({gpu.get('total_mib', 0) / 1024:g} GB VRAM)"
                if gpu
                else ""
            )
            device = _DEVICE_LABELS.get(
                engine.get("device"), engine.get("device") or "device not reported"
            )
            self.engine_label.setText(
                f"{'Running' if engine.get('available') else 'Not ready'}: "
                f"{engine.get('label') or 'No engine'} · {device}{hardware}"
            )
            job = result.get("download") or {}
            installation = result.get("installation") or {}
            self.install_status.setVisible(bool(installation))
            downloading = self._progress(self.download_progress, job, "downloading")
            installing = self._progress(
                self.install_progress, installation, "installing"
            )
            if downloading:
                percent = (
                    f" ({self.download_progress.value()}%)"
                    if job.get("total", 0) > 0
                    else ""
                )
                self.status.setText(
                    f"Downloading {job.get('label', 'model')} on host{percent}…"
                )
            elif job.get("state") == "failed":
                self.status.setText(
                    job.get("error") or "Model download failed. Retry the download."
                )
            elif job.get("state") == "complete":
                self.status.setText(
                    f"{job.get('label', 'Model')} downloaded. Choose a device, then Use on host."
                )
            else:
                self.status.setText(
                    "Download a model, prepare its device, then choose Use on host."
                )
            if installing:
                phase = installation.get("phase") or "Installing"
                self.install_status.setText(
                    f"{installation.get('label', 'Runtime')} · {phase}…"
                )
            elif installation.get("state") in ("failed", "restart_required"):
                self.install_status.setText(
                    installation.get("error")
                    or "Runtime setup needs attention on the host."
                )
            elif installation.get("state") == "complete":
                self.install_status.setText(
                    f"{installation.get('label', 'Runtime')} installed. Ready to use on the host."
                )
            else:
                self.install_status.clear()
            if self.isVisible() and (downloading or installing):
                self._poll.start()
        else:
            self._request("model_catalog")
        self._update_actions()

    def _download(self):
        choice = self._choice()
        if not self.download_button.isEnabled() or not choice:
            return
        if self._confirm(
            "Download on host?",
            f"Download {choice['label']} ({choice.get('download_size') or 'size unknown'}) "
            f"on {self._host_name}? This uses the host's network and disk space.",
        ):
            self._request(
                "download_model", family=choice["family"], model=choice["model"]
            )

    def _install(self):
        choice, dependency = self._choice(), self._dependency()
        if not self.install_button.isEnabled() or not dependency:
            return
        if self._confirm(
            "Install runtime on host?",
            f"Install {dependency['label']} on {self._host_name}? "
            "This downloads verified runtime files using the host's network and disk space.",
        ):
            self._request(
                "install_runtime",
                family=choice["family"],
                model=choice["model"],
                device=dependency["device"],
            )

    def _select(self):
        choice = self._choice()
        if not self.select_button.isEnabled() or not choice:
            return
        device = self.device_combo.currentData()
        where = f" using {_DEVICE_LABELS[device]}" if device else ""
        if self._confirm(
            "Change the host's engine?",
            f"Use {choice['label']} on {self._host_name}{where}? "
            "This changes the engine for the host and every connected client.",
        ):
            fields = {"family": choice["family"], "model": choice["model"]}
            if device and self._catalog.get("can_install_runtime"):
                fields["device"] = device
            self._request("select_model", **fields)

    def _confirm(self, title, text):
        return (
            QMessageBox.question(
                self,
                title,
                text,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )

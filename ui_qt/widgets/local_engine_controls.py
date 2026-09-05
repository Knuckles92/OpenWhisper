"""Local speech fields that persist settings and request controller reloads."""
import logging
import sys

from PyQt6.QtWidgets import QWidget, QHBoxLayout
from PyQt6.QtCore import pyqtSignal

from config import config
from services.settings import SettingsKey, settings_manager
from ui_qt.widgets.engine_field import engine_combo, engine_field

logger = logging.getLogger(__name__)


class LocalEngineControls(QWidget):
    """The Model / Device / Quant columns of the engine card's field row.

    Persists changes to settings and emits ``engine_settings_changed`` so the
    controller can reload the backend. Instantiate one per tab and keep them in
    sync via :meth:`set_values`, which blocks signals during the update.

    Optional speech families show their own models and devices; quantization
    belongs to their pinned runtime and is not an editable Whisper setting.
    """

    #: Emitted after a *user-initiated* change has been persisted to settings.
    engine_settings_changed = pyqtSignal()

    COMPUTE_CHOICES = ["auto", "float16", "float32", "int8"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()
        self.load_from_settings()
        self._connect_signals()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.model_combo = engine_combo(config.WHISPER_MODEL_CHOICES)
        # CUDA is unavailable on macOS (no Metal backend in faster-whisper).
        device_choices = (
            ["auto", "cpu"] if sys.platform == "darwin" else ["auto", "cuda", "cpu"]
        )
        self.device_combo = engine_combo(device_choices)
        self.compute_combo = engine_combo(self.COMPUTE_CHOICES)
        self.language_combo = engine_combo(["English", "Auto"])

        # Matches the Backend field's share, so Model reads as its peer and the
        # two runtime knobs stay visibly secondary.
        layout.addWidget(engine_field("Model", self.model_combo), stretch=2)
        layout.addWidget(engine_field("Device", self.device_combo), stretch=1)
        layout.addWidget(engine_field("Quant", self.compute_combo), stretch=1)
        self.language_field = engine_field("Language", self.language_combo)
        layout.addWidget(self.language_field, stretch=1)
        self.language_field.hide()

    def _connect_signals(self):
        self.model_combo.currentTextChanged.connect(self._on_changed)
        self.device_combo.currentTextChanged.connect(self._on_changed)
        self.compute_combo.currentTextChanged.connect(self._on_changed)
        self.language_combo.currentTextChanged.connect(self._on_changed)

    def set_backend(self, backend: str):
        from services.local_asr.catalog import MODELS, BACKENDS, selected_model, selected_device
        self._backend = backend
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        if backend in BACKENDS:
            for key, model in MODELS.items():
                if model.backend == backend:
                    self.model_combo.addItem(model.label, key)
            settings = settings_manager.load_all_settings()
            self.model_combo.setCurrentIndex(self.model_combo.findData(selected_model(backend, settings)))
            self.device_combo.blockSignals(True)
            self.device_combo.setCurrentText(selected_device(backend, settings))
            self.device_combo.blockSignals(False)
        else:
            self.model_combo.addItems(config.WHISPER_MODEL_CHOICES)
        self.model_combo.blockSignals(False)
        self.compute_combo.parentWidget().setVisible(backend not in BACKENDS)
        self.language_field.setVisible(backend in BACKENDS)
        self.language_combo.blockSignals(True)
        self.language_combo.setCurrentText("Auto" if settings_manager.get(SettingsKey.LOCAL_ASR_LANGUAGE, "en") == "auto" and backend != "moonshine" else "English")
        self.language_combo.blockSignals(False)
        self.language_combo.setEnabled(backend != "moonshine")
        self.device_combo.setEnabled(backend != "moonshine")
        if backend not in BACKENDS:
            self.load_from_settings()

    def _on_changed(self, _value: str):
        from services.local_asr.catalog import BACKENDS
        backend = getattr(self, "_backend", "local_whisper")
        if backend in BACKENDS:
            settings = settings_manager.load_all_settings()
            models = dict(settings.get(SettingsKey.LOCAL_ASR_MODELS) or {})
            devices = dict(settings.get(SettingsKey.LOCAL_ASR_DEVICES) or {})
            models[backend] = self.model_combo.currentData()
            devices[backend] = self.device_combo.currentText()
            settings_manager.update_settings({SettingsKey.LOCAL_ASR_MODELS: models, SettingsKey.LOCAL_ASR_DEVICES: devices,
                SettingsKey.LOCAL_ASR_LANGUAGE: "auto" if self.language_combo.currentText() == "Auto" else "en"})
            self.engine_settings_changed.emit()
            return
        settings = settings_manager.update_settings({
            SettingsKey.WHISPER_MODEL: self.model_combo.currentText(),
            SettingsKey.WHISPER_DEVICE: self.device_combo.currentText(),
            SettingsKey.WHISPER_COMPUTE_TYPE: self.compute_combo.currentText(),
        })
        logger.debug(
            "Engine settings changed: model=%s device=%s compute=%s",
            settings[SettingsKey.WHISPER_MODEL],
            settings[SettingsKey.WHISPER_DEVICE],
            settings[SettingsKey.WHISPER_COMPUTE_TYPE],
        )
        self.engine_settings_changed.emit()

    def load_from_settings(self):
        """Populate the fields from persisted settings (no signal emitted)."""
        from services.local_asr.catalog import BACKENDS
        if getattr(self, "_backend", "local_whisper") in BACKENDS:
            self.set_backend(self._backend)
            return
        settings = settings_manager.load_all_settings()
        self.set_values(
            settings.get(SettingsKey.WHISPER_MODEL, config.DEFAULT_WHISPER_MODEL),
            settings.get(SettingsKey.WHISPER_DEVICE, "auto"),
            settings.get(SettingsKey.WHISPER_COMPUTE_TYPE, "auto"),
        )

    def set_values(self, model: str, device: str, compute: str):
        """Reflect values without emitting signals."""
        from services.local_asr.catalog import BACKENDS
        if getattr(self, "_backend", "local_whisper") in BACKENDS:
            self.set_backend(self._backend)
            return
        for combo, value in (
            (self.model_combo, model or config.DEFAULT_WHISPER_MODEL),
            (self.device_combo, device or "auto"),
            (self.compute_combo, compute or "auto"),
        ):
            combo.blockSignals(True)
            combo.setCurrentText(value)
            combo.blockSignals(False)

    def set_busy(self, busy: bool):
        """Disable the fields while a reload is in flight or during recording."""
        for combo in (self.model_combo, self.device_combo, self.compute_combo, self.language_combo):
            combo.setEnabled(not busy)
        if getattr(self, "_backend", "") == "moonshine":
            self.device_combo.setEnabled(False)
            self.language_combo.setEnabled(False)

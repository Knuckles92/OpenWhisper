"""Local Whisper fields that persist settings and request controller reloads."""
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

    Only meaningful for the Local Whisper backend; the owning tab hides the
    whole group on an API backend rather than disabling the three fields.
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

        # Matches the Backend field's share, so Model reads as its peer and the
        # two runtime knobs stay visibly secondary.
        layout.addWidget(engine_field("Model", self.model_combo), stretch=2)
        layout.addWidget(engine_field("Device", self.device_combo), stretch=1)
        layout.addWidget(engine_field("Quant", self.compute_combo), stretch=1)

    def _connect_signals(self):
        self.model_combo.currentTextChanged.connect(self._on_changed)
        self.device_combo.currentTextChanged.connect(self._on_changed)
        self.compute_combo.currentTextChanged.connect(self._on_changed)

    def _on_changed(self, _value: str):
        settings = settings_manager.load_all_settings()
        settings[SettingsKey.WHISPER_MODEL] = self.model_combo.currentText()
        settings[SettingsKey.WHISPER_DEVICE] = self.device_combo.currentText()
        settings[SettingsKey.WHISPER_COMPUTE_TYPE] = self.compute_combo.currentText()
        settings_manager.save_all_settings(settings)
        logger.debug(
            "Engine settings changed: model=%s device=%s compute=%s",
            settings[SettingsKey.WHISPER_MODEL],
            settings[SettingsKey.WHISPER_DEVICE],
            settings[SettingsKey.WHISPER_COMPUTE_TYPE],
        )
        self.engine_settings_changed.emit()

    def load_from_settings(self):
        """Populate the fields from persisted settings (no signal emitted)."""
        settings = settings_manager.load_all_settings()
        self.set_values(
            settings.get(SettingsKey.WHISPER_MODEL, config.DEFAULT_WHISPER_MODEL),
            settings.get(SettingsKey.WHISPER_DEVICE, "auto"),
            settings.get(SettingsKey.WHISPER_COMPUTE_TYPE, "auto"),
        )

    def set_values(self, model: str, device: str, compute: str):
        """Reflect values without emitting signals."""
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
        for combo in (self.model_combo, self.device_combo, self.compute_combo):
            combo.setEnabled(not busy)

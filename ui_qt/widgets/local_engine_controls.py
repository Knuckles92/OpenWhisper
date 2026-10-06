"""Local speech fields that persist settings and request controller reloads.

Also the "Languages I dictate in" field, which only saves the dictation
language list and never reloads the engine.
"""
import logging
import sys

from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QMenu,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtCore import QPoint, QRect, QSize, Qt, pyqtSignal

from config import config
from services import dictation_language
from services.settings import SETTING_DEFAULTS, SettingsKey, setting_value, settings_manager
from services.local_asr.languages import LANGUAGE_LABELS, language_choices, selected_language
from ui_qt.utils.icons import design_icon
from ui_qt.widgets.buttons import Button, neutral_button
from ui_qt.widgets.engine_field import engine_combo, engine_field
from ui_qt.widgets.wrapped_label import WrappedLabel

logger = logging.getLogger(__name__)


class LocalEngineControls(QWidget):
    """The Model / Device / Quant columns of the engine card's field row.

    Persists changes to settings and emits ``engine_settings_changed`` so the
    controller can reload the backend. Instantiate one per tab and keep them in
    sync via :meth:`set_values`, which blocks signals during the update.

    Optional speech families show their own models and devices; quantization
    belongs to their pinned runtime and is not an editable Whisper setting.

    With ``dictation`` (Voice model, Quick Record), the Language field steps
    aside while "Languages I dictate in" sets the dictation language, so the
    page shows one language control. Upload File keeps it: files always use
    the engine's language.
    """

    #: Emitted after a *user-initiated* change has been persisted to settings.
    engine_settings_changed = pyqtSignal()
    help_requested = pyqtSignal(str)

    COMPUTE_CHOICES = ["auto", "float16", "float32", "int8"]

    def __init__(self, parent=None, *, dictation: bool = False):
        super().__init__(parent)
        self._dictation = dictation
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
        self.language_combo = engine_combo(())

        # Matches the Backend field's share, so Model reads as its peer and the
        # two runtime knobs stay visibly secondary.
        layout.addWidget(engine_field("Model", self.model_combo,
            'The speech model turns audio into text. Larger models generally improve accuracy but need more memory and processing time. Use Downloads to install models for this backend.',
            [('Open Settings → Voice model', 'ondemand'), ('Open Settings → Downloads', 'downloads')], self.help_requested.emit), stretch=2)
        layout.addWidget(engine_field("Device", self.device_combo,
            'Choose the hardware used for transcription. Auto picks an available device; Parakeet MLX uses the Apple GPU. CUDA uses a compatible NVIDIA graphics card; CPU uses your main processor. Moonshine uses CPU only.',
            [('Open Settings → Voice model', 'ondemand')], self.help_requested.emit), stretch=1)
        layout.addWidget(engine_field("Quant", self.compute_combo,
            'Controls how Whisper stores and calculates model numbers. Start with Auto. int8 uses less memory; float16 is suited to GPUs; float32 uses more memory. Lower precision may affect accuracy.',
            [('Open Settings → Voice model', 'ondemand')], self.help_requested.emit), stretch=1)
        self.language_field = engine_field("Language", self.language_combo,
            'Choose a language supported by this model, or Auto for language detection. Coverage and accuracy vary by model; this transcribes rather than translates. Moonshine supports English only.',
            [('Open Settings → Voice model', 'ondemand')], self.help_requested.emit)
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
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        self.device_combo.addItems(["auto", "cpu"] if backend == "parakeet_mlx" or sys.platform == "darwin" else ["auto", "cuda", "cpu"])
        self.device_combo.blockSignals(False)
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
            from services.whisper_sources import custom_models
            self.model_combo.addItems([*config.WHISPER_MODEL_CHOICES,
                                      *custom_models(settings_manager.load_all_settings())])
        self.model_combo.blockSignals(False)
        self.compute_combo.parentWidget().setVisible(backend not in BACKENDS)
        self.sync_language_field()
        self.language_combo.blockSignals(True)
        stored_lang = settings_manager.get(SettingsKey.LOCAL_ASR_LANGUAGE, SETTING_DEFAULTS[SettingsKey.LOCAL_ASR_LANGUAGE])
        self.language_combo.clear()
        for code in language_choices(backend):
            self.language_combo.addItem(LANGUAGE_LABELS[code], code)
        self.language_combo.setCurrentIndex(self.language_combo.findData(selected_language(backend, stored_lang)))
        self.language_combo.blockSignals(False)
        self.language_combo.setEnabled(self.language_combo.count() > 1)
        self.device_combo.setEnabled(backend != "moonshine")
        if backend not in BACKENDS:
            self.load_from_settings()

    def sync_language_field(self) -> None:
        """Show the Language field unless the dictation languages decide instead."""
        from services.local_asr.catalog import BACKENDS

        backend = getattr(self, "_backend", "local_whisper")
        shown = backend in BACKENDS
        if shown and self._dictation:
            settings = settings_manager.load_all_settings()
            settings[SettingsKey.SELECTED_MODEL] = backend
            # A one-language engine hides the chips and keeps its own field.
            shown = (not dictation_language.job_language(settings)
                     or bool(dictation_language.single_language_reason(settings)))
        self.language_field.setVisible(shown)

    def _on_changed(self, _value: str):
        from services.local_asr.catalog import BACKENDS
        backend = getattr(self, "_backend", "local_whisper")
        if backend in BACKENDS:
            settings = settings_manager.load_all_settings()
            models = dict(settings.get(SettingsKey.LOCAL_ASR_MODELS) or {})
            devices = dict(settings.get(SettingsKey.LOCAL_ASR_DEVICES) or {})
            models[backend] = self.model_combo.currentData()
            devices[backend] = self.device_combo.currentText()
            lang_code = selected_language(backend, self.language_combo.currentData())
            settings_manager.update_settings({SettingsKey.LOCAL_ASR_MODELS: models, SettingsKey.LOCAL_ASR_DEVICES: devices,
                SettingsKey.LOCAL_ASR_LANGUAGE: lang_code})
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
            setting_value(SettingsKey.WHISPER_MODEL, settings),
            setting_value(SettingsKey.WHISPER_DEVICE, settings),
            setting_value(SettingsKey.WHISPER_COMPUTE_TYPE, settings),
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
            if combo is self.model_combo and combo.findText(value) < 0:
                combo.addItem(value)
            # A paired client can select any precision supported by this
            # host, including mixed types beyond the short default menu.
            if combo is self.compute_combo and combo.findText(value) < 0:
                combo.addItem(value)
            combo.setCurrentText(value)
            combo.blockSignals(False)

    def set_busy(self, busy: bool):
        """Disable the fields while a reload is in flight or during recording."""
        for combo in (self.model_combo, self.device_combo, self.compute_combo, self.language_combo):
            combo.setEnabled(not busy)
        if getattr(self, "_backend", "") == "moonshine":
            self.device_combo.setEnabled(False)
            self.language_combo.setEnabled(False)


class _FlowLayout(QLayout):
    """Lays items out left to right, wrapping onto new rows that fit the width."""

    def __init__(self, parent=None, spacing: int = 6):
        super().__init__(parent)
        self._items = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(spacing)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            if not item.isEmpty():
                size = size.expandedTo(item.minimumSize())
        return size

    def _arrange(self, rect: QRect, *, apply: bool) -> int:
        space = self.spacing()
        rows, row, used = [], [], 0
        for item in self._items:
            if item.isEmpty():
                continue
            hint = item.sizeHint()
            if row and used + space + hint.width() > rect.width():
                rows.append(row)
                row, used = [], 0
            used += (space if row else 0) + hint.width()
            row.append((item, hint))
        if row:
            rows.append(row)
        y = rect.y()
        for row in rows:
            height = max(hint.height() for _item, hint in row)
            x = rect.x()
            for item, hint in row:
                if apply:
                    width = min(hint.width(), rect.width())
                    top = y + (height - hint.height()) // 2
                    item.setGeometry(QRect(QPoint(x, top), QSize(width, hint.height())))
                x += hint.width() + space
            y += height + space
        return max(0, y - space - rect.y()) if rows else 0


class _ChipArea(QWidget):
    """Holds the flow of chips and reports the height they need at its width.

    Like ``WrappedLabel``: the size hints carry the wrapped height, because
    Qt's height-for-width path estimates nested cards at the wrong width.
    """

    def sizeHint(self) -> QSize:
        flow = self.layout()
        widest = flow.minimumSize()
        width = self.width()
        if width <= 0:
            return widest
        return QSize(widest.width(), flow.heightForWidth(width))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if event.oldSize().width() != event.size().width():
            self.updateGeometry()


class _LanguageChip(QFrame):
    removed = pyqtSignal(str)

    def __init__(self, code: str, *, available: bool, active: bool, unavailable_reason: str):
        super().__init__()
        name = dictation_language.label(code)
        self.code = code
        self.setObjectName("dictationLanguageChip")
        self.setProperty("available", available)
        self.setProperty("active", active)
        self.setFixedHeight(30)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        if not available:
            self.setToolTip(unavailable_reason)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 5, 0)
        layout.setSpacing(4)
        label = QLabel(name)
        label.setObjectName("dictationLanguageChipLabel")
        layout.addWidget(label)
        self.remove_button = QToolButton()
        self.remove_button.setObjectName("dictationLanguageChipRemove")
        self.remove_button.setIcon(design_icon("x-gray.svg"))
        self.remove_button.setIconSize(QSize(13, 13))
        self.remove_button.setFixedSize(22, 22)
        self.remove_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remove_button.setToolTip(f"Remove {name}")
        self.remove_button.setAccessibleName(f"Remove {name}")
        self.remove_button.clicked.connect(lambda: self.removed.emit(self.code))
        layout.addWidget(self.remove_button)


class DictationLanguagesField(QWidget):
    """The languages someone dictates in: removable chips and an Add menu.

    Saves ``dictation_languages`` (keeping the active language valid) as soon
    as a chip is added or removed. Languages the current engine can't use stay
    saved and show muted, so switching engines back restores them.
    """

    #: The saved list changed (a person's edit, never a refresh).
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("dictationLanguages")
        self._backend = ""
        self._remaining: list = []
        self.chips: list = []
        self.add_menu = QMenu(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.chip_area = _ChipArea()
        self.chip_area.setObjectName("dictationLanguageChips")
        self._flow = _FlowLayout(self.chip_area)
        self.add_button = neutral_button(Button("Add language"))
        self.add_button.setIcon(design_icon("plus-blue.svg"))
        self.add_button.clicked.connect(self._open_add_menu)
        layout.addWidget(self.chip_area)
        self.caption = WrappedLabel("")
        self.caption.setObjectName("infoLabel")
        layout.addWidget(self.caption)
        self.refresh()

    def set_backend(self, backend: str) -> None:
        """Show the choices for ``backend``, which may not be saved yet."""
        self._backend = backend or ""
        self.refresh()

    def _settings(self) -> dict:
        settings = settings_manager.load_all_settings()
        if self._backend:
            settings[SettingsKey.SELECTED_MODEL] = self._backend
        return settings

    def refresh(self) -> None:
        from ui_qt.widgets.speech_backend_picker import backend_display_name

        settings = self._settings()
        chosen = dictation_language.chosen_languages(settings)
        accepted = dictation_language.accepted_languages(settings)
        choices = dictation_language.language_choices(settings)
        reason = dictation_language.single_language_reason(settings)
        active = dictation_language.job_language(settings)
        engine_name = backend_display_name(dictation_language.engine(settings))
        unavailable = f"{engine_name} can't use this language."
        self._flow.removeWidget(self.add_button)
        for chip in self.chips:
            self._flow.removeWidget(chip)
            chip.hide()
            chip.deleteLater()
        self.chips = []
        if not reason:
            for code in chosen:
                chip = _LanguageChip(
                    code, available=code in accepted, active=code == active and len(choices) > 1,
                    unavailable_reason=unavailable,
                )
                chip.removed.connect(self._remove)
                self._flow.addWidget(chip)
                self.chips.append(chip)
            self._flow.addWidget(self.add_button)
        self.chip_area.setVisible(not reason)
        self._remaining = [code for code in accepted if code not in chosen]
        self.add_button.setEnabled(bool(self._remaining))
        current = dictation_language.current_language(settings)
        self.caption.setText(reason or self._caption(chosen, choices, current, engine_name))
        self.chip_area.updateGeometry()

    @staticmethod
    def _caption(chosen, choices, current, engine_name) -> str:
        """Which language the next dictation uses, and what adding more does."""
        if current == dictation_language.AUTO:
            uses = "detects the language"
        else:
            uses = f"uses {dictation_language.label(current)}"
        if not chosen:
            return (f"Dictation {uses}. Add languages to switch between them "
                    "from the overlay, the tray or a shortcut.")
        if not choices:
            return f"{engine_name} can't use these, so dictation {uses}."
        if len(choices) == 1:
            return f"Dictation {uses}. Add another to switch between them."
        if current == dictation_language.AUTO:
            return f"Dictation {uses}. Switch from the overlay, the tray or a shortcut."
        return (f"Now dictating in {dictation_language.label(current)}. "
                "Switch from the overlay, the tray or a shortcut.")

    def _open_add_menu(self) -> None:
        self.add_menu.clear()
        for code in self._remaining:
            action = self.add_menu.addAction(dictation_language.label(code))
            action.triggered.connect(lambda _checked=False, code=code: self.add(code))
        self.add_menu.popup(self.add_button.mapToGlobal(QPoint(0, self.add_button.height())))

    def add(self, code: str) -> None:
        self._save(lambda settings: dictation_language.add_chosen(settings, code))

    def _remove(self, code: str) -> None:
        codes = [other for other in dictation_language.chosen_languages(self._settings()) if other != code]
        self._save(lambda settings: dictation_language.set_chosen(settings, codes))

    def _save(self, change) -> None:
        try:
            settings_manager.mutate_settings(change)
        except Exception:
            logger.exception("Couldn't save the dictation languages")
            self.caption.setText("Couldn't save your languages. Try again.")
            return
        self.refresh()
        self.changed.emit()

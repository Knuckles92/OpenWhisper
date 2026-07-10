"""Compact inline controls for the local Whisper engine (model / device / quant).

Surfaces the three most useful local-backend knobs — Whisper model, device
(GPU/CPU), and compute type (quantization) — directly on the main window so they
no longer require opening Settings → Advanced.

The widget is intentionally "dumb": on any user change it persists the three
settings keys and emits ``engine_settings_changed``. It does NOT reload the
backend itself — that stays a controller responsibility so all of the (slow,
threaded) reload logic lives in one place.
"""
import logging
import sys

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont

from config import config
from services.settings import SettingsKey, settings_manager
from ui_qt.widgets.collapsible_header import CollapsibleSectionToggle
from ui_qt.utils.collapse_animation import (
    UNLIMITED_HEIGHT,
    create_max_height_animation,
    run_max_height_animation,
)

logger = logging.getLogger(__name__)


class LocalEngineControls(QWidget):
    """Inline model/device/quant controls for the local Whisper backend.

    Persists changes to settings and emits ``engine_settings_changed`` so the
    controller can reload the backend. Instantiate one per tab and keep them in
    sync via :meth:`set_values` (which blocks signals during the update).
    """

    #: Emitted after a *user-initiated* change has been persisted to settings.
    engine_settings_changed = pyqtSignal()

    #: Emitted when the collapsed state changes (True == collapsed, delta in px).
    toggled = pyqtSignal(bool, int)

    _EXPANDED_MAX_HEIGHT = UNLIMITED_HEIGHT

    COMPUTE_CHOICES = ["auto", "float16", "float32", "int8"]

    _FIELD_LABEL_STYLE = "color: #8e8e93; font-size: 10px;"
    _RESOLVED_STYLE = "color: #8e8e93; margin-top: 2px;"
    _COMBO_STYLE = (
        "QComboBox { background-color: #2c2c2e; color: #f5f5f7; "
        "border: 1px solid #3a3a3c; border-radius: 6px; padding: 2px 8px; }"
        "QComboBox:hover { border-color: #0a84ff; }"
        "QComboBox::drop-down { border: none; }"
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._collapsed = False
        self._content_height = 0
        self._setup_ui()
        self.load_from_settings()
        self._connect_signals()
        self.set_collapsed(True, emit=False)

    # ── Construction ───────────────────────────────────────────────

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(6)

        # Disclosure header (centered)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self.section_toggle = CollapsibleSectionToggle(
            "Engine Settings",
            prefix="⚙ ",
            expanded=False,
            expand_tooltip="Show model, device, and quantization settings",
            collapse_tooltip="Hide engine settings",
        )
        header.addStretch()
        header.addWidget(self.section_toggle)
        header.addStretch()
        layout.addLayout(header)

        # Collapsible body: combos row + resolved readout
        self._content_widget = QWidget()
        content_layout = QVBoxLayout(self._content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(6)

        self.body = QWidget()
        body_layout = QHBoxLayout(self.body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(10)

        self.model_combo = self._make_combo(config.WHISPER_MODEL_CHOICES)
        # CUDA is unavailable on macOS (no Metal backend in faster-whisper).
        device_choices = (
            ["auto", "cpu"] if sys.platform == "darwin" else ["auto", "cuda", "cpu"]
        )
        self.device_combo = self._make_combo(device_choices)
        self.compute_combo = self._make_combo(self.COMPUTE_CHOICES)

        body_layout.addWidget(self._labeled("Model", self.model_combo), stretch=2)
        body_layout.addWidget(self._labeled("Device", self.device_combo), stretch=1)
        body_layout.addWidget(self._labeled("Quant", self.compute_combo), stretch=1)
        self.body.setMaximumWidth(480)

        body_row = QHBoxLayout()
        body_row.setContentsMargins(0, 0, 0, 0)
        body_row.addStretch()
        body_row.addWidget(self.body)
        body_row.addStretch()
        content_layout.addLayout(body_row)

        self.resolved_label = QLabel("")
        self.resolved_label.setFont(QFont("Segoe UI", 9))
        self.resolved_label.setStyleSheet(self._RESOLVED_STYLE)
        self.resolved_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content_layout.addWidget(self.resolved_label)

        layout.addWidget(self._content_widget)

    def _make_combo(self, items) -> QComboBox:
        combo = QComboBox()
        combo.addItems(items)
        combo.setMinimumHeight(28)
        combo.setFont(QFont("Segoe UI", 10))
        combo.setStyleSheet(self._COMBO_STYLE)
        return combo

    def _labeled(self, text: str, combo: QComboBox) -> QWidget:
        wrapper = QWidget()
        col = QVBoxLayout(wrapper)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        label = QLabel(text)
        label.setStyleSheet(self._FIELD_LABEL_STYLE)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(label)
        col.addWidget(combo)
        return wrapper

    def _connect_signals(self):
        self.model_combo.currentTextChanged.connect(self._on_changed)
        self.device_combo.currentTextChanged.connect(self._on_changed)
        self.compute_combo.currentTextChanged.connect(self._on_changed)
        self.section_toggle.toggled_expanded.connect(self._on_section_toggled)

    # ── Collapse support ───────────────────────────────────────────

    @property
    def is_collapsed(self) -> bool:
        """Whether the settings body is currently collapsed."""
        return self._collapsed

    @property
    def content_height(self) -> int:
        """Height of the body captured at the last collapse (resize delta)."""
        return self._content_height

    def _on_section_toggled(self, expanded: bool):
        """Handle user click on the shared section toggle."""
        self.set_collapsed(not expanded)

    def _content_natural_height(self) -> int:
        """Natural height of the combo row and resolved readout."""
        self._content_widget.setVisible(True)
        self._content_widget.adjustSize()
        return max(
            self._content_widget.sizeHint().height(),
            self._content_widget.minimumSizeHint().height(),
        )

    def _header_block_height(self) -> int:
        """Vertical space consumed by the disclosure header and outer margins."""
        margins = self.layout().contentsMargins()
        return (
            margins.top()
            + self.section_toggle.sizeHint().height()
            + margins.bottom()
        )

    def _expanded_minimum_height(self) -> int:
        """Minimum height when the settings body is fully expanded."""
        return (
            self._header_block_height()
            + self.layout().spacing()
            + self._content_natural_height()
        )

    def _collapsed_minimum_height(self) -> int:
        """Minimum height when only the disclosure header is shown."""
        return self._header_block_height()

    def _measure_collapse_delta(self) -> int:
        """Return the natural vertical space the collapsible body needs."""
        return max(
            self._expanded_minimum_height() - self._collapsed_minimum_height(),
            1,
        )

    def _apply_content_size_policy(self, collapsed: bool):
        """Reserve enough vertical space so the body is never clipped."""
        self.setMaximumHeight(self._EXPANDED_MAX_HEIGHT)
        self._content_widget.setMaximumHeight(self._EXPANDED_MAX_HEIGHT)

        if collapsed:
            self._content_widget.setMinimumHeight(0)
            self.setMinimumHeight(self._collapsed_minimum_height())
        else:
            content_height = self._content_natural_height()
            self._content_widget.setMinimumHeight(content_height)
            self.setMinimumHeight(self._expanded_minimum_height())

        self.updateGeometry()

    def set_collapsed(self, collapsed: bool, emit: bool = True):
        """Collapse or expand the settings body.

        Args:
            collapsed: True to hide the body, False to show it.
            emit: Whether to emit the ``toggled`` signal (False during tab sync).
        """
        if collapsed == self._collapsed:
            return

        delta = self._measure_collapse_delta()
        if collapsed:
            self._content_height = delta
        elif self._content_height <= 0:
            self._content_height = delta

        self._collapsed = collapsed
        self.section_toggle.set_expanded(not collapsed, emit=False)

        if emit:
            self.toggled.emit(collapsed, self._content_height)
            self._animate_content_visibility(collapsed)
        else:
            self._apply_collapsed_immediate(collapsed)

    def _apply_collapsed_immediate(self, collapsed: bool):
        """Apply collapsed state instantly (sync/initial setup)."""
        if hasattr(self, "_content_anim") and self._content_anim is not None:
            self._content_anim.stop()
        self._content_widget.setVisible(not collapsed)
        self._content_widget.setMaximumHeight(self._EXPANDED_MAX_HEIGHT)
        self._apply_content_size_policy(collapsed)

    def _content_animation(self):
        if not hasattr(self, "_content_anim") or self._content_anim is None:
            self._content_anim = create_max_height_animation(self._content_widget)
        return self._content_anim

    def _animate_content_visibility(self, collapsed: bool):
        """Animate the body height in parallel with the window resize."""
        natural = self._content_natural_height()
        self._content_widget.setVisible(True)
        self.setMinimumHeight(0)
        self.setMaximumHeight(self._EXPANDED_MAX_HEIGHT)
        self._content_widget.setMinimumHeight(0)

        if collapsed:
            start = self._content_widget.height() or natural
            end = 0
        else:
            start = 0
            end = natural

        def on_finished():
            self._content_widget.setMaximumHeight(self._EXPANDED_MAX_HEIGHT)
            if collapsed:
                self._content_widget.setVisible(False)
            self._apply_content_size_policy(collapsed)

        run_max_height_animation(
            self._content_animation(),
            start=start,
            end=end,
            on_finished=on_finished,
        )

    # ── Internal handlers ──────────────────────────────────────────

    def _on_changed(self, _text: str):
        """Persist the three keys and notify listeners of a user change."""
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

    # ── Public API ─────────────────────────────────────────────────

    def load_from_settings(self):
        """Populate combos from the persisted settings (no signal emitted)."""
        settings = settings_manager.load_all_settings()
        self.set_values(
            settings.get(SettingsKey.WHISPER_MODEL, config.DEFAULT_WHISPER_MODEL),
            settings.get(SettingsKey.WHISPER_DEVICE, "auto"),
            settings.get(SettingsKey.WHISPER_COMPUTE_TYPE, "auto"),
        )

    def set_values(self, model: str, device: str, compute: str):
        """Reflect values without emitting (used to mirror the other tab)."""
        for combo, value, fallback in (
            (self.model_combo, model, config.DEFAULT_WHISPER_MODEL),
            (self.device_combo, device, "auto"),
            (self.compute_combo, compute, "auto"),
        ):
            combo.blockSignals(True)
            self._select(combo, value, fallback)
            combo.blockSignals(False)

    def current_values(self) -> tuple:
        """Return the (model, device, compute) currently shown."""
        return (
            self.model_combo.currentText(),
            self.device_combo.currentText(),
            self.compute_combo.currentText(),
        )

    def set_resolved_info(self, info: str):
        """Update the small 'what auto resolved to' readout."""
        self.resolved_label.setText(info)

    def set_busy(self, busy: bool):
        """Disable combos while a reload is in flight or during recording."""
        for combo in (self.model_combo, self.device_combo, self.compute_combo):
            combo.setEnabled(not busy)

    # ── Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _select(combo: QComboBox, value: str, fallback: str = None):
        index = combo.findText(value)
        if index < 0 and fallback is not None:
            index = combo.findText(fallback)
        if index >= 0:
            combo.setCurrentIndex(index)

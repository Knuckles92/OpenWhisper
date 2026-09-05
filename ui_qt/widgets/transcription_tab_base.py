"""Shared model-selection and transcript tab scaffolding."""
import logging
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QFrame, QTextEdit,
    QButtonGroup, QPushButton, QScrollArea,
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtGui import QFont

from config import config
from services.settings import (
    LEGACY_STREAMING_KEYS,
    SettingsKey,
    resolve_api_transcription_model,
    settings_manager,
)
from ui_qt.overlay_state import OverlayState
from ui_qt.utils.collapse_animation import SECTION_COLLAPSE_DURATION_MS
from ui_qt.utils.font_scale import current_ui_font_scale
from ui_qt.utils.markdown_render import PREVIEW_STYLE, render_markdown
from ui_qt.widgets.cards import HeaderCard
from ui_qt.widgets.downloads_label import DownloadsLabel
from ui_qt.widgets.engine_field import (
    EngineStatus,
    StatusDot,
    engine_combo,
    engine_field,
)
from ui_qt.widgets.stats_display import TranscriptionStatsWidget
from ui_qt.widgets.local_engine_controls import LocalEngineControls

logger = logging.getLogger(__name__)


class TranscriptPane(QFrame):
    """The transcript's painted surface, with room for one floating corner action.

    The corner widget is parented to the pane and laid over the text's top-right
    padding rather than given a row of its own, so it costs the preview no
    height. The Fixed / Raw switch row ends in a stretch, so the two never meet
    when both are shown.
    """

    #: Clears the text edit's vertical scrollbar as well as its padding.
    CORNER_INSET_X = 14
    CORNER_INSET_Y = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self._corner_widget: Optional[QWidget] = None

    def set_corner_widget(self, widget: QWidget) -> None:
        self._corner_widget = widget
        widget.setParent(self)
        widget.raise_()
        self._place_corner_widget()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_corner_widget()

    def _place_corner_widget(self) -> None:
        widget = self._corner_widget
        if widget is None:
            return
        size = widget.size()
        widget.move(self.width() - size.width() - self.CORNER_INSET_X, self.CORNER_INSET_Y)
        widget.raise_()


class TranscriptionTabBase(QWidget):
    """Base widget for tabs that select a model and display a transcript."""

    model_changed = pyqtSignal(str)  # Model display name
    engine_settings_changed = pyqtSignal()  # Local engine chip changed
    manage_models_requested = pyqtSignal()  # "Manage models…" clicked
    engine_downloads_requested = pyqtSignal()
    transcription_collapsed = pyqtSignal(bool, int)  # collapsed, freed-height delta
    live_preview_changed = pyqtSignal()  # Live preview checkbox persisted a change

    CONTENT_OBJECT_NAME = "transcriptionTabContent"
    INITIAL_STATUS = ""
    TRANSCRIPT_PLACEHOLDER = "Transcription will appear here..."

    #: Show the Live preview checkbox in the engine footer. The preview only
    #: runs while dictating, so tabs without a microphone flow turn it off.
    LIVE_PREVIEW_CONTROL = True

    #: Render the transcript as Markdown. Off for dictation, whose cleanup
    #: returns prose; on where the transcript carries structure of its own.
    TRANSCRIPT_MARKDOWN = False

    BACKEND_CHIP_MAX_WIDTH = 150

    _BACKEND_SECTION = "backend"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_model = config.MODEL_CHOICES[0]
        self._fixed_text = ""
        self._raw_text: Optional[str] = None
        self._showing_raw = False
        self._device_info = ""
        self._engine_status = EngineStatus.UNKNOWN
        self._engine_busy = False
        self._model_downloads: set[str] = set()
        self._activity_state = OverlayState.NONE
        self._status_message = self.INITIAL_STATUS
        self._local_engine_visible = True
        self._setup_ui()
        self._connect_signals()
        self.load_cleanup_setting()
        self.load_live_preview_setting()

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("transcriptionTabScrollArea")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.scroll_area.setStyleSheet(
            "QScrollArea#transcriptionTabScrollArea { border: none; "
            "background: transparent; }"
        )

        scroll_host = QWidget()
        scroll_host.setObjectName("transcriptionTabScrollHost")
        scroll_host_layout = QVBoxLayout(scroll_host)
        scroll_host_layout.setContentsMargins(0, 0, 0, 0)
        scroll_host_layout.setSpacing(0)

        content_container = QWidget()
        content_container.setObjectName(self.CONTENT_OBJECT_NAME)
        content_layout = QVBoxLayout(content_container)
        content_layout.setContentsMargins(24, 14, 24, 16)
        content_layout.setSpacing(12)
        # No vertical alignment: the transcription card is the elastic element
        # (stretch=1). Horizontal centering is handled by the center_wrapper.
        self.content_layout = content_layout

        center_wrapper = QHBoxLayout()
        center_wrapper.setContentsMargins(0, 0, 0, 0)
        center_wrapper.setSpacing(0)
        center_wrapper.addStretch()
        center_wrapper.addWidget(content_container, stretch=1)
        center_wrapper.addStretch()

        content_container.setMaximumWidth(700)
        content_container.setMinimumWidth(500)

        scroll_host_layout.addLayout(center_wrapper)
        self.scroll_area.setWidget(scroll_host)
        main_layout.addWidget(self.scroll_area)

        # Engine card: four labeled fields across the full width, then a footer
        # line carrying the resolved engine and the two controls that apply to
        # every backend. A QFrame rather than Card because a plain QWidget will
        # not paint a QSS surface.
        self.engine_card = QFrame()
        self.engine_card.setObjectName("engineCard")
        engine_layout = QVBoxLayout(self.engine_card)
        engine_layout.setContentsMargins(14, 12, 14, 12)
        engine_layout.setSpacing(10)

        self.model_combo = engine_combo(config.MODEL_CHOICES, primary=True)
        self._apply_backend_status(EngineStatus.UNKNOWN)

        self.local_engine = LocalEngineControls()
        self.api_model_combo = engine_combo(config.API_MODEL_CHOICES)
        self.api_model_combo.setToolTip(
            "OpenAI transcription model. Requires an API key in Settings → API keys."
        )
        self.api_model_field = engine_field("Model", self.api_model_combo)
        self.api_model_field.hide()
        self.refresh_api_model()

        self._field_row = QHBoxLayout()
        self._field_row.setContentsMargins(0, 0, 0, 0)
        self._field_row.setSpacing(10)
        self._field_row.addWidget(engine_field("Backend", self.model_combo), stretch=2)
        self._field_row.addWidget(self.local_engine, stretch=4)
        self._field_row.addWidget(self.api_model_field, stretch=2)
        self._field_filler_index = self._field_row.count()
        self._field_row.addStretch(0)
        engine_layout.addLayout(self._field_row)

        self.status_dot = StatusDot(diameter=16)

        self.resolved_label = DownloadsLabel(self.INITIAL_STATUS)
        self.resolved_label.setObjectName("engineResolvedLabel")
        self.resolved_label.downloads_requested.connect(self.engine_downloads_requested)

        self.cleanup_check = QCheckBox("AI cleanup")
        self.cleanup_check.setObjectName("engineCleanupCheck")
        self.cleanup_check.setToolTip(
            "Clean up the transcript with an AI model after transcription "
            "(punctuation, fillers, light ASR fixes)"
        )

        self.live_preview_check = QCheckBox("Live preview")
        self.live_preview_check.setObjectName("engineLivePreviewCheck")
        self.live_preview_check.setToolTip(
            "Show text as you speak on the near-cursor overlay. Parakeet and "
            "Nemotron Streaming preview with the loaded engine; Local Whisper "
            "loads a small tiny.en preview model. Same setting as "
            "Settings → Recording → Live preview."
        )
        if not self.LIVE_PREVIEW_CONTROL:
            self.live_preview_check.hide()

        self.manage_models_button = QPushButton("Manage models…")
        self.manage_models_button.setObjectName("engineManageButton")
        self.manage_models_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.manage_models_button.setFlat(True)

        footer_row = QHBoxLayout()
        footer_row.setContentsMargins(0, 0, 0, 0)
        footer_row.setSpacing(6)
        footer_row.addWidget(self.status_dot)
        footer_row.addWidget(self.resolved_label, stretch=1)
        footer_row.addSpacing(6)
        footer_row.addWidget(self.cleanup_check)
        footer_row.addSpacing(6)
        footer_row.addWidget(self.live_preview_check)
        footer_row.addSpacing(6)
        footer_row.addWidget(self.manage_models_button)
        engine_layout.addLayout(footer_row)

        content_layout.addWidget(self.engine_card)

        self._build_content_before_status(content_layout)

        self._build_content_after_status(content_layout)

        self.transcription_card = HeaderCard("Transcription", collapsible=True)

        # One painted surface holds the Fixed / Raw switch and the text, and
        # the text edit inside it is borderless, so the switch reads as the
        # corner of the box rather than a row of buttons floating above it.
        self.transcript_pane = TranscriptPane()
        self.transcript_pane.setObjectName("transcriptPane")
        pane_layout = QVBoxLayout(self.transcript_pane)
        pane_layout.setContentsMargins(0, 0, 0, 0)
        pane_layout.setSpacing(0)

        self.version_toggle = QWidget()
        self.version_toggle.setObjectName("transcriptSwitchRow")
        version_row = QHBoxLayout(self.version_toggle)
        version_row.setContentsMargins(12, 10, 12, 0)
        version_row.setSpacing(0)

        switch = QFrame()
        switch.setObjectName("transcriptSwitch")
        switch_layout = QHBoxLayout(switch)
        switch_layout.setContentsMargins(2, 2, 2, 2)
        switch_layout.setSpacing(2)

        self._version_group = QButtonGroup(self)
        self.fixed_btn = QPushButton("Fixed")
        self.raw_btn = QPushButton("Raw")
        for btn in (self.fixed_btn, self.raw_btn):
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setObjectName("transcriptSwitchBtn")
            btn.setFixedHeight(22)
            self._version_group.addButton(btn)
            switch_layout.addWidget(btn)
        version_row.addWidget(switch)
        version_row.addStretch()
        self.fixed_btn.setChecked(True)
        self.version_toggle.hide()

        self.transcript_text = QTextEdit()
        self.transcript_text.setObjectName("transcriptText")
        self.transcript_text.setReadOnly(True)
        self.transcript_text.setMinimumHeight(130)
        self.transcript_text.setFont(QFont("Segoe UI", 13))
        self.transcript_text.setPlaceholderText(self.TRANSCRIPT_PLACEHOLDER)

        pane_layout.addWidget(self.version_toggle)
        pane_layout.addWidget(self.transcript_text, stretch=1)

        self.transcription_card.add_content_widget(self.transcript_pane)
        self.transcription_card.toggled.connect(self._on_transcription_toggled)

        # The transcription card is the elastic element: it expands to fill
        # spare height and shrinks first when the window gets smaller.
        content_layout.addWidget(self.transcription_card, stretch=1)

        self.stats_widget = TranscriptionStatsWidget()
        content_layout.addWidget(self.stats_widget)

        # Managed bottom stretch: 0 while expanded (card fills), 1 while
        # collapsed (pushes the compact content to the top).
        content_layout.addStretch()
        self._bottom_stretch_index = content_layout.count() - 1

        # Always start collapsed to keep the main window compact on launch.
        self.set_transcription_collapsed(True)

    def _build_content_before_status(self, layout: QVBoxLayout):
        """Insert tab-specific widgets after the engine card."""

    def _build_content_after_status(self, layout: QVBoxLayout):
        """Insert tab-specific widgets before the transcription card."""

    def _connect_signals(self):
        self.model_combo.currentTextChanged.connect(self._on_backend_changed)
        self.api_model_combo.currentTextChanged.connect(self._on_api_model_changed)
        self.local_engine.engine_settings_changed.connect(self.engine_settings_changed)
        self.manage_models_button.clicked.connect(self.manage_models_requested)
        self.cleanup_check.toggled.connect(self._on_cleanup_toggled)
        self.live_preview_check.toggled.connect(self._on_live_preview_toggled)
        self.fixed_btn.toggled.connect(self._on_version_toggled)
        self.raw_btn.toggled.connect(self._on_version_toggled)

    def _on_cleanup_toggled(self, checked: bool):
        settings_manager.save_setting(
            SettingsKey.TRANSCRIPT_CLEANUP_ENABLED, checked
        )

    def load_cleanup_setting(self):
        enabled = settings_manager.get(
            SettingsKey.TRANSCRIPT_CLEANUP_ENABLED,
            config.TRANSCRIPT_CLEANUP_ENABLED,
        )
        self.cleanup_check.blockSignals(True)
        self.cleanup_check.setChecked(bool(enabled))
        self.cleanup_check.blockSignals(False)

    def _on_live_preview_toggled(self, checked: bool):
        """Persist the preview toggle the same way the Settings dialog does.

        Writing the one key and dropping the legacy split switches in a single
        atomic update keeps the two entry points indistinguishable on disk. The
        signal tells the window to re-sync its other tabs and reconfigure the
        streaming runtime, which caches this value.
        """
        try:
            settings_manager.update_settings(
                {SettingsKey.STREAMING_ENABLED: bool(checked)},
                remove=LEGACY_STREAMING_KEYS,
            )
        except Exception as exc:
            logger.error("Couldn't save live preview setting: %s", exc)
            self.load_live_preview_setting()
            return
        self.live_preview_changed.emit()

    def load_live_preview_setting(self):
        enabled = settings_manager.get(
            SettingsKey.STREAMING_ENABLED,
            config.STREAMING_ENABLED,
        )
        self.live_preview_check.blockSignals(True)
        self.live_preview_check.setChecked(bool(enabled))
        self.live_preview_check.blockSignals(False)

    def _on_version_toggled(self, checked: bool):
        if not checked:
            return
        show_raw = self.raw_btn.isChecked()
        self._showing_raw = show_raw
        if show_raw and self._raw_text is not None:
            self._show_transcript_text(self._raw_text)
        else:
            self._show_transcript_text(self._fixed_text)

    def redraw_transcript(self) -> None:
        self._show_transcript_text(self.shown_transcript())

    def _show_transcript_text(self, text: str) -> None:
        if self.TRANSCRIPT_MARKDOWN:
            render_markdown(
                self.transcript_text.document(),
                text,
                PREVIEW_STYLE.scaled(current_ui_font_scale()),
            )
        else:
            self.transcript_text.setPlainText(text)

    def shown_transcript(self) -> str:
        """The source text of the version currently displayed (Fixed or Raw)."""
        if self._showing_raw and self._raw_text is not None:
            return self._raw_text
        return self._fixed_text

    def refresh_api_model(self):
        model = resolve_api_transcription_model(settings_manager.load_all_settings())
        blocked = self.api_model_combo.blockSignals(True)
        self.api_model_combo.setCurrentText(model)
        self.api_model_combo.blockSignals(blocked)

    def _on_api_model_changed(self, model: str):
        settings_manager.save_setting(SettingsKey.API_TRANSCRIPTION_MODEL, model)
        self.model_changed.emit(self.current_model)

    def _on_backend_changed(self, display_name: str):
        self.current_model = display_name
        self.refresh_api_model()
        self.local_engine.set_backend(config.MODEL_VALUE_MAP.get(display_name, "local_whisper"))
        self.set_local_engine_visible(display_name != "API")
        self.model_changed.emit(display_name)

    def choose_backend(self, display_name: str):
        """Select a backend and announce it, as if the user picked it."""
        if display_name == self.current_model:
            if display_name == "API":
                self.refresh_api_model()
                self.model_changed.emit(display_name)
            return
        self.model_combo.setCurrentText(display_name)

    def current_backend(self) -> str:
        """The selected backend's ``config.MODEL_CHOICES`` label."""
        return self.current_model

    def set_backend(self, display_name: str):
        """Show a backend as selected without emitting ``model_changed``."""
        index = self.model_combo.findText(display_name)
        if index < 0:
            return
        self.model_combo.blockSignals(True)
        self.model_combo.setCurrentIndex(index)
        self.model_combo.blockSignals(False)
        self.current_model = display_name
        self.refresh_api_model()
        self.local_engine.set_backend(config.MODEL_VALUE_MAP.get(display_name, "local_whisper"))
        self.set_local_engine_visible(display_name != "API")

    def set_backend_enabled(self, enabled: bool):
        """Lock the backend choice, e.g. while recording."""
        self.model_combo.setEnabled(enabled)
        self.api_model_combo.setEnabled(enabled)

    def set_model_selection(self, model_value: str):
        """Select a backend by its internal value (e.g. ``local_whisper``)."""
        for display_name, internal_value in config.MODEL_VALUE_MAP.items():
            if internal_value == model_value:
                self.set_backend(display_name)
                break

    def set_status(self, status_text: str):
        self.set_engine_message(status_text)

    def set_engine_message(self, status_text: str) -> None:
        self._status_message = status_text
        if status_text in ("Ready", "Ready to record", "Whisper engine ready"):
            self._status_message = ""
        self._refresh_engine_status()

    def set_device_info(self, device_info: str, ready: Optional[bool] = None):
        """Update the idle readout and readiness from a completed engine probe."""
        self._device_info = device_info
        self._status_message = ""
        self._engine_status = (
            EngineStatus.UNKNOWN if ready is None
            else EngineStatus.READY if ready else EngineStatus.ATTENTION
        )
        self._refresh_engine_status()

    def set_engine_busy(self, busy: bool) -> None:
        self.local_engine.set_busy(busy)
        self._engine_busy = busy
        if busy:
            self._status_message = "Loading speech engine..."
        elif self._status_message in (
            "Loading speech engine...", "Reloading speech engine...",
        ):
            self._status_message = ""
        self._refresh_engine_status()

    @property
    def engine_loading(self) -> bool:
        return self._engine_busy or bool(self._model_downloads)

    def set_model_downloading(self, model_name: str, downloading: bool) -> None:
        if downloading:
            self._model_downloads.add(model_name)
            self._status_message = f"Downloading model '{model_name}'..."
        else:
            self._model_downloads.discard(model_name)
            if self._status_message == f"Downloading model '{model_name}'...":
                self._status_message = ""
        self._refresh_engine_status()

    def set_activity_state(self, state: OverlayState) -> None:
        """Drive activity feedback from the same explicit state as the overlay."""
        messages = {
            OverlayState.RECORDING: "Recording...",
            OverlayState.PROCESSING: "Processing...",
            OverlayState.TRANSCRIBING: "Transcribing...",
            OverlayState.CLEANING: "Cleaning up...",
            OverlayState.CANCELING: "Canceling...",
        }
        previous = self._activity_state
        self._activity_state = state
        if state in messages:
            self._status_message = messages[state]
        elif self._status_message == messages.get(previous):
            self._status_message = ""
        self._refresh_engine_status()

    def _refresh_engine_status(self) -> None:
        busy = self.engine_loading or self._activity_state in (
            OverlayState.PROCESSING, OverlayState.TRANSCRIBING,
            OverlayState.CLEANING, OverlayState.CANCELING,
        )
        idle_message = self._device_info
        if self._engine_busy:
            idle_message = "Loading speech engine..."
        elif self._model_downloads:
            idle_message = f"Downloading model '{next(iter(self._model_downloads))}'..."
        message = self._status_message or idle_message
        self.resolved_label.setText(message or self.INITIAL_STATUS)
        self.resolved_label.setAccessibleName(message or self.INITIAL_STATUS)
        self.status_dot.set_status(self._engine_status)
        self.status_dot.set_busy(busy)
        self.status_dot.setVisible(self._local_engine_visible or busy)
        self._apply_backend_status(
            EngineStatus.UNKNOWN if self._engine_busy else self._engine_status
        )

    def _apply_backend_status(self, status: EngineStatus):
        self.model_combo.set_status(status)

    def set_local_engine_visible(self, visible: bool):
        """Switch between local runtime fields and the API model field."""
        self.local_engine.setVisible(visible)
        self.api_model_field.setVisible(not visible)
        self._field_row.setStretch(self._field_filler_index, 0 if visible else 2)
        self._local_engine_visible = visible
        self._refresh_engine_status()
        self.model_combo.set_status_visible(visible)

    def _apply_transcription_stretch(self, collapsed: bool):
        if collapsed:
            self.content_layout.setStretchFactor(self.transcription_card, 0)
            self.content_layout.setStretch(self._bottom_stretch_index, 1)
        else:
            self.content_layout.setStretchFactor(self.transcription_card, 1)
            self.content_layout.setStretch(self._bottom_stretch_index, 0)

    def _on_transcription_toggled(self, collapsed: bool):
        self.transcription_collapsed.emit(collapsed, self.transcription_card.content_height)
        QTimer.singleShot(
            SECTION_COLLAPSE_DURATION_MS,
            lambda c=collapsed: self._apply_transcription_stretch(c),
        )

    def set_transcription_collapsed(self, collapsed: bool):
        """Apply collapsed state without persisting or emitting (sync/restore)."""
        self.transcription_card.set_collapsed(collapsed, emit=False)
        self._apply_transcription_stretch(collapsed)

    def is_transcription_collapsed(self) -> bool:
        """Whether the transcription card is currently collapsed."""
        return self.transcription_card.is_collapsed

    def expand_transcription(self) -> None:
        """Expand the transcript card through the normal user-toggle path."""
        if self.is_transcription_collapsed():
            self.transcription_card.set_collapsed(False, emit=True)

    def set_transcript(self, text: str, raw: Optional[str] = None):
        """Display fixed text and an optional distinct raw ASR version."""
        self._fixed_text = text or ""
        self._raw_text = raw if raw and raw != text else None
        self._showing_raw = False

        self._show_version_toggle(self._raw_text is not None)
        self.fixed_btn.blockSignals(True)
        self.raw_btn.blockSignals(True)
        self.fixed_btn.setChecked(True)
        self.raw_btn.setChecked(False)
        self.fixed_btn.blockSignals(False)
        self.raw_btn.blockSignals(False)

        self._show_transcript_text(self._fixed_text)

    def clear_transcription(self):
        self._fixed_text = ""
        self._raw_text = None
        self._showing_raw = False
        self._show_version_toggle(False)
        self.transcript_text.clear()

    def _show_version_toggle(self, visible: bool) -> None:
        """Show or hide the Fixed / Raw switch in the pane's top corner.

        The text edit gives up most of its top padding while the switch is
        up, so the two do not stack a full margin each above the first line.
        """
        self.version_toggle.setVisible(visible)
        text = self.transcript_text
        if bool(text.property("headed")) != visible:
            text.setProperty("headed", visible)
            text.style().unpolish(text)
            text.style().polish(text)

    def set_transcription_stats(
        self,
        transcription_time: float,
        audio_duration: float,
        file_size: int,
        cleanup_time: Optional[float] = None,
    ):
        self.stats_widget.set_stats(
            transcription_time, audio_duration, file_size, cleanup_time
        )

    def clear_transcription_stats(self):
        self.stats_widget.clear()

"""Shared model-selection and transcript tab scaffolding."""
import logging
import time
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QCheckBox, QFrame, QTextEdit,
    QButtonGroup, QPushButton, QScrollArea, QLabel,
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtGui import QFont

from config import config
from services.settings import (
    SETTING_DEFAULTS,
    LEGACY_STREAMING_KEYS,
    SettingsKey,
    api_model_choices,
    api_model_label,
    resolve_api_transcription_model,
    settings_manager,
)
from ui_qt.overlay_state import OverlayState
from ui_qt.utils.collapse_animation import (
    SECTION_COLLAPSE_DURATION_MS,
    UNLIMITED_HEIGHT,
    create_max_height_animation,
    run_max_height_animation,
)
from ui_qt.utils.font_scale import current_ui_font_scale
from ui_qt.utils.markdown_render import PREVIEW_STYLE, render_markdown
from ui_qt.utils.restyle import set_style_property
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
from ui_qt.widgets.remote_link import RemoteLinkGlyph
from ui_qt.widgets.remote_engine_controls import RemoteEngineControls
from ui_qt.widgets.remote_model_notice import RemoteModelNotice
from ui_qt.widgets.wrapped_label import WrappedLabel

logger = logging.getLogger(__name__)

#: Engine-wide busy lines the Remote engine's link replaces with its own.
_LINK_SAYS_IT_BETTER = ("Loading speech engine...", "Reloading speech engine...")

_LIVE_PREVIEW_TIP = (
    "Show draft text near your cursor as you speak.\n"
    "The final transcript may change after you stop.\n"
    "Uses extra processing; requires a supported backend."
)


def _host_status_note(status: str) -> str:
    """What the host's engine status adds to the link tooltip's first line.

    A loaded engine reports ``"model | device (compute)"``, which that line
    already says; only a note after it, such as a GPU fallback, is news.
    """
    summary, _, note = status.partition(" — ")
    return note.strip() if " | " in summary else status


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
    remote_model_selected = pyqtSignal(str, str)  # Paired computer's (family, model)
    remote_runtime_selected = pyqtSignal(str, str, dict)
    remote_retry_requested = pyqtSignal()  # The link was clicked while offline
    help_requested = pyqtSignal(str)
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
        # False for the API backend, which has no engine for the dot to report on.
        self._engine_dot_visible = True
        self._backend_enabled = True
        # The paired computer's models (a RemoteModels); None until the
        # controller first reports them.
        self._remote_models = None
        self._remote_selectable = False
        # The Remote backend's connection (a RemoteLink), shown instead of the
        # status dot while Remote is selected; None until the controller says.
        self._remote_link = None
        self._remote_shown = False
        # Host side: whether a paired computer's request was being served,
        # and how many have been, so each finished one flies home.
        self._serving_busy = False
        self._served = 0
        self._serving_shown = False
        # The saved Live preview choice. The checkbox shows it only while the
        # selected engine can preview, and a recording locks it.
        self._live_preview_wanted = False
        self._live_preview_locked = False
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
        self.api_model_combo = engine_combo(())
        for model in api_model_choices():
            self.api_model_combo.addItem(api_model_label(model), model)
        self.api_model_combo.setToolTip(
            "OpenAI transcription model. Requires an API key in Settings → API keys."
        )
        self.api_model_field = engine_field(
            "Model", self.api_model_combo,
            "Choose the OpenAI speech model that turns your audio into text. Models differ in speed and accuracy. Audio is uploaded to OpenAI; an API key and internet connection are required.",
            [("Open Settings → API keys", "api_keys")], self.help_requested.emit,
        )
        self.api_model_field.hide()
        self.refresh_api_model()

        # The paired computer's own models. Choosing one switches that
        # computer to it, just as choosing it there would.
        self.remote_model_combo = engine_combo(())
        self.remote_model_field = engine_field(
            "Model", self.remote_model_combo,
            "The model the paired computer transcribes with. Choosing another switches that computer to it, as if it were chosen there. Only models already downloaded there are listed.",
            [("Open Settings → Remote engine", "remote_engine")], self.help_requested.emit,
        )
        self.remote_model_field.hide()
        self.remote_engine = RemoteEngineControls()
        self.remote_engine.hide()
        self.remote_engine.runtime_selected.connect(self.remote_runtime_selected)
        self.remote_engine.help_requested.connect(self.help_requested)

        self._field_row = QHBoxLayout()
        self._field_row.setContentsMargins(0, 0, 0, 0)
        self._field_row.setSpacing(10)
        self._field_row.addWidget(engine_field(
            "Backend", self.model_combo,
            "Choose which speech recognition engine turns your audio into text. Local engines process audio on this computer after download. OpenAI sends audio to the cloud and requires an API key. This choice determines the available models.",
            [("Open Settings → Voice model", "ondemand")], self.help_requested.emit,
        ), stretch=2)
        self._field_row.addWidget(self.local_engine, stretch=4)
        self._field_row.addWidget(self.api_model_field, stretch=2)
        self._field_row.addWidget(self.remote_model_field, stretch=2)
        self._field_row.addWidget(self.remote_engine, stretch=2)
        self._field_filler_index = self._field_row.count()
        self._field_row.addStretch(0)
        engine_layout.addLayout(self._field_row)

        self.remote_runtime_label = WrappedLabel("")
        self.remote_runtime_label.setObjectName("engineResolvedLabel")
        self.remote_runtime_label.setTextFormat(Qt.TextFormat.PlainText)
        self.remote_runtime_label.hide()
        engine_layout.addWidget(self.remote_runtime_label)

        # On the footer line, so the Remote card is as tall as the others.
        self.remote_manage_button = RemoteModelNotice()
        self.remote_manage_button.clicked.connect(lambda: self.help_requested.emit("remote_models"))
        self.remote_manage_button.hide()

        self.status_dot = StatusDot(diameter=16)
        # Stands in for the dot while Remote is selected.
        self.link_glyph = RemoteLinkGlyph()
        self.link_glyph.hide()
        self.link_glyph.clicked.connect(self._on_link_clicked)
        # Ticks the "retrying in N s" countdown, only while one is shown.
        self._link_countdown = QTimer(self)
        self._link_countdown.setInterval(1000)
        self._link_countdown.timeout.connect(self._refresh_engine_status)

        self.resolved_label = DownloadsLabel(self.INITIAL_STATUS)
        self.resolved_label.setObjectName("engineResolvedLabel")
        self.resolved_label.downloads_requested.connect(self.engine_downloads_requested)

        self.cleanup_check = QCheckBox("AI cleanup")
        self.cleanup_check.setObjectName("engineCleanupCheck")
        self.cleanup_check.setToolTip(
            "Use your AI model to fix punctuation, remove\n"
            "filler words, and apply your cleanup rules.\n"
            "May change wording and adds processing time."
        )

        self.live_preview_check = QCheckBox("Live preview")
        self.live_preview_check.setObjectName("engineLivePreviewCheck")
        self.live_preview_check.setToolTip(_LIVE_PREVIEW_TIP)
        if not self.LIVE_PREVIEW_CONTROL:
            self.live_preview_check.hide()

        footer_row = QHBoxLayout()
        footer_row.setContentsMargins(0, 0, 0, 0)
        footer_row.setSpacing(6)
        footer_row.addWidget(self.status_dot)
        footer_row.addWidget(self.link_glyph)
        footer_row.addWidget(self.resolved_label, stretch=1)
        footer_row.addWidget(self.remote_manage_button)
        footer_row.addSpacing(6)
        footer_row.addWidget(self.cleanup_check)
        footer_row.addSpacing(6)
        footer_row.addWidget(self.live_preview_check)
        engine_layout.addLayout(footer_row)

        # Host side: which paired computers use this one's engine, slid in
        # below the footer while any is connected.
        self.serving_row = QWidget()
        self.serving_row.setObjectName("remoteServingRow")
        serving_layout = QHBoxLayout(self.serving_row)
        serving_layout.setContentsMargins(0, 2, 0, 0)
        serving_layout.setSpacing(6)
        self.serving_glyph = RemoteLinkGlyph(mirrored=True)
        self.serving_label = QLabel()
        self.serving_label.setObjectName("remoteServingLabel")
        serving_layout.addWidget(self.serving_glyph)
        serving_layout.addWidget(self.serving_label, stretch=1)
        self.serving_row.setMaximumHeight(0)
        self.serving_row.hide()
        self._serving_anim = create_max_height_animation(self.serving_row, self)
        engine_layout.addWidget(self.serving_row)

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
        self.api_model_combo.currentIndexChanged.connect(self._on_api_model_changed)
        # activated, not currentIndexChanged: only a person's choice switches
        # the paired computer, never the field following what it reports.
        self.remote_model_combo.activated.connect(self._on_remote_model_activated)
        self.local_engine.engine_settings_changed.connect(self.engine_settings_changed)
        self.local_engine.help_requested.connect(self.help_requested)
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
            SETTING_DEFAULTS[SettingsKey.TRANSCRIPT_CLEANUP_ENABLED],
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
        self._live_preview_wanted = bool(checked)
        self.live_preview_changed.emit()

    def load_live_preview_setting(self):
        enabled = settings_manager.get(
            SettingsKey.STREAMING_ENABLED,
            SETTING_DEFAULTS[SettingsKey.STREAMING_ENABLED],
        )
        self._live_preview_wanted = bool(enabled)
        self._sync_live_preview()

    def set_live_preview_locked(self, locked: bool) -> None:
        """Lock the toggle, e.g. while recording, without unlocking it early."""
        self._live_preview_locked = locked
        self._sync_live_preview()

    def live_preview_unavailable_reason(self) -> str:
        """Why the selected engine can't preview, or "" when it can."""
        from services.runtime.streaming import preview_unavailable_reason

        backend = config.MODEL_VALUE_MAP.get(self.current_model, "local_whisper")
        models = self._remote_models
        current = models.current if models is not None else None
        return preview_unavailable_reason(
            backend,
            host=models.host if models is not None else "",
            host_family=current.family if current is not None else None,
        )

    def _sync_live_preview(self) -> None:
        """Show the saved choice, or grayed out and unchecked with why.

        An engine that can't preview doesn't change the saved choice, so it
        comes back as it was when the engine switches to one that can.
        """
        reason = self.live_preview_unavailable_reason()
        check = self.live_preview_check
        blocked = check.blockSignals(True)
        check.setChecked(self._live_preview_wanted and not reason)
        check.blockSignals(blocked)
        check.setEnabled(not reason and not self._live_preview_locked)
        if not reason:
            check.setToolTip(_LIVE_PREVIEW_TIP)
        elif self._live_preview_wanted:
            check.setToolTip(f"{reason}.\nIt turns back on when you switch to one.")
        else:
            check.setToolTip(f"{reason}.\nSwitch to one to use it.")

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
        index = self.api_model_combo.findData(model)
        blocked = self.api_model_combo.blockSignals(True)
        self.api_model_combo.setCurrentIndex(max(0, index))
        self.api_model_combo.blockSignals(blocked)

    def _on_api_model_changed(self, _index: int = 0):
        model = self.api_model_combo.currentData()
        if model is None:
            return
        settings_manager.save_setting(SettingsKey.API_TRANSCRIPTION_MODEL, model)
        self.model_changed.emit(self.current_model)

    @staticmethod
    def _has_local_fields(display_name: str) -> bool:
        """Whether the backend runs here, so its model, device and quant apply."""
        return config.MODEL_VALUE_MAP.get(display_name) not in ("api", "remote")

    def _on_backend_changed(self, display_name: str):
        self.current_model = display_name
        self.refresh_api_model()
        self.local_engine.set_backend(config.MODEL_VALUE_MAP.get(display_name, "local_whisper"))
        self.set_local_engine_visible(self._has_local_fields(display_name))
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
        self.set_local_engine_visible(self._has_local_fields(display_name))

    def set_backend_enabled(self, enabled: bool):
        """Lock the backend choice, e.g. while recording."""
        self.model_combo.setEnabled(enabled)
        self.api_model_combo.setEnabled(enabled)
        self._backend_enabled = enabled
        self._sync_remote_model_enabled()

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
        elif status_text and status_text == self._device_info:
            # A reload reports its engine as status too. That is the idle
            # readout already, and for Remote the link says it better.
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
        if self.remote_model_combo.itemData(self.remote_model_combo.currentIndex()) is None:
            # "Connecting..." and "Not connected" follow the reload.
            self._show_remote_models()
        self._sync_remote_model_enabled()
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
        host = self._connected_host()
        if host:
            messages[OverlayState.TRANSCRIBING] = f"Transcribing on {host}..."
        previous = self._activity_state
        self._activity_state = state
        if state in messages:
            self._status_message = messages[state]
        elif self._status_message == messages.get(previous) or (
            previous is OverlayState.TRANSCRIBING and self._status_message.startswith("Transcribing")
        ):
            self._status_message = ""
        self._refresh_engine_status()

    def _connected_host(self) -> str:
        """The paired computer's name while Remote is shown and connected."""
        link = self._remote_link
        if self._remote_shown and link is not None and link.state == "connected":
            return link.host
        return ""

    def _refresh_engine_status(self) -> None:
        busy = self.engine_loading or self._activity_state in (
            OverlayState.PROCESSING, OverlayState.TRANSCRIBING,
            OverlayState.CLEANING, OverlayState.CANCELING,
        )
        idle_message = self._device_info
        link_text = self._link_text() if self._remote_shown else ""
        if link_text and not (self._engine_busy and self._remote_link.state != "connecting"):
            # The link says what the busy state would: "Connecting to jed...".
            idle_message = link_text
        elif self._engine_busy:
            idle_message = "Loading speech engine..."
        elif self._model_downloads:
            idle_message = f"Downloading model '{next(iter(self._model_downloads))}'..."
        message = self._status_message or idle_message
        showing_link = self._remote_shown and self._remote_link is not None
        placeholders = _LINK_SAYS_IT_BETTER + ((self.INITIAL_STATUS,) if self.INITIAL_STATUS else ())
        if link_text and self._status_message in placeholders:
            # "Connecting to jed..." beats a generic loading line; a specific
            # one ("Switching jed to ...") still wins.
            message = link_text
        self.resolved_label.setText(message or self.INITIAL_STATUS)
        self.resolved_label.setAccessibleName(message or self.INITIAL_STATUS)
        self.status_dot.set_status(self._engine_status)
        self.status_dot.set_busy(busy)
        self.status_dot.setVisible(not showing_link and (self._engine_dot_visible or busy))
        self.link_glyph.setVisible(showing_link)
        if showing_link:
            # Otherwise the label keeps its own tooltip, the full message.
            self.resolved_label.setToolTip(self._link_tooltip())
        counting = showing_link and self._remote_link.retry_at is not None
        if counting and not self._link_countdown.isActive():
            self._link_countdown.start()
        elif not counting:
            self._link_countdown.stop()
        self._apply_backend_status(
            EngineStatus.UNKNOWN if self._engine_busy else self._engine_status
        )

    # ---- the Remote engine's link ----

    def set_remote_link(self, link) -> None:
        """The connection to the paired computer (a RemoteLink) changed."""
        self._remote_link = link
        if link is not None:
            self.link_glyph.set_link(link.state, busy=link.busy, replies=link.replies, beat=link.beat)
            self.link_glyph.setToolTip(self._link_tooltip())
        self._sync_remote_model_enabled()
        self._show_remote_runtime()
        self._refresh_engine_status()

    def _show_remote_runtime(self) -> None:
        link = self._remote_link
        show = self._remote_shown and link is not None and link.state == "connected"
        self.remote_runtime_label.setVisible(show)
        if not show:
            return
        device = {"cuda": "NVIDIA GPU", "cpu": "CPU"}.get(link.device, link.device)
        parts = [f"Running on {link.host}: {device or 'device not reported'}"]
        if link.compute_type:
            parts.append(link.compute_type)
        if link.gpu_name and link.device == "cuda":
            parts.append(link.gpu_name)
            if link.gpu_memory_mib:
                parts.append(f"{link.gpu_memory_mib / 1024:g} GB VRAM")
        self.remote_runtime_label.setText(" · ".join(parts))
        self.remote_runtime_label.setToolTip(self._link_tooltip())

    def _link_text(self) -> str:
        link = self._remote_link
        if link is None:
            return ""
        host = link.host or "the paired computer"
        if link.state == "connected":
            parts = [f"Connected to {host}", link.route]
            if link.latency_ms is not None:
                parts.append("<1 ms" if link.latency_ms < 1 else f"{link.latency_ms:.0f} ms")
            return " · ".join(part for part in parts if part)
        if link.state == "connecting":
            return f"Connecting to {host}..."
        if link.state == "unpaired":
            return link.detail or "Pair with a host in Settings → Remote engine."
        # Offline: the first sentence of why, then when it tries again.
        reason = (link.detail or f"Can't reach {host}.").split(". ")[0].rstrip(".") + "."
        if link.retry_at is not None:
            seconds = max(0, round(link.retry_at - time.monotonic()))
            return f"{reason} Trying again in {seconds} s."
        return f"{reason} Click to try again."

    def _link_tooltip(self) -> str:
        link = self._remote_link
        if link is None:
            return ""
        if link.state == "connected":
            engine = link.engine_label or "Its engine"
            where = f" at {link.address}" if link.address else ""
            device = f" on {link.device.upper()}" if link.device else ""
            compute = f" ({link.compute_type})" if link.compute_type else ""
            note = _host_status_note(link.runtime_status)
            detail = f"\n{note}" if note else ""
            return f"{engine}{device}{compute}, served by {link.host}{where}.{detail}"
        if link.state == "offline":
            return f"{link.detail}\nClick the link to try again now.".strip()
        return link.detail

    def _on_link_clicked(self) -> None:
        if self._remote_link is not None and self._remote_link.state == "offline":
            self.remote_retry_requested.emit()

    # ---- host side: the paired computers this one serves ----

    def set_remote_clients(self, clients) -> None:
        """This computer's engine is serving ``clients`` (connected_clients() rows)."""
        names = list(dict.fromkeys(str(c.get("name") or "a paired computer") for c in clients))
        busy = any(c.get("busy") for c in clients)
        if self._serving_busy and not busy:
            self._served += 1
        self._serving_busy = busy
        if names:
            who = names[0] if len(names) == 1 else f"{len(names)} computers"
            self.serving_label.setText(f"Transcribing for {who}" if busy else f"Sharing this engine with {who}")
            self.serving_glyph.set_link("connected", busy=busy, replies=self._served)
            self.serving_row.setToolTip("\n".join(f"{name} is connected" for name in names))
        self._show_serving_row(bool(names))

    def _show_serving_row(self, show: bool) -> None:
        if show == self._serving_shown:
            return
        self._serving_shown = show
        row = self.serving_row
        if show:
            row.show()
            run_max_height_animation(self._serving_anim, start=row.maximumHeight() if row.maximumHeight() < UNLIMITED_HEIGHT else 0,
                                     end=row.sizeHint().height(),
                                     on_finished=lambda: row.setMaximumHeight(UNLIMITED_HEIGHT))
        else:
            run_max_height_animation(self._serving_anim, start=row.height(), end=0,
                                     on_finished=row.hide)

    def _apply_backend_status(self, status: EngineStatus):
        self.model_combo.set_status(status)

    def set_local_engine_visible(self, visible: bool):
        """Switch between the local runtime fields and a single Model field.

        Without local fields, the API backend offers its OpenAI models and the
        Remote backend shows the model the paired computer runs.
        """
        remote = not visible and config.MODEL_VALUE_MAP.get(self.current_model) == "remote"
        self._remote_shown = remote
        self.local_engine.setVisible(visible)
        self.api_model_field.setVisible(not visible and not remote)
        self.remote_model_field.setVisible(remote)
        self.remote_engine.setVisible(remote)
        self.remote_manage_button.setVisible(remote)
        self._show_remote_runtime()
        if remote:
            self._show_remote_models()
        self._field_row.setStretch(self._field_filler_index, 0 if visible or remote else 2)
        # A remote engine is connected or it isn't; the API has no engine here.
        self._engine_dot_visible = visible or remote
        self._refresh_engine_status()
        self.model_combo.set_status_visible(self._engine_dot_visible)
        self._sync_live_preview()

    def set_remote_models(self, choices) -> None:
        """What the paired computer can run, and what it runs now (``RemoteModels``)."""
        self._remote_models = choices
        self.remote_engine.set_state(choices)
        dependencies = (getattr(choices, "runtime", None) or {}).get("dependencies", [])
        missing = [item["label"] for item in dependencies if item.get("installable")]
        self.remote_manage_button.set_available(missing)
        self._show_remote_models()
        # The host's engine decides whether the Remote engine can preview.
        self._sync_live_preview()

    def _current_remote_models(self):
        from transcriber.remote_backend import RemoteModels

        if self._remote_models is not None:
            return self._remote_models
        # Before the controller reports: name the host, if there is one.
        from services.remote_asr.settings import load_client_pairing

        pairing = load_client_pairing()
        return RemoteModels(host=pairing.host_name if pairing else "")

    def _show_remote_models(self) -> None:
        """Rebuild the Model field: the host's models, grouped by engine."""
        state = self._current_remote_models()
        host, models, current = state.host, state.models, state.current
        combo = self.remote_model_combo
        blocked = combo.blockSignals(True)
        combo.clear()
        selectable = False
        others = [entry for entry in models or () if current is None or entry.key != current.key]
        if not host:
            combo.addItem("Not paired", None)
            tip = "Pair with a computer in Settings → Remote engine."
        elif others:
            entries = list(models)
            if current is not None and all(entry.key != current.key for entry in entries):
                entries.insert(0, current)
            if current is None:
                combo.addItem("Choose a model", None)
            previous = None
            for entry in entries:
                if previous is not None and entry.family != previous:
                    combo.insertSeparator(combo.count())
                previous = entry.family
                combo.addItem(entry.label, entry)
            if current is not None:
                combo.setCurrentIndex(next(
                    index for index in range(combo.count())
                    if getattr(combo.itemData(index), "key", None) == current.key
                ))
                tip = f"{host} is running {current.label}. Choosing another model switches {host} to it."
            else:
                tip = f"{host} has no model this computer can use right now. Choose one to switch {host} to it."
            selectable = True
        elif current is not None:
            combo.addItem(current.label, current)
            tip = (
                f"{host} is running {current.label}. Update OpenWhisper there to choose its model from here."
                if models is None else
                f"{host} is running {current.label}, its only downloaded model."
            )
        elif models is not None:
            combo.addItem("No models ready", None)
            tip = f"{host} has no models ready. Open Manage host models to download models and install their runtimes."
        else:
            combo.addItem("Connecting..." if self._engine_busy else "Not connected", None)
            tip = f"{host}'s models show here once this computer connects."
        combo.blockSignals(blocked)
        combo.setToolTip(tip)
        self._remote_selectable = selectable
        self._sync_remote_model_enabled()

    def _sync_remote_model_enabled(self) -> None:
        self.remote_model_combo.setEnabled(
            self._remote_selectable and self._backend_enabled and not self._engine_busy
        )
        disconnected = self._remote_link is not None and self._remote_link.state != "connected"
        self.remote_engine.set_locked(not self._backend_enabled or self._engine_busy or disconnected)

    def _on_remote_model_activated(self, index: int) -> None:
        choice = self.remote_model_combo.itemData(index)
        if choice is None:
            return
        current = self._current_remote_models().current
        if current is not None and current.key == choice.key:
            return
        self.remote_model_selected.emit(choice.family, choice.model)

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
            set_style_property(text, "headed", visible)

    def set_transcription_stats(
        self,
        transcription_time: float,
        audio_duration: float,
        file_size: int,
        cleanup_time: Optional[float] = None,
        remote=None,
    ):
        self.stats_widget.set_stats(
            transcription_time, audio_duration, file_size, cleanup_time, remote=remote
        )

    def clear_transcription_stats(self):
        self.stats_widget.clear()

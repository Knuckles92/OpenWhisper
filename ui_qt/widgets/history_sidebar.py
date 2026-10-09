"""The sidebar animates a single ``sidebarWidth`` property and
emits ``width_animated`` every frame so the main window can resize in lockstep
(one animation clock for both). The inner content is a fixed-width child pinned
in ``resizeEvent`` rather than managed by a layout, so animating the sidebar
width clips/reveals pre-laid-out content instead of re-running layout and text
wrapping on every frame.
"""
import logging
import os
import threading
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QApplication, QLineEdit, QSizePolicy,
    QMessageBox, QCheckBox, QComboBox, QGridLayout,
)
from PyQt6.QtCore import QEvent, Qt, pyqtSignal, QPropertyAnimation, pyqtProperty, QSize, QTimer, QUrl
from PyQt6.QtGui import QFont, QDesktopServices

from config import config
from services.format_utils import format_file_size
from services.history_manager import (
    AI_VERSION,
    ORIGINAL_VERSION,
    HistoryEntry,
    entry_version,
    history_manager,
    is_local_entry,
    remote_history_entry,
)
from services.settings import SETTING_DEFAULTS, SettingsKey, settings_manager
from services.text_llm import profile_display_name
from ui_qt.utils.collapse_animation import (
    SECTION_COLLAPSE_DURATION_MS,
    SECTION_COLLAPSE_EASING,
)
from ui_qt.utils.file_reveal import reveal_in_file_manager
from ui_qt.utils.list_reconcile import HistoryDelivery, reconcile_cards
from ui_qt.widgets.context_menu import context_menu
from ui_qt.widgets.eliding_label import ElidingLabel
from ui_qt.widgets import history_playback
from ui_qt.widgets.history_playback import PlaybackControl
from ui_qt.widgets.history_calendar import CalendarGlyphButton
from ui_qt.widgets.past_meetings_panel import PastMeetingsPanel
from ui_qt.widgets.wrapped_label import WrappedLabel

logger = logging.getLogger(__name__)

_MODEL_DISPLAY_NAMES = {
    'local_whisper': 'Local',
    'api': 'API',
    'api_whisper': 'API',
    'api_gpt4o': 'GPT-4o',
    'api_gpt4o_mini': 'GPT-4o Mini',
}

_CLEANUP_PROVIDER_DISPLAY_NAMES = {
    'openai': 'OpenAI',
    'openrouter': 'OpenRouter',
}


def _format_model_name(model: str) -> str:
    """Format a backend model identifier for compact display.

    Stored values may carry device detail, e.g.
    ``local_whisper (turbo | cuda (float16))`` — reduce to ``Local · turbo``.
    """
    base, _, detail = model.partition('(')
    base = base.strip()
    name = _MODEL_DISPLAY_NAMES.get(base, base)
    detail = detail.rstrip(')').split('|')[0].strip()
    return f"{name} · {detail}" if detail else name


_CLEANUP_LEVEL_LABELS = {
    'light': 'Light',
    'medium': 'Medium',
    'high': 'High',
    'custom': 'Custom',
    'profile': 'Profile',
}


def _cleanup_level_label(entry: HistoryEntry) -> str:
    level = getattr(entry, "cleanup_level", None) or ""
    return _CLEANUP_LEVEL_LABELS.get(level.lower(), "") if isinstance(level, str) else ""


def _cleanup_provider(entry: HistoryEntry, settings=None) -> str:
    provider_id = entry.cleanup_provider or ""
    provider = _CLEANUP_PROVIDER_DISPLAY_NAMES.get(provider_id)
    if not provider and provider_id:
        try:
            provider = profile_display_name(
                provider_id, settings if settings is not None else settings_manager.load_all_settings()
            ) or provider_id
        except Exception:
            provider = provider_id
    return provider or ""


def _format_cleanup_info(entry: HistoryEntry, settings=None) -> str:
    """An entry's cleanup level and model for display: "Medium · gpt-4o-mini".

    Entries from before levels show the provider in the level's place, and
    entries written before the cleanup columns existed may carry raw_text
    without provider/model — report those as plain "Cleaned".
    """
    level = _cleanup_level_label(entry)
    if not entry.cleanup_model:
        return level or "Cleaned"
    if level:
        return f"{level} · {entry.cleanup_model}"
    provider = _cleanup_provider(entry, settings)
    return f"{provider} · {entry.cleanup_model}" if provider else entry.cleanup_model


def _cleanup_tooltip(entry: HistoryEntry, settings=None) -> str:
    if not entry.cleanup_model:
        return "Transcript was cleaned (model not recorded)"
    provider = _cleanup_provider(entry, settings)
    model = f"{provider} · {entry.cleanup_model}" if provider else entry.cleanup_model
    level = _cleanup_level_label(entry)
    return f"Transcript cleaned with {model}" + (f" ({level})" if level else "")


def _entry_was_cleaned(entry: HistoryEntry) -> bool:
    """Whether the text shown is AI cleanup's (incl. legacy entries).

    False once the user chose the original back; the chip says so instead.
    """
    return (
        bool(entry.cleanup_model or entry.raw_text)
        and entry_version(entry) != ORIGINAL_VERSION
    )


def _kind_label(entry: HistoryEntry) -> str:
    """"Command", "Transform · Polish", or "" for a dictation or a file.

    The runtime saves a transform's source name already prefixed
    ("Transform · Polish"); older or synced rows may hold the bare name.
    """
    kind = getattr(entry, "entry_kind", None)
    if kind == "command":
        return "Command"
    if kind == "transform":
        name = (getattr(entry, "source_name", None) or "").strip()
        name = name.removeprefix("Transform · ").strip()
        return f"Transform · {name}" if name else "Transform"
    return ""


def _record_sync():
    from services.remote_records.sync import record_sync

    return record_sync


def _location_chip(entry) -> tuple[str, str]:
    """``(text, tooltip)`` for where an entry is kept, or empty."""
    stored_on = getattr(entry, "stored_on", None)
    if stored_on:
        return f"On {stored_on}", f"Kept on {stored_on}; opened from there"
    also_on = getattr(entry, "also_on", None)
    if also_on:
        return f"Also on {also_on}", f"Kept here and on {also_on}"
    origin = getattr(entry, "origin_device_name", None)
    if origin:
        return f"From {origin}", f"Kept here for {origin}, a paired computer"
    return "", ""


class _ChipFlow(QWidget):
    """Chips in a line that wraps to another when they don't all fit.

    Eliding them all to share one line cut short chips down to "…" at
    large fonts; wrapping keeps each readable, and only a chip wider than a
    whole line is elided.

    Qt asks a History list's every card for its height many times per
    relayout, so the chips are measured once and re-measured only after an
    event that can change their size.
    """

    SPACING = 6
    #: A chip's updateGeometry posts LayoutRequest here only while this is
    #: visible; Show catches a change made while hidden.
    _REMEASURE = frozenset((
        QEvent.Type.LayoutRequest, QEvent.Type.Show,
        QEvent.Type.FontChange, QEvent.Type.StyleChange,
    ))

    def __init__(self, chips, parent=None):
        super().__init__(parent)
        self._chips = list(chips)
        self._measured: tuple[list[int], int] | None = None
        self._heights: dict[int, int] = {}
        for chip in self._chips:
            chip.setParent(self)
        policy = QSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        # Answers hasHeightForWidth in C++; a Python override of it was called
        # thousands of times per History relayout.
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def event(self, event) -> bool:
        if event.type() in self._REMEASURE and self._measured is not None:
            previous, self._measured = self._measured, None
            # Only a real change may invalidate the layouts above: Qt sends
            # these often while a long list is built.
            if self._measure() != previous:
                self._heights.clear()
                self.updateGeometry()
                self._arrange(self.width(), apply=True)
        return super().event(event)

    def _measure(self) -> tuple[list[int], int]:
        """Each chip's width and the shared line height."""
        if self._measured is None:
            for chip in self._chips:
                # The stylesheet sets the font their width is measured in.
                chip.ensurePolished()
            hints = [chip.sizeHint() for chip in self._chips]
            self._measured = (
                [hint.width() for hint in hints],
                max((hint.height() for hint in hints), default=0),
            )
        return self._measured

    def _arrange(self, width: int, apply: bool) -> int:
        widths, line = self._measure()
        x = y = 0
        for chip, natural in zip(self._chips, widths):
            chip_width = min(natural, max(1, width))
            if x and x + chip_width > width:
                x, y = 0, y + line + self.SPACING
            if apply:
                chip.setGeometry(x, y, chip_width, line)
            x += chip_width + self.SPACING
        return y + line

    def heightForWidth(self, width: int) -> int:
        height = self._heights.get(width)
        if height is None:
            if len(self._heights) >= 8:
                self._heights.clear()
            height = self._heights[width] = self._arrange(width, apply=False)
        return height

    def sizeHint(self) -> QSize:
        widths, _line = self._measure()
        width = sum(widths) + self.SPACING * max(0, len(widths) - 1)
        return QSize(width, self.heightForWidth(self.width() or width))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self._measure()[1])

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._arrange(self.width(), apply=True)


class HistoryItemWidget(QFrame):
    clicked = pyqtSignal(str)
    copy_requested = pyqtSignal(str)
    copy_raw_requested = pyqtSignal(str)
    delete_requested = pyqtSignal(str)
    retranscribe_requested = pyqtSignal(str)
    #: ``(entry_id, version)``: show the original or the AI's version.
    version_requested = pyqtSignal(str, str)
    _remote_audio_ready = pyqtSignal(str, str)

    def __init__(self, entry: HistoryEntry, parent=None, *, settings=None):
        super().__init__(parent)
        self.entry = entry
        self._settings = settings
        self._cleanup_info = _format_cleanup_info(entry, settings) if _entry_was_cleaned(entry) else ""
        self._audio_path = None
        self.playback = None
        self._stored_on = getattr(entry, "stored_on", None)
        self._remote_audio_ready.connect(self._on_remote_audio_ready)
        if self.entry.audio_file and not self._stored_on:
            self._audio_path = history_manager.get_recording_path(self.entry.audio_file)
        self.setObjectName("historyItem")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

        self._setup_ui()
        self._apply_style()

    @property
    def _has_audio(self) -> bool:
        if self._stored_on:
            return bool(getattr(self.entry, "remote_audio", False))
        return bool(self._audio_path)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        top_row.setContentsMargins(0, 0, 0, 0)

        self.timestamp_label = QLabel(self.entry.formatted_timestamp)
        self.timestamp_label.setObjectName("historyTimestamp")
        self.timestamp_label.setFont(QFont("Segoe UI", 10))
        self.timestamp_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        top_row.addWidget(
            self.timestamp_label, 0, Qt.AlignmentFlag.AlignVCenter
        )

        if self._has_audio:
            chip_text = (
                format_file_size(self.entry.file_size)
                if self.entry.file_size
                else "Audio"
            )
            audio_chip = QLabel(chip_text)
            audio_chip.setObjectName("historyAudioChip")
            audio_chip.setFont(QFont("Segoe UI", 9))
            audio_chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
            audio_chip.setToolTip("Recording available — play it or transcribe it again")
            audio_chip.setFixedHeight(20)
            top_row.addWidget(audio_chip, 0, Qt.AlignmentFlag.AlignVCenter)

        top_row.addStretch()

        model_badge = QLabel()
        model_badge.setObjectName("historyModelBadge")
        model_badge.setToolTip(self.entry.model)
        model_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        model_badge.setFixedHeight(20)
        # Elide so an unexpected model string can never widen the card beyond
        # the sidebar's fixed-width viewport.
        badge_text = _format_model_name(self.entry.model)
        model_badge.setText(
            model_badge.fontMetrics().elidedText(
                badge_text, Qt.TextElideMode.ElideRight, 120
            )
        )
        model_badge.setMaximumWidth(140)
        top_row.addWidget(model_badge, 0, Qt.AlignmentFlag.AlignVCenter)

        layout.addLayout(top_row)

        # What kind of entry, how AI cleanup changed it, the app it went into
        # and where it is kept share their own row (the top row is already
        # full), wrapping to another when they don't fit.
        chips = []
        kind_text = _kind_label(self.entry)
        if kind_text:
            self.kind_chip = self._chip("historyKindChip", kind_text)
            chips.append(self.kind_chip)
        if _entry_was_cleaned(self.entry):
            self.cleanup_chip = self._chip("historyCleanupChip", f"✦ {self._cleanup_info}")
            self.cleanup_chip.setToolTip(_cleanup_tooltip(self.entry, settings=self._settings))
            chips.append(self.cleanup_chip)
        elif entry_version(self.entry) == ORIGINAL_VERSION:
            self.cleanup_chip = self._chip("historyOriginalChip", "Original")
            self.cleanup_chip.setToolTip("AI edit undone: this is what you said")
            chips.append(self.cleanup_chip)
        app_name = (getattr(self.entry, "app_name", None) or "").strip()
        if app_name:
            self.app_chip = self._chip("historyAppChip", app_name)
            self.app_chip.setToolTip(f"Dictated into {app_name}")
            chips.append(self.app_chip)
        location_text, location_tip = _location_chip(self.entry)
        if location_text:
            self.location_chip = self._chip("historyLocationChip", location_text)
            self.location_chip.setToolTip(location_tip)
            chips.append(self.location_chip)
        if chips:
            self.chips = _ChipFlow(chips)
            layout.addWidget(self.chips)

        source_name = (getattr(self.entry, "source_name", None) or "").strip()
        title = (getattr(self.entry, "title", None) or "").strip()
        if not kind_text:
            # A rewrite's source is its transform or "Quick Record": the
            # kind chip says it better.
            title = title or source_name
        if title:
            self.title_label = WrappedLabel(title)
            self.title_label.setObjectName("historyTitle")
            self.title_label.setFont(QFont("Segoe UI", 12, QFont.Weight.DemiBold))
            self.title_label.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
            )
            layout.addWidget(self.title_label)

        # Preview text is already truncated by HistoryEntry.preview_text, so
        # let it size naturally — a hard maxHeight was clipping glyphs mid-line
        # and making the footer button look like it was cutting the text off.
        self.preview_label = WrappedLabel(self.entry.preview_text)
        self.preview_label.setObjectName("historyPreview")
        self.preview_label.setFont(QFont("Segoe UI", 11))
        self.preview_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self.preview_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        layout.addWidget(self.preview_label)

        if self._has_audio:
            footer = QHBoxLayout()
            footer.setContentsMargins(0, 2, 0, 0)
            footer.setSpacing(8)

            play_button = QPushButton("Play")
            play_button.setObjectName("historyPlayBtn")
            play_button.setCursor(Qt.CursorShape.PointingHandCursor)
            play_button.setFixedHeight(28)
            self.playback = PlaybackControl(self.entry, play_button, self)
            footer.addWidget(play_button)
            footer.addWidget(self.playback.time_label)
            footer.addStretch()

            self.retranscribe_btn = QPushButton("Transcribe again")
            self.retranscribe_btn.setObjectName("retranscribeBtn")
            self.retranscribe_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.retranscribe_btn.setFixedHeight(28)
            self.retranscribe_btn.setToolTip(
                "Run this recording through the current model "
                "using the current AI cleanup setting"
            )
            self.retranscribe_btn.clicked.connect(self._request_retranscribe)
            footer.addWidget(self.retranscribe_btn)
            layout.addLayout(footer)
            layout.addWidget(self.playback.progress)

    def _chip(self, name: str, text: str) -> ElidingLabel:
        chip = ElidingLabel(text)
        chip.setObjectName(name)
        chip.setFont(QFont("Segoe UI", 9))
        chip.setFixedHeight(20)
        return chip

    def _request_retranscribe(self) -> None:
        """Transcribe the recording again, fetching it first when the host keeps it."""
        if not self._stored_on:
            if self._audio_path:
                self.retranscribe_requested.emit(self._audio_path)
            return
        button = getattr(self, "retranscribe_btn", None)
        if button is not None:
            button.setEnabled(False)
            button.setText(f"Getting it from {self._stored_on}…")
        entry_id = self.entry.id

        def fetch() -> None:
            try:
                path = _record_sync().audio_for(entry_id)
            except Exception as exc:
                logger.warning("Could not fetch a recording from the host: %s", exc)
                self._remote_audio_ready.emit("", str(exc) or type(exc).__name__)
                return
            self._remote_audio_ready.emit(path, "")

        threading.Thread(target=fetch, name="history-remote-audio", daemon=True).start()

    def _on_remote_audio_ready(self, path: str, error: str) -> None:
        button = getattr(self, "retranscribe_btn", None)
        if button is not None:
            button.setEnabled(True)
            button.setText("Transcribe again")
            if error:
                button.setToolTip(f"Couldn't get the recording: {error}")
        if path:
            self.retranscribe_requested.emit(path)

    def _apply_style(self):
        self.setStyleSheet("""
            QFrame#historyItem {
                background-color: rgba(@surface-rgb, 0.5);
                border-radius: 12px;
                border: 1px solid rgba(@overlay-rgb, 0.05);
            }
            QFrame#historyItem:hover {
                background-color: rgba(@surface-hover-rgb, 0.6);
                border: 1px solid rgba(@accent-rgb, 0.35);
            }
            QLabel#historyTimestamp {
                color: @text-secondary-strong;
                background-color: transparent;
            }
            QLabel#historyAudioChip {
                background-color: rgba(@overlay-rgb, 0.06);
                color: @text-tertiary;
                border: 1px solid rgba(@overlay-rgb, 0.08);
                border-radius: 6px;
                padding: 0px 8px;
                font-size: 10px;
                font-weight: 500;
            }
            QLabel#historyLocationChip {
                background-color: rgba(@success-rgb, 0.10);
                color: @success-text;
                border: 1px solid rgba(@success-rgb, 0.24);
                border-radius: 6px;
                padding: 0px 8px;
                font-size: 10px;
                font-weight: 600;
            }
            QLabel#historyModelBadge {
                background-color: rgba(@accent-rgb, 0.14);
                color: @accent-soft;
                border: 1px solid rgba(@accent-rgb, 0.25);
                border-radius: 6px;
                padding: 0px 8px;
                font-size: 10px;
                font-weight: 600;
            }
            QLabel#historyCleanupChip {
                background-color: rgba(@purple-rgb, 0.12);
                color: @purple-text;
                border: 1px solid rgba(@purple-rgb, 0.25);
                border-radius: 6px;
                padding: 0px 8px;
                font-size: 10px;
                font-weight: 600;
            }
            QLabel#historyOriginalChip,
            QLabel#historyAppChip {
                background-color: rgba(@overlay-rgb, 0.06);
                color: @text-secondary-strong;
                border: 1px solid rgba(@overlay-rgb, 0.10);
                border-radius: 6px;
                padding: 0px 8px;
                font-size: 10px;
                font-weight: 600;
            }
            QLabel#historyKindChip {
                background-color: rgba(@warning-rgb, 0.12);
                color: @warning-text;
                border: 1px solid rgba(@warning-rgb, 0.28);
                border-radius: 6px;
                padding: 0px 8px;
                font-size: 10px;
                font-weight: 600;
            }
            QPushButton#historyPlayBtn {
                background-color: rgba(@overlay-rgb, 0.06);
                color: @text-body;
                border: 1px solid rgba(@overlay-rgb, 0.10);
                border-radius: 7px;
                padding: 4px 12px 4px 8px;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton#historyPlayBtn:hover {
                background-color: rgba(@accent-rgb, 0.14);
                border: 1px solid rgba(@accent-rgb, 0.35);
            }
            QPushButton#historyPlayBtn:disabled {
                color: @text-muted;
            }
            QLabel#historyPlayTime {
                color: @text-tertiary;
                background-color: transparent;
                font-size: 10px;
            }
            QLabel#historyTitle {
                color: @text;
                background-color: transparent;
            }
            QLabel#historyPreview {
                color: @text-body;
                background-color: transparent;
            }
            QPushButton#retranscribeBtn {
                background-color: rgba(@success-rgb, 0.12);
                color: @success-text-strong;
                border: 1px solid rgba(@success-rgb, 0.28);
                border-radius: 7px;
                padding: 4px 12px;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton#retranscribeBtn:hover {
                background-color: rgba(@success-rgb, 0.22);
                border: 1px solid rgba(@success-rgb, 0.45);
            }
            QPushButton#retranscribeBtn:pressed {
                background-color: rgba(@success-rgb, 0.32);
            }
        """)

    def _show_context_menu(self, pos):
        menu = context_menu(self)

        if self.entry.raw_text:
            copy_shown = menu.addAction("Copy")
            copy_shown.triggered.connect(
                lambda: self.copy_requested.emit(self.entry.id)
            )
            copy_raw = menu.addAction("Copy Raw")
            copy_raw.triggered.connect(
                lambda: self.copy_raw_requested.emit(self.entry.id)
            )
        else:
            copy_action = menu.addAction("Copy Text")
            copy_action.triggered.connect(
                lambda: self.copy_requested.emit(self.entry.id)
            )

        version = entry_version(self.entry)
        if version and is_local_entry(self.entry):
            if version == AI_VERSION:
                switch = menu.addAction("Undo AI edit")
                target = ORIGINAL_VERSION
            else:
                switch = menu.addAction("Use AI version")
                target = AI_VERSION
            switch.triggered.connect(
                lambda: self.version_requested.emit(self.entry.id, target)
            )

        if self._has_audio:
            retranscribe_action = menu.addAction("Transcribe again")
            retranscribe_action.triggered.connect(self._request_retranscribe)

        # Always listed so the menu keeps its shape, but only live for entries
        # whose recording is still on disk.
        show_in_folder_action = menu.addAction("Show in Folder")
        show_in_folder_action.setEnabled(bool(self._audio_path))
        show_in_folder_action.triggered.connect(self._on_show_in_folder)

        menu.addSeparator()

        delete_action = menu.addAction("Delete")
        delete_action.triggered.connect(
            lambda: self.delete_requested.emit(self.entry.id)
        )

        menu.exec(self.mapToGlobal(pos))

    def _on_show_in_folder(self):
        """Reveal this entry's recording; the path can go stale after deletion."""
        if not self._audio_path:
            return
        if not reveal_in_file_manager(self._audio_path):
            logger.warning(
                f"Could not show recording in the file manager: {self._audio_path}"
            )

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            child = self.childAt(event.pos())
            if child is not None and isinstance(child, QPushButton):
                super().mousePressEvent(event)
                return
            self.clicked.emit(self.entry.id)
        super().mousePressEvent(event)


class HistorySidebar(QWidget):
    entry_selected = pyqtSignal(str)
    entry_copied = pyqtSignal(str)
    entry_deleted = pyqtSignal(str)
    retranscribe_requested = pyqtSignal(str)
    past_meeting_selected = pyqtSignal(str)
    past_meeting_copy_requested = pyqtSignal(str)
    past_meeting_delete_requested = pyqtSignal(str, bool)
    past_meetings_clear_requested = pyqtSignal(bool)
    #: The calendar button in either page's header was clicked.
    calendar_requested = pyqtSignal()
    # Emits the sidebar width every animation frame so the owning window can
    # resize in lockstep (keeps the main content area a constant width).
    width_animated = pyqtSignal(int)
    _history_loaded = pyqtSignal(int, str, object, str)
    _remote_deleted = pyqtSignal(str, str)
    #: History waits this long for the host's entries before showing this
    #: computer's alone; the host's are merged in when they arrive.
    REMOTE_MERGE_WAIT_S = 0.4

    COLLAPSED_WIDTH = 0
    EXPANDED_WIDTH = config.MAIN_WINDOW_HISTORY_SIDEBAR_WIDTH
    # Cap rendered history widgets; search still filters the full history.
    MAX_HISTORY_ITEMS = 100

    def __init__(self, parent=None):
        super().__init__(parent)
        self._is_expanded = False
        self._current_width = self.COLLAPSED_WIDTH
        self._refresh_pending = True
        self._meeting_mode = False
        self._history_load_generation = 0
        self._history_cards = {}
        self._history_cancel = threading.Event()
        #: Host-kept entries shown now, by id, for opening, copying, deleting.
        self._remote_entries = {}
        #: Entries kept here and copied to the host: id -> the host's name.
        self._also_on = {}

        self._setup_ui()
        self._setup_meetings_ui()
        self._apply_style()
        self._history_loaded.connect(self._apply_history_results)
        self._remote_deleted.connect(self._on_remote_deleted)

        self.setMinimumWidth(self.COLLAPSED_WIDTH)
        self.setMaximumWidth(self.COLLAPSED_WIDTH)

    def _setup_ui(self):
        self.setObjectName("historySidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        # Fixed-width content pinned manually in resizeEvent (no layout on
        # self). Children are clipped to the parent rect, so animating the
        # sidebar width reveals the content without re-laying it out.
        self.content_widget = QWidget(self)
        self.content_widget.setObjectName("sidebarContent")
        self.content_widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.content_widget.setFixedWidth(self.EXPANDED_WIDTH)

        content_layout = QVBoxLayout(self.content_widget)
        content_layout.setContentsMargins(16, 16, 16, 16)
        content_layout.setSpacing(12)

        header_layout = QHBoxLayout()
        header_layout.setSpacing(8)

        self.menu_btn = QPushButton("☰")
        self.menu_btn.setObjectName("sidebarMenuBtn")
        self.menu_btn.setFixedSize(28, 28)
        self.menu_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.menu_btn.clicked.connect(self._show_header_menu)
        header_layout.addWidget(self.menu_btn)

        self.header_label = QLabel("History")
        self.header_label.setObjectName("sidebarHeader")
        self.header_label.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        header_layout.addWidget(self.header_label)

        header_layout.addStretch()

        self.calendar_btn = CalendarGlyphButton()
        self.calendar_btn.setToolTip("See history on a calendar (Ctrl+Shift+D)")
        self.calendar_btn.clicked.connect(self.calendar_requested.emit)
        header_layout.addWidget(self.calendar_btn)

        content_layout.addLayout(header_layout)

        self.search_input = QLineEdit()
        self.search_input.setObjectName("historySearchInput")
        self.search_input.setPlaceholderText("Search history...")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setFixedHeight(32)
        self.search_input.textChanged.connect(self._on_search_text_changed)
        content_layout.addWidget(self.search_input)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._load_history)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setObjectName("historyScrollArea")

        scroll_content = QWidget()
        # Ignored horizontal policy makes the scroll area size the content to
        # the viewport width even if a child's minimum hint is wider (e.g. an
        # unbreakable URL in a preview) — overflow clips inside its own card
        # instead of pushing the whole panel past the sidebar edge.
        scroll_content.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 0, 6, 0)
        scroll_layout.setSpacing(12)
        scroll_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.history_header = QLabel("HISTORY")
        self.history_header.setObjectName("sectionHeader")
        self.history_header.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
        self.history_header.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        scroll_layout.addWidget(self.history_header)

        self.history_list_layout = QVBoxLayout()
        self.history_list_layout.setSpacing(12)
        scroll_layout.addLayout(self.history_list_layout)

        self.scroll_area.setWidget(scroll_content)
        content_layout.addWidget(self.scroll_area, stretch=1)

        # Single animation drives both the sidebar width and (via
        # width_animated) the window width — shared timing with the other
        # collapsible sections in the app.
        self.animation = QPropertyAnimation(self, b"sidebarWidth")
        self.animation.setDuration(SECTION_COLLAPSE_DURATION_MS)
        self.animation.setEasingCurve(SECTION_COLLAPSE_EASING)
        self.animation.finished.connect(self._on_animation_finished)

    def _setup_meetings_ui(self):
        self.meetings_content_widget = PastMeetingsPanel(self)
        self.meetings_content_widget.setFixedWidth(self.EXPANDED_WIDTH)
        self.meetings_content_widget.meeting_selected.connect(
            self.past_meeting_selected.emit
        )
        self.meetings_content_widget.copy_transcript_requested.connect(
            self.past_meeting_copy_requested.emit
        )
        self.meetings_content_widget.delete_meeting_requested.connect(
            self.past_meeting_delete_requested.emit
        )
        self.meetings_content_widget.clear_meetings_requested.connect(
            self.past_meetings_clear_requested.emit
        )
        self.meetings_content_widget.calendar_requested.connect(
            self.calendar_requested.emit
        )
        self.meetings_content_widget.hide()

    def set_selected_past_meeting(self, meeting_id: str | None) -> None:
        """Highlight the Past Meetings tile that matches the leftover card."""
        self.meetings_content_widget.set_selected_meeting_id(meeting_id)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        geometry = (0, 0, self.EXPANDED_WIDTH, self.height())
        self.content_widget.setGeometry(*geometry)
        if hasattr(self, "meetings_content_widget"):
            self.meetings_content_widget.setGeometry(*geometry)

    def _get_sidebar_width(self):
        return self._current_width

    def _set_sidebar_width(self, width):
        self._current_width = int(width)
        self.setMinimumWidth(self._current_width)
        self.setMaximumWidth(self._current_width)
        self.width_animated.emit(self._current_width)

    sidebarWidth = pyqtProperty(int, _get_sidebar_width, _set_sidebar_width)

    def _on_animation_finished(self):
        target = self.EXPANDED_WIDTH if self._is_expanded else self.COLLAPSED_WIDTH
        self.setMinimumWidth(target)
        self.setMaximumWidth(target)

    def _apply_style(self):
        self.setStyleSheet("""
            QWidget#historySidebar {
                background-color: @bg;
            }
            QWidget#sidebarContent {
                background-color: @bg;
                border-left: 1px solid rgba(@overlay-rgb, 0.08);
            }
            QLabel#sidebarHeader {
                color: @text-heading;
                font-weight: 700;
            }
            QLabel#sectionHeader {
                color: @text-secondary-strong;
                padding-top: 4px;
                letter-spacing: 0.5px;
                text-transform: uppercase;
                font-size: 10px;
                font-weight: 600;
            }
            QPushButton#sidebarMenuBtn {
                background-color: transparent;
                color: @text-secondary;
                border: none;
                border-radius: 14px;
                padding: 0px;
                font-size: 15px;
            }
            QPushButton#sidebarMenuBtn:hover {
                background-color: rgba(@overlay-rgb, 0.1);
                color: @text-heading;
            }
            QLineEdit#historySearchInput {
                background-color: rgba(@surface-rgb, 0.8);
                color: @text;
                border: 1px solid rgba(@overlay-rgb, 0.08);
                border-radius: 8px;
                padding: 4px 10px;
                font-size: 12px;
            }
            QLineEdit#historySearchInput:focus {
                border: 1px solid @accent;
                background-color: rgba(@surface-rgb, 1.0);
            }
            QLineEdit#historySearchInput::placeholder {
                color: @text-muted;
            }
            QScrollArea#historyScrollArea {
                background-color: transparent;
                border: none;
            }
            QScrollArea#historyScrollArea > QWidget > QWidget {
                background-color: transparent;
            }
            QScrollArea#historyScrollArea QScrollBar:vertical {
                background: transparent;
                width: 8px;
                margin: 0px;
            }
            QScrollArea#historyScrollArea QScrollBar::handle:vertical {
                background: rgba(@overlay-rgb, 0.15);
                border-radius: 4px;
                min-height: 30px;
            }
            QScrollArea#historyScrollArea QScrollBar::handle:vertical:hover {
                background: rgba(@overlay-rgb, 0.3);
            }
            QScrollArea#historyScrollArea QScrollBar::add-line:vertical,
            QScrollArea#historyScrollArea QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QScrollArea#historyScrollArea QScrollBar::add-page:vertical,
            QScrollArea#historyScrollArea QScrollBar::sub-page:vertical {
                background: transparent;
            }
        """)

    def expand(self):
        if self._is_expanded:
            return

        self._is_expanded = True

        # Populate BEFORE animating so the first open reveals fully rendered
        # content instead of popping it in after the animation.
        if self._refresh_pending:
            self.refresh()
            self.content_widget.ensurePolished()
            layout = self.content_widget.layout()
            if layout is not None:
                layout.activate()

        self.animation.stop()
        self.animation.setStartValue(self._current_width)
        self.animation.setEndValue(self.EXPANDED_WIDTH)
        self.animation.start()

        logger.debug("Sidebar expanding")

    def collapse(self):
        if not self._is_expanded:
            return

        self._is_expanded = False
        # The cards' Stop buttons are about to go out of reach.
        history_playback.stop_all()

        self.animation.stop()
        self.animation.setStartValue(self._current_width)
        self.animation.setEndValue(self.COLLAPSED_WIDTH)
        self.animation.start()

        logger.debug("Sidebar collapsing")

    def toggle(self):
        if self._is_expanded:
            self.collapse()
        else:
            self.expand()

    def set_meeting_mode(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._meeting_mode:
            return
        self._meeting_mode = enabled
        if enabled:
            history_playback.stop_all()
        self.content_widget.setVisible(not enabled)
        self.meetings_content_widget.setVisible(enabled)
        self._refresh_pending = True
        if self._is_expanded:
            self.refresh()

    @property
    def is_expanded(self) -> bool:
        return self._is_expanded

    def refresh(self):
        """Refresh the active sidebar page (deferred while collapsed)."""
        if not self._is_expanded:
            self._refresh_pending = True
            return

        self._refresh_pending = False
        if self._meeting_mode:
            self.meetings_content_widget.refresh()
        else:
            self._load_history()

    @staticmethod
    def _clear_layout(layout: QVBoxLayout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _make_empty_label(self, message: str) -> QLabel:
        label = QLabel(message)
        label.setStyleSheet("color: @text-muted; font-size: 12px; padding: 8px 0px;")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return label

    def _on_search_text_changed(self, text: str):
        # Invalidate an in-flight result immediately; the debounced replacement
        # query starts after the user pauses typing.
        self._history_load_generation += 1
        self._search_timer.start()

    def _load_history(self):
        query = self.search_input.text().strip().lower()
        self._history_load_generation += 1
        generation = self._history_load_generation
        self._history_cancel.set()
        canceled = threading.Event()
        self._history_cancel = canceled
        self.destroyed.connect(canceled.set)
        delivery = HistoryDelivery()
        delivery.loaded.connect(self._apply_history_results)
        if self.history_list_layout.count() == 0:
            self.history_list_layout.addWidget(
                self._make_empty_label("Loading history…")
            )

        limit = self.MAX_HISTORY_ITEMS + 1

        def load() -> None:
            entries = []
            error = ""
            try:
                if query:
                    entries = history_manager.search_history(query, limit=limit)
                else:
                    entries = history_manager.get_history(limit=limit)
            except Exception as exc:
                logger.error("Failed to load transcription history: %s", exc)
                error = str(exc)
            if canceled.is_set():
                return
            if error:
                delivery.loaded.emit(generation, query, entries, error)
                return
            remote = self._start_remote_listing(query, limit, entries)
            if remote is None:
                delivery.loaded.emit(generation, query, entries, "")
                return
            done, merged = remote
            if not done.wait(self.REMOTE_MERGE_WAIT_S):
                # Show this computer's entries now rather than wait on the host.
                delivery.loaded.emit(generation, query, entries, "")
                while not done.wait(0.1):
                    if canceled.is_set():
                        return
            if not canceled.is_set():
                delivery.loaded.emit(generation, query, merged(), "")

        threading.Thread(
            target=load,
            name="history-sidebar-load",
            daemon=True,
        ).start()

    def _start_remote_listing(self, query: str, limit: int, entries):
        """Ask the host for this computer's entries it keeps, on another thread.

        Returns ``(done, merged)``: ``merged()`` is ``entries`` with the
        host's merged in by time, copies kept on both marked, and a note row
        when the host couldn't be asked. None when there's nothing to ask.
        """
        try:
            records = _record_sync()
            if not records.listing_wanted("dictation"):
                return None
        except Exception:
            logger.debug("Record sync unavailable for History", exc_info=True)
            return None
        done = threading.Event()
        result = {"items": [], "notice": "", "copies": set()}

        def fetch() -> None:
            try:
                result["copies"] = records.copies_on_host("dictation", [e.id for e in entries])
                result["items"] = records.list_remote("dictation", query, limit)
            except Exception as exc:
                result["notice"] = str(exc)
            finally:
                done.set()

        threading.Thread(target=fetch, name="history-remote-load", daemon=True).start()

        def merged():
            local_ids = {entry.id for entry in entries}
            host = ""
            for item in result["items"]:
                host = item.get("stored_on") or host
            try:
                host = host or records.status().host_name
            except Exception:
                pass
            for entry in entries:
                if entry.id in result["copies"]:
                    entry.also_on = host or "the host"
            remote = [remote_history_entry(item) for item in result["items"]
                      if item.get("id") not in local_ids]
            combined = sorted([*entries, *remote], key=lambda e: e.timestamp or "", reverse=True)
            return {"entries": combined[:limit], "notice": result["notice"]}

        return done, merged

    def _apply_history_results(
        self,
        generation: int,
        query: str,
        entries,
        error: str,
    ) -> None:
        if generation != self._history_load_generation:
            return
        if error:
            self._clear_layout(self.history_list_layout)
            self._history_cards = {}
            self.history_header.setText("HISTORY")
            self.history_list_layout.addWidget(
                self._make_empty_label("History could not be loaded")
            )
            return

        notice = ""
        if isinstance(entries, dict):
            # Merged with the host's: see _start_remote_listing.
            notice = str(entries.get("notice") or "")
            entries = entries.get("entries")
        entries = list(entries or [])
        self._remote_entries = {
            entry.id: entry for entry in entries if getattr(entry, "stored_on", None)
        }
        self._also_on = {
            entry.id: entry.also_on for entry in entries if getattr(entry, "also_on", None)
        }
        has_more = len(entries) > self.MAX_HISTORY_ITEMS
        shown = entries[:self.MAX_HISTORY_ITEMS]

        self.history_header.setText(
            (
                f"HISTORY ({len(shown)}{'+' if has_more else ''})"
                if shown else "HISTORY"
            )
        )

        if not shown:
            self._clear_layout(self.history_list_layout)
            self._history_cards = {}
            message = "No matching entries" if query else "No history yet"
            self.history_list_layout.addWidget(self._make_empty_label(message))
            return

        settings = settings_manager.load_all_settings()
        def create(entry):
            item = HistoryItemWidget(entry, settings=settings)
            replaced = self._history_cards.get(entry.id)
            old_playback = replaced[1].playback if replaced is not None else None
            if item.playback is not None and old_playback is not None:
                item.playback.take_over(old_playback)
            item.clicked.connect(self._on_entry_clicked)
            item.copy_requested.connect(self._on_copy_requested)
            item.copy_raw_requested.connect(self._on_copy_raw_requested)
            item.delete_requested.connect(self._on_delete_requested)
            item.retranscribe_requested.connect(self.retranscribe_requested.emit)
            item.version_requested.connect(self._on_version_requested)
            return item

        def fingerprint(entry):
            value = {name: value for name, value in vars(entry).items() if not name.startswith("_")}
            value["cleanup_display"] = _format_cleanup_info(entry, settings)
            if entry.audio_file and not getattr(entry, "stored_on", None):
                value["audio_available"] = bool(history_manager.get_recording_path(entry.audio_file))
            return value

        self._history_cards = reconcile_cards(
            self.history_list_layout, shown, self._history_cards, create, fingerprint
        )
        if notice:
            note = self._make_empty_label(
                f"{notice} Entries kept there show when it is."
                if "reachable" in notice else notice
            )
            note.setObjectName("historyRemoteNotice")
            note.setWordWrap(True)
            self.history_list_layout.insertWidget(0, note)
        if has_more:
            self.history_list_layout.addWidget(
                self._make_empty_label(
                    f"Showing the first {len(shown)} matches — refine your search "
                    "to find older entries"
                )
            )

    def entry_for(self, entry_id: str):
        """This computer's entry, or a host-kept one this list is showing."""
        return history_manager.get_entry_by_id(entry_id) or self._remote_entries.get(entry_id)

    def _on_entry_clicked(self, entry_id: str):
        entry = self.entry_for(entry_id)
        if entry:
            self.entry_selected.emit(entry_id)
            logger.debug(f"Entry selected: {entry_id[:8]}...")

    def _on_copy_requested(self, entry_id: str):
        entry = self.entry_for(entry_id)
        if entry:
            try:
                clipboard = QApplication.clipboard()
                clipboard.setText(entry.text)
                self.entry_copied.emit(entry_id)
                logger.info(f"Copied entry to clipboard: {entry_id[:8]}...")
            except Exception as e:
                logger.error(f"Failed to copy to clipboard: {e}")

    def _on_copy_raw_requested(self, entry_id: str):
        entry = self.entry_for(entry_id)
        if entry and entry.raw_text:
            try:
                clipboard = QApplication.clipboard()
                clipboard.setText(entry.raw_text)
                self.entry_copied.emit(entry_id)
                logger.info(f"Copied raw entry to clipboard: {entry_id[:8]}...")
            except Exception as e:
                logger.error(f"Failed to copy raw text to clipboard: {e}")

    def _on_version_requested(self, entry_id: str, version: str) -> None:
        if history_manager.use_version(entry_id, version) is not None:
            self.refresh()

    def _on_delete_requested(self, entry_id: str):
        delete_audio_file = False
        try:
            should_confirm = settings_manager.get(
                SettingsKey.CONFIRM_HISTORY_ENTRY_DELETE,
                SETTING_DEFAULTS[SettingsKey.CONFIRM_HISTORY_ENTRY_DELETE],
            )
        except Exception as exc:
            logger.warning("Failed to load history deletion preference: %s", exc)
            should_confirm = True

        remote = self._remote_entries.get(entry_id)
        also_on = self._also_on.get(entry_id)
        if should_confirm is not False:
            confirmation = QMessageBox(self)
            confirmation.setIcon(QMessageBox.Icon.Warning)
            confirmation.setWindowTitle("Delete History Entry")
            if remote is not None:
                confirmation.setText(f"Delete this transcription from {remote.stored_on}?")
                confirmation.setInformativeText(
                    "Its recording there is deleted too. This cannot be undone."
                )
            else:
                confirmation.setText("Delete this transcription from history?")
                confirmation.setInformativeText(
                    f"This cannot be undone. Its copy on {also_on} is deleted too."
                    if also_on else "This cannot be undone."
                )
            confirmation.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            confirmation.setDefaultButton(QMessageBox.StandardButton.No)

            audio_choice = None
            entry = None if remote is not None else history_manager.get_entry_by_id(entry_id)
            audio_path = (
                history_manager.get_recording_path(entry.audio_file)
                if entry and entry.audio_file
                else None
            )
            if audio_path:
                audio_label = QLabel("Delete saved audio file too?", confirmation)
                audio_choice = QComboBox(confirmation)
                audio_choice.addItem("No — keep the audio file", False)
                audio_choice.addItem("Yes — permanently delete it", True)
                audio_choice.setToolTip(
                    "Choose whether the recording attached to this transcript "
                    "should also be deleted"
                )

                message_layout = confirmation.layout()
                if isinstance(message_layout, QGridLayout):
                    row = message_layout.rowCount()
                    columns = max(1, message_layout.columnCount())
                    message_layout.addWidget(audio_label, row, 0, 1, columns)
                    message_layout.addWidget(
                        audio_choice,
                        row + 1,
                        0,
                        1,
                        columns,
                    )

            dont_ask_again = QCheckBox("Don't ask me again", confirmation)
            confirmation.setCheckBox(dont_ask_again)

            if confirmation.exec() != QMessageBox.StandardButton.Yes:
                return

            delete_audio_file = bool(
                audio_choice is not None and audio_choice.currentData()
            )
            if dont_ask_again.isChecked():
                try:
                    settings_manager.save_setting(
                        SettingsKey.CONFIRM_HISTORY_ENTRY_DELETE,
                        False,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to save history deletion preference: %s",
                        exc,
                    )

        if remote is not None:
            self.delete_remote_entry(entry_id)
            return
        if history_manager.delete_entry(
            entry_id,
            delete_audio_file=delete_audio_file,
        ):
            self.entry_deleted.emit(entry_id)
            self.refresh()  # Refresh the list
            logger.info(f"Deleted entry: {entry_id[:8]}...")

    def delete_remote_entry(self, entry_id: str) -> None:
        """Delete a host-kept entry there, off the UI thread."""
        def work() -> None:
            try:
                _record_sync().delete_remote("dictation", entry_id)
            except Exception as exc:
                logger.warning("Could not delete a host-kept entry: %s", exc)
                self._remote_deleted.emit(entry_id, str(exc) or type(exc).__name__)
                return
            history_manager.forget_dictation(entry_id)
            self._remote_deleted.emit(entry_id, "")

        threading.Thread(target=work, name="history-remote-delete", daemon=True).start()

    def _on_remote_deleted(self, entry_id: str, error: str) -> None:
        if error:
            QMessageBox.warning(
                self, "Delete History Entry", f"The entry couldn't be deleted: {error}"
            )
            return
        self._remote_entries.pop(entry_id, None)
        self.entry_deleted.emit(entry_id)
        self.refresh()
        logger.info(f"Deleted host-kept entry: {entry_id[:8]}...")

    def _host_clear_note(self) -> str:
        """What a clear here also deletes on the paired host, if anything."""
        try:
            records = _record_sync()
            if not records.listing_wanted("dictation"):
                return ""
            host = records.status().host_name or "the host"
        except Exception:
            return ""
        return f"\n\nThis computer's entries on {host} are deleted too."

    def _show_header_menu(self):
        menu = context_menu(self)

        export_action = menu.addAction("Export history…")
        export_action.triggered.connect(self._on_export_history)

        open_folder_action = menu.addAction("Open recordings folder")
        open_folder_action.triggered.connect(self._on_open_recordings_folder)

        menu.addSeparator()

        clear_action = menu.addAction("Clear history")
        clear_action.triggered.connect(self._on_clear_history)

        clear_all_action = menu.addAction("Clear history + recordings")
        clear_all_action.triggered.connect(self._on_clear_history_and_recordings)

        menu.exec(self.menu_btn.mapToGlobal(self.menu_btn.rect().bottomLeft()))

    def _on_clear_history(self):
        reply = QMessageBox.question(
            self,
            "Clear History",
            "Delete all history entries?\n\nSaved recordings will be kept."
            + self._host_clear_note(),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        history_manager.clear_history()
        self.refresh()

    def _on_clear_history_and_recordings(self):
        reply = QMessageBox.question(
            self,
            "Clear History and Recordings",
            "Delete all history entries AND permanently delete all saved "
            "recordings from disk?\n\nThis cannot be undone."
            + self._host_clear_note(),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        history_manager.clear_history_and_recordings()
        self.refresh()

    def _on_export_history(self):
        from ui_qt.dialogs.history_export_dialog import HistoryExportDialog

        dialog = HistoryExportDialog(self)
        dialog.exec()

    def _on_open_recordings_folder(self):
        folder = os.path.abspath(history_manager.recordings_folder)
        os.makedirs(folder, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))


class HistoryEdgeTab(QPushButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("historyEdgeTab")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedWidth(config.MAIN_WINDOW_HISTORY_EDGE_TAB_WIDTH)
        self.setMinimumHeight(80)
        self._is_expanded = False
        self._shortcut_hint = ""
        self._panel_name = "History"
        self._update_icon()
        self._apply_style()

    def set_expanded(self, expanded: bool):
        self._is_expanded = expanded
        self._update_icon()

    def set_shortcut_hint(self, shortcut: str):
        self._shortcut_hint = f" ({shortcut})" if shortcut else ""
        self._update_icon()

    def set_panel_name(self, name: str) -> None:
        self._panel_name = name
        self._update_icon()

    def _update_icon(self):
        if self._is_expanded:
            self.setText("›")
            self.setToolTip(f"Close {self._panel_name}{self._shortcut_hint}")
        else:
            self.setText("‹")
            self.setToolTip(f"Open {self._panel_name}{self._shortcut_hint}")

    def _apply_style(self):
        self.setStyleSheet("""
            QPushButton#historyEdgeTab {
                background-color: @surface;
                color: @text-secondary;
                border: 1px solid @border;
                border-right: none;
                border-top-left-radius: 8px;
                border-bottom-left-radius: 8px;
                border-top-right-radius: 0px;
                border-bottom-right-radius: 0px;
                font-size: 16px;
                font-weight: bold;
                padding: 0px;
            }
            QPushButton#historyEdgeTab:hover {
                background-color: @surface-hover;
                color: @text;
            }
            QPushButton#historyEdgeTab:pressed {
                background-color: @bg;
            }
        """)

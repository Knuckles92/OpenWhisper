"""
History sidebar widget for displaying transcription history.
Collapsible sidebar panel that slides in/out from the right side of the main window.

Animation design: the sidebar animates a single ``sidebarWidth`` property and
emits ``width_animated`` every frame so the main window can resize in lockstep
(one animation clock for both). The inner content is a fixed-width child pinned
in ``resizeEvent`` rather than managed by a layout, so animating the sidebar
width clips/reveals pre-laid-out content instead of re-running layout and text
wrapping on every frame.
"""
import logging
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QMenu, QApplication, QLineEdit, QSizePolicy,
)
from PyQt6.QtCore import Qt, pyqtSignal, QPropertyAnimation, pyqtProperty, QTimer
from PyQt6.QtGui import QFont

from config import config
from services.format_utils import format_file_size
from services.history_manager import HistoryEntry, history_manager
from ui_qt.utils.collapse_animation import (
    SECTION_COLLAPSE_DURATION_MS,
    SECTION_COLLAPSE_EASING,
)

logger = logging.getLogger(__name__)

_MODEL_DISPLAY_NAMES = {
    'local_whisper': 'Local',
    'api_whisper': 'API',
    'api_gpt4o': 'GPT-4o',
    'api_gpt4o_mini': 'GPT-4o Mini',
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


class HistoryItemWidget(QFrame):
    """Widget displaying a single history entry."""

    clicked = pyqtSignal(str)  # Emits entry_id
    copy_requested = pyqtSignal(str)  # Emits entry_id
    delete_requested = pyqtSignal(str)  # Emits entry_id
    retranscribe_requested = pyqtSignal(str)  # Emits audio file path

    def __init__(self, entry: HistoryEntry, parent=None):
        super().__init__(parent)
        self.entry = entry
        self._audio_path = None
        if self.entry.audio_file:
            self._audio_path = history_manager.get_recording_path(self.entry.audio_file)
        self.setObjectName("historyItem")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

        self._setup_ui()
        self._apply_style()

    def _setup_ui(self):
        """Setup the widget UI."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        # Top row: timestamp, optional audio chip, model badge
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

        if self._audio_path:
            chip_text = (
                format_file_size(self.entry.file_size)
                if self.entry.file_size
                else "Audio"
            )
            audio_chip = QLabel(chip_text)
            audio_chip.setObjectName("historyAudioChip")
            audio_chip.setFont(QFont("Segoe UI", 9))
            audio_chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
            audio_chip.setToolTip("Recording available — can be transcribed again")
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

        # Preview text is already truncated by HistoryEntry.preview_text, so
        # let it size naturally — a hard maxHeight was clipping glyphs mid-line
        # and making the footer button look like it was cutting the text off.
        self.preview_label = QLabel(self.entry.preview_text)
        self.preview_label.setObjectName("historyPreview")
        self.preview_label.setWordWrap(True)
        self.preview_label.setFont(QFont("Segoe UI", 11))
        self.preview_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self.preview_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        layout.addWidget(self.preview_label)

        if self._audio_path:
            footer = QHBoxLayout()
            footer.setContentsMargins(0, 2, 0, 0)
            footer.setSpacing(8)
            footer.addStretch()

            self.retranscribe_btn = QPushButton("Transcribe again")
            self.retranscribe_btn.setObjectName("retranscribeBtn")
            self.retranscribe_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.retranscribe_btn.setFixedHeight(28)
            self.retranscribe_btn.setToolTip(
                "Run this recording through the current model "
                "and copy the new transcript"
            )
            self.retranscribe_btn.clicked.connect(
                lambda: self.retranscribe_requested.emit(self._audio_path)
            )
            footer.addWidget(self.retranscribe_btn)
            layout.addLayout(footer)

    def _apply_style(self):
        """Apply custom styling."""
        self.setStyleSheet("""
            QFrame#historyItem {
                background-color: rgba(44, 44, 46, 0.5);
                border-radius: 12px;
                border: 1px solid rgba(255, 255, 255, 0.05);
            }
            QFrame#historyItem:hover {
                background-color: rgba(58, 58, 60, 0.6);
                border: 1px solid rgba(10, 132, 255, 0.35);
            }
            QLabel#historyTimestamp {
                color: #98989d;
                background-color: transparent;
            }
            QLabel#historyAudioChip {
                background-color: rgba(255, 255, 255, 0.06);
                color: #aeaeb2;
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 6px;
                padding: 0px 8px;
                font-size: 10px;
                font-weight: 500;
            }
            QLabel#historyModelBadge {
                background-color: rgba(10, 132, 255, 0.14);
                color: #6fb1ff;
                border: 1px solid rgba(10, 132, 255, 0.25);
                border-radius: 6px;
                padding: 0px 8px;
                font-size: 10px;
                font-weight: 600;
            }
            QLabel#historyPreview {
                color: #e5e5e7;
                background-color: transparent;
            }
            QPushButton#retranscribeBtn {
                background-color: rgba(48, 209, 88, 0.12);
                color: #32d74b;
                border: 1px solid rgba(48, 209, 88, 0.28);
                border-radius: 7px;
                padding: 4px 12px;
                font-size: 11px;
                font-weight: 600;
            }
            QPushButton#retranscribeBtn:hover {
                background-color: rgba(48, 209, 88, 0.22);
                border: 1px solid rgba(48, 209, 88, 0.45);
            }
            QPushButton#retranscribeBtn:pressed {
                background-color: rgba(48, 209, 88, 0.32);
            }
        """)

    def _show_context_menu(self, pos):
        """Show context menu."""
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: rgba(44, 44, 46, 0.95);
                color: #f5f5f7;
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 10px;
                padding: 6px;
            }
            QMenu::item {
                padding: 8px 28px 8px 14px;
                border-radius: 6px;
                font-size: 13px;
            }
            QMenu::item:selected {
                background-color: #0a84ff;
                color: #ffffff;
            }
            QMenu::separator {
                background-color: rgba(255, 255, 255, 0.08);
                height: 1px;
                margin: 4px 8px;
            }
            QMenu::item:disabled {
                color: #8e8e93;
            }
        """)

        # Model info (non-clickable, full detail including device)
        model_action = menu.addAction(f"Model: {self.entry.model}")
        model_action.setEnabled(False)

        menu.addSeparator()

        # Copy action
        copy_action = menu.addAction("Copy Text")
        copy_action.triggered.connect(lambda: self.copy_requested.emit(self.entry.id))

        # Re-transcribe action (only if audio exists)
        if self._audio_path:
            retranscribe_action = menu.addAction("Transcribe again")
            retranscribe_action.triggered.connect(
                lambda: self.retranscribe_requested.emit(self._audio_path)
            )

        menu.addSeparator()

        # Delete action
        delete_action = menu.addAction("Delete")
        delete_action.triggered.connect(lambda: self.delete_requested.emit(self.entry.id))

        menu.exec(self.mapToGlobal(pos))

    def mousePressEvent(self, event):
        """Handle click to view full transcription."""
        if event.button() == Qt.MouseButton.LeftButton:
            child = self.childAt(event.pos())
            if child is not None and isinstance(child, QPushButton):
                super().mousePressEvent(event)
                return
            self.clicked.emit(self.entry.id)
        super().mousePressEvent(event)


class HistorySidebar(QWidget):
    """Collapsible sidebar showing transcription history."""

    # Signals for Quick Record mode
    entry_selected = pyqtSignal(str)  # Emits entry_id when clicked
    entry_copied = pyqtSignal(str)  # Emits entry_id when copy requested
    entry_deleted = pyqtSignal(str)  # Emits entry_id when delete requested
    retranscribe_requested = pyqtSignal(str)  # Emits audio file path
    # Emits the sidebar width every animation frame so the owning window can
    # resize in lockstep (keeps the main content area a constant width).
    width_animated = pyqtSignal(int)

    COLLAPSED_WIDTH = 0
    EXPANDED_WIDTH = config.MAIN_WINDOW_HISTORY_SIDEBAR_WIDTH
    # Cap rendered history widgets; search still filters the full history.
    MAX_HISTORY_ITEMS = 100

    def __init__(self, parent=None):
        super().__init__(parent)
        self._is_expanded = False
        self._current_width = self.COLLAPSED_WIDTH
        self._refresh_pending = True

        self._setup_ui()
        self._apply_style()

        # Start collapsed - animate min/max width together via sidebarWidth
        self.setMinimumWidth(self.COLLAPSED_WIDTH)
        self.setMaximumWidth(self.COLLAPSED_WIDTH)

    def _setup_ui(self):
        """Setup the sidebar UI."""
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

        # Header with close button
        header_layout = QHBoxLayout()
        header_layout.setSpacing(8)

        self.header_label = QLabel("History")
        self.header_label.setObjectName("sidebarHeader")
        self.header_label.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        header_layout.addWidget(self.header_label)

        header_layout.addStretch()

        self.close_btn = QPushButton("×")  # X symbol
        self.close_btn.setObjectName("sidebarCloseBtn")
        self.close_btn.setFixedSize(28, 28)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.collapse)
        header_layout.addWidget(self.close_btn)

        content_layout.addLayout(header_layout)

        # Search bar for filtering history entries (always visible, above the
        # scrolling sections)
        self.search_input = QLineEdit()
        self.search_input.setObjectName("historySearchInput")
        self.search_input.setPlaceholderText("Search history...")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setFixedHeight(32)
        self.search_input.textChanged.connect(self._on_search_text_changed)
        content_layout.addWidget(self.search_input)

        # Debounce timer so the list isn't rebuilt on every keystroke
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

    def resizeEvent(self, event):
        """Pin the fixed-width content to the left edge at full height."""
        super().resizeEvent(event)
        self.content_widget.setGeometry(0, 0, self.EXPANDED_WIDTH, self.height())

    def _get_sidebar_width(self):
        """Get the current sidebar width."""
        return self._current_width

    def _set_sidebar_width(self, width):
        """Set the sidebar width (used by animation)."""
        self._current_width = int(width)
        self.setMinimumWidth(self._current_width)
        self.setMaximumWidth(self._current_width)
        self.width_animated.emit(self._current_width)

    sidebarWidth = pyqtProperty(int, _get_sidebar_width, _set_sidebar_width)

    def _on_animation_finished(self):
        """Snap to the exact final width when the animation completes."""
        target = self.EXPANDED_WIDTH if self._is_expanded else self.COLLAPSED_WIDTH
        self.setMinimumWidth(target)
        self.setMaximumWidth(target)

    def _apply_style(self):
        """Apply custom styling."""
        self.setStyleSheet("""
            QWidget#historySidebar {
                background-color: #1c1c1e;
            }
            QWidget#sidebarContent {
                background-color: #1c1c1e;
                border-left: 1px solid rgba(255, 255, 255, 0.08);
            }
            QLabel#sidebarHeader {
                color: #ffffff;
                font-weight: 700;
            }
            QLabel#sectionHeader {
                color: #98989d;
                padding-top: 4px;
                letter-spacing: 0.5px;
                text-transform: uppercase;
                font-size: 10px;
                font-weight: 600;
            }
            QPushButton#sidebarCloseBtn {
                background-color: transparent;
                color: #8e8e93;
                border: none;
                border-radius: 14px;
                font-size: 20px;
                font-weight: bold;
            }
            QPushButton#sidebarCloseBtn:hover {
                background-color: rgba(255, 255, 255, 0.1);
                color: #ffffff;
            }
            QLineEdit#historySearchInput {
                background-color: rgba(44, 44, 46, 0.8);
                color: #f5f5f7;
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 8px;
                padding: 4px 10px;
                font-size: 12px;
            }
            QLineEdit#historySearchInput:focus {
                border: 1px solid #0a84ff;
                background-color: rgba(44, 44, 46, 1.0);
            }
            QLineEdit#historySearchInput::placeholder {
                color: #636366;
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
                background: rgba(255, 255, 255, 0.15);
                border-radius: 4px;
                min-height: 30px;
            }
            QScrollArea#historyScrollArea QScrollBar::handle:vertical:hover {
                background: rgba(255, 255, 255, 0.3);
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
        """Expand the sidebar."""
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
        """Collapse the sidebar."""
        if not self._is_expanded:
            return

        self._is_expanded = False

        self.animation.stop()
        self.animation.setStartValue(self._current_width)
        self.animation.setEndValue(self.COLLAPSED_WIDTH)
        self.animation.start()

        logger.debug("Sidebar collapsing")

    def toggle(self):
        """Toggle sidebar visibility."""
        if self._is_expanded:
            self.collapse()
        else:
            self.expand()

    @property
    def is_expanded(self) -> bool:
        """Return whether sidebar is expanded."""
        return self._is_expanded

    def refresh(self):
        """Refresh sidebar content (deferred while collapsed)."""
        if not self._is_expanded:
            self._refresh_pending = True
            return

        self._refresh_pending = False
        self._load_history()

    @staticmethod
    def _clear_layout(layout: QVBoxLayout):
        """Remove and delete all widgets from a layout."""
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _make_empty_label(self, message: str) -> QLabel:
        """Create a styled placeholder label for an empty section."""
        label = QLabel(message)
        label.setStyleSheet("color: #636366; font-size: 12px; padding: 8px 0px;")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return label

    def _on_search_text_changed(self, text: str):
        """Restart the debounce timer on each keystroke."""
        self._search_timer.start()

    def _load_history(self):
        """Load and display transcription history, filtered by the search query."""
        self._clear_layout(self.history_list_layout)

        entries = history_manager.get_history()

        query = self.search_input.text().strip().lower()
        if query:
            entries = [
                entry for entry in entries
                if query in entry.text.lower()
                or query in entry.formatted_timestamp.lower()
            ]

        self.history_header.setText(
            f"HISTORY ({len(entries)})" if entries else "HISTORY"
        )

        if not entries:
            message = "No matching entries" if query else "No history yet"
            self.history_list_layout.addWidget(self._make_empty_label(message))
            return

        shown = entries[:self.MAX_HISTORY_ITEMS]
        for entry in shown:
            item = HistoryItemWidget(entry)
            item.clicked.connect(self._on_entry_clicked)
            item.copy_requested.connect(self._on_copy_requested)
            item.delete_requested.connect(self._on_delete_requested)
            item.retranscribe_requested.connect(self.retranscribe_requested.emit)
            self.history_list_layout.addWidget(item)

        if len(entries) > len(shown):
            self.history_list_layout.addWidget(
                self._make_empty_label(
                    f"Showing {len(shown)} of {len(entries)} — search to find older entries"
                )
            )

    def _on_entry_clicked(self, entry_id: str):
        """Handle history entry click."""
        entry = history_manager.get_entry_by_id(entry_id)
        if entry:
            self.entry_selected.emit(entry_id)
            logger.debug(f"Entry selected: {entry_id[:8]}...")

    def _on_copy_requested(self, entry_id: str):
        """Handle copy request."""
        entry = history_manager.get_entry_by_id(entry_id)
        if entry:
            try:
                clipboard = QApplication.clipboard()
                clipboard.setText(entry.text)
                self.entry_copied.emit(entry_id)
                logger.info(f"Copied entry to clipboard: {entry_id[:8]}...")
            except Exception as e:
                logger.error(f"Failed to copy to clipboard: {e}")

    def _on_delete_requested(self, entry_id: str):
        """Handle delete request."""
        if history_manager.delete_entry(entry_id):
            self.entry_deleted.emit(entry_id)
            self.refresh()  # Refresh the list
            logger.info(f"Deleted entry: {entry_id[:8]}...")


class HistoryEdgeTab(QPushButton):
    """Vertical edge tab button to toggle history sidebar - always visible."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("historyEdgeTab")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedWidth(config.MAIN_WINDOW_HISTORY_EDGE_TAB_WIDTH)
        self.setMinimumHeight(80)
        self._is_expanded = False
        self._shortcut_hint = ""
        self._update_icon()
        self._apply_style()

    def set_expanded(self, expanded: bool):
        """Update the tab state."""
        self._is_expanded = expanded
        self._update_icon()

    def set_shortcut_hint(self, shortcut: str):
        """Show a keyboard shortcut alongside the toggle tooltips.

        Args:
            shortcut: Display string such as "Ctrl+H"; empty hides the hint.
        """
        self._shortcut_hint = f" ({shortcut})" if shortcut else ""
        self._update_icon()

    def _update_icon(self):
        """Update the icon based on expanded state."""
        # Use arrow characters to indicate direction
        if self._is_expanded:
            self.setText("›")  # Arrow pointing right (to collapse)
            self.setToolTip(f"Close History{self._shortcut_hint}")
        else:
            self.setText("‹")  # Arrow pointing left (to expand)
            self.setToolTip(f"Open History{self._shortcut_hint}")

    def _apply_style(self):
        """Apply custom styling."""
        self.setStyleSheet("""
            QPushButton#historyEdgeTab {
                background-color: #2c2c2e;
                color: #8e8e93;
                border: 1px solid #3a3a3c;
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
                background-color: #3a3a3c;
                color: #f5f5f7;
            }
            QPushButton#historyEdgeTab:pressed {
                background-color: #1c1c1e;
            }
        """)

"""
History sidebar widget for displaying transcription history and saved recordings.
Collapsible sidebar panel that slides in/out from the right side of the main window.
"""
import logging
from typing import Optional, Callable, List
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QMenu, QSizePolicy, QApplication
)
from PyQt6.QtCore import Qt, pyqtSignal, QPropertyAnimation, QEasingCurve, QSize
from PyQt6.QtGui import QFont, QCursor

from history_manager import HistoryEntry, RecordingInfo, history_manager


class HistoryItemWidget(QFrame):
    """Widget displaying a single history entry."""
    
    clicked = pyqtSignal(str)  # Emits entry_id
    copy_requested = pyqtSignal(str)  # Emits entry_id
    delete_requested = pyqtSignal(str)  # Emits entry_id
    retranscribe_requested = pyqtSignal(str)  # Emits audio file path
    
    def __init__(self, entry: HistoryEntry, parent=None):
        super().__init__(parent)
        self.entry = entry
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
        layout.setSpacing(8)

        # Top row: timestamp and audio indicator
        top_row = QHBoxLayout()
        top_row.setSpacing(8)

        # Timestamp
        self.timestamp_label = QLabel(self.entry.formatted_timestamp)
        self.timestamp_label.setObjectName("historyTimestamp")
        self.timestamp_label.setFont(QFont("Segoe UI", 10))
        top_row.addWidget(self.timestamp_label)

        # Audio indicator if recording exists
        if self.entry.audio_file:
            audio_indicator = QLabel("🎤")
            audio_indicator.setToolTip("Audio recording available")
            top_row.addWidget(audio_indicator)

        top_row.addStretch()
        layout.addLayout(top_row)

        # Preview text
        self.preview_label = QLabel(self.entry.preview_text)
        self.preview_label.setObjectName("historyPreview")
        self.preview_label.setWordWrap(True)
        self.preview_label.setFont(QFont("Segoe UI", 11))
        self.preview_label.setMaximumHeight(60)
        layout.addWidget(self.preview_label)
    
    def _format_model_name(self, model: str) -> str:
        """Format model name for display."""
        model_display = {
            'local_whisper': 'Local',
            'api_whisper': 'API',
            'api_gpt4o': 'GPT-4o',
            'api_gpt4o_mini': 'GPT-4o Mini'
        }
        return model_display.get(model, model)
    
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
                border: 1px solid rgba(255, 255, 255, 0.1);
            }
            QLabel#historyTimestamp {
                color: #98989d;
                background-color: transparent;
            }
            QLabel#historyPreview {
                color: #e5e5e7;
                background-color: transparent;
                line-height: 1.4;
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

        # Model info (non-clickable)
        model_name = self._format_model_name(self.entry.model)
        model_action = menu.addAction(f"Model: {model_name}")
        model_action.setEnabled(False)

        menu.addSeparator()

        # Copy action
        copy_action = menu.addAction("Copy Text")
        copy_action.triggered.connect(lambda: self.copy_requested.emit(self.entry.id))
        
        # Re-transcribe action (only if audio exists)
        if self.entry.audio_file:
            audio_path = history_manager.get_recording_path(self.entry.audio_file)
            if audio_path:
                retranscribe_action = menu.addAction("Re-transcribe")
                retranscribe_action.triggered.connect(
                    lambda: self.retranscribe_requested.emit(audio_path)
                )
        
        menu.addSeparator()
        
        # Delete action
        delete_action = menu.addAction("Delete")
        delete_action.triggered.connect(lambda: self.delete_requested.emit(self.entry.id))
        
        menu.exec(self.mapToGlobal(pos))
    
    def mousePressEvent(self, event):
        """Handle click to view full transcription."""
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.entry.id)
        super().mousePressEvent(event)


class RecordingItemWidget(QFrame):
    """Widget displaying a saved recording."""
    
    retranscribe_requested = pyqtSignal(str)  # Emits file path
    
    def __init__(self, recording: RecordingInfo, parent=None):
        super().__init__(parent)
        self.recording = recording
        self.setObjectName("recordingItem")
        
        self._setup_ui()
        self._apply_style()
    
    def _setup_ui(self):
        """Setup the widget UI."""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(10)
        
        # Left side: info
        info_layout = QVBoxLayout()
        info_layout.setSpacing(2)
        
        # Timestamp
        self.timestamp_label = QLabel(self.recording.formatted_timestamp)
        self.timestamp_label.setObjectName("recordingTimestamp")
        self.timestamp_label.setFont(QFont("Segoe UI", 11))
        info_layout.addWidget(self.timestamp_label)
        
        # File size
        self.size_label = QLabel(self.recording.formatted_size)
        self.size_label.setObjectName("recordingSize")
        self.size_label.setFont(QFont("Segoe UI", 9))
        info_layout.addWidget(self.size_label)
        
        layout.addLayout(info_layout)
        layout.addStretch()
        
        # Re-transcribe button
        self.retranscribe_btn = QPushButton("Transcribe")
        self.retranscribe_btn.setObjectName("retranscribeBtn")
        self.retranscribe_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.retranscribe_btn.setFixedHeight(32)
        self.retranscribe_btn.clicked.connect(
            lambda: self.retranscribe_requested.emit(self.recording.file_path)
        )
        layout.addWidget(self.retranscribe_btn)
    
    def _apply_style(self):
        """Apply custom styling."""
        self.setStyleSheet("""
            QFrame#recordingItem {
                background-color: rgba(44, 44, 46, 0.5);
                border-radius: 12px;
                border: 1px solid rgba(255, 255, 255, 0.05);
            }
            QLabel#recordingTimestamp {
                color: #e5e5e7;
                background-color: transparent;
            }
            QLabel#recordingSize {
                color: #98989d;
                background-color: transparent;
            }
            QPushButton#retranscribeBtn {
                background-color: rgba(48, 209, 88, 0.15);
                color: #32d74b;
                border: 1px solid rgba(48, 209, 88, 0.3);
                border-radius: 8px;
                padding: 6px 16px;
                font-size: 12px;
                font-weight: 600;
            }
            QPushButton#retranscribeBtn:hover {
                background-color: rgba(48, 209, 88, 0.25);
                border: 1px solid rgba(48, 209, 88, 0.5);
            }
            QPushButton#retranscribeBtn:pressed {
                background-color: rgba(48, 209, 88, 0.35);
            }
        """)


class HistorySidebar(QWidget):
    """Collapsible sidebar showing transcription history and saved recordings."""
    
    # Signals
    entry_selected = pyqtSignal(str)  # Emits entry_id when clicked
    entry_copied = pyqtSignal(str)  # Emits entry_id when copy requested
    entry_deleted = pyqtSignal(str)  # Emits entry_id when delete requested
    retranscribe_requested = pyqtSignal(str)  # Emits audio file path
    
    COLLAPSED_WIDTH = 0
    EXPANDED_WIDTH = 380
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.logger = logging.getLogger(__name__)
        self._is_expanded = False
        
        self._setup_ui()
        self._apply_style()
        
        # Start collapsed
        self.setFixedWidth(self.COLLAPSED_WIDTH)
    
    def _setup_ui(self):
        """Setup the sidebar UI."""
        self.setObjectName("historySidebar")
        
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        # Content container (will be animated)
        self.content_widget = QWidget()
        self.content_widget.setObjectName("sidebarContent")
        content_layout = QVBoxLayout(self.content_widget)
        content_layout.setContentsMargins(16, 16, 16, 16)
        content_layout.setSpacing(16)
        
        # Header with close button
        header_layout = QHBoxLayout()
        header_layout.setSpacing(8)
        
        header_label = QLabel("History")
        header_label.setObjectName("sidebarHeader")
        header_label.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        header_layout.addWidget(header_label)
        
        header_layout.addStretch()
        
        self.close_btn = QPushButton("×")
        self.close_btn.setObjectName("sidebarCloseBtn")
        self.close_btn.setFixedSize(28, 28)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.collapse)
        header_layout.addWidget(self.close_btn)
        
        content_layout.addLayout(header_layout)
        
        # Recordings section
        recordings_header = QLabel("RECENT RECORDINGS")
        recordings_header.setObjectName("sectionHeader")
        recordings_header.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
        content_layout.addWidget(recordings_header)
        
        # Recordings container
        self.recordings_container = QVBoxLayout()
        self.recordings_container.setSpacing(12)
        content_layout.addLayout(self.recordings_container)
        
        # Divider
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setStyleSheet("background-color: rgba(255, 255, 255, 0.06); max-height: 1px; margin: 8px 0px;")
        content_layout.addWidget(divider)
        
        # History section
        history_header = QLabel("TRANSCRIPTION HISTORY")
        history_header.setObjectName("sectionHeader")
        history_header.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
        content_layout.addWidget(history_header)
        
        # Scrollable history list
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setObjectName("historyScrollArea")
        
        self.history_list_widget = QWidget()
        self.history_list_layout = QVBoxLayout(self.history_list_widget)
        self.history_list_layout.setContentsMargins(0, 0, 0, 0)
        self.history_list_layout.setSpacing(12)
        self.history_list_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        
        self.scroll_area.setWidget(self.history_list_widget)
        content_layout.addWidget(self.scroll_area, stretch=1)
        
        main_layout.addWidget(self.content_widget)
        
        # Animation for expand/collapse
        self.animation = QPropertyAnimation(self, b"minimumWidth")
        self.animation.setDuration(250)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
    
    def _apply_style(self):
        """Apply custom styling."""
        self.setStyleSheet("""
            QWidget#historySidebar {
                background-color: #1c1c1e;
                border-left: 1px solid rgba(255, 255, 255, 0.08);
            }
            QWidget#sidebarContent {
                background-color: #1c1c1e;
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
            QScrollArea#historyScrollArea {
                background-color: transparent;
                border: none;
            }
            QScrollArea#historyScrollArea > QWidget > QWidget {
                background-color: transparent;
            }
        """)
    
    def expand(self):
        """Expand the sidebar."""
        if self._is_expanded:
            return
        
        self._is_expanded = True
        self.animation.stop()
        self.animation.setStartValue(self.width())
        self.animation.setEndValue(self.EXPANDED_WIDTH)
        self.animation.start()
        
        # Refresh content when expanding
        self.refresh()
        
        self.logger.debug("Sidebar expanded")
    
    def collapse(self):
        """Collapse the sidebar."""
        if not self._is_expanded:
            return
        
        self._is_expanded = False
        self.animation.stop()
        self.animation.setStartValue(self.width())
        self.animation.setEndValue(self.COLLAPSED_WIDTH)
        self.animation.start()
        
        self.logger.debug("Sidebar collapsed")
    
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
        """Refresh the sidebar content."""
        self._load_recordings()
        self._load_history()
    
    def _load_recordings(self):
        """Load and display saved recordings."""
        # Clear existing items
        while self.recordings_container.count():
            item = self.recordings_container.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        
        recordings = history_manager.get_recordings()
        
        if not recordings:
            no_recordings_label = QLabel("No saved recordings")
            no_recordings_label.setStyleSheet("color: #636366; font-size: 12px;")
            no_recordings_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.recordings_container.addWidget(no_recordings_label)
            return
        
        for recording in recordings:
            item = RecordingItemWidget(recording)
            item.retranscribe_requested.connect(self.retranscribe_requested.emit)
            self.recordings_container.addWidget(item)
    
    def _load_history(self):
        """Load and display transcription history."""
        # Clear existing items
        while self.history_list_layout.count():
            item = self.history_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        
        entries = history_manager.get_history()
        
        if not entries:
            no_history_label = QLabel("No transcription history")
            no_history_label.setStyleSheet("color: #636366; font-size: 12px;")
            no_history_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.history_list_layout.addWidget(no_history_label)
            return
        
        for entry in entries:
            item = HistoryItemWidget(entry)
            item.clicked.connect(self._on_entry_clicked)
            item.copy_requested.connect(self._on_copy_requested)
            item.delete_requested.connect(self._on_delete_requested)
            item.retranscribe_requested.connect(self.retranscribe_requested.emit)
            self.history_list_layout.addWidget(item)
    
    def _on_entry_clicked(self, entry_id: str):
        """Handle history entry click."""
        entry = history_manager.get_entry_by_id(entry_id)
        if entry:
            self.entry_selected.emit(entry_id)
            self.logger.debug(f"Entry selected: {entry_id[:8]}...")
    
    def _on_copy_requested(self, entry_id: str):
        """Handle copy request."""
        entry = history_manager.get_entry_by_id(entry_id)
        if entry:
            try:
                clipboard = QApplication.clipboard()
                clipboard.setText(entry.text)
                self.entry_copied.emit(entry_id)
                self.logger.info(f"Copied entry to clipboard: {entry_id[:8]}...")
            except Exception as e:
                self.logger.error(f"Failed to copy to clipboard: {e}")
    
    def _on_delete_requested(self, entry_id: str):
        """Handle delete request."""
        if history_manager.delete_entry(entry_id):
            self.entry_deleted.emit(entry_id)
            self.refresh()  # Refresh the list
            self.logger.info(f"Deleted entry: {entry_id[:8]}...")


class HistoryToggleButton(QPushButton):
    """Toggle button to show/hide the history sidebar."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setText("History")
        self.setObjectName("historyToggleBtn")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(36)
        
        self._apply_style()
    
    def _apply_style(self):
        """Apply custom styling."""
        self.setStyleSheet("""
            QPushButton#historyToggleBtn {
                background-color: #2c2c2e;
                color: #f5f5f7;
                border: 1px solid #3a3a3c;
                border-radius: 8px;
                padding: 8px 16px;
                font-size: 13px;
                font-weight: 500;
            }
            QPushButton#historyToggleBtn:hover {
                background-color: #3a3a3c;
                border-color: #48484a;
            }
            QPushButton#historyToggleBtn:pressed {
                background-color: #1c1c1e;
            }
        """)


class HistoryEdgeTab(QPushButton):
    """Vertical edge tab button to toggle history sidebar - always visible."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("historyEdgeTab")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedWidth(24)
        self.setMinimumHeight(80)
        self._is_expanded = False
        self._update_icon()
        self._apply_style()
    
    def set_expanded(self, expanded: bool):
        """Update the tab state."""
        self._is_expanded = expanded
        self._update_icon()
    
    def _update_icon(self):
        """Update the icon based on expanded state."""
        # Use arrow characters to indicate direction
        if self._is_expanded:
            self.setText("›")  # Arrow pointing right (to collapse)
            self.setToolTip("Close History")
        else:
            self.setText("‹")  # Arrow pointing left (to expand)
            self.setToolTip("Open History")
    
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


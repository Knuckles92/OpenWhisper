"""
Tabbed content widget for switching between Quick Record and Meeting Mode.
Provides a tab bar with a stacked widget to display the appropriate content.
"""
import logging
from typing import Optional
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QTabBar, QStackedWidget
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont

from services.settings import settings_manager


class TabbedContentWidget(QWidget):
    """Container widget with tab bar and stacked widget for Quick Record / Meeting Mode."""

    # Signals
    tab_changed = pyqtSignal(int)  # Emitted when tab selection changes

    # Tab indices
    TAB_QUICK_RECORD = 0
    TAB_MEETING_MODE = 1

    def __init__(self, parent=None):
        super().__init__(parent)
        self.logger = logging.getLogger(__name__)

        # State
        self._recording_active = False
        self._active_recording_tab = -1  # Which tab has active recording

        self._setup_ui()
        self._apply_style()
        self._connect_signals()
        self._restore_last_tab()

    def _setup_ui(self):
        """Setup the widget UI."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Tab bar
        self.tab_bar = QTabBar()
        self.tab_bar.setObjectName("contentTabBar")
        self.tab_bar.setFont(QFont("Segoe UI", 13, QFont.Weight.DemiBold))
        self.tab_bar.setDrawBase(False)  # Don't draw base line
        self.tab_bar.setExpanding(False)  # Don't expand tabs to fill width

        # Add tabs
        self.tab_bar.addTab("Quick Record")
        self.tab_bar.addTab("Meeting Mode")

        # Center the tab bar
        tab_container = QWidget()
        tab_container_layout = QVBoxLayout(tab_container)
        tab_container_layout.setContentsMargins(24, 16, 24, 8)
        tab_container_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        tab_container_layout.addWidget(self.tab_bar)

        layout.addWidget(tab_container)

        # Stacked widget for content
        self.stack = QStackedWidget()
        self.stack.setObjectName("contentStack")
        layout.addWidget(self.stack, stretch=1)

    def _apply_style(self):
        """Apply custom styling to the tab bar."""
        self.tab_bar.setStyleSheet("""
            QTabBar::tab {
                background-color: transparent;
                color: #8e8e93;
                border: none;
                padding: 12px 24px;
                font-size: 14px;
                font-weight: 600;
                min-width: 120px;
            }
            QTabBar::tab:selected {
                color: #0a84ff;
                border-bottom: 2px solid #0a84ff;
            }
            QTabBar::tab:hover:!selected {
                color: #f5f5f7;
            }
            QTabBar::tab:disabled {
                color: #48484a;
            }
        """)

    def _connect_signals(self):
        """Connect internal signals."""
        self.tab_bar.currentChanged.connect(self._on_tab_changed)

    def _restore_last_tab(self):
        """Restore the last selected tab from settings."""
        try:
            settings = settings_manager.load_all_settings()
            last_tab = settings.get('last_tab_index', self.TAB_QUICK_RECORD)
            if 0 <= last_tab < self.tab_bar.count():
                self.tab_bar.setCurrentIndex(last_tab)
        except Exception as e:
            self.logger.warning(f"Failed to restore last tab: {e}")

    def _on_tab_changed(self, index: int):
        """Handle tab selection change."""
        self.stack.setCurrentIndex(index)

        # Save the selected tab
        try:
            settings_manager.save_setting('last_tab_index', index)
        except Exception as e:
            self.logger.warning(f"Failed to save tab selection: {e}")

        self.tab_changed.emit(index)
        self.logger.debug(f"Tab changed to index {index}")

    def add_tab(self, widget: QWidget, title: str) -> int:
        """Add a widget to the stacked widget.

        Note: The tab bar tabs are created in _setup_ui, this method
        just adds the content widgets to the stack.

        Args:
            widget: The widget to add
            title: Tab title (for logging purposes)

        Returns:
            Index of the added widget
        """
        index = self.stack.addWidget(widget)
        self.logger.debug(f"Added tab '{title}' at index {index}")
        return index

    def sync_stack_with_tab_bar(self):
        """Synchronize the stacked widget with the tab bar selection.

        This method should be called AFTER all tabs have been added via add_tab().
        It fixes a timing issue where _restore_last_tab() sets the tab bar index
        before the stack has any widgets, causing a desync between the visual
        tab selection and the displayed content.
        """
        current_tab = self.tab_bar.currentIndex()
        if self.stack.currentIndex() != current_tab:
            self.logger.debug(
                f"Syncing stack (was {self.stack.currentIndex()}) "
                f"with tab bar (index {current_tab})"
            )
            self.stack.setCurrentIndex(current_tab)

    def current_index(self) -> int:
        """Get the current tab index."""
        return self.tab_bar.currentIndex()

    def set_current_index(self, index: int):
        """Set the current tab index.

        Args:
            index: Tab index to switch to
        """
        if 0 <= index < self.tab_bar.count():
            self.tab_bar.setCurrentIndex(index)

    def set_recording_state(self, is_recording: bool, source_tab: int):
        """Lock or unlock tabs based on recording state.

        When recording is active, the other tab is disabled to prevent
        accidental tab switching that could disrupt the recording.

        Args:
            is_recording: True if recording started, False if stopped
            source_tab: The tab index where recording is active
        """
        self._recording_active = is_recording
        self._active_recording_tab = source_tab if is_recording else -1

        if is_recording:
            # Disable the other tab
            for i in range(self.tab_bar.count()):
                self.tab_bar.setTabEnabled(i, i == source_tab)
        else:
            # Re-enable all tabs
            for i in range(self.tab_bar.count()):
                self.tab_bar.setTabEnabled(i, True)

        self.logger.debug(
            f"Recording state: active={is_recording}, source_tab={source_tab}"
        )

    def is_recording_active(self) -> bool:
        """Check if recording is currently active."""
        return self._recording_active

    def get_active_recording_tab(self) -> int:
        """Get the tab index where recording is active, or -1 if not recording."""
        return self._active_recording_tab

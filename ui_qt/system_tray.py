"""
System Tray Implementation for PyQt6 UI.
Manages system tray icon and menu.
"""
import logging
from typing import Optional, Callable
from PyQt6.QtWidgets import QSystemTrayIcon, QMenu, QApplication
from PyQt6.QtGui import QAction
from PyQt6.QtCore import pyqtSignal

from ui_qt.utils.app_icon import app_icon

logger = logging.getLogger(__name__)


class SystemTrayManager(QSystemTrayIcon):
    """Manages system tray icon and menu."""

    show_requested = pyqtSignal()
    hide_requested = pyqtSignal()
    exit_requested = pyqtSignal()
    toggle_recording = pyqtSignal()

    def __init__(self, main_window=None):
        """Initialize system tray manager."""
        super().__init__()
        self.main_window = main_window

        # Callbacks
        self.on_show: Optional[Callable] = None
        self.on_hide: Optional[Callable] = None
        self.on_exit: Optional[Callable] = None

        self._setup_icon()
        self._setup_menu()
        self._connect_signals()

        self.show()
        logger.info("System tray initialized")

    def _setup_icon(self):
        """Setup the tray icon."""
        self.setIcon(app_icon())

    def _setup_menu(self):
        """Setup the tray context menu."""
        self.menu = QMenu()  # Styled by the app-wide theme's QMenu rules

        # Show action
        show_action = self.menu.addAction("Show")
        show_action.triggered.connect(self._on_show)

        # Hide action
        hide_action = self.menu.addAction("Hide")
        hide_action.triggered.connect(self._on_hide)

        # Toggle recording action
        self.menu.addSeparator()
        toggle_action = self.menu.addAction("Start Recording")
        toggle_action.triggered.connect(self._on_toggle)

        # Settings action
        self.menu.addSeparator()
        settings_action = self.menu.addAction("Settings")
        settings_action.setMenuRole(QAction.MenuRole.NoRole)
        settings_action.triggered.connect(self._on_settings)

        # Exit action
        self.menu.addSeparator()
        exit_action = self.menu.addAction("Exit")
        exit_action.setMenuRole(QAction.MenuRole.NoRole)
        exit_action.triggered.connect(self._on_exit)

        self.setContextMenu(self.menu)

    def _connect_signals(self):
        """Connect signals."""
        self.activated.connect(self._on_activated)

    def _on_activated(self, reason):
        """Handle tray icon activation."""
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._on_show()

    def _on_show(self):
        """Handle show action."""
        if self.main_window:
            self.main_window.restore_from_tray()

        if self.on_show:
            self.on_show()

        self.show_requested.emit()

    def _on_hide(self):
        """Handle hide action."""
        if self.main_window:
            self.main_window.hide()

        if self.on_hide:
            self.on_hide()

        self.hide_requested.emit()

    def _on_toggle(self):
        """Handle toggle recording action."""
        self.toggle_recording.emit()

    def _on_settings(self):
        """Handle settings action."""
        if self.main_window:
            self.main_window.open_settings()

    def _on_exit(self):
        """Handle exit action."""
        if self.on_exit:
            self.on_exit()

        self.exit_requested.emit()
        QApplication.instance().quit()

    def set_recording(self, is_recording: bool):
        """Update the menu based on recording state."""
        for action in self.menu.actions():
            if "Recording" in action.text():
                if is_recording:
                    action.setText("Stop Recording")
                else:
                    action.setText("Start Recording")
                break


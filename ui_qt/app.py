"""
PyQt6 Application base class.
Handles application initialization and event loop management.
"""
import logging
from typing import Optional
from PyQt6.QtWidgets import QApplication, QMainWindow, QStyleFactory
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont

from ui_qt.utils.theme_manager import ThemeManager
from ui_qt.utils.tooltip_filter import RoundedTooltipFilter, SnappyTooltipStyle


class QtApplication:
    """PyQt6 Application wrapper."""

    def __init__(self):
        """Initialize the Qt application."""
        # Set before the QApplication is created so macOS shows the right name
        # in the menu bar and About panel.
        QApplication.setApplicationName("OpenWhisper")
        QApplication.setApplicationDisplayName("OpenWhisper")
        QApplication.setOrganizationName("OpenWhisper")

        self.app = QApplication.instance()
        if self.app is None:
            self.app = QApplication([])

        self.theme_manager = ThemeManager()
        self._tooltip_filter = RoundedTooltipFilter(self.app)
        self.app.installEventFilter(self._tooltip_filter)
        self._setup_tooltip_style()
        self._setup_fonts()
        self._apply_theme()

    def _setup_tooltip_style(self):
        """Wrap the platform style so native tooltips show without the long delay."""
        base_style = QStyleFactory.create(self.app.style().objectName())
        if base_style is not None:
            # Keep a Python reference: without it the proxy can be garbage
            # collected while QApplication still points at it, crashing at exit.
            self._tooltip_style = SnappyTooltipStyle(base_style)
            self.app.setStyle(self._tooltip_style)

    def _setup_fonts(self):
        """Setup default fonts for the application."""
        # Set default font
        default_font = QFont("Segoe UI", 10)
        self.app.setFont(default_font)

    def _apply_theme(self):
        """Apply the current theme stylesheet."""
        stylesheet = self.theme_manager.stylesheet
        if stylesheet:
            self.app.setStyleSheet(stylesheet)

    def set_theme(self, theme_name: str):
        """Change the application theme."""
        self.theme_manager.set_theme(theme_name)
        self._apply_theme()

    def run(self, main_window: Optional[QMainWindow] = None):
        """Start the application event loop."""
        if main_window:
            main_window.show()

        logging.info("Starting PyQt6 event loop")
        return self.app.exec()

    def quit(self):
        """Quit the application."""
        self.app.quit()

    def exit(self, code: int = 0):
        """Exit the application with a code."""
        self.app.exit(code)

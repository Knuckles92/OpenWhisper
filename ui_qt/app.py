"""
PyQt6 Application base class.
Handles application initialization and event loop management.
"""
import logging
from typing import Optional
from PyQt6.QtWidgets import QApplication, QMainWindow
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont

from ui_qt.utils.theme_manager import ThemeManager


class QtApplication:
    """PyQt6 Application wrapper."""

    def __init__(self):
        """Initialize the Qt application."""
        self.app = QApplication.instance()
        if self.app is None:
            self.app = QApplication([])

        self.theme_manager = ThemeManager()
        self._setup_fonts()
        self._apply_theme()

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

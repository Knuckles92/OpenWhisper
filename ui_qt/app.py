"""
PyQt6 Application base class.
Handles application initialization and event loop management.
"""
import logging
import sys
from typing import Optional
from PyQt6.QtWidgets import QApplication, QMainWindow, QStyleFactory
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont

from ui_qt.utils.theme_manager import ThemeManager
from ui_qt.utils.tooltip_filter import RoundedTooltipFilter, SnappyTooltipStyle

APP_NAME = "OpenWhisper"


def _override_macos_bundle_name(name: str) -> None:
    """Make the macOS menu bar show our name when launched via Python.app.

    Dev launches go through the framework ``Python.app`` (see
    ``scripts/openwhisper``) so Accessibility can target a real bundle.
    macOS then labels the Apple menu from that bundle's ``CFBundleName``
    ("Python") unless we rewrite the in-memory Info.plist before
    ``QApplication`` starts.
    """
    if sys.platform != "darwin":
        return

    try:
        from ctypes import c_char_p, c_uint32, c_void_p, cdll, util

        cf_path = util.find_library("CoreFoundation")
        if not cf_path:
            return

        cf = cdll.LoadLibrary(cf_path)
        kCFStringEncodingUTF8 = 0x08000100

        cf.CFBundleGetMainBundle.restype = c_void_p
        cf.CFBundleGetInfoDictionary.argtypes = [c_void_p]
        cf.CFBundleGetInfoDictionary.restype = c_void_p
        cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
        cf.CFStringCreateWithCString.restype = c_void_p
        cf.CFDictionarySetValue.argtypes = [c_void_p, c_void_p, c_void_p]

        bundle = cf.CFBundleGetMainBundle()
        if not bundle:
            return
        info = cf.CFBundleGetInfoDictionary(bundle)
        if not info:
            return

        value = cf.CFStringCreateWithCString(None, name.encode("utf-8"), kCFStringEncodingUTF8)
        if not value:
            return
        for key in (b"CFBundleName", b"CFBundleDisplayName"):
            cf_key = cf.CFStringCreateWithCString(None, key, kCFStringEncodingUTF8)
            if cf_key:
                cf.CFDictionarySetValue(info, cf_key, value)
    except Exception:
        logging.debug("Could not override macOS bundle name", exc_info=True)


class QtApplication:
    """PyQt6 Application wrapper."""

    def __init__(self):
        """Initialize the Qt application."""
        # Set before the QApplication is created so macOS shows the right name
        # in the menu bar and About panel.
        QApplication.setApplicationName(APP_NAME)
        QApplication.setApplicationDisplayName(APP_NAME)
        QApplication.setOrganizationName(APP_NAME)
        _override_macos_bundle_name(APP_NAME)

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

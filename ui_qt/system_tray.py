"""System tray icon and menu."""
import logging
from typing import Optional
from PyQt6.QtWidgets import QSystemTrayIcon, QMenu
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
    meeting_toggle_requested = pyqtSignal()
    meeting_dashboard_requested = pyqtSignal()

    def __init__(self, main_window=None):
        super().__init__()
        self.main_window = main_window
        self._meeting_active = False

        self._setup_icon()
        self._setup_menu()
        self._connect_signals()

        self.show()
        logger.info("System tray initialized")

    def _setup_icon(self):
        self.setIcon(app_icon())

    def _setup_menu(self):
        self.menu = QMenu()

        show_action = self.menu.addAction("Show")
        show_action.triggered.connect(self._on_show)

        hide_action = self.menu.addAction("Hide")
        hide_action.triggered.connect(self._on_hide)

        self.menu.addSeparator()
        self.toggle_action = self.menu.addAction("Start Recording")
        self.toggle_action.triggered.connect(self._on_toggle)

        self.menu.addSeparator()
        self.meeting_toggle_action = self.menu.addAction("Start Meeting")
        self.meeting_toggle_action.triggered.connect(self._on_meeting_toggle)
        self.meeting_dashboard_action = self.menu.addAction("Open Meeting Dashboard")
        self.meeting_dashboard_action.triggered.connect(self._on_meeting_dashboard)
        self.meeting_dashboard_action.setEnabled(False)

        self.menu.addSeparator()
        settings_action = self.menu.addAction("Settings")
        settings_action.setMenuRole(QAction.MenuRole.NoRole)
        settings_action.triggered.connect(self._on_settings)

        self.menu.addSeparator()
        exit_action = self.menu.addAction("Exit")
        exit_action.setMenuRole(QAction.MenuRole.NoRole)
        exit_action.triggered.connect(self._on_exit)

        self.setContextMenu(self.menu)

    def _connect_signals(self):
        self.activated.connect(self._on_activated)

    def _on_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._on_show()

    def _on_show(self):
        if self.main_window:
            self.main_window.restore_from_tray()

        self.show_requested.emit()

    def _on_hide(self):
        if self.main_window:
            self.main_window.hide()

        self.hide_requested.emit()

    def _on_toggle(self):
        self.toggle_recording.emit()

    def _on_meeting_toggle(self):
        self.meeting_toggle_requested.emit()

    def _on_meeting_dashboard(self):
        self.meeting_dashboard_requested.emit()

    def _on_settings(self):
        if self.main_window:
            self.main_window.open_settings()

    def _on_exit(self):
        self.exit_requested.emit()

    def set_recording(self, is_recording: bool):
        if is_recording:
            self.toggle_action.setText("Stop Recording")
        else:
            self.toggle_action.setText("Start Recording")

    def set_meeting_active(
        self, active: bool, dashboard_available: Optional[bool] = None
    ):
        """Update meeting actions, optionally overriding dashboard availability."""
        self._meeting_active = bool(active)
        if self._meeting_active:
            self.meeting_toggle_action.setText("End Meeting")
        else:
            self.meeting_toggle_action.setText("Start Meeting")
        if dashboard_available is None:
            dashboard_available = self._meeting_active
        self.meeting_dashboard_action.setEnabled(bool(dashboard_available))

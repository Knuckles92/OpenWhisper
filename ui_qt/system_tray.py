"""System tray icon and menu."""
import logging
import sys
from typing import Optional
from PyQt6.QtWidgets import QSystemTrayIcon, QMenu
from PyQt6.QtGui import QAction, QCursor, QGuiApplication
from PyQt6.QtCore import Qt, pyqtSignal

from ui_qt.utils.app_icon import app_icon

logger = logging.getLogger(__name__)

# macOS status items never report DoubleClick, and an attached context menu
# swallows the left click to pop itself up. There, a left click restores the
# window and the menu is shown on right/Ctrl-click instead.
_CLICK_TO_RESTORE = sys.platform == "darwin"


class SystemTrayManager(QSystemTrayIcon):
    """Manages system tray icon and menu."""

    show_requested = pyqtSignal()
    hide_requested = pyqtSignal()
    exit_requested = pyqtSignal()
    toggle_recording = pyqtSignal()
    meeting_toggle_requested = pyqtSignal()
    meeting_dashboard_requested = pyqtSignal()
    paste_last_original_requested = pyqtSignal()
    scratchpad_requested = pyqtSignal()
    stats_requested = pyqtSignal()
    # Emitted as the Language submenu opens, so a connected slot can refill
    # it with the current choices before it is drawn.
    language_menu_requested = pyqtSignal(object)

    def __init__(self, main_window=None):
        super().__init__()
        self.main_window = main_window
        self._meeting_active = False
        self.available = bool(QSystemTrayIcon.isSystemTrayAvailable())

        self._setup_icon()
        self._setup_menu()
        self._connect_signals()

        if self.available:
            self.show()
            logger.info("System tray initialized")
        else:
            logger.warning(
                "No system tray is available; close-to-tray and tray controls "
                "are disabled"
            )

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
        self.paste_original_action = self.menu.addAction(
            "Paste original of last dictation"
        )
        self.paste_original_action.triggered.connect(
            self.paste_last_original_requested
        )
        self.language_menu = self.menu.addMenu("Language")
        self.language_menu.aboutToShow.connect(self._on_language_menu_about_to_show)

        self.menu.addSeparator()
        self.meeting_toggle_action = self.menu.addAction("Start Meeting")
        self.meeting_toggle_action.triggered.connect(self._on_meeting_toggle)
        self.meeting_dashboard_action = self.menu.addAction("Open Meeting Dashboard")
        self.meeting_dashboard_action.triggered.connect(self._on_meeting_dashboard)
        self.meeting_dashboard_action.setEnabled(False)

        self.menu.addSeparator()
        self.scratchpad_action = self.menu.addAction("Scratchpad")
        self.scratchpad_action.triggered.connect(self.scratchpad_requested)
        self.stats_action = self.menu.addAction("Stats")
        self.stats_action.triggered.connect(self.stats_requested)
        settings_action = self.menu.addAction("Settings")
        settings_action.setMenuRole(QAction.MenuRole.NoRole)
        settings_action.triggered.connect(self._on_settings)

        self.menu.addSeparator()
        exit_action = self.menu.addAction("Exit")
        exit_action.setMenuRole(QAction.MenuRole.NoRole)
        exit_action.triggered.connect(self._on_exit)

        if not _CLICK_TO_RESTORE:
            self.setContextMenu(self.menu)

    def _connect_signals(self):
        self.activated.connect(self._on_activated)

    def _on_activated(self, reason):
        Reason = QSystemTrayIcon.ActivationReason
        if not _CLICK_TO_RESTORE:
            if reason == Reason.DoubleClick:
                self._on_show()
            return

        # Qt reports a Ctrl-click (the Mac right-click) as a plain Trigger.
        ctrl_click = bool(
            QGuiApplication.queryKeyboardModifiers()
            & Qt.KeyboardModifier.MetaModifier
        )
        if reason == Reason.Context or (reason == Reason.Trigger and ctrl_click):
            self._popup_menu()
        elif reason in (Reason.Trigger, Reason.DoubleClick):
            self._on_show()

    def _popup_menu(self):
        """Show the menu under the status item (it is not attached on macOS)."""
        geometry = self.geometry()
        if geometry.isValid():
            self.menu.popup(geometry.bottomLeft())
        else:
            self.menu.popup(QCursor.pos())

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

    def _on_language_menu_about_to_show(self):
        self.language_menu_requested.emit(self.language_menu)

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

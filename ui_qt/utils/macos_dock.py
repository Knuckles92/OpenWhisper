"""Keep the macOS Dock icon in step with the app's visible windows.

Hiding to the tray only orders the window out, so the Dock icon stayed behind
and clicking it (or its "Show All Windows" item) had nothing to bring back.
Like other menu-bar apps, OpenWhisper leaves the Dock while no regular window
is showing and returns as soon as one is shown again. Only install this when a
tray icon exists; otherwise a hidden window would leave the app unreachable.
"""
import logging

from PyQt6.QtCore import QEvent, QObject, Qt, QTimer
from PyQt6.QtWidgets import QApplication, QWidget

logger = logging.getLogger(__name__)

# Floating overlays (Tool), popup menus, and tooltips never keep the Dock icon.
_DOCK_WINDOW_TYPES = (Qt.WindowType.Window, Qt.WindowType.Dialog)


def _keeps_dock_icon(widget: QWidget) -> bool:
    return widget.isWindow() and widget.windowType() in _DOCK_WINDOW_TYPES


class DockIconSync(QObject):
    """Switch between regular and accessory activation as windows show/hide."""

    def __init__(self, app: QApplication):
        super().__init__(app)
        import AppKit

        self._appkit = AppKit
        self._ns_app = AppKit.NSApplication.sharedApplication()
        app.installEventFilter(self)

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind in (QEvent.Type.Show, QEvent.Type.Hide) and isinstance(obj, QWidget):
            if _keeps_dock_icon(obj):
                if kind == QEvent.Type.Show:
                    # Before the NSWindow orders front, so it opens as a
                    # regular app window rather than an accessory panel.
                    self._set_dock_icon_visible(True)
                else:
                    # Deferred so a window replacing another is counted.
                    QTimer.singleShot(0, self.sync)
        return False

    def sync(self) -> None:
        visible = any(
            _keeps_dock_icon(widget) and widget.isVisible()
            for widget in QApplication.topLevelWidgets()
        )
        self._set_dock_icon_visible(visible)

    def _set_dock_icon_visible(self, visible: bool) -> None:
        AppKit = self._appkit
        policy = (
            AppKit.NSApplicationActivationPolicyRegular
            if visible
            else AppKit.NSApplicationActivationPolicyAccessory
        )
        if self._ns_app.activationPolicy() == policy:
            return
        self._ns_app.setActivationPolicy_(policy)
        if visible:
            # A regular app coming back from accessory needs activating, or
            # its window can open behind the frontmost app.
            self._ns_app.activateIgnoringOtherApps_(True)
        logger.info("Dock icon %s", "shown" if visible else "hidden")


def install_dock_icon_sync(app: QApplication) -> DockIconSync | None:
    try:
        return DockIconSync(app)
    except Exception as e:
        logger.warning(f"Could not sync the Dock icon with window visibility: {e}")
        return None

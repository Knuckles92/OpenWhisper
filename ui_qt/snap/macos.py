"""macOS window plumbing for Snap: the Screen Recording grant,
the cover's window level, and handing the focus back.

Qt's screen grab works on macOS, but without the Screen Recording grant it
returns only the wallpaper and menu bar (and our own windows): other apps'
windows are left out, so Snap asks for the grant before it captures.

Snap briefly hides its own windows before capture: the legacy
``NSWindowSharingNone`` flag does not reliably exclude them on macOS.
The window functions do nothing (returning False or None) unless
Qt runs on Cocoa, or when AppKit can't be reached.
"""

from __future__ import annotations

import logging

from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import QWidget

logger = logging.getLogger(__name__)

#: Above the menu bar (24) and status items (25), so a snap covers them;
#: below pop-up menus (101).
COVER_LEVEL = 26
#: The recording overlay parked above an open snap.
PARKED_LEVEL = COVER_LEVEL + 1
#: The pane holding the Screen Recording grant (named "Screen & System Audio
#: Recording" since macOS 14).
SCREEN_RECORDING_SETTINGS_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
)

#: The app that had the focus before a snap took it; given back after.
_focus_owner = None


def _on_macos() -> bool:
    """Whether windows are Cocoa ones: elsewhere (offscreen tests too) a
    winId is no NSView, and treating it as one would crash."""
    return QGuiApplication.platformName() == "cocoa"


def capture_granted() -> bool:
    """Whether macOS lets us capture other apps' windows (no prompt)."""
    try:
        from Quartz import CGPreflightScreenCaptureAccess

        return bool(CGPreflightScreenCaptureAccess())
    except Exception:
        logger.warning("Could not check the Screen Recording grant", exc_info=True)
        return False


def request_capture() -> bool:
    """Ask for the Screen Recording grant; macOS prompts only the first time.

    After that the request returns False at once and the grant can only be
    given in System Settings (``open_capture_settings``). Either way macOS
    applies a new grant only once OpenWhisper is reopened.
    """
    try:
        from Quartz import CGRequestScreenCaptureAccess

        if CGRequestScreenCaptureAccess():
            return True
    except Exception:
        logger.warning("Could not request the Screen Recording grant", exc_info=True)
        return False
    return False


def open_capture_settings() -> None:
    """Open System Settings at the Screen Recording grant."""
    from PyQt6.QtCore import QUrl
    from PyQt6.QtGui import QDesktopServices

    QDesktopServices.openUrl(QUrl(SCREEN_RECORDING_SETTINGS_URL))


def _ns_window(widget: QWidget):
    """The ``NSWindow`` behind ``widget``'s top-level window."""
    import objc

    view = objc.objc_object(c_void_p=int(widget.window().winId()))
    return view.window()


def set_level(widget: QWidget, level: int) -> int | None:
    """Move ``widget``'s window to ``level``; the level it had, or None on failure."""
    if not _on_macos():
        return None
    try:
        window = _ns_window(widget)
        previous = int(window.level())
        window.setLevel_(level)
    except Exception:
        logger.warning("Setting a window's level failed", exc_info=True)
        return None
    return previous


def cover(widget: QWidget) -> None:
    """Lift a snap's overlay above the menu bar and Dock, on full-screen Spaces too.

    Qt's showFullScreen would open a Space of its own, sliding in with an
    animation and leaving the menu bar's strip out, so the overlay is shown
    at the screen's geometry instead and lifted here.
    """
    if set_level(widget, COVER_LEVEL) is None:
        return
    try:
        import AppKit

        window = _ns_window(widget)
        window.setCollectionBehavior_(
            window.collectionBehavior()
            | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
        )
    except Exception:
        logger.warning("Letting the snap overlay join full-screen Spaces failed", exc_info=True)


def make_nonactivating(widget: QWidget) -> bool:
    """Let ``widget``'s panel take clicks without activating OpenWhisper.

    The app being dictated into keeps the focus, as WS_EX_NOACTIVATE gives
    on Windows. Qt makes Tool windows panels but never sets this style.
    """
    if not _on_macos():
        return False
    try:
        import AppKit

        window = _ns_window(widget)
        if not isinstance(window, AppKit.NSPanel):
            return False
        window.setStyleMask_(
            window.styleMask() | AppKit.NSWindowStyleMaskNonactivatingPanel
        )
    except Exception:
        logger.warning("Making the overlay a non-activating panel failed", exc_info=True)
        return False
    return True


def remember_focus() -> None:
    """Note the frontmost app, before a snap makes OpenWhisper active."""
    global _focus_owner
    if not _on_macos():
        return
    try:
        import AppKit

        front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        mine = AppKit.NSRunningApplication.currentApplication()
        _focus_owner = None if front is None or front.isEqual_(mine) else front
    except Exception:
        logger.warning("Could not tell which app had the focus", exc_info=True)
        _focus_owner = None


def return_focus() -> None:
    """Give the focus back to the app that had it before the snap.

    An accessory app left active with no window shows nothing, and the next
    dictation's paste would have nowhere to go.
    """
    global _focus_owner
    owner, _focus_owner = _focus_owner, None
    if owner is None or not _on_macos() or owner.isTerminated():
        return
    try:
        import AppKit

        app = AppKit.NSApplication.sharedApplication()
        if not app.isActive():
            return
        # Cooperative activation (macOS 14+): we hand it over, they take it.
        if hasattr(app, "yieldActivationToApplication_"):
            app.yieldActivationToApplication_(owner)
        owner.activateWithOptions_(0)
    except Exception:
        logger.warning("Could not give the focus back", exc_info=True)

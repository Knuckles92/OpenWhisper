"""Grabbing the screen under the pointer without our own windows in the shot."""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable

from PyQt6 import sip
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QCursor, QGuiApplication, QImage, QScreen
from PyQt6.QtWidgets import QWidget

from ui_qt.snap import affinity, grim, support

logger = logging.getLogger(__name__)

#: Two frames at 60 Hz: a hidden window is gone from the composed screen by then.
MIN_SETTLE_MS = 34
#: Hyprland plays a close animation from a snapshot of the window (about
#: 150 ms in its default and Omarchy's configs) and grim would capture it
#: mid-fade, so a hidden window gets this long to be gone.
HYPRLAND_SETTLE_MS = 200
_PAINTABLE = (
    QImage.Format.Format_RGB32,
    QImage.Format.Format_ARGB32,
    QImage.Format.Format_ARGB32_Premultiplied,
)


def screen_under_cursor() -> QScreen | None:
    """The screen the user is pointing at: the one Snap captures and covers."""
    if support.uses_grim():
        # Wayland keeps the pointer's global position from clients.
        screen = grim.focused_screen()
        if screen is not None:
            return screen
    return QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()


def _grab_screen(screen: QScreen) -> QImage | None:
    """``screen``'s pixels at device resolution, or None. Tests replace this seam."""
    if support.uses_grim():
        return grim.grab(screen)
    pixmap = screen.grabWindow(0)
    return None if pixmap.isNull() else pixmap.toImage()


def grab(screen: QScreen) -> QImage | None:
    """The frozen screen for a snap, tagged with its pixel ratio; None on failure.

    The ratio comes from the captured size against the screen's logical
    size rather than from the screen alone, so grim's physical-pixel PNG on a
    fractionally scaled output still maps one image pixel to one device pixel.
    """
    try:
        image = _grab_screen(screen)
    except Exception:
        logger.warning("Capturing the screen for a snap failed", exc_info=True)
        return None
    if image is None or image.isNull():
        return None
    if image.format() not in _PAINTABLE:
        image = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    width = screen.geometry().width()
    image.setDevicePixelRatio(
        image.width() / width if width > 0 else screen.devicePixelRatio()
    )
    return image


def settle_ms(screen: QScreen | None) -> int:
    """How long a hidden window takes to leave: two frames, or Hyprland's fade."""
    rate = screen.refreshRate() if screen is not None else 0.0
    two_frames = math.ceil(2000 / rate) if rate >= 24 else MIN_SETTLE_MS
    floor = HYPRLAND_SETTLE_MS if support.uses_grim() else MIN_SETTLE_MS
    return max(two_frames, floor)


def _top_levels(windows: Iterable[QWidget]) -> list[QWidget]:
    found: list[QWidget] = []
    for widget in windows:
        if widget is None or sip.isdeleted(widget):
            continue
        top = widget.window()
        if not any(top is seen for seen in found):
            found.append(top)
    return found


def hide_windows(windows: Iterable[QWidget]) -> list[QWidget]:
    """Hide the windows that would show up in a capture; returns the ones hidden.

    Windows already kept out by affinity stay put (no flicker), as do hidden
    and minimised ones, which are not on screen anyway.
    """
    hidden = []
    for window in _top_levels(windows):
        if (
            window.isVisible()
            and not window.isMinimized()
            and not affinity.is_kept_out(window)
        ):
            window.hide()
            hidden.append(window)
    return hidden


def restore_windows(windows: Iterable[QWidget]) -> None:
    """Show windows hidden for a capture again, without taking the focus back."""
    for window in windows:
        if sip.isdeleted(window):
            continue
        quiet = window.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        window.show()
        window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, quiet)

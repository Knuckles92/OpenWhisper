"""The window a snap of "the window under the pointer" keeps.

That is the window under the pointer, unless the snap was asked for from
our own windows (the walkthrough column's Snap button or menu), or the
pointer is on one of them: then it is the window in front, which kept the
focus because those windows never take it.

Windows asks the window manager (Win32), macOS the window server
(Quartz). Other desktops have no answer here, so their Snap menu leaves
the choice out.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence

from PyQt6 import sip
from PyQt6.QtCore import QPoint, QRect
from PyQt6.QtGui import QCursor, QGuiApplication, QScreen
from PyQt6.QtWidgets import QWidget

logger = logging.getLogger(__name__)


def supported() -> bool:
    """Whether this desktop can say which window is under the pointer."""
    return QGuiApplication.platformName() in ("windows", "cocoa")


def window_under_pointer(screen: QScreen, ours: Sequence[QWidget] = (), *,
                         front: bool = False) -> QRect | None:
    """The window to keep, in ``screen``'s logical pixels from its top left, or None.

    Clipped to ``screen``. ``front`` asks for the window in front, as does
    the pointer on one of ``ours``, our own windows.
    """
    pointer = QCursor.pos()
    front = front or any(
        not sip.isdeleted(widget) and widget.isVisible()
        and widget.window().frameGeometry().contains(pointer)
        for widget in ours
    )
    try:
        if QGuiApplication.platformName() == "windows":
            found = _win32_window(screen, front=front)
        elif QGuiApplication.platformName() == "cocoa":
            found = _quartz_window(screen, pointer, front=front)
        else:
            return None
    except Exception:
        logger.warning("Could not find the window under the pointer", exc_info=True)
        return None
    if found is None:
        return None
    area = QRect(QPoint(0, 0), screen.geometry().size())
    clipped = found.intersected(area)
    return clipped if not clipped.isEmpty() else None


def _win32_window(screen: QScreen, *, front: bool) -> QRect | None:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi")
    user32.WindowFromPoint.argtypes = (wintypes.POINT,)
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.MonitorFromPoint.argtypes = (wintypes.POINT, wintypes.DWORD)
    user32.MonitorFromPoint.restype = wintypes.HMONITOR

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    point = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    if front:
        window = user32.GetForegroundWindow()
    else:
        window = user32.GetAncestor(user32.WindowFromPoint(point), 2)  # GA_ROOT
    if not window:
        return None
    rect = wintypes.RECT()
    # The frame as drawn, without the invisible resize borders.
    if dwmapi.DwmGetWindowAttribute(wintypes.HWND(window), 9, ctypes.byref(rect),
                                    ctypes.sizeof(rect)) != 0:
        user32.GetWindowRect(wintypes.HWND(window), ctypes.byref(rect))
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    monitor = user32.MonitorFromPoint(point, 2)  # MONITOR_DEFAULTTONEAREST
    if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return None
    # Physical pixels from the monitor's corner, then the screen's logical ones.
    origin = info.rcMonitor
    scale = screen.devicePixelRatio() or 1.0
    return QRect(round((rect.left - origin.left) / scale), round((rect.top - origin.top) / scale),
                 round((rect.right - rect.left) / scale), round((rect.bottom - rect.top) / scale))


def _quartz_window(screen: QScreen, pointer: QPoint, *, front: bool) -> QRect | None:
    import AppKit
    from Quartz import (
        CGWindowListCopyWindowInfo,
        kCGNullWindowID,
        kCGWindowListExcludeDesktopElements,
        kCGWindowListOptionOnScreenOnly,
    )

    listed = CGWindowListCopyWindowInfo(
        kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements, kCGNullWindowID,
    ) or []
    mine = os.getpid()

    def bounds(info) -> QRect:
        box = info["kCGWindowBounds"]
        return QRect(int(box["X"]), int(box["Y"]), int(box["Width"]), int(box["Height"]))

    # Front to back; layer 0 is ordinary windows, not menus, docks or panels.
    windows = [info for info in listed
               if info.get("kCGWindowLayer", 0) == 0 and info.get("kCGWindowOwnerPID") != mine
               and info.get("kCGWindowAlpha", 1.0) > 0]
    if front:
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        owner = app.processIdentifier() if app is not None else None
        chosen = next((info for info in windows if info.get("kCGWindowOwnerPID") == owner), None)
    else:
        chosen = next((info for info in windows if bounds(info).contains(pointer)), None)
    if chosen is None:
        return None
    # Global points, which are Qt's logical pixels on macOS.
    return bounds(chosen).translated(-screen.geometry().topLeft())

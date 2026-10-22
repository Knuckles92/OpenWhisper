"""Windows-only window plumbing for Snap: capture exclusion and taking focus.

Windows 10 2004 and later can leave a window out of every screen capture
(``SetWindowDisplayAffinity`` with ``WDA_EXCLUDEFROMCAPTURE``): the user
still sees it, but captured pixels show what is behind it. A window kept
out that way needs no hiding before a snap, so nothing flickers.
"""

from __future__ import annotations

import functools
import logging

from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import QWidget

from ui_qt.snap import support

logger = logging.getLogger(__name__)

WDA_NONE = 0x0
WDA_EXCLUDEFROMCAPTURE = 0x11
#: The native handle the exclusion was applied to. Qt can recreate a
#: window's native handle (new flags, translucency), which drops the
#: exclusion, so a stale handle reads as "not excluded".
_EXCLUDED_HWND = "openwhisperSnapExcludedHwnd"


@functools.cache
def _user32():
    """user32 with typed signatures; a private instance, so others' argtypes stay."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    signatures = {
        "SetWindowDisplayAffinity": ((wintypes.HWND, wintypes.DWORD), wintypes.BOOL),
        "GetForegroundWindow": ((), wintypes.HWND),
        "GetWindowThreadProcessId": ((wintypes.HWND, ctypes.c_void_p), wintypes.DWORD),
        "AttachThreadInput": (
            (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL),
            wintypes.BOOL,
        ),
        "SetForegroundWindow": ((wintypes.HWND,), wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(user32, name)
        function.argtypes = arguments
        function.restype = result
    return user32


@functools.cache
def _kernel32():
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentThreadId.argtypes = ()
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    return kernel32


def keep_out_of_screenshots(window: QWidget, enabled: bool) -> bool:
    """Leave ``window`` out of screen captures (Windows), or put it back.

    Returns True when Windows applied the change. Elsewhere this does nothing
    and returns False, and Snap hides the window for the moment of capture
    instead.
    """
    if support._platform() != "win32":
        return False
    top = window.window()
    hwnd = int(top.winId())
    affinity = WDA_EXCLUDEFROMCAPTURE if enabled else WDA_NONE
    try:
        applied = bool(_user32().SetWindowDisplayAffinity(hwnd, affinity))
    except OSError:
        logger.warning("Setting a window's capture affinity failed", exc_info=True)
        applied = False
    top.setProperty(_EXCLUDED_HWND, hwnd if enabled and applied else 0)
    if not applied:
        # Before Windows 10 2004 there is no exclusion: Snap hides it instead.
        logger.info(
            "Windows did not change the window's capture affinity (%#x)", affinity
        )
    return applied


def is_kept_out(window: QWidget) -> bool:
    """Whether keep_out_of_screenshots currently excludes ``window`` from captures."""
    top = window.window()
    recorded = top.property(_EXCLUDED_HWND) or 0
    if not recorded or top.windowHandle() is None:
        return False
    return int(top.winId()) == recorded


def bring_to_front(window: QWidget) -> None:
    """Make ``window`` the foreground window though another app has the focus.

    Snap opens from a global hotkey while the user works elsewhere, and
    Windows lets only the foreground process hand out focus: without this
    the overlay would cover the screen while Enter went to the other app.
    Attaching to the foreground thread's input for the call is what Qt
    itself does under its AlwaysActivateWindow option, which PyQt can't set.
    """
    if QGuiApplication.platformName() != "windows":
        return
    try:
        _force_foreground(int(window.winId()))
    except OSError:
        logger.warning("Bringing the snap overlay to the front failed", exc_info=True)


def _force_foreground(hwnd: int) -> None:
    user32 = _user32()
    foreground = user32.GetForegroundWindow()
    if foreground == hwnd:
        return
    theirs = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
    ours = _kernel32().GetCurrentThreadId()
    attached = bool(
        theirs and theirs != ours and user32.AttachThreadInput(theirs, ours, True)
    )
    try:
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(theirs, ours, False)

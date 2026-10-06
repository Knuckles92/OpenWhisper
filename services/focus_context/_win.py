"""The foreground app on Windows, from plain Win32 calls (no UI Automation).

Takes well under a millisecond, so it runs on whichever thread asks. The
window title is read into memory only to name a browser's site.
"""

from __future__ import annotations

import ctypes
import ntpath
import os
from ctypes import wintypes
from functools import lru_cache
from typing import Optional

from services.focus_context import AppIdentity, catalog

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_UWP_FRAME_HOST = "applicationframehost.exe"
_UWP_CORE_WINDOW = "Windows.UI.Core.CoreWindow"
_TITLE_CHARS = 512


class _Win32:
    """Private DLL handles with their own prototypes.

    Setting argtypes on the shared ``ctypes.windll.user32`` would change it
    for the keyboard library's hook, which uses that object too.
    """

    def __init__(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.EnumChildWindows.argtypes = [wintypes.HWND, self.enum_proc, wintypes.LPARAM]
        user32.EnumChildWindows.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        self.user32 = user32
        self.kernel32 = kernel32

    def window_pid(self, hwnd) -> int:
        pid = wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def class_name(self, hwnd) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        length = self.user32.GetClassNameW(hwnd, buffer, len(buffer))
        return buffer.value[:length] if length > 0 else ""

    def title(self, hwnd) -> str:
        # Another process's caption comes from the window structure without
        # a message to that process, so a hung app cannot block this.
        buffer = ctypes.create_unicode_buffer(_TITLE_CHARS + 1)
        length = self.user32.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value[:length] if length > 0 else ""

    def image_name(self, pid: int) -> str:
        """The lowercase executable name of ``pid``, or "" when access is denied."""
        if pid <= 0:
            return ""
        handle = self.kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            if not self.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return ""
            return ntpath.basename(buffer.value[:size.value]).lower()
        finally:
            self.kernel32.CloseHandle(handle)

    def uwp_app_window(self, frame, frame_pid: int) -> tuple[Optional[int], int]:
        """The packaged app's CoreWindow under an ApplicationFrameHost frame."""
        found: list[tuple[int, int]] = []

        def visit(child, _lparam):
            if self.class_name(child) == _UWP_CORE_WINDOW:
                pid = self.window_pid(child)
                if pid and pid != frame_pid:
                    found.append((child, pid))
                    return False
            return True

        self.user32.EnumChildWindows(frame, self.enum_proc(visit), 0)
        return found[0] if found else (None, 0)


@lru_cache(maxsize=1)
def _win32() -> _Win32:
    return _Win32()


def _hwnd_value(hwnd) -> int:
    return int(getattr(hwnd, "value", hwnd) or 0)


def foreground_identity() -> Optional[AppIdentity]:
    """The app owning the foreground window, or None during focus changes."""
    win = _win32()
    hwnd = win.user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = win.window_pid(hwnd)
    if not pid:
        return None
    if pid == os.getpid():
        return AppIdentity("openwhisper", "OpenWhisper", pid=pid,
                           window=hex(_hwnd_value(hwnd)), is_self=True, platform="windows")
    exe = win.image_name(pid)
    if exe == _UWP_FRAME_HOST:
        # Store apps draw inside a frame owned by ApplicationFrameHost; the
        # app itself is the process behind the frame's CoreWindow child.
        child, child_pid = win.uwp_app_window(hwnd, pid)
        if child is not None:
            child_exe = win.image_name(child_pid)
            if child_exe:
                pid, exe = child_pid, child_exe
    if not exe:
        return None
    hint = catalog.title_hint(exe, win.title(hwnd)) if catalog.is_browser(exe) else ""
    return AppIdentity(
        exe,
        catalog.app_name(exe),
        pid=pid,
        window=hex(_hwnd_value(hwnd)),
        title_hint=hint,
        platform="windows",
    )

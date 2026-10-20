"""A picture of the focused window, for an agent action that asks for one.

Taken as the action's recording starts, on its own thread, and kept in
memory as PNG bytes until the action runs: an ask attaches the bytes, a
handoff saves them to a file in :func:`folder` and names that file in the
prompt. Saved files older than a day are removed on the next save; a
session the user is still in may still be reading a newer one.

Never taken of OpenWhisper's own windows or of an app excluded from app
awareness, and only for a job the user started (``begin_job``). Windows
draws the window itself (``PrintWindow``), so windows above it, such as
the recording overlay, are left out. Hyprland copies the window's area of
the screen with ``grim``. Other desktops have no capture yet.
"""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from functools import lru_cache
from typing import Mapping, Optional

from services.focus_context import AppIdentity, catalog

logger = logging.getLogger(__name__)

#: How long an action waits for a capture still running when it starts.
WAIT_S = 3.0
#: The longer side is scaled down to this: agents downscale larger pictures
#: anyway, and a 4K window's PNG can pass an API's per-image limit.
MAX_EDGE_PX = 1920
#: Saved files older than this are removed on the next save.
KEEP_S = 24 * 3600
_GRIM_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class WindowImage:
    png: bytes
    #: The app's display name, for the prompt.
    app: str


def supported() -> bool:
    """Whether this desktop can capture the focused window."""
    return _platform() is not None


def request(settings: Mapping) -> Future:
    """Start capturing the focused window; resolves to a WindowImage or None.

    Never raises. A window that can't be captured, or mustn't be, resolves
    to None.
    """
    future: Future = Future()
    excluded = (settings or {}).get("app_context_excluded_apps")

    def run() -> None:
        try:
            image = _capture(excluded)
        except Exception as exc:
            # A daemon thread per capture: a hung window can only keep its own.
            logger.debug("Window screenshot failed (%s)", type(exc).__name__)
            image = None
        future.set_result(image)

    threading.Thread(target=run, name="window-screenshot", daemon=True).start()
    return future


def folder() -> str:
    # realpath: Windows' TEMP can be an 8.3 short name, which the prompt shows.
    return os.path.join(os.path.realpath(tempfile.gettempdir()), "OpenWhisper",
                        "window-screenshots")


def save(image: WindowImage) -> str:
    """Write ``image`` to a new PNG in :func:`folder`; its path, or "" if it can't be written."""
    try:
        directory = folder()
        os.makedirs(directory, exist_ok=True)
        _prune(directory)
        handle, path = tempfile.mkstemp(
            prefix=time.strftime("window-%Y%m%d-%H%M%S-"), suffix=".png", dir=directory)
        with os.fdopen(handle, "wb") as file:
            file.write(image.png)
        return path
    except OSError as exc:
        logger.warning("Couldn't save the window screenshot (%s)", type(exc).__name__)
        return ""


def _prune(directory: str) -> None:
    cutoff = time.time() - KEEP_S
    for entry in os.scandir(directory):
        try:
            if entry.is_file() and entry.name.endswith(".png") and entry.stat().st_mtime < cutoff:
                os.remove(entry.path)
        except OSError:
            logger.debug("Couldn't remove an old window screenshot", exc_info=True)


def _capture(excluded) -> Optional[WindowImage]:
    platform = _platform()
    target = platform.focused() if platform is not None else None
    if target is None:
        return None
    identity, handle = target
    if identity.is_self or catalog.excluded(identity, excluded):
        return None
    image = platform.grab(handle)
    if image is None or image.isNull():
        return None
    png = _png(image)
    return WindowImage(png, identity.name) if png else None


def _png(image) -> bytes:
    from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, Qt

    if max(image.width(), image.height()) > MAX_EDGE_PX:
        image = image.scaled(MAX_EDGE_PX, MAX_EDGE_PX, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    ok = image.save(buffer, "PNG")
    buffer.close()
    return bytes(data) if ok else b""


def _platform():
    if sys.platform == "win32":
        return _WindowsPlatform()
    if sys.platform.startswith("linux"):
        from services import hyprland

        if hyprland.available() and shutil.which("grim"):
            return _HyprlandPlatform()
    return None


class _WindowsPlatform:
    def focused(self) -> Optional[tuple[AppIdentity, int]]:
        from services.focus_context._win import foreground_identity

        identity = foreground_identity()
        if identity is None or not identity.window:
            return None
        return identity, int(identity.window, 16)

    def grab(self, hwnd: int):
        return _gdi().grab(hwnd)


class _HyprlandPlatform:
    def focused(self) -> Optional[tuple[AppIdentity, tuple[int, int, int, int]]]:
        from services import hyprland
        from services.focus_context._linux import _hyprland_identity

        client = hyprland.query("activewindow")
        identity = _hyprland_identity(client)
        if identity is None:
            return None
        (x, y), (width, height) = client["at"], client["size"]
        return identity, (int(x), int(y), int(width), int(height))

    def grab(self, geometry: tuple[int, int, int, int]):
        from PyQt6.QtGui import QImage

        x, y, width, height = geometry
        if width <= 0 or height <= 0:
            return None
        # Layout coordinates, which grim's -g takes too; "-" writes the PNG to stdout.
        result = subprocess.run(["grim", "-g", f"{x},{y} {width}x{height}", "-"],
                                capture_output=True, timeout=_GRIM_TIMEOUT_S, check=True)
        return QImage.fromData(result.stdout, "PNG")


# ---- Windows ----

_PW_RENDERFULLCONTENT = 0x2
_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_DIB_RGB_COLORS = 0
_BI_RGB = 0
#: DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2: rectangles and pixels in the
#: same physical units, whatever the process's own awareness.
_PER_MONITOR_AWARE_V2 = -4


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32), ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32), ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32),
    ]


class _Gdi:
    """Private DLL handles with their own prototypes (see _win._Win32)."""

    def __init__(self):
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        dwmapi = ctypes.WinDLL("dwmapi")
        hwnd, hdc, handle = wintypes.HWND, wintypes.HDC, wintypes.HANDLE
        rect = ctypes.POINTER(wintypes.RECT)
        for name, args, result in (
            ("IsWindow", [hwnd], wintypes.BOOL),
            ("IsIconic", [hwnd], wintypes.BOOL),
            ("IsHungAppWindow", [hwnd], wintypes.BOOL),
            ("GetWindowRect", [hwnd, rect], wintypes.BOOL),
            ("GetDC", [hwnd], hdc),
            ("ReleaseDC", [hwnd, hdc], ctypes.c_int),
            ("PrintWindow", [hwnd, hdc, wintypes.UINT], wintypes.BOOL),
        ):
            function = getattr(user32, name)
            function.argtypes, function.restype = args, result
        for name, args, result in (
            ("CreateCompatibleDC", [hdc], hdc),
            ("CreateCompatibleBitmap", [hdc, ctypes.c_int, ctypes.c_int], handle),
            ("SelectObject", [hdc, handle], handle),
            ("GetDIBits", [hdc, handle, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                           ctypes.POINTER(_BitmapInfoHeader), wintypes.UINT], ctypes.c_int),
            ("DeleteObject", [handle], wintypes.BOOL),
            ("DeleteDC", [hdc], wintypes.BOOL),
        ):
            function = getattr(gdi32, name)
            function.argtypes, function.restype = args, result
        dwmapi.DwmGetWindowAttribute.argtypes = [hwnd, wintypes.DWORD, ctypes.c_void_p,
                                                 wintypes.DWORD]
        dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
        self._set_dpi = getattr(user32, "SetThreadDpiAwarenessContext", None)
        if self._set_dpi is not None:
            self._set_dpi.argtypes, self._set_dpi.restype = [handle], handle
        self.user32, self.gdi32, self.dwmapi = user32, gdi32, dwmapi
        self._rect = wintypes.RECT

    def grab(self, hwnd: int):
        """The window's own pixels without its invisible resize border, or None."""
        user32 = self.user32
        if not user32.IsWindow(hwnd) or user32.IsIconic(hwnd) or user32.IsHungAppWindow(hwnd):
            return None  # PrintWindow waits on a hung window's thread
        previous = self._set_dpi(_PER_MONITOR_AWARE_V2) if self._set_dpi is not None else None
        try:
            window = self._rect()
            if not user32.GetWindowRect(hwnd, ctypes.byref(window)):
                return None
            frame = self._rect()
            if self.dwmapi.DwmGetWindowAttribute(
                    hwnd, _DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(frame),
                    ctypes.sizeof(frame)) != 0:
                frame = window
            image = self._print(hwnd, window.right - window.left, window.bottom - window.top)
        finally:
            if previous:
                self._set_dpi(previous)
        if image is None:
            return None
        return image.copy(frame.left - window.left, frame.top - window.top,
                          frame.right - frame.left, frame.bottom - frame.top)

    def _print(self, hwnd: int, width: int, height: int):
        from PyQt6.QtGui import QImage

        if width <= 0 or height <= 0:
            return None
        user32, gdi32 = self.user32, self.gdi32
        screen = user32.GetDC(None)
        memory = gdi32.CreateCompatibleDC(screen)
        bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
        try:
            if not memory or not bitmap:
                return None
            previous = gdi32.SelectObject(memory, bitmap)
            printed = user32.PrintWindow(hwnd, memory, _PW_RENDERFULLCONTENT)
            gdi32.SelectObject(memory, previous)
            if not printed:
                return None
            header = _BitmapInfoHeader(
                biSize=ctypes.sizeof(_BitmapInfoHeader), biWidth=width,
                biHeight=-height,  # top-down rows, as QImage stores them
                biPlanes=1, biBitCount=32, biCompression=_BI_RGB)
            pixels = ctypes.create_string_buffer(width * height * 4)
            if gdi32.GetDIBits(memory, bitmap, 0, height, pixels, ctypes.byref(header),
                               _DIB_RGB_COLORS) != height:
                return None
            # BGRX bytes are RGB32 on little-endian; copy() owns them.
            return QImage(pixels.raw, width, height, width * 4, QImage.Format.Format_RGB32).copy()
        finally:
            if bitmap:
                gdi32.DeleteObject(bitmap)
            if memory:
                gdi32.DeleteDC(memory)
            user32.ReleaseDC(None, screen)


@lru_cache(maxsize=1)
def _gdi() -> _Gdi:
    return _Gdi()

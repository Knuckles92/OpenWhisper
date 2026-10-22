"""Where Snap can capture the screen, and through what.

Windows and X11 hand the screen's pixels to Qt. Wayland does not, by
design: on Hyprland we ask ``grim``, the screenshot tool for wlroots-style
compositors. Other Wayland desktops only offer the screenshot portal, which
asks the user for permission on every capture, so they wait for now.
"""

from __future__ import annotations

import shutil
import sys

from services import hyprland
from services.desktop_session import is_wayland_session
from ui_qt.utils.desktop import is_wayland

MAC_REASON = "Snap isn't available on macOS yet."
WAYLAND_REASON = (
    "Snap doesn't work on this Wayland desktop yet. "
    "It works on Windows, X11 and Hyprland."
)
GRIM_REASON = "Snap on Hyprland needs grim. Install the grim package, then try again."
OTHER_REASON = "Snap isn't available on this system yet."


def _platform() -> str:
    """``sys.platform``, behind a seam so tests can pose as another system."""
    return sys.platform


def uses_grim() -> bool:
    """Whether captures go through grim: a Linux Hyprland session."""
    return _platform().startswith("linux") and hyprland.available()


def snap_support() -> tuple[bool, str]:
    """``(supported, reason)``; ``reason`` is a short user-facing sentence, or "".

    Windows and X11 are supported. Hyprland is supported once ``grim`` is on
    the PATH. Other Wayland desktops and macOS are not supported yet.
    """
    platform = _platform()
    if platform == "win32":
        return True, ""
    if platform == "darwin":
        return False, MAC_REASON
    if not platform.startswith("linux"):
        return False, OTHER_REASON
    if hyprland.available():
        return (True, "") if shutil.which("grim") else (False, GRIM_REASON)
    # XWayland makes Qt look like X11, but grabbing its root window shows
    # only other X clients: the session decides, not Qt's platform.
    if is_wayland() or is_wayland_session():
        return False, WAYLAND_REASON
    return True, ""

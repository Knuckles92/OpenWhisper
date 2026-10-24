"""Where Snap can capture the screen, and through what.

Windows and X11 hand the screen's pixels to Qt, and so does macOS once the
user grants OpenWhisper Screen Recording. Wayland does not, by
design: on Hyprland we ask ``grim``, the screenshot tool for wlroots-style
compositors. Other Wayland desktops only offer the screenshot portal, which
asks the user for permission on every capture, so they wait for now.
"""

from __future__ import annotations

import shutil
import sys

from services import hyprland
from services.desktop_session import is_wayland_session
from ui_qt.snap import macos
from ui_qt.utils.desktop import is_wayland

MAC_REASON = (
    "Snap needs Screen Recording permission. Allow OpenWhisper in System Settings "
    "› Privacy & Security › Screen & System Audio Recording, then reopen OpenWhisper."
)
WAYLAND_REASON = (
    "Snap doesn't work on this Wayland desktop yet. "
    "It works on Windows, X11 and Hyprland."
)
GRIM_REASON = "Snap on Hyprland needs grim. Install the grim package, then try again."
OTHER_REASON = "Snap isn't available on this system yet."
#: request_access has asked macOS once in this run.
_asked = False


def _platform() -> str:
    """``sys.platform``, behind a seam so tests can pose as another system."""
    return sys.platform


def uses_grim() -> bool:
    """Whether captures go through grim: a Linux Hyprland session."""
    return _platform().startswith("linux") and hyprland.available()


def snap_support() -> tuple[bool, str]:
    """``(supported, reason)``; ``reason`` is a short user-facing sentence, or "".

    Windows and X11 are supported. macOS is once Screen Recording is granted,
    and Hyprland once ``grim`` is on the PATH. Other Wayland desktops are not
    supported yet. Never prompts: see ``request_access``.
    """
    platform = _platform()
    if platform == "win32":
        return True, ""
    if platform == "darwin":
        return (True, "") if macos.capture_granted() else (False, MAC_REASON)
    if not platform.startswith("linux"):
        return False, OTHER_REASON
    if hyprland.available():
        return (True, "") if shutil.which("grim") else (False, GRIM_REASON)
    # XWayland makes Qt look like X11, but grabbing its root window shows
    # only other X clients: the session decides, not Qt's platform.
    if is_wayland() or is_wayland_session():
        return False, WAYLAND_REASON
    return True, ""


def request_access() -> None:
    """Ask for what snap_support found missing, where the system can be asked.

    On macOS that is the Screen Recording grant: the first request in a run
    asks macOS (which prompts once ever, and lists OpenWhisper in Settings);
    later ones open System Settings at the grant.
    """
    global _asked
    if _platform() != "darwin" or macos.capture_granted():
        return
    if not _asked:
        _asked = True
        macos.request_capture()
    else:
        macos.open_capture_settings()

"""Synthetic keystrokes for reading the selection: copy, and waiting it out.

A copy is only ever sent after the hotkey's own modifiers are up, and never
to a terminal, where Ctrl+C interrupts the running program.
"""

from __future__ import annotations

import functools
import logging
import re
import sys
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

#: How long a copy waits by default for the shortcut's modifiers to come up.
MODIFIER_WAIT_S = 0.8
_POLL_S = 0.01
#: Where the held keys can't be read (X11, Wayland), how long a copy waits
#: for the hand to come off the shortcut instead.
_BLIND_WAIT_S = 0.15

_WINDOWS_MODIFIERS = ("ctrl", "alt", "shift", "windows")
# VK_CONTROL, VK_MENU, VK_SHIFT, VK_LWIN/VK_RWIN.
_WINDOWS_VIRTUAL_KEYS = {
    "ctrl": (0x11,), "alt": (0x12,), "shift": (0x10,), "windows": (0x5B, 0x5C),
}

# Editors whose Ctrl+C with nothing selected copies the caret's whole line,
# matched against an app's id and name, lowercased, with any path, ".exe" or
# ".app" removed.
_COPY_LINE_EDITORS = frozenset({
    "code", "code - insiders", "code-oss", "visual studio code",
    "com.microsoft.vscode", "cursor", "com.todesktop.230313mzl4w4u92",
    "windsurf", "com.exafunction.windsurf", "devenv", "visual studio",
    "sublime_text", "sublime text", "com.sublimetext.4", "com.sublimetext.3",
    "zed", "dev.zed.zed", "zeditor",
})
_JETBRAINS = re.compile(
    r"^(?:jetbrains-|com\.jetbrains\.)"
    r"|^(?:idea|pycharm|webstorm|phpstorm|clion|rider|goland|rubymine|datagrip"
    r"|dataspell|rustrover|aqua|studio|fleet)(?:64)?$"
    r"|^(?:intellij idea|android studio)\b"
)

_controller_lock = threading.Lock()
_pynput_controller = None


def _names(identity) -> set[str]:
    names = set()
    for value in (getattr(identity, "app_id", ""), getattr(identity, "name", "")):
        if not isinstance(value, str) or not value.strip():
            continue
        name = value.strip().lower().replace("\\", "/").rsplit("/", 1)[-1]
        for suffix in (".exe", ".app"):
            if name.endswith(suffix):
                name = name[: -len(suffix)]
        names.add(name)
    return names


def _catalog_surface(identity) -> str:
    """The app catalogue's surface for ``identity`` ("text", "code", "terminal")."""
    try:
        from services import app_styles
        from services.focus_context import FocusSnapshot

        style = app_styles.style_for(FocusSnapshot(identity=identity), {})
    except Exception:
        logger.debug("App catalogue unavailable", exc_info=True)
        return ""
    surface = getattr(style, "surface", "")
    return surface if isinstance(surface, str) else ""


def is_terminal(identity) -> bool:
    """Whether ``identity`` (an AppIdentity or None) is a terminal.

    The app catalogue is the one list of terminals; Hyprland's paste and copy
    keys read this same check.
    """
    if identity is None:
        return False
    try:
        from services.app_styles import Surface

        return _catalog_surface(identity) == Surface.TERMINAL
    except Exception:
        logger.debug("Terminal check failed", exc_info=True)
        return False


def copies_line_without_selection(identity) -> bool:
    """Whether the app copies the whole line when nothing is selected."""
    if identity is None:
        return False
    try:
        names = _names(identity)
        if names & _COPY_LINE_EDITORS or any(_JETBRAINS.search(name) for name in names):
            return True
        from services.app_styles import Surface

        return _catalog_surface(identity) == Surface.CODE
    except Exception:
        logger.debug("Editor check failed", exc_info=True)
        return False


def _controller():
    global _pynput_controller
    with _controller_lock:
        if _pynput_controller is None:
            from pynput import keyboard as pynput_keyboard

            _pynput_controller = pynput_keyboard.Controller()
        return _pynput_controller


def send_copy() -> None:
    """Send the platform's copy shortcut to the focused app.

    Raises:
        RuntimeError: When this desktop can't send keystrokes to other apps.
    """
    if sys.platform == "win32":
        import keyboard

        keyboard.send("ctrl+c")
        return
    from services.desktop_session import is_wayland_session

    if is_wayland_session():
        from services import hyprland

        if hyprland.available():
            hyprland.send_copy()
            return
        raise RuntimeError("This Wayland desktop can't copy the selection for OpenWhisper")
    if sys.platform == "darwin":
        from services.hotkey_manager import is_accessibility_trusted

        if not is_accessibility_trusted():
            raise RuntimeError("Copying the selection needs Accessibility access")
    from pynput.keyboard import Key

    controller = _controller()
    with controller.pressed(Key.cmd if sys.platform == "darwin" else Key.ctrl):
        controller.press("c")
        controller.release("c")


@functools.lru_cache(maxsize=1)
def _physical_key_reader() -> Optional[Callable[[int], bool]]:
    """A check for a virtual key being down in the OS's own key state; None off Windows."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        # Private, so these argtypes never reach other windll.user32 users.
        state = ctypes.WinDLL("user32").GetAsyncKeyState
        state.argtypes = [ctypes.c_int]
        state.restype = ctypes.c_short
    except Exception:
        logger.debug("The OS key state is unavailable", exc_info=True)
        return None
    return lambda vk: bool(state(vk) & 0x8000)


def _windows_modifiers_held() -> bool:
    import keyboard

    held = [name for name in _WINDOWS_MODIFIERS if keyboard.is_pressed(name)]
    if not held:
        return False
    # The hook keeps a modifier down when its key-up went to an elevated
    # window. A copy waits for these keys and is refused while they are
    # held, so the OS state has the last word.
    physical = _physical_key_reader()
    if physical is None:
        return True
    return any(physical(vk) for name in held for vk in _WINDOWS_VIRTUAL_KEYS[name])


def _mac_modifiers_held() -> bool:
    import Quartz

    mask = (
        Quartz.kCGEventFlagMaskCommand
        | Quartz.kCGEventFlagMaskControl
        | Quartz.kCGEventFlagMaskAlternate
        | Quartz.kCGEventFlagMaskShift
    )
    flags = Quartz.CGEventSourceFlagsState(Quartz.kCGEventSourceStateHIDSystemState)
    return bool(flags & mask)


def _modifier_probe() -> Optional[Callable[[], bool]]:
    """A check for held modifiers, or None where key state can't be read."""
    if sys.platform == "win32":
        return _windows_modifiers_held
    if sys.platform == "darwin":
        return _mac_modifiers_held
    return None


def wait_for_modifiers_released(timeout_s: float = MODIFIER_WAIT_S) -> bool:
    """Wait until no modifier key is held; False when one still is at the timeout.

    Blocks the calling thread, so never call it on the Qt thread. A key
    state that can't be read counts as released.
    """
    probe = _modifier_probe()
    if probe is None:
        time.sleep(max(0.0, min(_BLIND_WAIT_S, timeout_s)))
        return True
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        try:
            if not probe():
                return True
        except Exception:
            logger.debug("Couldn't read the modifier keys", exc_info=True)
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_S)

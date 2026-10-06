"""Platform-specific global hotkey capture controls."""

import logging
import queue
import sys
from typing import Optional

from PyQt6.QtCore import QThread, Qt, pyqtSignal
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QLineEdit

from services._hotkey_common import MOUSE_KEYS, is_mouse_key
from services.hotkey_manager import (
    USE_PYNPUT_BACKEND,
    format_hotkey,
    mouse_shortcuts_supported,
    parse_hotkey,
)
from ui_qt.utils.restyle import set_style_property

if USE_PYNPUT_BACKEND:
    from services.hotkey_manager import (
        get_listener_class,
        key_to_name,
        modifier_of,
    )
else:
    import keyboard

logger = logging.getLogger(__name__)

if USE_PYNPUT_BACKEND:
    HOTKEY_CAPTURE_FAILURE_MESSAGE = (
        "Could not capture hotkey. Enable Accessibility and Input Monitoring "
        "permissions for OpenWhisper in macOS System Settings, then try again."
    )
else:
    HOTKEY_CAPTURE_FAILURE_MESSAGE = "Could not capture hotkey. Please try again."

MOUSE_UNSUPPORTED_MESSAGE = (
    "Mouse buttons aren't supported as shortcuts on this desktop yet"
)

_SIDE_BUTTONS = {
    Qt.MouseButton.BackButton: "mouse4",
    Qt.MouseButton.ForwardButton: "mouse5",
}


def qt_modifier_names(flags) -> set:
    """Qt keyboard modifiers as the active backend's modifier names."""
    names = set()
    for flag, name in (
        (
            Qt.KeyboardModifier.ControlModifier,
            "cmd" if sys.platform == "darwin" else "ctrl",
        ),
        (
            Qt.KeyboardModifier.MetaModifier,
            "ctrl"
            if sys.platform == "darwin"
            else "win"
            if sys.platform == "win32"
            else "cmd",
        ),
        (Qt.KeyboardModifier.AltModifier, "alt"),
        (Qt.KeyboardModifier.ShiftModifier, "shift"),
    ):
        if flags & flag:
            names.add(name)
    return names


def side_button_hotkey(event: QMouseEvent) -> Optional[str]:
    """The shortcut a Back or Forward click spells, with its modifiers."""
    button = _SIDE_BUTTONS.get(event.button())
    if button is None:
        return None
    return format_hotkey(qt_modifier_names(event.modifiers()), button)


def mouse_shortcut_note(hotkey: str) -> str:
    """What binding a side button changes about it in other apps, if anything."""
    modifiers, key = parse_hotkey(hotkey or "")
    if not is_mouse_key(key):
        return ""
    name = MOUSE_KEYS[key]
    action = "Back" if key == "mouse4" else "Forward"
    if not USE_PYNPUT_BACKEND:
        return f"{name} won't work as {action} in other apps while it's a shortcut."
    if not modifiers:
        # X11 cannot keep the click from reaching the focused app.
        return f"{name} also still goes {action} in the app you're using."
    return ""


class HotkeyCaptureInput(QLineEdit):
    """Read-only shortcut field that requests capture when clicked.

    While capturing, a Back or Forward click on the field is a shortcut too:
    ``mouse_captured`` carries it, or ``notice`` explains that this desktop
    cannot use mouse buttons.
    """

    capture_requested = pyqtSignal()
    mouse_captured = pyqtSignal(str)
    notice = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("hotkeyInput")
        self.setReadOnly(True)
        self.setMinimumHeight(38)
        self.setPlaceholderText("Click to set hotkey")
        self._capture_active = False

    def mousePressEvent(self, event: QMouseEvent) -> None:
        hotkey = side_button_hotkey(event)
        if hotkey is None:
            self.capture_requested.emit()
            super().mousePressEvent(event)
            return
        event.accept()
        if not self._capture_active:
            return
        if mouse_shortcuts_supported():
            self._side_button_captured(hotkey)
        else:
            self.notice.emit(MOUSE_UNSUPPORTED_MESSAGE)

    def _side_button_captured(self, hotkey: str) -> None:
        self.mouse_captured.emit(hotkey)

    def set_capturing(self, capturing: bool) -> None:
        self._capture_active = capturing
        set_style_property(self, "capturing", capturing)


if USE_PYNPUT_BACKEND:

    class HotkeyCaptureThread(QThread):
        """Capture one shortcut with the pynput backend."""

        captured = pyqtSignal(str)
        failed = pyqtSignal(str)

        def __init__(self, parent=None):
            super().__init__(parent)
            self._listener = None
            self._canceled = False

        def run(self) -> None:
            self._canceled = False
            pressed_modifiers = set()
            result = {"hotkey": None}

            def on_press(key):
                modifier = modifier_of(key)
                if modifier is not None:
                    pressed_modifiers.add(modifier)
                    return True

                name = key_to_name(key)
                if name is None:
                    return True

                result["hotkey"] = format_hotkey(
                    frozenset(pressed_modifiers), name
                )
                return False

            def on_release(key):
                modifier = modifier_of(key)
                if modifier is not None:
                    pressed_modifiers.discard(modifier)
                return True

            try:
                listener_class = get_listener_class()
                self._listener = listener_class(
                    on_press=on_press,
                    on_release=on_release,
                    suppress=False,
                )
                self._listener.start()
                self._listener.join()
                if result["hotkey"]:
                    self.captured.emit(result["hotkey"])
                elif not self._canceled:
                    logger.error(
                        "Hotkey capture listener stopped without capturing a key"
                    )
                    self.failed.emit(HOTKEY_CAPTURE_FAILURE_MESSAGE)
            except Exception as exc:
                if not self._canceled:
                    logger.error("Error capturing hotkey: %s", exc)
                    self.failed.emit(HOTKEY_CAPTURE_FAILURE_MESSAGE)

        def stop(self) -> None:
            self._canceled = True
            listener = self._listener
            if listener is not None:
                try:
                    listener.stop()
                except Exception:
                    pass

else:

    class HotkeyCaptureThread(QThread):
        """Capture one shortcut with the Windows keyboard backend."""

        captured = pyqtSignal(str)
        failed = pyqtSignal(str)

        def __init__(self, parent=None):
            super().__init__(parent)
            self._events: queue.Queue = queue.Queue()
            self._stopped = False

        def run(self) -> None:
            hooked = None
            try:
                events = []
                # Non-suppressing, and only this hook: the app's own
                # suppressing shortcut hook must survive a canceled capture.
                hooked = keyboard.hook(self._events.put, suppress=False)
                while not self._stopped:
                    event = self._events.get()
                    if event is None:
                        return
                    events.append(event)
                    if event.event_type == keyboard.KEY_UP:
                        names = [
                            event.name
                            if not event.is_keypad
                            else f"kp_{event.name}"
                            for event in events
                        ]
                        name = keyboard.get_hotkey_name(names)
                        self.captured.emit(format_hotkey(*parse_hotkey(name)))
                        return
            except Exception as exc:
                if not self._stopped:
                    logger.error("Error capturing hotkey: %s", exc)
                    self.failed.emit(HOTKEY_CAPTURE_FAILURE_MESSAGE)
            finally:
                if hooked is not None:
                    try:
                        keyboard.unhook(hooked)
                    except Exception:
                        pass

        def stop(self) -> None:
            self._stopped = True
            self._events.put(None)

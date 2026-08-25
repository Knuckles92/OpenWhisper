"""Platform-specific global hotkey capture controls."""

import logging

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QLineEdit

from services.hotkey_manager import USE_PYNPUT_BACKEND

if USE_PYNPUT_BACKEND:
    from services.hotkey_manager import (
        format_hotkey,
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


class HotkeyCaptureInput(QLineEdit):
    """Read-only shortcut field that requests capture when clicked."""

    capture_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("hotkeyInput")
        self.setReadOnly(True)
        self.setMinimumHeight(38)
        self.setPlaceholderText("Click to set hotkey")

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self.capture_requested.emit()
        super().mousePressEvent(event)

    def set_capturing(self, capturing: bool) -> None:
        self.setProperty("capturing", capturing)
        self.style().unpolish(self)
        self.style().polish(self)


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

        def run(self) -> None:
            try:
                events = []
                queue = keyboard._queue.Queue()
                callback = (
                    lambda event: queue.put(event)
                    or event.event_type == keyboard.KEY_DOWN
                )
                hooked = keyboard.hook(callback, suppress=False)
                while True:
                    event = queue.get()
                    events.append(event)
                    if event.event_type == keyboard.KEY_UP:
                        keyboard.unhook(hooked)
                        names = [
                            event.name
                            if not event.is_keypad
                            else f"kp_{event.name}"
                            for event in events
                        ]
                        self.captured.emit(keyboard.get_hotkey_name(names))
                        break
            except Exception as exc:
                logger.error("Error capturing hotkey: %s", exc)
                self.failed.emit(HOTKEY_CAPTURE_FAILURE_MESSAGE)

        def stop(self) -> None:
            try:
                keyboard.unhook_all()
            except Exception:
                pass
            self.terminate()

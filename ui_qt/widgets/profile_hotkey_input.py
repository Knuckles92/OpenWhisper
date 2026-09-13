"""Capture a single shortcut in Qt while application hotkeys are suspended."""

import sys

from PyQt6.QtCore import QEvent, Qt, QTimer, pyqtSignal

from services.hotkey_manager import format_hotkey, format_hotkey_display
from ui_qt.widgets.hotkey_capture import HotkeyCaptureInput


class ProfileHotkeyInput(HotkeyCaptureInput):
    captured = pyqtSignal(str)
    capture_changed = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.hotkey = ""
        self._capturing = False
        self._pending = None
        self.setAccessibleName("Profile recording shortcut")
        self.setToolTip(
            "Click, then press a shortcut. Escape cancels. Profile shortcuts toggle recording."
        )
        self.capture_requested.connect(self.begin_capture)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(10000)
        self._timer.timeout.connect(self.cancel_capture)

    def set_hotkey(self, hotkey: str) -> None:
        self.hotkey = hotkey
        self.setText(format_hotkey_display(hotkey))

    def begin_capture(self) -> None:
        if self._capturing:
            return
        self._capturing = True
        self._pending = None
        self.set_capturing(True)
        self.setText("Press keys… (Esc cancels)")
        self.capture_changed.emit(True)
        self._timer.start()

    def cancel_capture(self) -> None:
        if not self._capturing:
            return
        self._capturing = False
        self._pending = None
        self._timer.stop()
        self.set_capturing(False)
        self.set_hotkey(self.hotkey)
        self.capture_changed.emit(False)

    def focusOutEvent(self, event):
        self.cancel_capture()
        super().focusOutEvent(event)

    def hideEvent(self, event):
        self.cancel_capture()
        super().hideEvent(event)

    def event(self, event):
        if getattr(self, "_capturing", False):
            if event.type() == QEvent.Type.ShortcutOverride:
                event.accept()
                return True
            if event.type() == QEvent.Type.KeyPress and event.key() in (
                Qt.Key.Key_Tab,
                Qt.Key.Key_Backtab,
            ):
                self.keyPressEvent(event)
                return True
        return super().event(event)

    def keyPressEvent(self, event):
        key = event.key()
        if not self._capturing:
            if key in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.begin_capture()
                event.accept()
                return
            return super().keyPressEvent(event)
        event.accept()
        if event.isAutoRepeat():
            return
        if key == Qt.Key.Key_Escape:
            self.cancel_capture()
            return
        modifiers = set()
        flags = event.modifiers()
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
                modifiers.add(name)
        names = {
            Qt.Key.Key_Space: "space",
            Qt.Key.Key_Return: "enter",
            Qt.Key.Key_Enter: "enter",
            Qt.Key.Key_Backspace: "backspace",
            Qt.Key.Key_Delete: "delete",
            Qt.Key.Key_Insert: "insert",
            Qt.Key.Key_Home: "home",
            Qt.Key.Key_End: "end",
            Qt.Key.Key_PageUp: "page up" if sys.platform == "win32" else "page_up",
            Qt.Key.Key_PageDown: "page down"
            if sys.platform == "win32"
            else "page_down",
            Qt.Key.Key_Left: "left",
            Qt.Key.Key_Right: "right",
            Qt.Key.Key_Up: "up",
            Qt.Key.Key_Down: "down",
            Qt.Key.Key_Tab: "tab",
            Qt.Key.Key_Backtab: "tab",
        }
        name = names.get(key)
        if Qt.Key.Key_F1 <= key <= Qt.Key.Key_F24:
            name = f"f{key - Qt.Key.Key_F1 + 1}"
        elif 33 <= key <= 126 and key != Qt.Key.Key_Plus:
            name = chr(key).lower()
        if not name:
            return
        if sys.platform == "win32" and flags & Qt.KeyboardModifier.KeypadModifier:
            name = f"kp {name}"
        self._pending = (key, format_hotkey(modifiers, name))

    def keyReleaseEvent(self, event):
        if self._capturing and self._pending and not event.isAutoRepeat():
            key, hotkey = self._pending
            if event.key() == key:
                self.set_hotkey(hotkey)
                self.cancel_capture()
                self.captured.emit(hotkey)
                event.accept()
                return
        super().keyReleaseEvent(event)

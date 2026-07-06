"""Hotkey setup and watchdog behavior for the application controller."""

from __future__ import annotations

import logging
import sys
import time
from typing import TYPE_CHECKING, Dict, Optional

from PyQt6.QtCore import QTimer, Qt

from config import config
from services.hotkey_manager import HotkeyManager, USE_PYNPUT_BACKEND
from services.settings import settings_manager
from ui_qt.overlay_state import OverlayState

if TYPE_CHECKING:
    from services.application_controller import ApplicationController

logger = logging.getLogger(__name__)


# The Qt focus-window hotkey fallback is only needed for the pynput backend
# (macOS/Linux), which cannot suppress global keys. On Windows the keyboard
# backend swallows hotkeys globally, so none of this machinery is imported or
# defined there.
if USE_PYNPUT_BACKEND:
    from PyQt6.QtCore import QObject, QEvent
    from PyQt6.QtWidgets import QApplication

    _MAC_NATIVE_SHIFT = 1 << 17
    _MAC_NATIVE_CTRL = 1 << 18
    _MAC_NATIVE_ALT = 1 << 19
    _MAC_NATIVE_CMD = 1 << 20

    def _qt_key_value(key) -> int:
        return key.value if hasattr(key, "value") else int(key)

    _QT_MAIN_KEY_NAMES = {
        _qt_key_value(Qt.Key.Key_Space): "space",
        _qt_key_value(Qt.Key.Key_Escape): "esc",
        _qt_key_value(Qt.Key.Key_Return): "enter",
        _qt_key_value(Qt.Key.Key_Enter): "enter",
        _qt_key_value(Qt.Key.Key_Tab): "tab",
        _qt_key_value(Qt.Key.Key_Backtab): "tab",
        _qt_key_value(Qt.Key.Key_Backspace): "backspace",
        _qt_key_value(Qt.Key.Key_Delete): "delete",
        _qt_key_value(Qt.Key.Key_Insert): "insert",
        _qt_key_value(Qt.Key.Key_Home): "home",
        _qt_key_value(Qt.Key.Key_End): "end",
        _qt_key_value(Qt.Key.Key_PageUp): "page_up",
        _qt_key_value(Qt.Key.Key_PageDown): "page_down",
        _qt_key_value(Qt.Key.Key_Left): "left",
        _qt_key_value(Qt.Key.Key_Right): "right",
        _qt_key_value(Qt.Key.Key_Up): "up",
        _qt_key_value(Qt.Key.Key_Down): "down",
    }

    _QT_MODIFIER_KEYS = {
        _qt_key_value(Qt.Key.Key_Control),
        _qt_key_value(Qt.Key.Key_Meta),
        _qt_key_value(Qt.Key.Key_Alt),
        _qt_key_value(Qt.Key.Key_Shift),
    }

    def _qt_event_modifiers(event) -> frozenset:
        """Return canonical modifier names for a Qt key event."""
        modifiers = set()

        if sys.platform == "darwin" and hasattr(event, "nativeModifiers"):
            native_modifiers = int(event.nativeModifiers())
            if native_modifiers:
                if native_modifiers & _MAC_NATIVE_CMD:
                    modifiers.add("cmd")
                if native_modifiers & _MAC_NATIVE_CTRL:
                    modifiers.add("ctrl")
                if native_modifiers & _MAC_NATIVE_ALT:
                    modifiers.add("alt")
                if native_modifiers & _MAC_NATIVE_SHIFT:
                    modifiers.add("shift")
                return frozenset(modifiers)

        qt_modifiers = event.modifiers()
        if qt_modifiers & Qt.KeyboardModifier.MetaModifier:
            modifiers.add("cmd")
        if qt_modifiers & Qt.KeyboardModifier.ControlModifier:
            modifiers.add("ctrl")
        if qt_modifiers & Qt.KeyboardModifier.AltModifier:
            modifiers.add("alt")
        if qt_modifiers & Qt.KeyboardModifier.ShiftModifier:
            modifiers.add("shift")
        return frozenset(modifiers)

    def _qt_event_key_name(event) -> Optional[str]:
        """Return a canonical main-key name for a Qt key event."""
        key = int(event.key())
        if key in _QT_MODIFIER_KEYS:
            return None

        mapped_name = _QT_MAIN_KEY_NAMES.get(key)
        if mapped_name:
            return mapped_name

        if _qt_key_value(Qt.Key.Key_A) <= key <= _qt_key_value(Qt.Key.Key_Z):
            return chr(ord("a") + key - _qt_key_value(Qt.Key.Key_A))

        if _qt_key_value(Qt.Key.Key_0) <= key <= _qt_key_value(Qt.Key.Key_9):
            return chr(ord("0") + key - _qt_key_value(Qt.Key.Key_0))

        if _qt_key_value(Qt.Key.Key_F1) <= key <= _qt_key_value(Qt.Key.Key_F24):
            return f"f{key - _qt_key_value(Qt.Key.Key_F1) + 1}"

        text = event.text()
        if text and not text.isspace():
            return text.lower()

        return None

    class ActiveWindowHotkeyFilter(QObject):
        """Qt fallback for hotkeys while the OpenWhisper window is focused."""

        def __init__(self, controller: "ApplicationController"):
            super().__init__()
            self.controller = controller

        def eventFilter(self, obj, event):
            if event.type() != QEvent.Type.KeyPress:
                return False
            if event.isAutoRepeat() or not self._main_window_is_active():
                return False

            hotkey_manager = self.controller.hotkey_manager
            if hotkey_manager is None:
                return False

            main_key = _qt_event_key_name(event)
            if main_key is None:
                return False

            handled = hotkey_manager.handle_hotkey_press(
                _qt_event_modifiers(event),
                main_key,
                source="qt",
            )
            if handled:
                event.accept()
            return handled

        def _main_window_is_active(self) -> bool:
            app = QApplication.instance()
            if app is None:
                return False

            active_window = app.activeWindow()
            return active_window is self.controller.ui_controller.main_window


class HotkeyRuntime:
    """Owns hotkey configuration and keyboard hook lifecycle."""

    def __init__(self, controller: "ApplicationController"):
        self.controller = controller
        self._active_window_hotkey_filter: Optional[ActiveWindowHotkeyFilter] = None

    def setup_hotkeys(self) -> None:
        """Setup hotkey management."""
        logger.info("Setting up hotkeys...")
        hotkeys = settings_manager.load_hotkey_settings()
        self.controller.hotkey_manager = HotkeyManager(hotkeys)
        self.controller.hotkey_manager.set_callbacks(
            on_record_toggle=self.controller.toggle_recording,
            on_cancel=self.controller.cancel,
            on_status_update=self.controller.update_status_with_auto_hide,
            on_status_update_auto_hide=self.controller.update_status_with_auto_hide,
        )
        self.controller.ui_controller.update_hotkey_display(hotkeys)
        self._install_active_window_hotkey_filter()

    def update_hotkeys(self, hotkeys: Dict[str, str]) -> None:
        """Update application hotkeys."""
        logger.info(f"Updating hotkeys: {hotkeys}")
        if self.controller.hotkey_manager:
            self.controller.hotkey_manager.update_hotkeys(hotkeys)
            settings_manager.save_hotkey_settings(hotkeys)
            self.controller.ui_controller.set_status("Hotkeys updated")

    def setup_hook_watchdog(self) -> None:
        """Setup timers to detect sleep and refresh the keyboard hook."""
        self.controller._watchdog_interval_ms = config.HOTKEY_WATCHDOG_INTERVAL_MS
        self.controller._sleep_gap_threshold_sec = config.HOTKEY_SLEEP_GAP_THRESHOLD_SEC
        self.controller._expected_watchdog_time = time.monotonic() + (
            self.controller._watchdog_interval_ms / 1000.0
        )
        self.controller._last_rehook_time = 0.0

        self.controller._watchdog_timer = QTimer()
        self.controller._watchdog_timer.setTimerType(Qt.TimerType.CoarseTimer)
        self.controller._watchdog_timer.timeout.connect(self.on_watchdog_tick)
        self.controller._watchdog_timer.start(self.controller._watchdog_interval_ms)

        self.controller._periodic_refresh_interval_ms = config.HOTKEY_HOOK_REFRESH_INTERVAL_MS
        self.controller._periodic_refresh_timer = QTimer()
        self.controller._periodic_refresh_timer.setTimerType(Qt.TimerType.VeryCoarseTimer)
        self.controller._periodic_refresh_timer.timeout.connect(
            self.on_periodic_hook_refresh
        )
        self.controller._periodic_refresh_timer.start(
            self.controller._periodic_refresh_interval_ms
        )

        logger.info(
            "Hook watchdog started: sleep detection every %dms, periodic refresh every %dms",
            config.HOTKEY_WATCHDOG_INTERVAL_MS,
            config.HOTKEY_HOOK_REFRESH_INTERVAL_MS,
        )

    def _install_active_window_hotkey_filter(self) -> None:
        """Install focused-window hotkey handling as a fallback to global hooks.

        Only needed for the pynput backend (macOS/Linux), which cannot suppress
        keys. On Windows the keyboard backend already swallows hotkeys globally
        and its HotkeyManager has no ``handle_hotkey_press`` method.
        """
        if not USE_PYNPUT_BACKEND:
            return

        app = QApplication.instance()
        if app is None:
            logger.warning("Could not install active-window hotkey filter: no QApplication")
            return

        if self._active_window_hotkey_filter is None:
            self._active_window_hotkey_filter = ActiveWindowHotkeyFilter(self.controller)
            app.installEventFilter(self._active_window_hotkey_filter)
            logger.info("Active-window hotkey filter installed")

    def cleanup(self) -> None:
        """Remove Qt hotkey filter owned by this runtime."""
        if self._active_window_hotkey_filter is None:
            return

        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self._active_window_hotkey_filter)
        self._active_window_hotkey_filter = None

    def on_watchdog_tick(self) -> None:
        """Check for time gaps indicating system sleep/resume."""
        now = time.monotonic()
        gap = now - self.controller._expected_watchdog_time
        self.controller._expected_watchdog_time = now + (
            self.controller._watchdog_interval_ms / 1000.0
        )

        if gap > self.controller._sleep_gap_threshold_sec:
            logger.warning(
                f"Sleep/resume detected: time gap of {gap:.1f}s. "
                "Re-registering keyboard hook."
            )
            self.rehook_keyboard()

    def on_periodic_hook_refresh(self) -> None:
        """Periodically re-register the keyboard hook."""
        now = time.monotonic()
        if now - self.controller._last_rehook_time < 60.0:
            return

        logger.info("Periodic keyboard hook refresh")
        self.rehook_keyboard()

    def rehook_keyboard(self) -> None:
        """Re-register the keyboard hook via HotkeyManager."""
        if self.controller.hotkey_manager:
            try:
                self.controller.hotkey_manager.rehook()
                self.controller._last_rehook_time = time.monotonic()
                self.controller._expected_watchdog_time = time.monotonic() + (
                    self.controller._watchdog_interval_ms / 1000.0
                )
            except Exception as exc:
                logger.error(f"Failed to re-register keyboard hook: {exc}")

    def on_stt_state_changed(self, enabled: bool) -> None:
        """Handle STT state changes on the main thread."""
        state = OverlayState.STT_ENABLED if enabled else OverlayState.STT_DISABLED
        self.controller.overlay_state_update.emit(state)

    def update_status_with_auto_hide(self, status: str) -> None:
        """Emit a thread-safe status update and optional STT state change."""
        self.controller.status_update.emit(status)

        if status == "STT Enabled":
            self.controller.stt_state_changed.emit(True)
        elif status == "STT Disabled":
            self.controller.stt_state_changed.emit(False)

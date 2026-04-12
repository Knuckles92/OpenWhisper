"""Hotkey setup and watchdog behavior for the application controller."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Dict

from PyQt6.QtCore import QTimer, Qt

from services.hotkey_manager import HotkeyManager
from services.settings import settings_manager

if TYPE_CHECKING:
    from services.application_controller import ApplicationController


class HotkeyRuntime:
    """Owns hotkey configuration and keyboard hook lifecycle."""

    def __init__(self, controller: "ApplicationController"):
        self.controller = controller

    def setup_hotkeys(self) -> None:
        """Setup hotkey management."""
        logging.info("Setting up hotkeys...")
        hotkeys = settings_manager.load_hotkey_settings()
        self.controller.hotkey_manager = HotkeyManager(hotkeys)
        self.controller.hotkey_manager.set_callbacks(
            on_record_toggle=self.controller.toggle_recording,
            on_cancel=self.controller.cancel_recording,
            on_status_update=self.controller.update_status_with_auto_hide,
            on_status_update_auto_hide=self.controller.update_status_with_auto_hide,
        )
        self.controller.ui_controller.update_hotkey_display(hotkeys)

    def update_hotkeys(self, hotkeys: Dict[str, str]) -> None:
        """Update application hotkeys."""
        logging.info(f"Updating hotkeys: {hotkeys}")
        if self.controller.hotkey_manager:
            self.controller.hotkey_manager.update_hotkeys(hotkeys)
            settings_manager.save_hotkey_settings(hotkeys)
            self.controller.ui_controller.set_status("Hotkeys updated")

    def setup_hook_watchdog(self) -> None:
        """Setup timers to detect sleep and refresh the keyboard hook."""
        self.controller._watchdog_interval_ms = 10_000
        self.controller._sleep_gap_threshold_sec = 30.0
        self.controller._expected_watchdog_time = time.monotonic() + (
            self.controller._watchdog_interval_ms / 1000.0
        )
        self.controller._last_rehook_time = 0.0

        self.controller._watchdog_timer = QTimer()
        self.controller._watchdog_timer.setTimerType(Qt.TimerType.CoarseTimer)
        self.controller._watchdog_timer.timeout.connect(self.on_watchdog_tick)
        self.controller._watchdog_timer.start(self.controller._watchdog_interval_ms)

        self.controller._periodic_refresh_interval_ms = 5 * 60 * 1000
        self.controller._periodic_refresh_timer = QTimer()
        self.controller._periodic_refresh_timer.setTimerType(Qt.TimerType.VeryCoarseTimer)
        self.controller._periodic_refresh_timer.timeout.connect(
            self.on_periodic_hook_refresh
        )
        self.controller._periodic_refresh_timer.start(
            self.controller._periodic_refresh_interval_ms
        )

        logging.info(
            "Hook watchdog started: sleep detection every 10s, periodic refresh every 5m"
        )

    def on_watchdog_tick(self) -> None:
        """Check for time gaps indicating system sleep/resume."""
        now = time.monotonic()
        gap = now - self.controller._expected_watchdog_time
        self.controller._expected_watchdog_time = now + (
            self.controller._watchdog_interval_ms / 1000.0
        )

        if gap > self.controller._sleep_gap_threshold_sec:
            logging.warning(
                f"Sleep/resume detected: time gap of {gap:.1f}s. "
                "Re-registering keyboard hook."
            )
            self.rehook_keyboard()

    def on_periodic_hook_refresh(self) -> None:
        """Periodically re-register the keyboard hook."""
        now = time.monotonic()
        if now - self.controller._last_rehook_time < 60.0:
            return

        logging.info("Periodic keyboard hook refresh")
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
                logging.error(f"Failed to re-register keyboard hook: {exc}")

    def on_stt_state_changed(self, enabled: bool) -> None:
        """Handle STT state changes on the main thread."""
        overlay = self.controller.ui_controller.overlay
        if enabled:
            overlay.show_at_cursor(overlay.STATE_STT_ENABLE)
        else:
            overlay.show_at_cursor(overlay.STATE_STT_DISABLE)

    def update_status_with_auto_hide(self, status: str) -> None:
        """Emit a thread-safe status update and optional STT state change."""
        self.controller.status_update.emit(status)

        if status == "STT Enabled":
            self.controller.stt_state_changed.emit(True)
        elif status == "STT Disabled":
            self.controller.stt_state_changed.emit(False)

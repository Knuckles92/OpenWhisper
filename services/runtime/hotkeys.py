"""Hotkey setup and watchdog behavior for the application controller."""

from __future__ import annotations

import itertools
import logging
import sys
import time
from typing import TYPE_CHECKING, Dict, Optional

from PyQt6.QtCore import QTimer, Qt

from config import config
from services import synthetic_keys, text_transforms
from services._hotkey_common import OrderedDispatcher
from services.hotkey_manager import HotkeyManager, USE_PYNPUT_BACKEND
from services.cleanup_profiles import load_cleanup_profiles
from services.hotkey_conflicts import PROFILE, TRANSFORM, hotkey_conflict
from services.settings import (
    SETTING_DEFAULTS,
    RecordingTriggerMode,
    SettingsKey,
    resolve_recording_hands_free_latch,
    resolve_recording_trigger_mode,
    settings_manager,
)
from ui_qt.overlay_state import OverlayState

if TYPE_CHECKING:
    from services.application_controller import ApplicationController

logger = logging.getLogger(__name__)

#: Set to True on a top-level window (the Scratchpad) to give it the same
#: focused-window shortcuts as the main window.
HOTKEY_WINDOW_PROPERTY = "openwhisperHotkeyWindow"

KEYS_STILL_HELD = "Didn't paste the original: the shortcut keys were still held"


def is_hotkey_window(window, main_window) -> bool:
    return window is not None and (
        window is main_window or window.property(HOTKEY_WINDOW_PROPERTY) is True
    )


# Push-and-hold states. HOLDING: our press started a recording. TAP_PENDING: a
# short tap ended and a second press within RECORD_LATCH_WINDOW_MS latches the
# recording; otherwise it cancels. LATCHED: hands-free until the next press.
_IDLE = "idle"
_HOLDING = "holding"
_TAP_PENDING = "tap_pending"
_LATCHED = "latched"


# The Qt focus-window hotkey fallback is only needed for the pynput backend
# (macOS/Linux), which cannot suppress global keys. On Windows the keyboard
# backend swallows hotkeys globally, so none of this machinery is imported or
# defined there.
if USE_PYNPUT_BACKEND:
    from PyQt6.QtCore import QObject, QEvent
    from PyQt6.QtWidgets import QApplication

    from services.hotkey_manager import (
        is_accessibility_trusted,
    )

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
        key = int(event.key())
        if key in _QT_MODIFIER_KEYS:
            return None

        if sys.platform.startswith("linux") and event.modifiers() & Qt.KeyboardModifier.KeypadModifier:
            name = _QT_MAIN_KEY_NAMES.get(key)
            if name is None and 33 <= key <= 126:
                name = chr(key).lower()
            if name is not None:
                return f"kp {name}"

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
            is_press = event.type() == QEvent.Type.KeyPress
            is_release = event.type() == QEvent.Type.KeyRelease
            if not is_press and not is_release:
                return False
            if event.isAutoRepeat() or not self._hotkey_window_is_active():
                return False

            hotkey_manager = self.controller.hotkey_manager
            if hotkey_manager is None:
                return False

            main_key = _qt_event_key_name(event)
            if main_key is None:
                return False

            if is_release:
                handled = hotkey_manager.handle_hotkey_release(
                    _qt_event_modifiers(event),
                    main_key,
                    source="qt",
                )
            else:
                handled = hotkey_manager.handle_hotkey_press(
                    _qt_event_modifiers(event),
                    main_key,
                    source="qt",
                )
            if handled:
                event.accept()
            return handled

        def _hotkey_window_is_active(self) -> bool:
            app = QApplication.instance()
            if app is None:
                return False
            return is_hotkey_window(
                app.activeWindow(), self.controller.ui_controller.main_window
            )


class HotkeyRuntime:
    """Owns hotkey configuration and keyboard hook lifecycle."""

    def __init__(self, controller: "ApplicationController"):
        self.controller = controller
        self._active_window_hotkey_filter: Optional[ActiveWindowHotkeyFilter] = None
        self._omarchy_controls = None
        # Record and Command Mode events run here in hook order, so a release
        # never overtakes its press and the latch sees real timestamps.
        self._dispatcher = OrderedDispatcher("hotkey-dispatch")
        # Push-and-hold bookkeeping, owned by the dispatcher thread; a mode
        # change resets it from the Qt thread.
        self._record_press_monotonic: Optional[float] = None
        self._record_start_accepted = False
        self._hold_state = _IDLE
        self._latch_generation = 0
        self._latch_deadline = 0.0
        # Counts recordings that ended for any reason (stop, cancel, tray,
        # error). A latch belongs to the recording its hold started, so a
        # changed count means that recording is gone.
        self._recording_end_counter = itertools.count(1)
        self._recordings_ended = 0
        self._hold_epoch = 0
        # A periodic refresh that found a shortcut held; the watchdog runs it
        # once the key is up.
        self._refresh_pending = False

    def setup_hotkeys(self) -> None:
        logger.info("Setting up hotkeys...")
        # Backfill any newly-introduced default actions (e.g. minimize_tray) over
        # saved settings, so existing users get new hotkeys without reconfiguring.
        # load_hotkey_settings deliberately returns saved data unmerged, so the
        # merge happens here at the point of use.
        hotkeys = {**config.DEFAULT_HOTKEYS, **settings_manager.load_hotkey_settings()}
        controller = self.controller
        controller.hotkey_manager = HotkeyManager(hotkeys)
        controller.hotkey_manager.set_record_mode(resolve_recording_trigger_mode())
        controller.hotkey_manager.set_callbacks(
            on_record_toggle=controller.toggle_recording,
            on_record_press=self._queue_record_press,
            on_record_release=self._queue_record_release,
            on_command_press=self._queue_command_press,
            on_command_release=self._queue_command_release,
            on_cancel=controller.cancel,
            on_minimize_tray=controller.minimize_to_tray,
            on_meeting_toggle=controller.toggle_meeting_mode,
            on_scratchpad_toggle=controller.scratchpad_toggle_requested.emit,
            on_cycle_language=controller.cycle_language_requested.emit,
            on_paste_last_original=self._paste_last_original_after_keys_up,
            on_status_update=controller.update_status_with_auto_hide,
            on_status_update_auto_hide=controller.update_status_with_auto_hide,
        )
        controller.recording_state_changed.connect(self._on_recording_state_changed)
        controller.ui_controller.update_hotkey_display(hotkeys)
        self.refresh_profile_hotkeys()
        self._install_active_window_hotkey_filter()
        self._check_autopaste_permission()
        from services.desktop_session import is_wayland_session, use_omarchy_ui

        if is_wayland_session() and use_omarchy_ui():
            from services.omarchy_controls import OmarchyControls

            controls = OmarchyControls(controller)
            if controls.start():
                self._omarchy_controls = controls

    def set_recording_trigger_mode(self, mode: str) -> None:
        """Apply a new record hotkey activation mode without re-hooking."""
        if mode not in RecordingTriggerMode.ALL:
            mode = config.RECORDING_TRIGGER_MODE
        logger.info("Recording trigger mode set to %s", mode)
        self._reset_hold()
        if self.controller.hotkey_manager:
            self.controller.hotkey_manager.set_record_mode(mode)
        if mode == RecordingTriggerMode.PUSH_HOLD:
            self.controller.status_update.emit("Push-and-hold recording enabled")
        else:
            self.controller.status_update.emit("Toggle recording enabled")

    def _paste_last_original_after_keys_up(self) -> None:
        """Hand the paste to the Qt thread once the shortcut's modifiers are up.

        Runs on the backend's own thread for this press. The paste keystroke
        does not release keys the user still holds, so Ctrl+Alt+O would reach
        the app as Ctrl+Alt+V (Paste Special, or nothing).
        """
        if not synthetic_keys.wait_for_modifiers_released():
            self.controller.status_update.emit(KEYS_STILL_HELD)
            return
        self.controller.paste_last_original_requested.emit()

    def _queue_record_press(self, at: float) -> None:
        self._dispatcher.submit(self.record_key_pressed, at)

    def _queue_record_release(self, at: float) -> None:
        self._dispatcher.submit(self.record_key_released, at)

    def _queue_command_press(self, at: float) -> None:
        self._dispatcher.submit(self.controller.command_key_pressed, at)

    def _queue_command_release(self, at: float) -> None:
        self._dispatcher.submit(self.controller.command_key_released, at)

    def _on_recording_state_changed(self, recording: bool) -> None:
        if recording:
            return
        self._recordings_ended = next(self._recording_end_counter)
        if self._hold_state in (_TAP_PENDING, _LATCHED):
            self._dispatcher.submit(self._drop_stale_latch)

    def record_key_pressed(self, at: Optional[float] = None) -> None:
        """Push-and-hold: the record hotkey went down at ``at`` (monotonic).

        Runs on the dispatcher thread; ``at`` defaults to now.
        """
        at = time.monotonic() if at is None else at
        self._expire_tap(at)
        self._drop_stale_latch()
        if self._hold_state == _TAP_PENDING:
            self._latch()
            return
        if self._hold_state == _LATCHED:
            self._reset_hold()
            self.controller.stop_recording()
            return
        if (
            self._hold_state == _HOLDING
            and self._record_start_accepted
            and self._recordings_ended == self._hold_epoch
            and self.controller.recorder.is_recording
        ):
            # This hold's release was lost (a hook refresh forgot the held
            # key), so the press continues the hold and its release stops it.
            return
        self._reset_hold()
        if self.controller.recorder.is_recording:
            # A previous stop's post-roll still owns the recorder; ignore the
            # press rather than surfacing a failed-start status.
            return
        self._record_press_monotonic = at
        self._hold_epoch = self._recordings_ended
        self._record_start_accepted = bool(self.controller.start_recording())
        if self._record_start_accepted:
            self._hold_state = _HOLDING

    def record_key_released(self, at: Optional[float] = None) -> None:
        """Push-and-hold: the record hotkey came up at ``at``; stop, cancel or wait.

        Runs on the dispatcher thread; ``at`` defaults to now.
        """
        at = time.monotonic() if at is None else at
        self._expire_tap(at)
        if self._hold_state in (_TAP_PENDING, _LATCHED):
            return  # the latching press's own release
        press_time = self._record_press_monotonic
        self._record_press_monotonic = None
        self._hold_state = _IDLE
        if not self._record_start_accepted:
            return  # start was refused; its status message already surfaced
        self._record_start_accepted = False
        # The start can return before the stream opens, so wait briefly for
        # is_recording before deciding between stop and cancel.
        deadline = time.monotonic() + 0.5
        while not self.controller.recorder.is_recording and time.monotonic() < deadline:
            time.sleep(0.01)
        if not self.controller.recorder.is_recording:
            return  # start failed internally; its failure path updated status
        if getattr(self.controller.recorder, "capture_canceled", False) is True:
            # The cancel hotkey already claimed this hold. No audio yet is
            # not a cancel: the first block can arrive after a quick tap.
            return
        held_ms = (at - press_time) * 1000 if press_time else 0
        if held_ms >= config.RECORD_MIN_HOLD_MS:
            self.controller.stop_recording()
        elif not resolve_recording_hands_free_latch():
            self.controller.cancel()
        elif self._recordings_ended == self._hold_epoch:
            self._await_second_tap(at)

    def _await_second_tap(self, released_at: float) -> None:
        self._hold_state = _TAP_PENDING
        self._latch_generation += 1
        self._latch_deadline = released_at + config.RECORD_LATCH_WINDOW_MS / 1000
        self._dispatcher.call_at(
            self._latch_deadline, self._on_tap_deadline, self._latch_generation
        )

    def _on_tap_deadline(self, generation: int) -> None:
        if generation == self._latch_generation and self._hold_state == _TAP_PENDING:
            self._cancel_tap()

    def _expire_tap(self, at: float) -> None:
        """Settle a pending tap whose window closed before an event at ``at``."""
        if self._hold_state == _TAP_PENDING and at > self._latch_deadline:
            self._cancel_tap()

    def _cancel_tap(self) -> None:
        self._reset_hold()
        if self._recordings_ended != self._hold_epoch:
            return  # it already ended another way, maybe into transcription
        recorder = self.controller.recorder
        if recorder.is_recording and getattr(recorder, "capture_canceled", False) is not True:
            self.controller.cancel()

    def _latch(self) -> None:
        from services.hotkey_manager import format_hotkey_display

        self._hold_state = _LATCHED
        self._latch_generation += 1
        self._dispatcher.cancel_deadline()
        manager = self.controller.hotkey_manager
        hotkey = manager.hotkeys.get("record_toggle", "") if manager else ""
        self.controller.status_update.emit(
            f"Hands-free · press {format_hotkey_display(hotkey)} to stop"
        )
        self.controller.hands_free_changed.emit(True)

    def _drop_stale_latch(self) -> None:
        if (
            self._hold_state in (_TAP_PENDING, _LATCHED)
            and self._recordings_ended != self._hold_epoch
        ):
            self._reset_hold()

    def _reset_hold(self) -> None:
        was_latched = self._hold_state == _LATCHED
        self._hold_state = _IDLE
        self._latch_generation += 1
        self._record_press_monotonic = None
        self._record_start_accepted = False
        if was_latched:
            self.controller.hands_free_changed.emit(False)

    @staticmethod
    def _auto_paste_enabled() -> bool:
        key = SettingsKey.AUTO_PASTE
        return bool(settings_manager.get(key, SETTING_DEFAULTS[key]))

    @staticmethod
    def _accessibility_intro_seen() -> bool:
        key = SettingsKey.MACOS_ACCESSIBILITY_INTRO_SEEN
        return bool(settings_manager.get(key, SETTING_DEFAULTS[key]))

    def _check_autopaste_permission(self) -> None:
        """Offer optional setup once, remembering dismissal across launches."""
        if sys.platform != "darwin" or not USE_PYNPUT_BACKEND:
            return
        if not self._auto_paste_enabled() or self._accessibility_intro_seen():
            return
        if is_accessibility_trusted():
            return
        QTimer.singleShot(0, self._warn_autopaste_not_trusted)

    def _warn_autopaste_not_trusted(self) -> None:
        # Preferences or trust may change before this deferred call runs.
        if (
            is_accessibility_trusted()
            or not self._auto_paste_enabled()
            or self._accessibility_intro_seen()
        ):
            return
        from ui_qt.dialogs.accessibility_dialog import show_accessibility_setup

        show_accessibility_setup(self.controller.ui_controller.main_window)

    def update_hotkeys(self, hotkeys: Dict[str, str]) -> None:
        logger.info(f"Updating hotkeys: {hotkeys}")
        if self.controller.hotkey_manager:
            self.controller.hotkey_manager.update_hotkeys(hotkeys)
            settings_manager.save_hotkey_settings(hotkeys)
            self.controller.ui_controller.update_hotkey_display(hotkeys)
            self.controller.ui_controller.set_status("Hotkeys updated")
            self.refresh_profile_hotkeys()

    def refresh_profile_hotkeys(self) -> None:
        """Register profile and transform shortcuts.

        A shortcut that collides with any other binding is skipped rather than
        letting whichever matches first win.
        """
        manager = self.controller.hotkey_manager
        if manager is None:
            return
        settings = settings_manager.load_all_settings()

        def free(hotkey: str, kind: str, item_id: str) -> bool:
            return bool(hotkey) and not hotkey_conflict(
                hotkey, settings, exclude=(kind, item_id), standard_hotkeys=manager.hotkeys
            )

        profiles = {
            p.id: p.hotkey for p in load_cleanup_profiles(settings)
            if free(p.hotkey, PROFILE, p.id)
        }
        try:
            transforms = {
                t.id: t.hotkey for t in text_transforms.load_transforms(settings)
                if free(t.hotkey, TRANSFORM, t.id)
            }
        except Exception:
            logger.exception("Could not load transform shortcuts")
            transforms = {}
        manager.set_dynamic_hotkeys(
            PROFILE, profiles, self.controller.profile_record_requested.emit
        )
        manager.set_dynamic_hotkeys(
            TRANSFORM, transforms, self.controller.transform_requested.emit
        )
        if self._omarchy_controls:
            self._omarchy_controls.refresh()

    def set_capture_suspended(self, suspended: bool) -> None:
        if self.controller.hotkey_manager:
            self.controller.hotkey_manager.set_capture_suspended(suspended)
            if self._omarchy_controls:
                self._omarchy_controls.refresh()

    def setup_hook_watchdog(self) -> None:
        from services.desktop_session import is_wayland_session

        if is_wayland_session():
            logger.info("Wayland focused-window shortcuts do not need a hook watchdog")
            return
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
        self._dispatcher.stop()
        if self._omarchy_controls:
            self._omarchy_controls.close()
            self._omarchy_controls = None
        if self._active_window_hotkey_filter is None:
            return

        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self._active_window_hotkey_filter)
        self._active_window_hotkey_filter = None

    def on_watchdog_tick(self) -> None:
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
        elif self._refresh_pending:
            self.on_periodic_hook_refresh()

    def on_periodic_hook_refresh(self) -> None:
        now = time.monotonic()
        if now - self.controller._last_rehook_time < 60.0:
            self._refresh_pending = False
            return
        # A rehook forgets held keys, so refreshing in the middle of a
        # push-and-hold would drop its release and leave the recording running.
        hold_active = getattr(self.controller.hotkey_manager, "keyboard_hold_active", None)
        if hold_active is not None and hold_active():
            if not self._refresh_pending:
                logger.info("Keyboard hook refresh waits for the held shortcut")
            self._refresh_pending = True
            return

        self._refresh_pending = False
        logger.info("Periodic keyboard hook refresh")
        self.rehook_keyboard()

    def rehook_keyboard(self) -> None:
        from services.desktop_session import is_wayland_session

        if is_wayland_session():
            return
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
        state = OverlayState.STT_ENABLED if enabled else OverlayState.STT_DISABLED
        self.controller.overlay_state_update.emit(state)

    def update_status_with_auto_hide(self, status: str) -> None:
        """Emit a thread-safe status update and optional STT state change."""
        self.controller.status_update.emit(status)

        if status == "STT Enabled":
            self.controller.stt_state_changed.emit(True)
        elif status == "STT Disabled":
            self.controller.stt_state_changed.emit(False)

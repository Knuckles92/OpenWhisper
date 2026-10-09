"""Windows hotkeys with per-key suppression via ``keyboard``."""
import keyboard
import logging
import threading
import time
from typing import Dict, Callable, Optional, Tuple
from config import config
from services._hotkey_common import (
    DYNAMIC_NAMESPACES,
    MOUSE_KEYS,
    PRESS_ACTIONS,
    Debouncer,
    format_hotkey_string,
    is_mouse_key,
    notify_stt_toggle,
    parse_hotkey_string,
)
from services._mouse_hook_win import MouseButtonHook
from services.settings import RecordingTriggerMode

logger = logging.getLogger(__name__)


_MODIFIER_ALIASES: Dict[str, str] = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "shift": "shift",
    "win": "win",
    # keyboard.get_hotkey_name spells the Windows key this way.
    "windows": "win",
    "super": "win",
    "cmd": "win",
    "meta": "win",
}

_ALL_MODIFIERS = ("ctrl", "alt", "shift", "win")

# Windows repeats a held key within its repeat delay (at most 1 s, 2 s with
# Filter Keys). A later key-down of the same key is a new press whose key-up
# the hook missed, as happens while an elevated window has focus.
_REPEAT_WINDOW_S = 2.5


def parse_hotkey(hotkey_string: str) -> Tuple[frozenset, Optional[str]]:
    """Parse a hotkey while preserving ``keyboard`` event key names."""
    return parse_hotkey_string(hotkey_string, _MODIFIER_ALIASES)


def format_hotkey(modifiers, main_key: Optional[str]) -> str:
    """Build a canonical hotkey string from a modifier set and a main key name."""
    return format_hotkey_string(modifiers, main_key, _ALL_MODIFIERS)


_DISPLAY_MODIFIERS: Dict[str, str] = {
    "ctrl": "Ctrl",
    "alt": "Alt",
    "shift": "Shift",
    "win": "Win",
}


def format_hotkey_display(hotkey_string: str) -> str:
    """Format a canonical hotkey string for on-screen display (plain text)."""
    if not hotkey_string:
        return ""

    modifiers, main_key = parse_hotkey(hotkey_string)
    if main_key is None:
        return hotkey_string

    parts = [_DISPLAY_MODIFIERS[m] for m in _ALL_MODIFIERS if m in modifiers]
    # Numpad keys are stored as "kp *" / "kp -"; show just the symbol.
    if main_key.startswith("kp "):
        main_display = main_key[3:]
    elif main_key in MOUSE_KEYS:
        main_display = MOUSE_KEYS[main_key]
    elif len(main_key) == 1:
        main_display = main_key.upper()
    else:
        main_display = main_key.title()
    parts.append(main_display)
    return "+".join(parts)


def mouse_shortcuts_supported() -> bool:
    return True


def send_paste() -> None:
    """Simulate a paste keystroke (Ctrl+V) via the keyboard library."""
    keyboard.send("ctrl+v")


def is_accessibility_trusted() -> bool:
    """No-op on Windows: the keyboard backend needs no Accessibility grant."""
    return True


def request_accessibility_trust() -> bool:
    """No-op on Windows; present so the dispatcher's API is uniform."""
    return True


def accessibility_permission_instructions() -> str:
    """No-op on Windows; present so the dispatcher's API is uniform."""
    return ""


def accessibility_permission_diagnostics() -> str:
    """No-op on Windows; present so the dispatcher's API is uniform."""
    return ""


def _spawn(callback: Callable, *args) -> None:
    threading.Thread(target=callback, args=args, daemon=True).start()


def _physical_key(event) -> tuple:
    """Identify the key behind an event.

    ``keyboard`` names an event from the Shift state at that moment, so
    Ctrl+Shift+1 can go down as "!" and come up as "1". Repeats and the
    key-up keep the scan code; the numpad flag separates keys that share one.
    """
    scan_code = getattr(event, "scan_code", None)
    if scan_code is None:
        return (event.name or "").lower(), bool(event.is_keypad)
    return scan_code, bool(event.is_keypad)


class HotkeyManager:
    """Manages global hotkeys and keyboard event handling."""

    def __init__(self, hotkeys: Dict[str, str] = None):
        self.hotkeys = hotkeys or config.DEFAULT_HOTKEYS.copy()
        self.program_enabled = True
        self.record_mode = RecordingTriggerMode.TOGGLE
        self._debouncer = Debouncer(config.HOTKEY_DEBOUNCE_MS)
        # Guard auto-repeat presses while a hold key is down, and pair each
        # suppressed press with its release.
        self._record_key_held = False
        self._command_key_held = False
        self._press_held: Dict[str, str] = {}
        self._dynamic_hotkeys: Dict[str, Dict[str, str]] = {}
        self._dynamic_callbacks: Dict[str, Callable] = {}
        self._dynamic_debouncers: Dict[Tuple[str, str], Debouncer] = {}
        self._dynamic_held: Dict[Tuple[str, str], str] = {}
        # The physical key each keyboard hold was pressed on, by hold
        # ("record_toggle", "command_mode", "enable_disable", a press action
        # or a family key), and the key that went down last (with its time),
        # the only one Windows auto-repeats.
        self._hold_keys: Dict[object, tuple] = {}
        self._last_down: Optional[Tuple[tuple, float]] = None
        self.capture_suspended = False
        # The keyboard hook thread and the mouse dispatch thread share the
        # held flags above.
        self._dispatch_lock = threading.RLock()
        self._mouse_hook = MouseButtonHook(self._claims_mouse_button, self._on_mouse_button)

        self.on_record_toggle: Optional[Callable] = None
        self.on_record_press: Optional[Callable] = None
        self.on_record_release: Optional[Callable] = None
        self.on_command_press: Optional[Callable] = None
        self.on_command_release: Optional[Callable] = None
        self.on_cancel: Optional[Callable] = None
        self.on_enable_toggle: Optional[Callable] = None
        self.on_minimize_tray: Optional[Callable] = None
        self.on_meeting_toggle: Optional[Callable] = None
        self.on_scratchpad_toggle: Optional[Callable] = None
        self.on_cycle_language: Optional[Callable] = None
        self.on_paste_last_original: Optional[Callable] = None
        self.on_status_update: Optional[Callable] = None
        self.on_status_update_auto_hide: Optional[Callable] = None
        self.is_transcribing_fn: Optional[Callable[[], bool]] = None

        self._setup_keyboard_hook()

    def _setup_keyboard_hook(self):
        # A rehook may miss a KEY_UP while unhooked, so forget keyboard holds.
        # The mouse hook still swallows and reports the release of a button
        # it swallowed before the restart, so side-button holds carry over.
        self._forget_held_keys(keep_mouse=True)
        keyboard.hook(self._handle_keyboard_event, suppress=True)
        self._sync_mouse_hook()

    def _forget_held_keys(self, keep_mouse: bool = False) -> None:
        def forget(hotkey: Optional[str]) -> bool:
            return not (keep_mouse and is_mouse_key(parse_hotkey(hotkey or "")[1]))

        if forget(self.hotkeys.get('record_toggle')):
            self._record_key_held = False
        if forget(self.hotkeys.get('command_mode')):
            self._command_key_held = False
        for held in (self._press_held, self._dynamic_held):
            for hold, hotkey in tuple(held.items()):
                if forget(hotkey):
                    held.pop(hold, None)
        self._hold_keys.clear()
        self._last_down = None

    def _handle_keyboard_event(self, event):
        if self.capture_suspended:
            return True
        at = time.monotonic()
        key = _physical_key(event)

        def is_on_key(hold, hotkey: Optional[str]) -> bool:
            held_key = self._hold_keys.get(hold)
            if held_key is None:
                return self._matches_main_key(event, hotkey)
            return held_key == key

        if event.event_type == keyboard.KEY_DOWN:
            with self._dispatch_lock:
                last = self._last_down
                repeat = last is not None and last[0] == key and at - last[1] <= _REPEAT_WINDOW_S
                self._last_down = (key, at)
                # Windows keeps repeating the main key after the user lets go
                # of a modifier, so a repeat can match a different shortcut
                # (Ctrl+Num* held, Ctrl released: the repeat matches Num*). A
                # repeat of a held key is never a new press.
                if repeat and self._holds(is_on_key):
                    return False
                if not repeat:
                    # Press-only holds exist only to absorb repeats, and a new
                    # key ends those, even when a key-up was never seen.
                    self._end_press_holds()
                return not self._press(
                    lambda hotkey: self._matches_hotkey(event, hotkey), at, key
                )
        if event.event_type == keyboard.KEY_UP:
            with self._dispatch_lock:
                if self._last_down is not None and self._last_down[0] == key:
                    self._last_down = None
                return not self._release(is_on_key, at)
        return True

    def _holds(self, is_on_key: Callable[[object, Optional[str]], bool]) -> bool:
        return (
            (self._record_key_held and is_on_key('record_toggle', self.hotkeys.get('record_toggle')))
            or (self._command_key_held and is_on_key('command_mode', self.hotkeys.get('command_mode')))
            or any(is_on_key(hold, hotkey) for hold, hotkey in self._press_held.items())
            or any(is_on_key(hold, hotkey) for hold, hotkey in self._dynamic_held.items())
        )

    def keyboard_hold_active(self, now: Optional[float] = None) -> bool:
        """Whether the record or Command Mode shortcut is held on a key right now.

        A rehook forgets keyboard holds, so a refresh during one would lose
        its release. Windows keeps repeating a held key and only a live hook
        sees the repeats: a hold with no recent key-down has lost its release
        or its hook, and then a refresh should go ahead.
        """
        now = time.monotonic() if now is None else now
        with self._dispatch_lock:
            last = self._last_down
            if last is None or now - last[1] > _REPEAT_WINDOW_S:
                return False
            return (
                (self._record_key_held and self._hold_keys.get('record_toggle') == last[0])
                or (self._command_key_held and self._hold_keys.get('command_mode') == last[0])
            )

    def _hold_on(self, hold, key: Optional[tuple]) -> None:
        """Remember the physical key a hold was pressed on (None for a mouse button)."""
        if key is None:
            self._hold_keys.pop(hold, None)
        else:
            self._hold_keys[hold] = key

    def _end_press_holds(self) -> None:
        for held in (self._press_held, self._dynamic_held):
            for hold in tuple(held):
                self._hold_keys.pop(hold, None)
            held.clear()

    def _press(
        self,
        matches: Callable[[Optional[str]], bool],
        at: float,
        key: Optional[tuple] = None,
    ) -> bool:
        """Dispatch a press; True when a shortcut claimed it (and suppresses it).

        ``key`` is the physical key of a keyboard press, None for a mouse
        button. Record and Command Mode presses carry the hook's timestamp to
        callbacks that must only enqueue; other actions run on their own thread.
        """
        with self._dispatch_lock:
            enable_disable = self.hotkeys.get('enable_disable')
            if matches(enable_disable):
                self._press_held['enable_disable'] = enable_disable
                self._hold_on('enable_disable', key)
                self._toggle_program_enabled()
                return True
            if not self.program_enabled:
                return False

            if matches(self.hotkeys.get('record_toggle')):
                if not self._record_key_held:
                    self._record_key_held = True
                    self._hold_on('record_toggle', key)
                    if self.record_mode == RecordingTriggerMode.PUSH_HOLD:
                        if self.on_record_press:
                            self.on_record_press(at)
                    elif self._should_trigger_record_toggle() and self.on_record_toggle:
                        _spawn(self.on_record_toggle)
                return True

            if matches(self.hotkeys.get('command_mode')):
                if not self._command_key_held:
                    self._command_key_held = True
                    self._hold_on('command_mode', key)
                    if self.on_command_press:
                        self.on_command_press(at)
                return True

            for action, attribute in PRESS_ACTIONS:
                hotkey = self.hotkeys.get(action)
                if matches(hotkey):
                    self._press_held[action] = hotkey
                    self._hold_on(action, key)
                    callback = getattr(self, attribute)
                    if callback:
                        _spawn(callback)
                    return True

            for namespace, hotkeys in self._dynamic_hotkeys.items():
                for item_id, hotkey in hotkeys.items():
                    if not matches(hotkey):
                        continue
                    hold = (namespace, item_id)
                    if hold not in self._dynamic_held:
                        self._dynamic_held[hold] = hotkey
                        self._hold_on(hold, key)
                        debouncer = self._dynamic_debouncers.get(hold)
                        callback = self._dynamic_callbacks.get(namespace)
                        if debouncer and debouncer.should_trigger() and callback:
                            _spawn(callback, item_id)
                    return True
            return False

    def _release(self, is_on_key: Callable[[object, Optional[str]], bool], at: float) -> bool:
        """Dispatch a release; True when it belongs to a claimed press.

        ``is_on_key(hold, hotkey)`` says whether the released key or button is
        the one a hold was pressed on. It ignores modifiers, since users often
        let go of them first, and every hold on that key ends so none is
        stranded. Releases skip the program_enabled gate on purpose: disabling
        hotkeys mid-hold must not strand a recording.
        """
        with self._dispatch_lock:
            claimed = False
            if self._record_key_held and is_on_key('record_toggle', self.hotkeys.get('record_toggle')):
                self._record_key_held = False
                self._hold_keys.pop('record_toggle', None)
                if (self.record_mode == RecordingTriggerMode.PUSH_HOLD
                        and self.on_record_release):
                    self.on_record_release(at)
                claimed = True

            if self._command_key_held and is_on_key('command_mode', self.hotkeys.get('command_mode')):
                self._command_key_held = False
                self._hold_keys.pop('command_mode', None)
                if self.on_command_release:
                    self.on_command_release(at)
                claimed = True

            for held in (self._press_held, self._dynamic_held):
                released = [hold for hold, hotkey in tuple(held.items()) if is_on_key(hold, hotkey)]
                for hold in released:
                    held.pop(hold, None)
                    self._hold_keys.pop(hold, None)
                claimed = claimed or bool(released)
            return claimed

    def _all_hotkeys(self) -> dict:
        # Snapshots: the mouse hook reads this while a refresh may replace a family.
        return {
            **dict(self.hotkeys),
            **{
                f"{namespace}:{item_id}": hotkey
                for namespace, hotkeys in tuple(self._dynamic_hotkeys.items())
                for item_id, hotkey in hotkeys.items()
            },
        }

    def _claims_mouse_button(self, button: str, modifiers: frozenset) -> bool:
        """Whether a side-button press belongs to a shortcut; asked inside the hook."""
        if self.capture_suspended:
            return False
        signature = (modifiers, button)
        if parse_hotkey(self.hotkeys.get('enable_disable') or "") == signature:
            return True
        if not self.program_enabled:
            return False
        return any(
            parse_hotkey(hotkey) == signature
            for hotkey in self._all_hotkeys().values() if hotkey
        )

    def _on_mouse_button(self, button: str, pressed: bool, modifiers: frozenset, at: float) -> None:
        if pressed:
            if self.capture_suspended:
                return
            self._press(lambda hotkey: parse_hotkey(hotkey or "") == (modifiers, button), at)
        else:
            self._release(lambda _hold, hotkey: parse_hotkey(hotkey or "")[1] == button, at)

    def _sync_mouse_hook(self) -> None:
        """Run the mouse hook only while a side button is bound and capture is live."""
        wanted = not self.capture_suspended and any(
            is_mouse_key(parse_hotkey(hotkey)[1])
            for hotkey in self._all_hotkeys().values() if hotkey
        )
        try:
            if wanted:
                self._mouse_hook.start()
            else:
                self._mouse_hook.stop()
        except Exception as exc:
            logger.warning("Mouse button shortcuts unavailable: %s", exc)

    def set_dynamic_hotkeys(
        self, namespace: str, hotkeys: Dict[str, str], callback: Callable[[str], None]
    ) -> None:
        """Replace one runtime family of shortcuts; ``callback(id)`` fires per press.

        Args:
            namespace: One of DYNAMIC_NAMESPACES ("profile", "transform").
        """
        if namespace not in DYNAMIC_NAMESPACES:
            raise ValueError(f"Unknown shortcut family: {namespace}")
        with self._dispatch_lock:
            self._dynamic_callbacks[namespace] = callback
            self._dynamic_hotkeys[namespace] = dict(hotkeys)
            self._dynamic_debouncers = {
                key: debouncer for key, debouncer in self._dynamic_debouncers.items()
                if key[0] != namespace
            }
            for item_id in hotkeys:
                self._dynamic_debouncers[(namespace, item_id)] = Debouncer(
                    config.HOTKEY_DEBOUNCE_MS
                )
        self._sync_mouse_hook()

    def set_profile_hotkeys(self, hotkeys: Dict[str, str], callback: Callable) -> None:
        self.set_dynamic_hotkeys("profile", hotkeys, callback)

    def set_capture_suspended(self, suspended: bool) -> None:
        self.capture_suspended = suspended
        self._forget_held_keys()
        self._sync_mouse_hook()

    def set_record_mode(self, mode: str) -> None:
        """Switch the record hotkey between toggle and push-and-hold."""
        self.record_mode = mode
        self._record_key_held = False
        self._hold_keys.pop('record_toggle', None)

    def _toggle_program_enabled(self):
        self.program_enabled = not self.program_enabled

        self._debouncer.reset()

        notify_stt_toggle(
            self.program_enabled, self.on_status_update_auto_hide, self.on_status_update
        )

    def _should_trigger_record_toggle(self) -> bool:
        return self._debouncer.should_trigger()

    def _matches_record_main_key(self, event) -> bool:
        """Match a KEY_UP against the record hotkey's main key, modifiers aside.

        The user may release modifiers before the main key, so release
        matching cannot require the hotkey's modifier set.
        """
        return self._matches_main_key(event, self.hotkeys.get('record_toggle'))

    @staticmethod
    def _matches_main_key(event, hotkey_string: str) -> bool:
        if not hotkey_string:
            return False

        main_key = hotkey_string.lower().split('+')[-1]
        is_numpad_hotkey = main_key.startswith('kp ')
        expected_key_name = main_key[3:] if is_numpad_hotkey else main_key

        if not event.name or event.name.lower() != expected_key_name:
            return False

        if (is_numpad_hotkey and not event.is_keypad) or (
            not is_numpad_hotkey and event.is_keypad
        ):
            return False

        return True

    def _matches_hotkey(self, event, hotkey_string: str) -> bool:
        if not self._matches_main_key(event, hotkey_string):
            return False
        required, _ = parse_hotkey(hotkey_string)
        return all(
            keyboard.is_pressed(modifier) == (modifier in required)
            for modifier in _ALL_MODIFIERS
        )

    def rehook(self):
        """Re-register the keyboard hook after sleep/resume or degradation.

        Preserves all state (hotkeys, callbacks, enabled status).
        Must be called from the main thread.
        """
        logger.info("Re-registering keyboard hook...")
        try:
            self.cleanup()
        except Exception as e:
            logger.warning(f"Error during rehook cleanup: {e}")
        try:
            self._setup_keyboard_hook()
            logger.info("Keyboard hook re-registered successfully")
        except Exception as e:
            logger.error(f"Failed to re-register keyboard hook: {e}")

    def update_hotkeys(self, new_hotkeys: Dict[str, str]):
        """Replace the configured hotkey mappings."""
        self.hotkeys.update(new_hotkeys)
        self.cleanup()
        self._setup_keyboard_hook()
        logger.info("Hotkeys updated successfully")

    def cleanup(self):
        """Remove the keyboard and mouse hooks."""
        try:
            self._mouse_hook.stop()
        except Exception as e:
            logger.error(f"Error removing the mouse hook: {e}")
        try:
            # Use a timeout to avoid blocking if cleanup is called from wrong thread
            if threading.current_thread() is threading.main_thread():
                keyboard.unhook_all()
            else:
                # If called from non-main thread, just log a warning
                logger.warning("Hotkey cleanup called from non-main thread, skipping unhook")
        except RuntimeError as e:
            # Joining the current hook thread is harmless during shutdown.
            if "cannot join" not in str(e).lower():
                logger.error(f"Error cleaning up keyboard hooks: {e}")
        except Exception as e:
            logger.error(f"Error cleaning up keyboard hooks: {e}")

    def set_callbacks(self,
                     on_record_toggle: Callable = None,
                     on_record_press: Callable = None,
                     on_record_release: Callable = None,
                     on_cancel: Callable = None,
                     on_enable_toggle: Callable = None,
                     on_minimize_tray: Callable = None,
                     on_meeting_toggle: Callable = None,
                     on_status_update: Callable = None,
                     on_status_update_auto_hide: Callable = None,
                     is_transcribing_fn: Callable[[], bool] = None,
                     on_command_press: Callable = None,
                     on_command_release: Callable = None,
                     on_scratchpad_toggle: Callable = None,
                     on_cycle_language: Callable = None,
                     on_paste_last_original: Callable = None):
        """Set callbacks invoked by hotkey events.

        ``on_record_press``/``on_record_release`` and the command pair get the
        hook's ``time.monotonic()`` and run inside the hook, so they must only
        hand the event on. The rest run on a new thread per press.
        """
        self.on_record_toggle = on_record_toggle
        self.on_record_press = on_record_press
        self.on_record_release = on_record_release
        self.on_command_press = on_command_press
        self.on_command_release = on_command_release
        self.on_cancel = on_cancel
        self.on_enable_toggle = on_enable_toggle
        self.on_minimize_tray = on_minimize_tray
        self.on_meeting_toggle = on_meeting_toggle
        self.on_scratchpad_toggle = on_scratchpad_toggle
        self.on_cycle_language = on_cycle_language
        self.on_paste_last_original = on_paste_last_original
        self.on_status_update = on_status_update
        self.on_status_update_auto_hide = on_status_update_auto_hide
        self.is_transcribing_fn = is_transcribing_fn

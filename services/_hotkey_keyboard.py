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
        # A rehook may miss the KEY_UP while unhooked, so forget held state.
        self._forget_held_keys()
        keyboard.hook(self._handle_keyboard_event, suppress=True)
        self._sync_mouse_hook()

    def _forget_held_keys(self) -> None:
        self._record_key_held = False
        self._command_key_held = False
        self._press_held.clear()
        self._dynamic_held.clear()

    def _handle_keyboard_event(self, event):
        if self.capture_suspended:
            return True
        at = time.monotonic()
        if event.event_type == keyboard.KEY_DOWN:
            return not self._press(
                lambda hotkey: self._matches_hotkey(event, hotkey), at,
                lambda hotkey: self._matches_main_key(event, hotkey),
            )
        if event.event_type == keyboard.KEY_UP:
            return not self._release(
                lambda hotkey: self._matches_main_key(event, hotkey), at
            )
        return True

    def _holds_main_key(self, matches_main_key: Callable[[Optional[str]], bool]) -> bool:
        return (
            (self._record_key_held and matches_main_key(self.hotkeys.get('record_toggle')))
            or (self._command_key_held and matches_main_key(self.hotkeys.get('command_mode')))
            or any(matches_main_key(hotkey) for hotkey in self._press_held.values())
            or any(matches_main_key(hotkey) for hotkey in self._dynamic_held.values())
        )

    def _press(
        self,
        matches: Callable[[Optional[str]], bool],
        at: float,
        matches_main_key: Optional[Callable[[Optional[str]], bool]] = None,
    ) -> bool:
        """Dispatch a press; True when a shortcut claimed it (and suppresses it).

        Record and Command Mode presses carry the hook's timestamp to callbacks
        that must only enqueue; other actions run on their own thread.
        """
        with self._dispatch_lock:
            # Windows keeps repeating the main key after the user lets go of a
            # modifier, so a repeat can match a different shortcut (Ctrl+Num*
            # held, Ctrl released: the repeat matches Num*). A repeat of a held
            # key is never a new press.
            if matches_main_key is not None and self._holds_main_key(matches_main_key):
                return True
            if matches(self.hotkeys.get('enable_disable')):
                self._toggle_program_enabled()
                return True
            if not self.program_enabled:
                return False

            if matches(self.hotkeys.get('record_toggle')):
                if not self._record_key_held:
                    self._record_key_held = True
                    if self.record_mode == RecordingTriggerMode.PUSH_HOLD:
                        if self.on_record_press:
                            self.on_record_press(at)
                    elif self._should_trigger_record_toggle() and self.on_record_toggle:
                        _spawn(self.on_record_toggle)
                return True

            if matches(self.hotkeys.get('command_mode')):
                if not self._command_key_held:
                    self._command_key_held = True
                    if self.on_command_press:
                        self.on_command_press(at)
                return True

            for action, attribute in PRESS_ACTIONS:
                hotkey = self.hotkeys.get(action)
                if matches(hotkey):
                    self._press_held[action] = hotkey
                    callback = getattr(self, attribute)
                    if callback:
                        _spawn(callback)
                    return True

            for namespace, hotkeys in self._dynamic_hotkeys.items():
                for item_id, hotkey in hotkeys.items():
                    if not matches(hotkey):
                        continue
                    key = (namespace, item_id)
                    if key not in self._dynamic_held:
                        self._dynamic_held[key] = hotkey
                        debouncer = self._dynamic_debouncers.get(key)
                        callback = self._dynamic_callbacks.get(namespace)
                        if debouncer and debouncer.should_trigger() and callback:
                            _spawn(callback, item_id)
                    return True
            return False

    def _release(self, matches_main_key: Callable[[Optional[str]], bool], at: float) -> bool:
        """Dispatch a release; True when it belongs to a claimed press.

        Matching uses only the main key, since users often let go of the
        modifiers first, and every hold on that key ends so none is stranded.
        Releases skip the program_enabled gate on purpose: disabling hotkeys
        mid-hold must not strand a recording.
        """
        with self._dispatch_lock:
            claimed = False
            if self._record_key_held and matches_main_key(self.hotkeys.get('record_toggle')):
                self._record_key_held = False
                if (self.record_mode == RecordingTriggerMode.PUSH_HOLD
                        and self.on_record_release):
                    self.on_record_release(at)
                claimed = True

            if self._command_key_held and matches_main_key(self.hotkeys.get('command_mode')):
                self._command_key_held = False
                if self.on_command_release:
                    self.on_command_release(at)
                claimed = True

            for held in (self._press_held, self._dynamic_held):
                released = [key for key, hotkey in tuple(held.items()) if matches_main_key(hotkey)]
                for key in released:
                    held.pop(key, None)
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
            self._release(lambda hotkey: parse_hotkey(hotkey or "")[1] == button, at)

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

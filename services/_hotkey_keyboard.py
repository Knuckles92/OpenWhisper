"""Windows hotkeys with per-key suppression via ``keyboard``."""
import keyboard
import logging
import threading
from typing import Dict, Callable, Optional, Tuple
from config import config
from services._hotkey_common import (
    Debouncer,
    format_hotkey_string,
    notify_stt_toggle,
    parse_hotkey_string,
)
from services.settings import RecordingTriggerMode

logger = logging.getLogger(__name__)


_MODIFIER_ALIASES: Dict[str, str] = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "shift": "shift",
    "win": "win",
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
    elif len(main_key) == 1:
        main_display = main_key.upper()
    else:
        main_display = main_key.title()
    parts.append(main_display)
    return "+".join(parts)


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


class HotkeyManager:
    """Manages global hotkeys and keyboard event handling."""

    def __init__(self, hotkeys: Dict[str, str] = None):
        self.hotkeys = hotkeys or config.DEFAULT_HOTKEYS.copy()
        self.program_enabled = True
        self.record_mode = RecordingTriggerMode.TOGGLE
        self._debouncer = Debouncer(config.HOTKEY_DEBOUNCE_MS)
        # Guards auto-repeat KEY_DOWNs while the record key is held.
        self._record_key_held = False

        self.on_record_toggle: Optional[Callable] = None
        self.on_record_press: Optional[Callable] = None
        self.on_record_release: Optional[Callable] = None
        self.on_cancel: Optional[Callable] = None
        self.on_enable_toggle: Optional[Callable] = None
        self.on_minimize_tray: Optional[Callable] = None
        self.on_meeting_toggle: Optional[Callable] = None
        self.on_status_update: Optional[Callable] = None
        self.on_status_update_auto_hide: Optional[Callable] = None
        self.is_transcribing_fn: Optional[Callable[[], bool]] = None

        self._setup_keyboard_hook()

    def _setup_keyboard_hook(self):
        # A rehook may miss the KEY_UP while unhooked, so forget held state.
        self._record_key_held = False
        keyboard.hook(self._handle_keyboard_event, suppress=True)

    def _handle_keyboard_event(self, event):
        if event.event_type == keyboard.KEY_DOWN:
            if self._matches_hotkey(event, self.hotkeys['enable_disable']):
                self._toggle_program_enabled()
                return False

            if not self.program_enabled:
                if not self._matches_hotkey(event, self.hotkeys['enable_disable']):
                    return True

            elif self._matches_hotkey(event, self.hotkeys['record_toggle']):
                if not self._record_key_held:
                    self._record_key_held = True
                    if self.record_mode == RecordingTriggerMode.PUSH_HOLD:
                        if self.on_record_press:
                            threading.Thread(
                                target=self.on_record_press, daemon=True
                            ).start()
                    elif self._should_trigger_record_toggle():
                        if self.on_record_toggle:
                            threading.Thread(
                                target=self.on_record_toggle, daemon=True
                            ).start()
                return False

            elif (self.hotkeys.get('meeting_toggle')
                  and self._matches_hotkey(
                      event, self.hotkeys.get('meeting_toggle')
                  )):
                if self.on_meeting_toggle:
                    threading.Thread(
                        target=self.on_meeting_toggle, daemon=True
                    ).start()
                return False

            elif self._matches_hotkey(event, self.hotkeys['cancel']):
                if self.on_cancel:
                    threading.Thread(target=self.on_cancel, daemon=True).start()
                return False

            elif self._matches_hotkey(event, self.hotkeys.get('minimize_tray')):
                if self.on_minimize_tray:
                    threading.Thread(target=self.on_minimize_tray, daemon=True).start()
                return False

        elif (event.event_type == keyboard.KEY_UP
              and self._record_key_held
              and self._matches_record_main_key(event)):
            self._record_key_held = False
            # The press was suppressed, so swallow its release too. Release
            # dispatch skips the program_enabled gate on purpose: disabling
            # hotkeys mid-hold must not strand an in-progress recording.
            if (self.record_mode == RecordingTriggerMode.PUSH_HOLD
                    and self.on_record_release):
                threading.Thread(
                    target=self.on_record_release, daemon=True
                ).start()
            return False

        return True

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
        hotkey_string = self.hotkeys.get('record_toggle')
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
        if not hotkey_string:
            return False

        parts = hotkey_string.lower().split('+')
        main_key = parts[-1]
        modifiers = parts[:-1]

        is_numpad_hotkey = main_key.startswith('kp ')
        expected_key_name = main_key[3:] if is_numpad_hotkey else main_key

        if not event.name or event.name.lower() != expected_key_name:
            return False

        if (is_numpad_hotkey and not event.is_keypad) or (not is_numpad_hotkey and event.is_keypad):
            return False

        for modifier in modifiers:
            if modifier == 'ctrl' and not keyboard.is_pressed('ctrl'):
                return False
            elif modifier == 'alt' and not keyboard.is_pressed('alt'):
                return False
            elif modifier == 'shift' and not keyboard.is_pressed('shift'):
                return False
            elif modifier == 'win' and not keyboard.is_pressed('win'):
                return False

        if 'ctrl' not in modifiers and keyboard.is_pressed('ctrl'):
            return False
        if 'alt' not in modifiers and keyboard.is_pressed('alt'):
            return False
        if 'shift' not in modifiers and keyboard.is_pressed('shift'):
            return False
        if 'win' not in modifiers and keyboard.is_pressed('win'):
            return False

        return True

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
        """Clean up keyboard hooks."""
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
                     is_transcribing_fn: Callable[[], bool] = None):
        """Set callbacks invoked by hotkey events."""
        self.on_record_toggle = on_record_toggle
        self.on_record_press = on_record_press
        self.on_record_release = on_record_release
        self.on_cancel = on_cancel
        self.on_enable_toggle = on_enable_toggle
        self.on_minimize_tray = on_minimize_tray
        self.on_meeting_toggle = on_meeting_toggle
        self.on_status_update = on_status_update
        self.on_status_update_auto_hide = on_status_update_auto_hide
        self.is_transcribing_fn = is_transcribing_fn

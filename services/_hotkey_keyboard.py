"""
``keyboard``-based hotkey backend (Windows).

Uses the ``keyboard`` library, which supports per-key suppression so that
configured hotkeys are swallowed before reaching the focused application. This
backend is selected on Windows; macOS and Linux use the pynput backend.

This module is imported only by ``services.hotkey_manager`` when the active
platform selects the keyboard backend; nothing else should import it directly.
"""
import keyboard
import time
import logging
from typing import Dict, Callable, Optional, Tuple
from config import config

logger = logging.getLogger(__name__)


# --- Key naming / parsing helpers (shared with the hotkey capture dialog) ----

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
    """Parse a hotkey string into ``(modifier_set, main_key_name)``.

    The main key keeps its raw form (including a ``"kp "`` numpad prefix), since
    the ``keyboard`` library matches on event names directly.
    """
    if not hotkey_string:
        return frozenset(), None

    parts = [p.strip().lower() for p in hotkey_string.split("+") if p.strip()]
    if not parts:
        return frozenset(), None

    main_key = parts[-1]
    modifiers = set()
    for token in parts[:-1]:
        canonical = _MODIFIER_ALIASES.get(token)
        if canonical:
            modifiers.add(canonical)

    return frozenset(modifiers), main_key


def format_hotkey(modifiers, main_key: Optional[str]) -> str:
    """Build a canonical hotkey string from a modifier set and a main key name."""
    ordered = [m for m in _ALL_MODIFIERS if m in modifiers]
    if main_key:
        ordered.append(main_key)
    return "+".join(ordered)


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


class HotkeyManager:
    """Manages global hotkeys and keyboard event handling."""

    def __init__(self, hotkeys: Dict[str, str] = None):
        """Initialize the hotkey manager.

        Args:
            hotkeys: Dictionary of hotkey mappings. Uses defaults if None.
        """
        self.hotkeys = hotkeys or config.DEFAULT_HOTKEYS.copy()
        self.program_enabled = True
        self._last_trigger_time: Optional[float] = None

        # Callback functions
        self.on_record_toggle: Optional[Callable] = None
        self.on_cancel: Optional[Callable] = None
        self.on_enable_toggle: Optional[Callable] = None
        self.on_minimize_tray: Optional[Callable] = None
        self.on_status_update: Optional[Callable] = None
        self.on_status_update_auto_hide: Optional[Callable] = None
        self.is_transcribing_fn: Optional[Callable[[], bool]] = None

        # Setup keyboard hook
        self._setup_keyboard_hook()

    def _setup_keyboard_hook(self):
        """Setup the global keyboard hook."""
        keyboard.hook(self._handle_keyboard_event, suppress=True)

    def _handle_keyboard_event(self, event):
        """Global keyboard event handler with suppression."""
        if event.event_type == keyboard.KEY_DOWN:
            # Check enable/disable hotkey
            if self._matches_hotkey(event, self.hotkeys['enable_disable']):
                self._toggle_program_enabled()
                return False  # Suppress the key combination

            # If program is disabled, only allow enable/disable hotkey
            if not self.program_enabled:
                if not self._matches_hotkey(event, self.hotkeys['enable_disable']):
                    return True

            # Check record toggle hotkey
            elif self._matches_hotkey(event, self.hotkeys['record_toggle']):
                # Always suppress record toggle key first
                suppress = False
                if self._should_trigger_record_toggle():
                    if self.on_record_toggle:
                        # Run callback in a separate thread to avoid blocking
                        import threading
                        threading.Thread(target=self.on_record_toggle, daemon=True).start()
                return False  # Always suppress record toggle key

            # Check cancel hotkey
            elif self._matches_hotkey(event, self.hotkeys['cancel']):
                if self.on_cancel:
                    # Run callback in a separate thread to avoid blocking
                    import threading
                    threading.Thread(target=self.on_cancel, daemon=True).start()
                return False  # Suppress cancel key when handling

            # Check minimize-to-tray hotkey
            elif self._matches_hotkey(event, self.hotkeys.get('minimize_tray')):
                if self.on_minimize_tray:
                    # Run callback in a separate thread to avoid blocking
                    import threading
                    threading.Thread(target=self.on_minimize_tray, daemon=True).start()
                return False  # Suppress minimize key when handling

        # Let all other keys pass through
        return True

    def _toggle_program_enabled(self):
        """Toggle the program enabled state."""
        old_state = self.program_enabled
        self.program_enabled = not self.program_enabled

        # Reset debounce timing when toggling to avoid stale state.
        self._last_trigger_time = None

        if self.on_status_update_auto_hide:
            if not self.program_enabled:
                self.on_status_update_auto_hide("STT Disabled")
            else:
                self.on_status_update_auto_hide("STT Enabled")
        elif self.on_status_update:
            # Fallback to regular status update if auto-hide not available
            if not self.program_enabled:
                self.on_status_update("STT Disabled")
                logger.info("STT has been disabled")
            else:
                self.on_status_update("STT Enabled")
                logger.info("STT has been enabled")

    def _should_trigger_record_toggle(self) -> bool:
        """Check if record toggle should trigger (with debounce)."""
        current_time = time.monotonic()
        if self._last_trigger_time is None:
            self._last_trigger_time = current_time
            return True

        if current_time - self._last_trigger_time > (config.HOTKEY_DEBOUNCE_MS / 1000.0):
            self._last_trigger_time = current_time
            return True
        return False

    def _matches_hotkey(self, event, hotkey_string: str) -> bool:
        """Check if the current event matches a hotkey string.

        Args:
            event: Keyboard event from the keyboard library.
            hotkey_string: Hotkey string (e.g., "ctrl+alt+*", "*", "shift+f1", "ctrl+kp *").

        Returns:
            True if the event matches the hotkey string.
        """
        if not hotkey_string:
            return False

        # Parse hotkey string (e.g., "ctrl+alt+*", "*", "shift+f1")
        parts = hotkey_string.lower().split('+')
        main_key = parts[-1]  # Last part is the main key
        modifiers = parts[:-1]  # Everything else are modifiers

        # Check if main key is a numpad key (starts with "kp ")
        is_numpad_hotkey = main_key.startswith('kp ')
        expected_key_name = main_key[3:] if is_numpad_hotkey else main_key

        # Check if main key matches
        if not event.name or event.name.lower() != expected_key_name:
            return False

        # Check if numpad status matches
        if (is_numpad_hotkey and not event.is_keypad) or (not is_numpad_hotkey and event.is_keypad):
            return False

        # Check modifiers
        for modifier in modifiers:
            if modifier == 'ctrl' and not keyboard.is_pressed('ctrl'):
                return False
            elif modifier == 'alt' and not keyboard.is_pressed('alt'):
                return False
            elif modifier == 'shift' and not keyboard.is_pressed('shift'):
                return False
            elif modifier == 'win' and not keyboard.is_pressed('win'):
                return False

        # Check that no extra modifiers are pressed
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
        """Update the hotkey mappings.

        Args:
            new_hotkeys: Dictionary of new hotkey mappings.
        """
        self.hotkeys.update(new_hotkeys)
        # Restart keyboard hook with new hotkeys
        self.cleanup()
        self._setup_keyboard_hook()
        logger.info("Hotkeys updated successfully")

    def cleanup(self):
        """Clean up keyboard hooks."""
        try:
            # Use a timeout to avoid blocking if cleanup is called from wrong thread
            import threading
            if threading.current_thread() is threading.main_thread():
                keyboard.unhook_all()
            else:
                # If called from non-main thread, just log a warning
                logger.warning("Hotkey cleanup called from non-main thread, skipping unhook")
        except RuntimeError as e:
            # Ignore "cannot join current thread" errors - they're harmless during shutdown
            if "cannot join" not in str(e).lower():
                logger.error(f"Error cleaning up keyboard hooks: {e}")
        except Exception as e:
            logger.error(f"Error cleaning up keyboard hooks: {e}")

    def set_callbacks(self,
                     on_record_toggle: Callable = None,
                     on_cancel: Callable = None,
                     on_enable_toggle: Callable = None,
                     on_minimize_tray: Callable = None,
                     on_status_update: Callable = None,
                     on_status_update_auto_hide: Callable = None,
                     is_transcribing_fn: Callable[[], bool] = None):
        """Set callback functions for hotkey events.

        Args:
            on_record_toggle: Called when record toggle hotkey is pressed.
            on_cancel: Called when cancel hotkey is pressed.
            on_enable_toggle: Called when enable/disable hotkey is pressed.
            on_minimize_tray: Called when minimize-to-tray hotkey is pressed.
            on_status_update: Called to update status display.
            on_status_update_auto_hide: Called to update status with auto-hide.
            is_transcribing_fn: Function to check if transcription is in progress.
        """
        self.on_record_toggle = on_record_toggle
        self.on_cancel = on_cancel
        self.on_enable_toggle = on_enable_toggle
        self.on_minimize_tray = on_minimize_tray
        self.on_status_update = on_status_update
        self.on_status_update_auto_hide = on_status_update_auto_hide
        self.is_transcribing_fn = is_transcribing_fn

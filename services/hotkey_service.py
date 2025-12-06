"""
Hotkey management for the OpenWhisper application.

Uses Qt signals for thread-safe communication with the main thread.
"""
import keyboard
import time
import logging
from typing import Dict

from PyQt6.QtCore import QObject, pyqtSignal

from config import config


class HotkeyService(QObject):
    """Manages global hotkeys and keyboard event handling.

    Uses Qt signals instead of callbacks for thread-safe communication.
    Keyboard events are handled in a background thread, but signals are
    emitted and should be connected with QueuedConnection to ensure
    handlers run on the main thread.
    """

    # Signals for thread-safe communication
    record_toggle_requested = pyqtSignal()  # Request to toggle recording
    cancel_requested = pyqtSignal()  # Request to cancel current operation
    stt_enabled_changed = pyqtSignal(bool)  # STT state changed (True=enabled, False=disabled)
    status_update = pyqtSignal(str)  # Status message update

    def __init__(self, hotkeys: Dict[str, str] = None):
        """Initialize the hotkey manager.

        Args:
            hotkeys: Dictionary of hotkey mappings. Uses defaults if None.
        """
        super().__init__()
        self.hotkeys = hotkeys or config.DEFAULT_HOTKEYS.copy()
        self.program_enabled = True
        self._last_trigger_time = 0

        # Setup keyboard hook
        self._setup_keyboard_hook()

    def _setup_keyboard_hook(self):
        """Setup the global keyboard hook."""
        keyboard.hook(self._handle_keyboard_event, suppress=True)

    def _handle_keyboard_event(self, event):
        """Global keyboard event handler with suppression.

        Note: This runs in the keyboard library's hook thread, not the main thread.
        All communication with the main thread happens via Qt signals.
        """
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
                if self._should_trigger_record_toggle():
                    # Emit signal - will be queued to main thread if connected with QueuedConnection
                    self.record_toggle_requested.emit()
                return False  # Always suppress record toggle key

            # Check cancel hotkey
            elif self._matches_hotkey(event, self.hotkeys['cancel']):
                # Emit signal - will be queued to main thread if connected with QueuedConnection
                self.cancel_requested.emit()
                return False  # Suppress cancel key when handling

        # Let all other keys pass through
        return True

    def _toggle_program_enabled(self):
        """Toggle the program enabled state."""
        self.program_enabled = not self.program_enabled

        # Reset debounce timing when toggling to avoid stale state
        self._last_trigger_time = 0

        # Emit signals for state change
        self.stt_enabled_changed.emit(self.program_enabled)

        if self.program_enabled:
            self.status_update.emit("STT Enabled")
            logging.info("STT has been enabled")
        else:
            self.status_update.emit("STT Disabled")
            logging.info("STT has been disabled")

    def _should_trigger_record_toggle(self) -> bool:
        """Check if record toggle should trigger (with debounce)."""
        current_time = time.time()
        if current_time - self._last_trigger_time > (config.HOTKEY_DEBOUNCE_MS / 1000.0):
            self._last_trigger_time = current_time
            return True
        return False

    def _matches_hotkey(self, event, hotkey_string: str) -> bool:
        """Check if the current event matches a hotkey string.

        Args:
            event: Keyboard event from the keyboard library.
            hotkey_string: Hotkey string (e.g., "ctrl+alt+*", "*", "shift+f1").

        Returns:
            True if the event matches the hotkey string.
        """
        if not hotkey_string:
            return False

        # Parse hotkey string (e.g., "ctrl+alt+*", "*", "shift+f1")
        parts = hotkey_string.lower().split('+')
        main_key = parts[-1]  # Last part is the main key
        modifiers = parts[:-1]  # Everything else are modifiers

        # Check if main key matches
        if not event.name or event.name.lower() != main_key:
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

    def update_hotkeys(self, new_hotkeys: Dict[str, str]):
        """Update the hotkey mappings.

        Args:
            new_hotkeys: Dictionary of new hotkey mappings.
        """
        self.hotkeys.update(new_hotkeys)
        # Restart keyboard hook with new hotkeys
        self.cleanup()
        self._setup_keyboard_hook()
        logging.info("Hotkeys updated successfully")

    def cleanup(self):
        """Clean up keyboard hooks."""
        try:
            # Use a timeout to avoid blocking if cleanup is called from wrong thread
            import threading
            if threading.current_thread() is threading.main_thread():
                keyboard.unhook_all()
            else:
                # If called from non-main thread, just log a warning
                logging.warning("Hotkey cleanup called from non-main thread, skipping unhook")
        except RuntimeError as e:
            # Ignore "cannot join current thread" errors - they're harmless during shutdown
            if "cannot join" not in str(e).lower():
                logging.error(f"Error cleaning up keyboard hooks: {e}")
        except Exception as e:
            logging.error(f"Error cleaning up keyboard hooks: {e}")


__all__ = ["HotkeyService"]

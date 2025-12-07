"""
Hotkey management for the OpenWhisper application.
"""
import keyboard
import time
import logging
from typing import Dict, Callable, Optional
from config import config


class HotkeyManager:
    """Manages global hotkeys and keyboard event handling."""
    
    def __init__(self, hotkeys: Dict[str, str] = None):
        """Initialize the hotkey manager.
        
        Args:
            hotkeys: Dictionary of hotkey mappings. Uses defaults if None.
        """
        self.hotkeys = hotkeys or config.DEFAULT_HOTKEYS.copy()
        self.program_enabled = True
        self._last_trigger_time = 0
        
        # Callback functions
        self.on_record_toggle: Optional[Callable] = None
        self.on_cancel: Optional[Callable] = None
        self.on_enable_toggle: Optional[Callable] = None
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

        # Let all other keys pass through
        return True
    
    def _toggle_program_enabled(self):
        """Toggle the program enabled state."""
        old_state = self.program_enabled
        self.program_enabled = not self.program_enabled
        
        # Reset debounce timing when toggling to avoid stale state
        self._last_trigger_time = 0
        
        if self.on_status_update_auto_hide:
            if not self.program_enabled:
                self.on_status_update_auto_hide("STT Disabled")
            else:
                self.on_status_update_auto_hide("STT Enabled")
        elif self.on_status_update:
            # Fallback to regular status update if auto-hide not available
            if not self.program_enabled:
                self.on_status_update("STT Disabled")
                logging.info("STT has been disabled")
            else:
                self.on_status_update("STT Enabled")
                logging.info("STT has been enabled")
    
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
    
    def set_callbacks(self, 
                     on_record_toggle: Callable = None,
                     on_cancel: Callable = None,
                     on_enable_toggle: Callable = None,
                     on_status_update: Callable = None,
                     on_status_update_auto_hide: Callable = None,
                     is_transcribing_fn: Callable[[], bool] = None):
        """Set callback functions for hotkey events.
        
        Args:
            on_record_toggle: Called when record toggle hotkey is pressed.
            on_cancel: Called when cancel hotkey is pressed.
            on_enable_toggle: Called when enable/disable hotkey is pressed.
            on_status_update: Called to update status display.
            on_status_update_auto_hide: Called to update status with auto-hide.
            is_transcribing_fn: Function to check if transcription is in progress.
        """
        self.on_record_toggle = on_record_toggle
        self.on_cancel = on_cancel
        self.on_enable_toggle = on_enable_toggle
        self.on_status_update = on_status_update
        self.on_status_update_auto_hide = on_status_update_auto_hide
        self.is_transcribing_fn = is_transcribing_fn

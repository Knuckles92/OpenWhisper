"""
pynput-based hotkey backend (macOS and Linux).

Used on platforms where the Windows ``keyboard`` library is unavailable or
requires root. Unlike the ``keyboard`` backend (which uses per-key suppression),
pynput cannot selectively suppress individual key events on macOS, so hotkeys
are observed but not swallowed. macOS default hotkeys therefore use modifier
combinations that do not collide with normal typing.

Global hooks on macOS require the app to be granted Accessibility (Input
Monitoring) permission in System Settings > Privacy & Security.

This module is imported only by ``services.hotkey_manager`` when the active
platform selects the pynput backend; nothing else should import it directly.
"""
import logging
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from pynput import keyboard as pynput_keyboard

from config import config

logger = logging.getLogger(__name__)


def _get_listener_class():
    """Return a pynput listener class that avoids unsafe macOS layout lookup."""
    if sys.platform != "darwin":
        return pynput_keyboard.Listener

    try:
        from pynput._util.darwin import ListenerMixin
    except Exception as exc:
        logger.warning(f"Could not load macOS pynput listener shim: {exc}")
        return pynput_keyboard.Listener

    class MacOSHotkeyListener(pynput_keyboard.Listener):
        def _run(self):
            # pynput's default macOS Listener enters keycode_context() here,
            # which calls HIToolbox input-source APIs from this background
            # thread. macOS 26 traps that dispatch queue violation. Hotkey
            # matching below uses event keycodes/unicode strings directly, so
            # the layout context is not needed for observing shortcuts.
            self._context = None
            ListenerMixin._run(self)

    return MacOSHotkeyListener


# --- Key naming / parsing helpers (shared with the hotkey capture dialog) ----

# Modifier aliases -> canonical modifier name. ``win``/``super`` map to ``cmd``
# so legacy Windows-style settings still resolve to the Command key on macOS.
_MODIFIER_ALIASES: Dict[str, str] = {
    "cmd": "cmd",
    "command": "cmd",
    "win": "cmd",
    "super": "cmd",
    "meta": "cmd",
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "option": "alt",
    "opt": "alt",
    "shift": "shift",
}

# Friendly aliases for non-modifier "main" keys, normalized to the name that
# pynput reports (``Key.<name>``) so capture and matching round-trip cleanly.
_MAIN_KEY_ALIASES: Dict[str, str] = {
    "escape": "esc",
    "return": "enter",
    "del": "delete",
    "ins": "insert",
    "pgup": "page_up",
    "pgdn": "page_down",
}

# pynput modifier Key objects -> canonical modifier name.
_MODIFIER_KEYS: Dict[object, str] = {
    pynput_keyboard.Key.cmd: "cmd",
    pynput_keyboard.Key.cmd_l: "cmd",
    pynput_keyboard.Key.cmd_r: "cmd",
    pynput_keyboard.Key.ctrl: "ctrl",
    pynput_keyboard.Key.ctrl_l: "ctrl",
    pynput_keyboard.Key.ctrl_r: "ctrl",
    pynput_keyboard.Key.alt: "alt",
    pynput_keyboard.Key.alt_l: "alt",
    pynput_keyboard.Key.alt_r: "alt",
    pynput_keyboard.Key.shift: "shift",
    pynput_keyboard.Key.shift_l: "shift",
    pynput_keyboard.Key.shift_r: "shift",
}

# pynput's alt_gr only exists on some platforms; include it defensively.
if hasattr(pynput_keyboard.Key, "alt_gr"):
    _MODIFIER_KEYS[pynput_keyboard.Key.alt_gr] = "alt"

_ALL_MODIFIERS = ("cmd", "ctrl", "alt", "shift")


def modifier_of(key) -> Optional[str]:
    """Return the canonical modifier name for ``key`` or ``None`` if it is not a modifier."""
    return _MODIFIER_KEYS.get(key)


def key_to_name(key) -> Optional[str]:
    """Return a canonical lowercase name for a non-modifier key.

    The same function is used by the hotkey capture dialog so that whatever the
    user presses to set a hotkey produces the identical name used at match time.
    """
    if isinstance(key, pynput_keyboard.Key):
        return key.name
    if isinstance(key, pynput_keyboard.KeyCode):
        if key.char:
            return key.char.lower()
        if key.vk is not None:
            return f"vk{key.vk}"
    return None


def parse_hotkey(hotkey_string: str) -> Tuple[frozenset, Optional[str]]:
    """Parse a hotkey string into ``(modifier_set, main_key_name)``.

    Example: ``"ctrl+alt+r"`` -> ``(frozenset({"ctrl", "alt"}), "r")``.
    Unknown modifier tokens are ignored; the last token is always the main key.
    """
    if not hotkey_string:
        return frozenset(), None

    parts = [p.strip().lower() for p in hotkey_string.split("+") if p.strip()]
    if not parts:
        return frozenset(), None

    main_raw = parts[-1]
    main_key = _MAIN_KEY_ALIASES.get(main_raw, main_raw)

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
    "cmd": "⌘",
    "ctrl": "⌃",
    "alt": "⌥",
    "shift": "⇧",
}

_DISPLAY_MAIN_KEYS: Dict[str, str] = {
    "escape": "⎋",
    "esc": "⎋",
    "enter": "↩",
    "space": "Space",
    "tab": "⇥",
    "delete": "⌫",
    "backspace": "⌫",
}


def format_hotkey_display(hotkey_string: str) -> str:
    """Format a canonical hotkey string for on-screen display (macOS symbols)."""
    if not hotkey_string:
        return ""

    modifiers, main_key = parse_hotkey(hotkey_string)
    if main_key is None:
        return hotkey_string

    parts = [_DISPLAY_MODIFIERS[m] for m in _ALL_MODIFIERS if m in modifiers]
    if len(main_key) == 1:
        main_display = main_key.upper()
    else:
        main_display = _DISPLAY_MAIN_KEYS.get(main_key, main_key.title())
    parts.append(main_display)
    return "".join(parts)


# --- macOS Accessibility (Input Monitoring) trust ------------------------------


def is_accessibility_trusted() -> bool:
    """Return whether this process may observe global key events on macOS.

    The pynput global event tap only receives keystrokes when the host process
    is granted Accessibility permission (System Settings > Privacy & Security >
    Accessibility). Without it the tap is silently inert, so hotkeys only work
    while the OpenWhisper window itself is focused (via the Qt fallback).

    Returns True on non-macOS platforms, which have no equivalent gate, and also
    fails open (True) if the trust state cannot be queried, so the user is never
    nagged on a false negative.
    """
    if sys.platform != "darwin":
        return True
    try:
        import HIServices

        return bool(HIServices.AXIsProcessTrusted())
    except Exception as exc:
        logger.warning(f"Could not query macOS Accessibility trust state: {exc}")
        return True


def request_accessibility_trust() -> bool:
    """Register this process with macOS Accessibility and show the system prompt.

    Calling the options API is what makes the host binary appear in the
    Accessibility list so the user can toggle it on; passing the prompt option
    also surfaces the native permission dialog. Returns the current trust state
    (granting takes effect on the next launch). No-op on non-macOS platforms.
    """
    if sys.platform != "darwin":
        return True
    try:
        import HIServices

        options = {HIServices.kAXTrustedCheckOptionPrompt: True}
        return bool(HIServices.AXIsProcessTrustedWithOptions(options))
    except Exception as exc:
        logger.warning(f"Could not request macOS Accessibility trust: {exc}")
        return False


# --- Synthetic paste -----------------------------------------------------------

_paste_controller: Optional[pynput_keyboard.Controller] = None


def send_paste() -> None:
    """Simulate a paste keystroke via pynput.

    Uses Cmd+V on macOS and Ctrl+V on Linux. Requires Accessibility permission
    for the host process to post synthetic key events on macOS.
    """
    global _paste_controller
    if _paste_controller is None:
        _paste_controller = pynput_keyboard.Controller()
    modifier = pynput_keyboard.Key.cmd if sys.platform == "darwin" else pynput_keyboard.Key.ctrl
    with _paste_controller.pressed(modifier):
        _paste_controller.press("v")
        _paste_controller.release("v")


class HotkeyManager:
    """Manages global hotkeys and keyboard event handling via pynput."""

    def __init__(self, hotkeys: Dict[str, str] = None):
        """Initialize the hotkey manager.

        Args:
            hotkeys: Dictionary of hotkey mappings. Uses defaults if None.
        """
        self.hotkeys = hotkeys or config.DEFAULT_HOTKEYS.copy()
        self.program_enabled = True
        self._last_trigger_time: Optional[float] = None
        self._last_action_times: Dict[str, float] = {}

        # Callback functions
        self.on_record_toggle: Optional[Callable] = None
        self.on_cancel: Optional[Callable] = None
        self.on_enable_toggle: Optional[Callable] = None
        self.on_status_update: Optional[Callable] = None
        self.on_status_update_auto_hide: Optional[Callable] = None
        self.is_transcribing_fn: Optional[Callable[[], bool]] = None

        # Live keyboard state
        self._pressed_modifiers: set = set()
        self._pressed_main_keys: set = set()
        self._listener: Optional[pynput_keyboard.Listener] = None
        # macOS detection runs through Carbon RegisterEventHotKey (no
        # Accessibility permission); Linux keeps the pynput global listener.
        self._carbon_registrar = None
        self._use_carbon = sys.platform == "darwin"

        # Setup keyboard hook
        self._setup_keyboard_hook()

    def _setup_keyboard_hook(self):
        """Start global hotkey detection (Carbon on macOS, pynput on Linux)."""
        self._pressed_modifiers.clear()
        self._pressed_main_keys.clear()

        if self._use_carbon and self._setup_carbon_hotkeys():
            return

        # suppress=False: pynput cannot selectively suppress keys, so hotkeys are
        # observed but still pass through to the focused app.
        listener_class = _get_listener_class()
        self._listener = listener_class(
            on_press=self._on_press,
            on_release=self._on_release,
            suppress=False,
        )
        self._listener.daemon = True
        self._listener.start()
        logger.info("Keyboard hook started")

    def _setup_carbon_hotkeys(self) -> bool:
        """Register hotkeys via Carbon. Returns False to fall back to pynput."""
        try:
            from services import _hotkey_carbon
        except Exception as exc:
            logger.warning(f"Carbon hotkey backend unavailable, using pynput: {exc}")
            return False

        if not _hotkey_carbon.is_available():
            logger.warning("Carbon hotkey backend not available, using pynput")
            return False

        if self._carbon_registrar is None:
            self._carbon_registrar = _hotkey_carbon.CarbonHotkeyRegistrar(
                on_action=self.trigger_action
            )
        self._carbon_registrar.register_hotkeys(self.hotkeys)
        logger.info("Carbon global hotkeys registered (no Accessibility required)")
        return True

    def _on_press(self, key) -> None:
        """Handle a global key-press event."""
        modifier = modifier_of(key)
        if modifier is not None:
            self._pressed_modifiers.add(modifier)
            return

        name = key_to_name(key)
        if name is None:
            return

        # Ignore auto-repeat: only act on the initial press of a main key.
        if name in self._pressed_main_keys:
            return
        self._pressed_main_keys.add(name)

        active_modifiers = frozenset(self._pressed_modifiers)
        self.handle_hotkey_press(active_modifiers, name, source="global")

    def handle_hotkey_press(
        self,
        active_modifiers: frozenset,
        main_key: str,
        source: str = "global",
    ) -> bool:
        """Handle a normalized hotkey press from either pynput or Qt.

        Matches the key state against the configured hotkeys and dispatches the
        action. Returns True when the key state matched a configured hotkey, even
        if the action was suppressed by debounce or disabled state.
        """
        # Enable/disable toggle works even while the program is disabled.
        if self._matches_hotkey(active_modifiers, main_key, self.hotkeys.get("enable_disable")):
            logger.debug(f"Enable/disable hotkey matched from {source}")
            self.trigger_action("enable_disable")
            return True

        if not self.program_enabled:
            return False

        if self._matches_hotkey(active_modifiers, main_key, self.hotkeys.get("record_toggle")):
            logger.debug(f"Record toggle hotkey matched from {source}")
            self.trigger_action("record_toggle")
            return True

        if self._matches_hotkey(active_modifiers, main_key, self.hotkeys.get("cancel")):
            logger.debug(f"Cancel hotkey matched from {source}")
            self.trigger_action("cancel")
            return True

        return False

    def trigger_action(self, action: str) -> None:
        """Apply enable/debounce gating and invoke the callback for an action.

        Shared dispatch for every input source: the Carbon registrar (macOS),
        the pynput global listener (Linux), and the Qt focused-window fallback.
        ``_should_accept_action`` dedupes a press seen by more than one source.
        """
        if action == "enable_disable":
            # Works even while the program is disabled.
            if self._should_accept_action("enable_disable"):
                self._toggle_program_enabled()
            return

        if not self.program_enabled:
            return

        if action == "record_toggle":
            if (
                self._should_trigger_record_toggle()
                and self._should_accept_action("record_toggle")
                and self.on_record_toggle
            ):
                threading.Thread(target=self.on_record_toggle, daemon=True).start()
        elif action == "cancel":
            if self._should_accept_action("cancel") and self.on_cancel:
                threading.Thread(target=self.on_cancel, daemon=True).start()

    def _on_release(self, key) -> None:
        """Handle a global key-release event."""
        modifier = modifier_of(key)
        if modifier is not None:
            self._pressed_modifiers.discard(modifier)
            return

        name = key_to_name(key)
        if name is not None:
            self._pressed_main_keys.discard(name)

    def _toggle_program_enabled(self):
        """Toggle the program enabled state."""
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

    def _should_accept_action(self, action: str) -> bool:
        """Suppress duplicate delivery when Qt and pynput both see a hotkey."""
        current_time = time.monotonic()
        last_time = self._last_action_times.get(action)
        if last_time is not None and current_time - last_time < 0.2:
            return False
        self._last_action_times[action] = current_time
        return True

    def _matches_hotkey(self, active_modifiers: frozenset, main_key: str, hotkey_string: Optional[str]) -> bool:
        """Check if the current key state matches a hotkey string.

        Args:
            active_modifiers: Set of currently-pressed canonical modifier names.
            main_key: Canonical name of the just-pressed non-modifier key.
            hotkey_string: Hotkey string (e.g. "ctrl+alt+r", "ctrl+alt+shift+r").

        Returns:
            True if the modifier set and main key both match exactly (no extra modifiers).
        """
        if not hotkey_string:
            return False

        required_modifiers, expected_key = parse_hotkey(hotkey_string)
        if expected_key is None:
            return False

        if main_key != expected_key:
            return False

        # Exact modifier match: required set must equal the active set.
        return active_modifiers == required_modifiers

    def rehook(self):
        """Re-register the keyboard listener after sleep/resume or degradation.

        Preserves all state (hotkeys, callbacks, enabled status).
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
        if self._carbon_registrar is not None:
            # Carbon hotkeys are registered with the OS, not matched live, so the
            # new combos must be re-registered. (pynput matches self.hotkeys live.)
            self._carbon_registrar.register_hotkeys(self.hotkeys)
        logger.info("Hotkeys updated successfully")

    def cleanup(self):
        """Stop global hotkey detection (Carbon unregister and/or pynput stop)."""
        if self._carbon_registrar is not None:
            try:
                self._carbon_registrar.unregister_all()
            except Exception as e:
                logger.error(f"Error unregistering Carbon hotkeys: {e}")

        listener = self._listener
        self._listener = None
        self._pressed_modifiers.clear()
        self._pressed_main_keys.clear()
        if listener is None:
            return
        try:
            listener.stop()
        except Exception as e:
            logger.error(f"Error cleaning up keyboard listener: {e}")

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

"""macOS/Linux hotkeys via Carbon or pynput.

Carbon hotkeys need no Accessibility grant; synthetic paste and pynput event
taps do. Pynput observes configured hotkeys but cannot selectively suppress
them.
"""
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from config import config
from services._hotkey_common import (
    Debouncer,
    format_hotkey_string,
    notify_stt_toggle,
    parse_hotkey_string,
)
from services.settings import RecordingTriggerMode

logger = logging.getLogger(__name__)


class HotkeyBackendUnavailable(RuntimeError):
    """Raised when pynput cannot provide a native keyboard backend."""


_pynput_keyboard_module: Optional[Any] = None
_pynput_keyboard_error: Optional[Exception] = None


def _load_pynput_keyboard():
    """Import pynput only when native keyboard access is actually requested.

    Importing pynput selects and initializes its platform backend. On Linux that
    opens the X display immediately, so importing it from a UI or service module
    used to make pure-Wayland and headless startup fail before the application
    could offer its in-window controls. Keep that native boundary lazy and turn
    backend initialization failures into a normal unavailable state.
    """
    global _pynput_keyboard_module, _pynput_keyboard_error
    if _pynput_keyboard_module is not None:
        return _pynput_keyboard_module

    try:
        from pynput import keyboard as keyboard_module
    except Exception as exc:
        _pynput_keyboard_error = exc
        raise HotkeyBackendUnavailable(
            f"pynput keyboard backend is unavailable: {exc}"
        ) from exc

    _pynput_keyboard_module = keyboard_module
    _pynput_keyboard_error = None
    return keyboard_module


class _LazyPynputKeyboard:
    """Compatibility proxy for callers that inspect ``pynput_keyboard``."""

    def __getattr__(self, name: str):
        return getattr(_load_pynput_keyboard(), name)


pynput_keyboard = _LazyPynputKeyboard()


def get_listener_class():
    """Return a pynput listener class that avoids unsafe macOS layout lookup.

    Used by both the global hotkey manager and the hotkey capture dialog.
    pynput's default macOS Listener enters ``keycode_context()`` on its
    background thread, which calls HIToolbox input-source APIs and triggers a
    SIGTRAP (``trace trap``) on macOS 26+. Hotkey matching and capture use
    event keycodes/unicode strings directly, so the layout context is not
    needed.
    """
    keyboard_module = _load_pynput_keyboard()
    if sys.platform != "darwin":
        return keyboard_module.Listener

    try:
        from pynput._util.darwin import ListenerMixin
    except Exception as exc:
        logger.warning(f"Could not load macOS pynput listener shim: {exc}")
        return keyboard_module.Listener

    class MacOSHotkeyListener(keyboard_module.Listener):
        def _run(self):
            self._context = None
            ListenerMixin._run(self)

    return MacOSHotkeyListener


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

_modifier_keys: Optional[Dict[object, str]] = None


def _get_modifier_keys() -> Dict[object, str]:
    """Build the pynput-key lookup after its native backend is available."""
    global _modifier_keys
    if _modifier_keys is not None:
        return _modifier_keys

    key_type = _load_pynput_keyboard().Key
    modifier_keys = {
        key_type.cmd: "cmd",
        key_type.cmd_l: "cmd",
        key_type.cmd_r: "cmd",
        key_type.ctrl: "ctrl",
        key_type.ctrl_l: "ctrl",
        key_type.ctrl_r: "ctrl",
        key_type.alt: "alt",
        key_type.alt_l: "alt",
        key_type.alt_r: "alt",
        key_type.shift: "shift",
        key_type.shift_l: "shift",
        key_type.shift_r: "shift",
    }
    # pynput's alt_gr only exists on some platforms; include it defensively.
    if hasattr(key_type, "alt_gr"):
        modifier_keys[key_type.alt_gr] = "alt"
    _modifier_keys = modifier_keys
    return modifier_keys

_ALL_MODIFIERS = ("cmd", "ctrl", "alt", "shift")


def modifier_of(key) -> Optional[str]:
    """Return the canonical modifier name for ``key`` or ``None`` if it is not a modifier."""
    return _get_modifier_keys().get(key)


def key_to_name(key) -> Optional[str]:
    """Return a canonical lowercase name for a non-modifier key.

    The same function is used by the hotkey capture dialog so that whatever the
    user presses to set a hotkey produces the identical name used at match time.
    """
    keyboard_module = _load_pynput_keyboard()
    if isinstance(key, keyboard_module.Key):
        return key.name
    if isinstance(key, keyboard_module.KeyCode):
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
    return parse_hotkey_string(hotkey_string, _MODIFIER_ALIASES, _MAIN_KEY_ALIASES)


def format_hotkey(modifiers, main_key: Optional[str]) -> str:
    """Build a canonical hotkey string from a modifier set and a main key name."""
    return format_hotkey_string(modifiers, main_key, _ALL_MODIFIERS)


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


def is_accessibility_trusted() -> bool:
    """Return whether this process has macOS Accessibility trust.

    Synthetic Cmd+V auto-paste and pynput event taps only work when the host
    process is granted Accessibility permission (System Settings > Privacy &
    Security > Accessibility). Carbon global hotkeys do not need this grant.

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

    Calling the options API is what makes the current launch identity eligible
    for the Accessibility list; passing the prompt option also surfaces the
    native permission dialog. Returns the current trust state (granting takes
    effect on the next launch). No-op on non-macOS platforms.
    """
    if sys.platform != "darwin":
        return True
    try:
        import HIServices

        logger.info("Requesting macOS Accessibility trust for %s", sys.executable)
        options = {HIServices.kAXTrustedCheckOptionPrompt: True}
        trusted = bool(HIServices.AXIsProcessTrustedWithOptions(options))
        logger.info("macOS Accessibility trust request returned %s", trusted)
        return trusted
    except Exception as exc:
        logger.warning(f"Could not request macOS Accessibility trust: {exc}")
        return False


def _find_containing_app_bundle(path: str) -> Optional[Path]:
    if not path:
        return None

    candidates = [Path(path)]
    try:
        real_path = Path(os.path.realpath(path))
        if real_path != candidates[0]:
            candidates.append(real_path)
    except Exception:
        pass

    for candidate in candidates:
        for parent in (candidate, *candidate.parents):
            if parent.name.endswith(".app"):
                return parent
    return None


def _find_python_framework_app_bundle() -> Optional[Path]:
    prefixes = {
        getattr(sys, "base_prefix", ""),
        getattr(sys, "exec_prefix", ""),
        getattr(sys, "prefix", ""),
    }
    for prefix in prefixes:
        if not prefix:
            continue
        app_bundle = Path(os.path.realpath(prefix)) / "Resources" / "Python.app"
        executable = app_bundle / "Contents" / "MacOS" / "Python"
        if executable.is_file() and os.access(executable, os.X_OK):
            return app_bundle
    return None


def accessibility_permission_instructions() -> str:
    """Return user-facing macOS Accessibility instructions for this launch."""
    if sys.platform != "darwin":
        return ""

    launch_app = os.environ.get("OPENWHISPER_MACOS_LAUNCH_APP", "")
    launch_app_bundle = Path(launch_app) if launch_app else None
    if (
        launch_app_bundle is not None
        and launch_app_bundle.name.endswith(".app")
        and launch_app_bundle.is_dir()
    ):
        launch_app_name = (
            launch_app_bundle.name
            if launch_app_bundle.stem.lower().startswith("python")
            else launch_app_bundle.stem
        )
        return (
            "Development launch note: OpenWhisper is running through "
            f"{launch_app_name}, so OpenWhisper may not appear by name.\n"
            f"Enable {launch_app_name} in the Accessibility list. If it "
            "is not already listed, use the + button and add:\n"
            f"{launch_app_bundle}"
        )

    app_bundle = (
        _find_containing_app_bundle(sys.executable)
        or _find_containing_app_bundle(getattr(sys, "_base_executable", ""))
    )
    if app_bundle is not None:
        return (
            f"Enable {app_bundle.stem} in the Accessibility list. If it is not "
            "already listed, add the app bundle manually."
        )

    python_app_bundle = _find_python_framework_app_bundle()
    if python_app_bundle is not None:
        return (
            "Development launch note: virtualenv Python files can be greyed out "
            "in this macOS picker.\n"
            "Quit OpenWhisper and relaunch it with scripts/openwhisper or ow, "
            "then enable Python.app in Accessibility. If it is not already "
            "listed, use the + button and add:\n"
            f"{python_app_bundle}"
        )

    executable = sys.executable
    real_executable = os.path.realpath(executable)
    real_name = os.path.basename(real_executable) or "python"
    lines = [
        "Development launch note: OpenWhisper may not appear by name.",
        (
            "Enable the launcher or interpreter instead. Look for your terminal "
            f"or IDE, Python, or {real_name}."
        ),
        "If nothing appears, use the + button and add:",
        executable,
    ]
    if real_executable != executable:
        lines.extend(
            ["", "If macOS resolves the virtualenv symlink, add:", real_executable]
        )
    lines.extend(
        [
            "",
            "If the picker greys out those Python files, launch OpenWhisper with "
            "scripts/openwhisper so macOS can select Python.app.",
        ]
    )
    return "\n".join(lines)


def accessibility_permission_diagnostics() -> str:
    """Return detailed macOS permission identity data for troubleshooting."""
    if sys.platform != "darwin":
        return ""

    executable = sys.executable
    real_executable = os.path.realpath(executable)
    app_bundle = (
        _find_containing_app_bundle(executable)
        or _find_containing_app_bundle(getattr(sys, "_base_executable", ""))
        or (Path(os.environ["OPENWHISPER_MACOS_LAUNCH_APP"])
            if os.environ.get("OPENWHISPER_MACOS_LAUNCH_APP")
            else None)
        or _find_python_framework_app_bundle()
    )
    lines = [
        f"sys.executable: {executable}",
        f"resolved executable: {real_executable}",
        "launcher app: "
        f"{os.environ.get('OPENWHISPER_MACOS_LAUNCH_APP', 'not set')}",
        f"app bundle: {app_bundle if app_bundle else 'not detected'}",
    ]
    return "\n".join(lines)


_paste_controller: Optional[Any] = None


def send_paste() -> None:
    """Simulate a paste keystroke via pynput.

    Uses Cmd+V on macOS and Ctrl+V on Linux. Requires Accessibility permission
    for the host process to post synthetic key events on macOS.
    """
    global _paste_controller
    keyboard_module = _load_pynput_keyboard()
    if _paste_controller is None:
        _paste_controller = keyboard_module.Controller()
    modifier = (
        keyboard_module.Key.cmd
        if sys.platform == "darwin"
        else keyboard_module.Key.ctrl
    )
    with _paste_controller.pressed(modifier):
        _paste_controller.press("v")
        _paste_controller.release("v")


class HotkeyManager:
    """Manages global hotkeys and keyboard event handling via pynput."""

    def __init__(self, hotkeys: Dict[str, str] = None):
        self.hotkeys = hotkeys or config.DEFAULT_HOTKEYS.copy()
        self.program_enabled = True
        self.record_mode = RecordingTriggerMode.TOGGLE
        self._debouncer = Debouncer(config.HOTKEY_DEBOUNCE_MS)
        self._last_action_times: Dict[str, float] = {}
        # Set while the record hotkey is held in push-and-hold mode.
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

        self._pressed_modifiers: set = set()
        self._pressed_main_keys: set = set()
        self._listener: Optional[Any] = None
        # macOS detection runs through Carbon RegisterEventHotKey (no
        # Accessibility permission); Linux keeps the pynput global listener.
        self._carbon_registrar = None
        self._use_carbon = sys.platform == "darwin"
        self.backend_available = False
        self.backend_name = "unavailable"
        self.backend_error = ""

        self._setup_keyboard_hook()

    def _setup_keyboard_hook(self):
        """Start global hotkey detection (Carbon on macOS, pynput on Linux)."""
        self._pressed_modifiers.clear()
        self._pressed_main_keys.clear()
        # A rehook may miss the release while stopped, so forget held state.
        self._record_key_held = False

        if self._use_carbon and self._setup_carbon_hotkeys():
            self.backend_available = True
            self.backend_name = "carbon"
            self.backend_error = ""
            return

        # suppress=False: pynput cannot selectively suppress keys, so hotkeys are
        # observed but still pass through to the focused app.
        try:
            listener_class = get_listener_class()
            self._listener = listener_class(
                on_press=self._on_press,
                on_release=self._on_release,
                suppress=False,
            )
            self._listener.daemon = True
            self._listener.start()
        except Exception as exc:
            self._listener = None
            self.backend_available = False
            self.backend_name = "unavailable"
            self.backend_error = str(exc)
            logger.warning(
                "Global hotkeys unavailable; continuing with in-window controls: %s",
                exc,
            )
            return

        self.backend_available = True
        self.backend_name = "pynput"
        self.backend_error = ""
        logger.info("Keyboard hook started")

    def _setup_carbon_hotkeys(self) -> bool:
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
        """Dispatch a normalized press and return whether a hotkey matched."""
        # Enable/disable toggle works even while the program is disabled.
        if self._matches_hotkey(active_modifiers, main_key, self.hotkeys.get("enable_disable")):
            logger.debug(f"Enable/disable hotkey matched from {source}")
            self.trigger_action("enable_disable")
            return True

        if not self.program_enabled:
            return False

        if self._matches_hotkey(active_modifiers, main_key, self.hotkeys.get("record_toggle")):
            logger.debug(f"Record hotkey matched from {source}")
            if self.record_mode == RecordingTriggerMode.PUSH_HOLD:
                self._record_key_held = True
            self.trigger_action("record_toggle")
            return True

        if self._matches_hotkey(
            active_modifiers, main_key, self.hotkeys.get("meeting_toggle")
        ):
            logger.debug(f"Meeting toggle hotkey matched from {source}")
            self.trigger_action("meeting_toggle")
            return True

        if self._matches_hotkey(active_modifiers, main_key, self.hotkeys.get("cancel")):
            logger.debug(f"Cancel hotkey matched from {source}")
            self.trigger_action("cancel")
            return True

        if self._matches_hotkey(active_modifiers, main_key, self.hotkeys.get("minimize_tray")):
            logger.debug(f"Minimize-to-tray hotkey matched from {source}")
            self.trigger_action("minimize_tray")
            return True

        return False

    def handle_hotkey_release(
        self,
        active_modifiers: frozenset,
        main_key: str,
        source: str = "qt",
    ) -> bool:
        """Dispatch release of the record hotkey; return whether it matched.

        Entry point for sources that see individual key events (the Qt
        focused-window fallback). Modifiers are ignored on purpose: the user
        may release them before the main key.
        """
        if not self._record_key_held:
            return False

        _, expected_key = parse_hotkey(self.hotkeys.get("record_toggle") or "")
        if expected_key is None or main_key != expected_key:
            return False

        logger.debug(f"Record hotkey released from {source}")
        self._record_key_held = False
        self.trigger_action("record_release")
        return True

    def trigger_action(self, action: str, released: bool = False) -> None:
        """Apply enable/debounce gating and invoke the callback for an action.

        Shared dispatch for every input source: the Carbon registrar (macOS),
        the pynput global listener (Linux), and the Qt focused-window fallback.
        ``_should_accept_action`` dedupes a press seen by more than one source.
        ``released`` routes Carbon's hotkey-released events, which only the
        record hotkey acts on (push-and-hold mode).
        """
        if released:
            if action == "record_toggle":
                action = "record_release"
            else:
                return

        if (
            action == "record_toggle"
            and self.record_mode == RecordingTriggerMode.PUSH_HOLD
        ):
            # Carbon delivers presses straight here (macOS has no per-key
            # events for registered hotkeys), so the mode must route the
            # toggle action to push-and-hold press dispatch.
            action = "record_press"

        if action == "enable_disable":
            # Works even while the program is disabled.
            if self._should_accept_action("enable_disable"):
                self._toggle_program_enabled()
            return

        if action == "record_release":
            # Ends an in-progress hold; skips the program_enabled gate so a
            # recording cannot be stranded by disabling hotkeys mid-hold.
            if self._should_accept_action("record_release") and self.on_record_release:
                threading.Thread(target=self.on_record_release, daemon=True).start()
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
        elif action == "record_press":
            if self._should_accept_action("record_press") and self.on_record_press:
                threading.Thread(target=self.on_record_press, daemon=True).start()
        elif action == "cancel":
            if self._should_accept_action("cancel") and self.on_cancel:
                threading.Thread(target=self.on_cancel, daemon=True).start()
        elif action == "minimize_tray":
            if self._should_accept_action("minimize_tray") and self.on_minimize_tray:
                threading.Thread(target=self.on_minimize_tray, daemon=True).start()
        elif action == "meeting_toggle":
            if self._should_accept_action("meeting_toggle") and self.on_meeting_toggle:
                threading.Thread(target=self.on_meeting_toggle, daemon=True).start()

    def _on_release(self, key) -> None:
        modifier = modifier_of(key)
        if modifier is not None:
            self._pressed_modifiers.discard(modifier)
            return

        name = key_to_name(key)
        if name is not None:
            self._pressed_main_keys.discard(name)
            self.handle_hotkey_release(
                frozenset(self._pressed_modifiers), name, source="global"
            )

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

    def _should_accept_action(self, action: str) -> bool:
        """Suppress duplicate delivery when Qt and pynput both see a hotkey."""
        current_time = time.monotonic()
        last_time = self._last_action_times.get(action)
        if last_time is not None and current_time - last_time < 0.2:
            return False
        self._last_action_times[action] = current_time
        return True

    def _matches_hotkey(self, active_modifiers: frozenset, main_key: str, hotkey_string: Optional[str]) -> bool:
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
            if self.backend_available:
                logger.info("Keyboard hook re-registered successfully")
            else:
                logger.warning(
                    "Keyboard hook remains unavailable: %s", self.backend_error
                )
        except Exception as e:
            logger.error(f"Failed to re-register keyboard hook: {e}")

    def update_hotkeys(self, new_hotkeys: Dict[str, str]):
        """Replace configured hotkeys and update OS registrations."""
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

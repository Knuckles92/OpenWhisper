"""Carbon ``RegisterEventHotKey`` global-hotkey backend (macOS only).

Unlike an event tap (pynput), Carbon hotkeys register *specific* key
combinations with the window server and fire only for those exact combos. The
OS never exposes other keystrokes to the process, so — like the global
shortcuts in VS Code, Slack, and Electron — this requires **no Accessibility
permission**. That is the whole reason this backend exists: it lets OpenWhisper
hotkeys work while the window is in the background without prompting the user.

Limitations inherent to ``RegisterEventHotKey``:
  * Only modifier+key combinations work; modifier-only chords (e.g. double-tap
    Control) cannot be registered. OpenWhisper's hotkeys are all modifier+key.
  * A combo already claimed system-wide by another app may fail to register
    (``eventHotKeyExistsErr``); we log and continue.

The Carbon event handler is installed on the application event target and is
dispatched by the main run loop, which Qt pumps on macOS — so the press
callback arrives on the Qt main thread. Heavy work must therefore be handed off
to a thread by the caller (``HotkeyManager.trigger_action`` already does this).

This module is imported only by the pynput ``HotkeyManager`` on macOS; nothing
else should import it directly.
"""
import ctypes
import ctypes.util
import logging
import sys
from typing import Callable, Dict, List, Optional

from pynput import keyboard as pynput_keyboard

from services._hotkey_pynput import parse_hotkey

logger = logging.getLogger(__name__)


# --- Carbon constants ----------------------------------------------------------

# Carbon modifier masks (from <Carbon/HIToolbox/Events.h>).
_CARBON_MODIFIERS: Dict[str, int] = {
    "cmd": 0x0100,    # cmdKey
    "shift": 0x0200,  # shiftKey
    "alt": 0x0800,    # optionKey
    "ctrl": 0x1000,   # controlKey
}


def _four_char_code(code: str) -> int:
    """Pack a 4-character string into a big-endian OSType integer."""
    return (
        (ord(code[0]) << 24)
        | (ord(code[1]) << 16)
        | (ord(code[2]) << 8)
        | ord(code[3])
    )


_K_EVENT_CLASS_KEYBOARD = _four_char_code("keyb")
_K_EVENT_HOTKEY_PRESSED = 5
_K_EVENT_PARAM_DIRECT_OBJECT = _four_char_code("----")
_TYPE_EVENT_HOTKEY_ID = _four_char_code("hkid")
_HOTKEY_SIGNATURE = _four_char_code("OWHK")  # OpenWhisper HotKey


# --- ANSI virtual key codes ----------------------------------------------------

# kVK_ANSI_* codes (physical key positions, layout-independent), used so that
# letter/digit/symbol hotkeys resolve without touching layout APIs. Special and
# named keys (space, esc, arrows, F-keys, ...) are pulled from pynput's Key enum
# below so the two backends agree on every key name.
_ANSI_KEYCODES: Dict[str, int] = {
    "a": 0x00, "s": 0x01, "d": 0x02, "f": 0x03, "h": 0x04, "g": 0x05,
    "z": 0x06, "x": 0x07, "c": 0x08, "v": 0x09, "b": 0x0B, "q": 0x0C,
    "w": 0x0D, "e": 0x0E, "r": 0x0F, "y": 0x10, "t": 0x11,
    "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "6": 0x16, "5": 0x17,
    "=": 0x18, "9": 0x19, "7": 0x1A, "-": 0x1B, "8": 0x1C, "0": 0x1D,
    "]": 0x1E, "o": 0x1F, "u": 0x20, "[": 0x21, "i": 0x22, "p": 0x23,
    "l": 0x25, "j": 0x26, "'": 0x27, "k": 0x28, ";": 0x29, "\\": 0x2A,
    ",": 0x2B, "/": 0x2C, "n": 0x2D, "m": 0x2E, ".": 0x2F, "`": 0x32,
}


def _build_special_keycodes() -> Dict[str, int]:
    """Map pynput special-key names (esc, space, f5, ...) to virtual keycodes."""
    mapping: Dict[str, int] = {}
    for key in pynput_keyboard.Key:
        keycode = getattr(key.value, "vk", None)
        # Skip media keys (registered with synthetic vks that aren't real
        # keyboard codes); RegisterEventHotKey can't bind those.
        if keycode is None or getattr(key.value, "_is_media", False):
            continue
        mapping.setdefault(key.name, keycode)
    return mapping


_SPECIAL_KEYCODES: Dict[str, int] = _build_special_keycodes()


def keycode_for(main_key: Optional[str]) -> Optional[int]:
    """Resolve a canonical main-key name to a macOS virtual keycode.

    Returns ``None`` if the key cannot be mapped (the caller should skip that
    hotkey). ``vk<N>`` names — produced by pynput for keys with no character —
    carry the raw keycode directly.
    """
    if not main_key:
        return None
    if main_key in _SPECIAL_KEYCODES:
        return _SPECIAL_KEYCODES[main_key]
    if main_key in _ANSI_KEYCODES:
        return _ANSI_KEYCODES[main_key]
    if main_key.startswith("vk"):
        try:
            return int(main_key[2:])
        except ValueError:
            return None
    return None


# --- ctypes structures / prototypes --------------------------------------------


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


class _EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


# OSStatus handler(EventHandlerCallRef, EventRef, void* userData)
_EVENT_HANDLER_PROC = ctypes.CFUNCTYPE(
    ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
)


def _load_carbon() -> Optional[ctypes.CDLL]:
    """Load the Carbon framework and configure function signatures."""
    if sys.platform != "darwin":
        return None
    try:
        path = ctypes.util.find_library("Carbon")
        carbon = ctypes.cdll.LoadLibrary(path)
    except Exception as exc:
        logger.warning(f"Could not load Carbon framework: {exc}")
        return None

    # Pointer-returning/accepting calls MUST declare types, or ctypes truncates
    # 64-bit pointers to int and crashes.
    carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
    carbon.GetApplicationEventTarget.argtypes = []

    carbon.InstallEventHandler.restype = ctypes.c_int32
    carbon.InstallEventHandler.argtypes = [
        ctypes.c_void_p,            # EventTargetRef
        _EVENT_HANDLER_PROC,        # EventHandlerUPP
        ctypes.c_uint32,            # numTypes
        ctypes.POINTER(_EventTypeSpec),
        ctypes.c_void_p,            # userData
        ctypes.POINTER(ctypes.c_void_p),  # EventHandlerRef*
    ]

    carbon.RegisterEventHotKey.restype = ctypes.c_int32
    carbon.RegisterEventHotKey.argtypes = [
        ctypes.c_uint32,            # hotKeyCode
        ctypes.c_uint32,            # hotKeyModifiers
        _EventHotKeyID,             # hotKeyID (by value)
        ctypes.c_void_p,            # EventTargetRef
        ctypes.c_uint32,            # options
        ctypes.POINTER(ctypes.c_void_p),  # EventHotKeyRef*
    ]

    carbon.UnregisterEventHotKey.restype = ctypes.c_int32
    carbon.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]

    carbon.GetEventParameter.restype = ctypes.c_int32
    carbon.GetEventParameter.argtypes = [
        ctypes.c_void_p,            # EventRef
        ctypes.c_uint32,            # name
        ctypes.c_uint32,            # desiredType
        ctypes.c_void_p,            # outActualType
        ctypes.c_ulong,             # bufferSize
        ctypes.c_void_p,            # outActualSize
        ctypes.c_void_p,            # outData
    ]
    return carbon


_carbon = _load_carbon()


def is_available() -> bool:
    """Whether the Carbon hotkey backend can be used on this system."""
    return _carbon is not None


class CarbonHotkeyRegistrar:
    """Registers OpenWhisper's hotkeys as Carbon global hotkeys.

    A single application-level event handler is installed once; each configured
    action is registered as its own hotkey and mapped back to the action name by
    a small integer id, so the press handler can dispatch without re-matching
    modifiers.
    """

    def __init__(self, on_action: Callable[[str], None]):
        self._on_action = on_action
        self._handler_proc = _EVENT_HANDLER_PROC(self._handle_event)
        self._handler_ref = ctypes.c_void_p()
        self._handler_installed = False
        # action name <-> hotkey id, and the live EventHotKeyRefs to unregister.
        self._id_to_action: Dict[int, str] = {}
        self._hotkey_refs: List[ctypes.c_void_p] = []
        self._next_id = 1

    def _install_handler(self) -> bool:
        if self._handler_installed:
            return True
        if _carbon is None:
            return False

        spec = _EventTypeSpec(_K_EVENT_CLASS_KEYBOARD, _K_EVENT_HOTKEY_PRESSED)
        target = _carbon.GetApplicationEventTarget()
        status = _carbon.InstallEventHandler(
            target,
            self._handler_proc,
            1,
            ctypes.byref(spec),
            None,
            ctypes.byref(self._handler_ref),
        )
        if status != 0:
            logger.error(f"InstallEventHandler failed (OSStatus={status})")
            return False
        self._handler_installed = True
        logger.info("Carbon hotkey event handler installed")
        return True

    def _handle_event(self, _next_handler, event, _user_data) -> int:
        """Carbon callback (main thread): resolve the hotkey id and dispatch."""
        try:
            hotkey_id = _EventHotKeyID()
            status = _carbon.GetEventParameter(
                event,
                _K_EVENT_PARAM_DIRECT_OBJECT,
                _TYPE_EVENT_HOTKEY_ID,
                None,
                ctypes.sizeof(hotkey_id),
                None,
                ctypes.byref(hotkey_id),
            )
            if status == 0:
                action = self._id_to_action.get(hotkey_id.id)
                if action is not None:
                    self._on_action(action)
        except Exception as exc:
            logger.error(f"Error handling Carbon hotkey event: {exc}")
        return 0  # noErr — always let the system continue.

    def register_hotkeys(self, hotkeys: Dict[str, str]) -> None:
        """Register the given ``{action: hotkey_string}`` map, replacing any prior."""
        if _carbon is None:
            return
        if not self._install_handler():
            return

        self.unregister_all()

        for action, hotkey_string in hotkeys.items():
            modifiers, main_key = parse_hotkey(hotkey_string)
            keycode = keycode_for(main_key)
            if keycode is None:
                logger.warning(
                    f"Cannot register hotkey '{hotkey_string}' for {action}: "
                    f"unsupported key '{main_key}'"
                )
                continue

            carbon_mods = 0
            for modifier in modifiers:
                carbon_mods |= _CARBON_MODIFIERS.get(modifier, 0)

            hotkey_id = _EventHotKeyID(_HOTKEY_SIGNATURE, self._next_id)
            ref = ctypes.c_void_p()
            status = _carbon.RegisterEventHotKey(
                keycode,
                carbon_mods,
                hotkey_id,
                _carbon.GetApplicationEventTarget(),
                0,
                ctypes.byref(ref),
            )
            if status != 0:
                logger.error(
                    f"RegisterEventHotKey failed for {action} "
                    f"('{hotkey_string}', OSStatus={status})"
                )
                continue

            self._id_to_action[self._next_id] = action
            self._hotkey_refs.append(ref)
            self._next_id += 1
            logger.info(f"Registered Carbon hotkey for {action}: {hotkey_string}")

    def unregister_all(self) -> None:
        """Unregister every currently-registered hotkey (keeps the handler)."""
        if _carbon is None:
            return
        for ref in self._hotkey_refs:
            try:
                _carbon.UnregisterEventHotKey(ref)
            except Exception as exc:
                logger.debug(f"Error unregistering Carbon hotkey: {exc}")
        self._hotkey_refs.clear()
        self._id_to_action.clear()

    def cleanup(self) -> None:
        """Unregister all hotkeys. The app event handler persists for process life."""
        self.unregister_all()

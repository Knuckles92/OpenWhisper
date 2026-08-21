"""Load only the hotkey backend supported by the current platform."""
import sys

USE_PYNPUT_BACKEND = sys.platform != "win32"

if USE_PYNPUT_BACKEND:
    from services._hotkey_pynput import (
        HotkeyManager,
        parse_hotkey,
        format_hotkey,
        format_hotkey_display,
        send_paste,
        modifier_of,
        key_to_name,
        get_listener_class,
        is_accessibility_trusted,
        request_accessibility_trust,
        accessibility_permission_instructions,
        accessibility_permission_diagnostics,
    )
else:
    from services._hotkey_keyboard import (
        HotkeyManager,
        parse_hotkey,
        format_hotkey,
        format_hotkey_display,
        send_paste,
        is_accessibility_trusted,
        request_accessibility_trust,
        accessibility_permission_instructions,
        accessibility_permission_diagnostics,
    )

__all__ = [
    "HotkeyManager",
    "parse_hotkey",
    "format_hotkey",
    "format_hotkey_display",
    "send_paste",
    "is_accessibility_trusted",
    "request_accessibility_trust",
    "accessibility_permission_instructions",
    "accessibility_permission_diagnostics",
    "USE_PYNPUT_BACKEND",
]

if USE_PYNPUT_BACKEND:
    __all__ += [
        "modifier_of",
        "key_to_name",
        "get_listener_class",
    ]

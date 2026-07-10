"""
Hotkey management for the OpenWhisper application.

This module is a thin platform dispatcher. The actual implementation lives in
one of two backends, and only the selected backend's library is imported:

* Windows  -> ``services._hotkey_keyboard`` (uses the ``keyboard`` library,
  which can suppress keys before they reach the focused app).
* macOS / Linux -> ``services._hotkey_pynput`` (uses ``pynput``; cannot suppress
  keys, so a Qt focus-window fallback is layered on in ``services.runtime``).

Importing ``keyboard`` on macOS (or the pynput-only paths on Windows) would fail
because the platform library is not installed there, so the unused backend is
never imported.
"""
import sys

# pynput on macOS and Linux; the keyboard library on Windows.
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

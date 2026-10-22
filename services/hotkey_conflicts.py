"""One answer to "is this shortcut taken?" across every kind of binding.

Standard actions, cleanup profiles and transforms share the global hotkey
space, so each editor asks here before saving a shortcut.
"""

from __future__ import annotations

from typing import Callable, Iterable, Mapping

from config import config
from services.cleanup_profiles import load_cleanup_profiles, normalize_hotkey
from services.hotkey_manager import parse_hotkey
from services.settings import SettingsKey

STANDARD = "standard"
PROFILE = "profile"
TRANSFORM = "transform"
ACTION = "action"

#: How messages name each standard shortcut's action.
STANDARD_LABELS: dict[str, str] = {
    "record_toggle": "Recording",
    "cancel": "Cancel",
    "meeting_toggle": "Meeting Mode",
    "enable_disable": "Enable/disable",
    "minimize_tray": "Minimize to tray",
    "command_mode": "Command Mode",
    "scratchpad_toggle": "Scratchpad",
    "cycle_language": "Switch language",
    "paste_last_original": "Paste original",
    "route_record": "Route by voice",
    "walkthrough": "Walkthrough",
    "screenshot": "Screenshot",
}


def _signature(hotkey: str):
    return parse_hotkey(normalize_hotkey(hotkey))


def hotkey_conflict(
    hotkey: str,
    settings: Mapping,
    *,
    exclude: tuple[str, str] = ("", ""),
    standard_hotkeys: Mapping[str, str] | None = None,
) -> str:
    """Return what already uses ``hotkey``, or "" when it is free.

    Args:
        hotkey: The shortcut being assigned; modifier aliases and order
            don't matter.
        settings: Loaded settings, for the saved profiles, transforms and
            standard shortcuts.
        exclude: ``(kind, id)`` of the binding being edited, so it does not
            conflict with itself. ``kind`` is "standard" (id is the action),
            "profile", "transform" or "action" (a saved action).
        standard_hotkeys: Standard shortcuts not saved yet; they replace the
            saved ones.

    Returns:
        The standard shortcut's label (STANDARD_LABELS), or the profile's,
        transform's or action's name.
    """
    if not hotkey:
        return ""
    signature = _signature(hotkey)
    conflict = _standard_conflict(signature, settings, exclude, standard_hotkeys)
    if conflict is not None:
        return conflict
    conflict = _binding_conflict(
        load_cleanup_profiles(settings), PROFILE, signature, exclude, parse_hotkey
    )
    if conflict is not None:
        return conflict
    # Imported on use, so text_transforms may validate its own shortcuts
    # through this module.
    from services import text_transforms

    conflict = _binding_conflict(
        text_transforms.load_transforms(settings), TRANSFORM, signature, exclude, _signature
    )
    if conflict is not None:
        return conflict
    from services.actions.model import load_actions

    conflict = _binding_conflict(
        load_actions(settings), ACTION, signature, exclude, _signature
    )
    if conflict is not None:
        return conflict
    return ""


def _standard_conflict(
    signature,
    settings: Mapping,
    exclude: tuple[str, str],
    standard_hotkeys: Mapping[str, str] | None,
) -> str | None:
    saved = standard_hotkeys
    if saved is None:
        saved = settings.get(SettingsKey.HOTKEYS) or {}
    standard = {**config.DEFAULT_HOTKEYS, **saved}
    for action, value in standard.items():
        if exclude == (STANDARD, action):
            continue
        if value and _signature(value) == signature:
            return STANDARD_LABELS.get(action, action.replace("_", " "))
    return None


def _binding_conflict(
    bindings: Iterable,
    kind: str,
    signature,
    exclude: tuple[str, str],
    signature_of: Callable,
) -> str | None:
    """Return the name of the first binding of ``kind`` that uses ``signature``."""
    for binding in bindings:
        if exclude == (kind, binding.id):
            continue
        if binding.hotkey and signature_of(binding.hotkey) == signature:
            return binding.name
    return None

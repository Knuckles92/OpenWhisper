"""One answer to "is this shortcut taken?" across every kind of binding.

Standard actions, cleanup profiles and transforms share the global hotkey
space, so each editor asks here before saving a shortcut.
"""

from __future__ import annotations

from typing import Mapping

from config import config
from services.cleanup_profiles import load_cleanup_profiles, normalize_hotkey
from services.hotkey_manager import parse_hotkey
from services.settings import SettingsKey

STANDARD = "standard"
PROFILE = "profile"
TRANSFORM = "transform"


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
            "profile" or "transform".
        standard_hotkeys: Standard shortcuts not saved yet; they replace the
            saved ones.

    Returns:
        The action's name with spaces, the profile's name, or the
        transform's name.
    """
    if not hotkey:
        return ""
    signature = _signature(hotkey)
    saved = standard_hotkeys
    if saved is None:
        saved = settings.get(SettingsKey.HOTKEYS) or {}
    standard = {**config.DEFAULT_HOTKEYS, **saved}
    for action, value in standard.items():
        if exclude == (STANDARD, action):
            continue
        if value and _signature(value) == signature:
            return action.replace("_", " ")
    for profile in load_cleanup_profiles(settings):
        if exclude == (PROFILE, profile.id):
            continue
        if profile.hotkey and parse_hotkey(profile.hotkey) == signature:
            return profile.name
    # Imported on use, so text_transforms may validate its own shortcuts
    # through this module.
    from services import text_transforms

    for transform in text_transforms.load_transforms(settings):
        if exclude == (TRANSFORM, transform.id):
            continue
        if transform.hotkey and _signature(transform.hotkey) == signature:
            return transform.name
    return ""

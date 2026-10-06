"""Which AI cleanup preset applies, and its base prompt.

The AI cleanup switch stays the master on/off; a level only picks the
preset while it is on. A saved custom prompt replaces the preset.
"""

from __future__ import annotations

from typing import Final, Mapping

from config import config
from services.settings import (
    SettingsKey,
    TranscriptCleanupLevel,
    resolve_bool_setting,
    resolve_transcript_cleanup_level,
    resolve_transcript_cleanup_prompt,
)


class CleanupLevel:
    NONE: Final[str] = "none"
    LIGHT: Final[str] = TranscriptCleanupLevel.LIGHT
    MEDIUM: Final[str] = TranscriptCleanupLevel.MEDIUM
    HIGH: Final[str] = TranscriptCleanupLevel.HIGH

    STORED: Final[tuple[str, ...]] = (LIGHT, MEDIUM, HIGH)


def resolve_level(settings: Mapping) -> str:
    """The preset in use: "none" while AI cleanup is off."""
    if not resolve_bool_setting(SettingsKey.TRANSCRIPT_CLEANUP_ENABLED, settings):
        return CleanupLevel.NONE
    return resolve_transcript_cleanup_level(settings)


def custom_prompt(settings: Mapping) -> str:
    """The user's saved prompt, or "" when none is saved.

    A saved copy of the old built-in prompt counts as none, so installs that
    stored the default are not shown as Custom.
    """
    stored = settings.get(SettingsKey.TRANSCRIPT_CLEANUP_PROMPT)
    if not isinstance(stored, str):
        return ""
    stripped = stored.strip()
    if not stripped or stripped == config.TRANSCRIPT_CLEANUP_PROMPT.strip():
        return ""
    return stripped


def base_prompt(settings: Mapping, level: str) -> str:
    """The custom prompt, or ``level``'s preset."""
    return custom_prompt(settings) or resolve_transcript_cleanup_prompt(settings)

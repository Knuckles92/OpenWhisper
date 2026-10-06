"""Which AI cleanup preset applies, and its base prompt.

The AI cleanup switch stays the master on/off; a level only picks the
preset while it is on. A saved custom prompt replaces the preset.
"""

from __future__ import annotations

from typing import Final, Mapping

from config import config
from services import synthetic_keys
from services.settings import (
    SettingsKey,
    TranscriptCleanupLevel,
    resolve_bool_setting,
    resolve_transcript_cleanup_level,
)


class CleanupLevel:
    NONE: Final[str] = "none"
    LIGHT: Final[str] = TranscriptCleanupLevel.LIGHT
    MEDIUM: Final[str] = TranscriptCleanupLevel.MEDIUM
    HIGH: Final[str] = TranscriptCleanupLevel.HIGH

    STORED: Final[tuple[str, ...]] = (LIGHT, MEDIUM, HIGH)
    LABELS: Final[Mapping[str, str]] = {
        NONE: "Off", LIGHT: "Light", MEDIUM: "Medium", HIGH: "High",
    }


#: What the level control and rail show while a custom prompt replaces the preset.
CUSTOM_LABEL: Final[str] = "Custom"


def _normalized(text: str) -> str:
    return " ".join(text.split())


_BUILT_IN_PROMPTS: Final[frozenset[str]] = frozenset(
    _normalized(prompt)
    for prompt in (
        *config.LEGACY_DEFAULT_CLEANUP_PROMPTS,
        *config.TRANSCRIPT_CLEANUP_LEVEL_PROMPTS.values(),
    )
)


def is_built_in(prompt: str) -> bool:
    """Whether ``prompt`` is a level preset or the old default, spacing aside."""
    return _normalized(prompt) in _BUILT_IN_PROMPTS


def resolve_level(settings: Mapping) -> str:
    """The preset in use: "none" while AI cleanup is off."""
    if not resolve_bool_setting(SettingsKey.TRANSCRIPT_CLEANUP_ENABLED, settings):
        return CleanupLevel.NONE
    return resolve_transcript_cleanup_level(settings)


def custom_prompt(settings: Mapping) -> str:
    """The user's saved prompt, or "" when none is saved.

    A saved copy of a built-in prompt counts as none: Reset used to store the
    old default, and those installs should get the presets.
    """
    stored = settings.get(SettingsKey.TRANSCRIPT_CLEANUP_PROMPT)
    if not isinstance(stored, str):
        return ""
    stripped = stored.strip()
    if not stripped or is_built_in(stripped):
        return ""
    return stripped


def preset(level: str) -> str:
    """``level``'s built-in prompt; Medium's for a level that has none."""
    prompts = config.TRANSCRIPT_CLEANUP_LEVEL_PROMPTS
    return prompts.get(level) or prompts[CleanupLevel.MEDIUM]


def base_prompt(settings: Mapping, level: str) -> str:
    """The custom prompt, or ``level``'s preset.

    For "none" (cleanup off) it is the saved level's preset: the prompt
    cleanup would use once it is turned on.
    """
    custom = custom_prompt(settings)
    if custom:
        return custom
    if level not in CleanupLevel.STORED:
        level = resolve_transcript_cleanup_level(settings)
    return preset(level)


def inline_lists_block(settings: Mapping, snapshot) -> str:
    """Keeps a live dictation on one line where line breaks may not belong.

    Medium and High put each spoken list item on its own line and break long
    dictations into paragraphs. Pasted into a terminal a line break runs the
    command, and an app the focus capture could not identify may be one. ""
    when the preset adds no breaks (Light, a custom prompt) or the app is
    known to take them.

    Args:
        settings: Settings loaded for the dictation.
        snapshot: The job's FocusSnapshot, or None when it has none.
    """
    if custom_prompt(settings) or resolve_level(settings) not in (
        CleanupLevel.MEDIUM, CleanupLevel.HIGH,
    ):
        return ""
    identity = getattr(snapshot, "identity", None)
    if identity is None:
        return config.TRANSCRIPT_CLEANUP_UNKNOWN_APP_LINES
    if synthetic_keys.is_terminal(identity):
        return config.TRANSCRIPT_CLEANUP_TERMINAL_LINES
    return ""


def level_label(settings: Mapping) -> str:
    """"Off", "Custom", or the level's name, as the rail and Basic show it."""
    level = resolve_level(settings)
    if level == CleanupLevel.NONE:
        return CleanupLevel.LABELS[CleanupLevel.NONE]
    if custom_prompt(settings):
        return CUSTOM_LABEL
    return CleanupLevel.LABELS[level]

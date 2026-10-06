"""Writing styles per kind of app: the tone AI cleanup uses where you dictate.

Styles only change output while AI cleanup is on, and an explicit cleanup
profile always wins over a style.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Mapping

from services.focus_context import FocusSnapshot
from services.settings import SettingsKey


class AppCategory:
    EMAIL: Final[str] = "email"
    WORK: Final[str] = "work"
    PERSONAL: Final[str] = "personal"
    OTHER: Final[str] = "other"

    ALL: Final[tuple[str, ...]] = (EMAIL, WORK, PERSONAL, OTHER)


class Tone:
    FORMAL: Final[str] = "formal"
    CASUAL: Final[str] = "casual"
    VERY_CASUAL: Final[str] = "very_casual"

    ALL: Final[tuple[str, ...]] = (FORMAL, CASUAL, VERY_CASUAL)


class Surface:
    TEXT: Final[str] = "text"
    CODE: Final[str] = "code"
    TERMINAL: Final[str] = "terminal"


@dataclass(frozen=True)
class AppStyle:
    category: str
    tone: str
    app_name: str = ""
    surface: str = Surface.TEXT


#: Seeded once on a brand-new install. Existing installs have no
#: app_style_tones key, which reads as every category Formal: today's output.
NEW_INSTALL_TONES: Final[dict[str, str]] = {
    AppCategory.EMAIL: Tone.FORMAL,
    AppCategory.WORK: Tone.CASUAL,
    AppCategory.PERSONAL: Tone.CASUAL,
    AppCategory.OTHER: Tone.FORMAL,
}


def resolve_tones(settings: Mapping) -> dict[str, str]:
    """``{category: tone}`` for every category; invalid or missing ones are Formal."""
    stored = settings.get(SettingsKey.APP_STYLE_TONES)
    if not isinstance(stored, Mapping):
        stored = {}
    return {
        category: stored[category] if stored.get(category) in Tone.ALL else Tone.FORMAL
        for category in AppCategory.ALL
    }


def style_for(snapshot: FocusSnapshot | None, settings: Mapping) -> AppStyle | None:
    """The style for the app in ``snapshot``, or None when none applies."""
    return None


def prompt_block(style: AppStyle | None) -> str:
    """The cleanup-prompt block asking for ``style``'s tone, or ""."""
    return ""

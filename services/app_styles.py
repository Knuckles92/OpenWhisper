"""Writing styles per kind of app: the tone AI cleanup uses where you dictate.

Styles only change output while AI cleanup is on, and an explicit cleanup
profile always wins over a style. The style block names a kind of writing,
never the app, so styles alone send nothing about where you dictate to the
cleanup provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Mapping, Optional

from services.focus_context import FocusSnapshot, catalog
from services.settings import SettingsKey, resolve_app_styles_enabled


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

_WRITING: Final[dict[str, str]] = {
    AppCategory.EMAIL: "an email",
    AppCategory.WORK: "a work chat message",
    AppCategory.PERSONAL: "a personal message",
    AppCategory.OTHER: "a note",
}

_TONE_PROMPTS: Final[dict[str, str]] = {
    Tone.CASUAL: (
        "Tone: casual and conversational, as in {writing}. Use sentence case "
        "and light punctuation; contractions are fine. Leave off the final "
        "period when the whole text is one short sentence. Keep the speaker's "
        "words, and add no greeting or sign-off."
    ),
    Tone.VERY_CASUAL: (
        "Tone: very casual, as in {writing} to a friend. All lowercase is "
        "fine, keep punctuation minimal and never end with a period. Keep the "
        "speaker's words and slang as spoken, and add no greeting, sign-off or "
        "emoji."
    ),
}

_SURFACE_PROMPTS: Final[dict[str, str]] = {
    Surface.TERMINAL: (
        "The text goes into a terminal: never insert line breaks, and keep any "
        "list items inline on one line."
    ),
    Surface.CODE: (
        "The text goes into a code editor: keep identifiers, file names, paths "
        "and their casing (snake_case, camelCase) exactly as written."
    ),
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


def resolve_overrides(settings: Mapping) -> tuple[tuple[str, str], ...]:
    """The user's valid ``(match, category)`` choices, first one per app wins."""
    return catalog.parse_overrides(settings.get(SettingsKey.APP_STYLE_OVERRIDES))


def category_for(snapshot: Optional[FocusSnapshot], settings: Mapping) -> str:
    """The category of the app in ``snapshot``, styles on or off; "" when unknown."""
    identity = snapshot.identity if snapshot is not None else None
    if identity is None or identity.is_self:
        return ""
    kind = catalog.classify(identity, resolve_overrides(settings or {}))
    return kind.category if kind is not None else ""


def style_for(snapshot: FocusSnapshot | None, settings: Mapping) -> AppStyle | None:
    """The style for the app in ``snapshot``, or None when none applies."""
    settings = settings or {}
    if not resolve_app_styles_enabled(settings):
        return None
    identity = snapshot.identity if snapshot is not None else None
    if identity is None or identity.is_self:
        return None
    kind = catalog.classify(identity, resolve_overrides(settings))
    if kind is None:
        return None
    return AppStyle(
        category=kind.category,
        tone=resolve_tones(settings)[kind.category],
        app_name=kind.name,
        surface=kind.surface,
    )


def prompt_block(style: AppStyle | None) -> str:
    """The cleanup-prompt block asking for ``style``'s tone, or "".

    Formal is the cleanup prompt's own voice, so it adds nothing. Terminals
    and code editors get their rule whatever the tone: a line break in a
    terminal can run a command.
    """
    if style is None:
        return ""
    lines = []
    tone = _TONE_PROMPTS.get(style.tone)
    if tone:
        lines.append(tone.format(writing=_WRITING.get(style.category, "a note")))
    surface = _SURFACE_PROMPTS.get(style.surface)
    if surface:
        lines.append(surface)
    return "\n".join(lines)


def set_tone(settings: dict[str, Any], category: str, tone: str) -> bool:
    """Mutator for ``mutate_settings``: store one category's tone.

    Writing any tone stores all four, so a category left unset stays Formal
    rather than picking up a later default. Returns whether it changed.
    """
    if category not in AppCategory.ALL or tone not in Tone.ALL:
        raise ValueError(f"Unknown style {category!r}: {tone!r}")
    tones = resolve_tones(settings)
    changed = tones[category] != tone or SettingsKey.APP_STYLE_TONES not in settings
    tones[category] = tone
    settings[SettingsKey.APP_STYLE_TONES] = tones
    return changed


def set_override(settings: dict[str, Any], match: str, category: str) -> None:
    """Mutator: put the app or site named ``match`` in ``category``.

    Replaces any earlier choice for the same name; a choice that equals the
    built-in category is dropped instead of stored.
    """
    match = (match or "").strip()[:catalog.MAX_MATCH_CHARS]
    if not match or category not in AppCategory.ALL:
        raise ValueError("Choose an app and a style")
    kept = [
        {"match": existing, "category": chosen}
        for existing, chosen in resolve_overrides(settings)
        if existing.casefold() != match.casefold()
    ]
    if catalog.builtin_category(match) != category:
        kept.insert(0, {"match": match, "category": category})
    settings[SettingsKey.APP_STYLE_OVERRIDES] = kept[:catalog.MAX_OVERRIDES]


def remove_override(settings: dict[str, Any], match: str) -> None:
    """Mutator: forget the user's choice for ``match``."""
    key = (match or "").strip().casefold()
    settings[SettingsKey.APP_STYLE_OVERRIDES] = [
        {"match": existing, "category": chosen}
        for existing, chosen in resolve_overrides(settings)
        if existing.casefold() != key
    ]

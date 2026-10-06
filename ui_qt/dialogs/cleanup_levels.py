"""Copy for the AI cleanup level control, shared by Advanced and Basic."""

from services.cleanup_prompts import CleanupLevel

#: The control's choice while a saved custom prompt replaces the presets.
CUSTOM = "custom"

#: (value, title, detail) per segment, in order, Custom last.
SEGMENTS = (
    (CleanupLevel.LIGHT, "Light", "Punctuation & fillers"),
    (CleanupLevel.MEDIUM, "Medium", "+ corrections & lists"),
    (CleanupLevel.HIGH, "High", "+ tighter wording"),
    (CUSTOM, "Custom", "Your own prompt"),
)

#: What the chosen level does, under the AI cleanup page's control.
NOTES = {
    CleanupLevel.LIGHT: (
        "Fixes punctuation, capitalization, filler words, and obvious "
        "mishearings. Your words stay as you said them."
    ),
    CleanupLevel.MEDIUM: (
        "Also keeps just the fix when you correct yourself (“at 2, actually "
        "3” becomes “at 3”), turns spoken lists into lists, and adds "
        "paragraph breaks."
    ),
    CleanupLevel.HIGH: (
        "Also drops false starts and repeats, and tightens the wording while "
        "keeping your meaning and voice."
    ),
    CUSTOM: (
        "Your custom prompt below replaces the presets. Pick a level to go "
        "back to one."
    ),
}

#: The same, in a few words, for the Basic view's row.
SHORT_NOTES = {
    CleanupLevel.LIGHT: "Punctuation and filler words only.",
    CleanupLevel.MEDIUM: "Also handles self-corrections and spoken lists.",
    CleanupLevel.HIGH: "Also tightens your wording.",
    CUSTOM: "Uses your custom prompt from AI cleanup.",
}


def segment_index(value: str) -> int:
    """The segment for a level or ``CUSTOM``; Medium's for anything else."""
    values = [segment[0] for segment in SEGMENTS]
    return values.index(value) if value in values else values.index(CleanupLevel.MEDIUM)

"""Write-up styles: named prompts a walkthrough can be written up with.

The default, "Walkthrough", is built in and always first. The rest are the
user's to add, replace and delete; until any is saved, the starters show.
Picking a style only fills in the instructions: what's saved for the next
walkthrough is still the words, not the name.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass

from services.settings import SettingsKey
from services.walkthrough.write_up import DEFAULT_INSTRUCTIONS

#: The longest name a style may have.
MAX_NAME = 60
#: How many styles may be saved beside the default.
MAX_STYLES = 30


@dataclass(frozen=True)
class WriteUpStyle:
    id: str
    name: str
    instructions: str

    def to_dict(self) -> dict:
        return asdict(self)


DEFAULT_STYLE = WriteUpStyle("walkthrough", "Walkthrough", DEFAULT_INSTRUCTIONS)

STARTER_STYLES = (
    WriteUpStyle("step-by-step", "Step-by-step guide", (
        "Turn this into a how-to guide someone could follow on their own. Give "
        "it a short title, then numbered steps in the order I did them, each "
        "saying what to do and what you should see, with the screenshot for "
        "that step right under it. Tidy my wording and drop filler, but keep "
        "every detail someone would need to repeat it."
    )),
    WriteUpStyle("bug-report", "Bug report", (
        "Write this up as a bug report. Start with a short title and a "
        "one-sentence summary of the problem, then the steps to reproduce it, "
        "what happened, and what I expected instead. Put each screenshot where "
        "it shows the problem best. Keep exact names, numbers and error "
        "messages as I said or showed them, and don't guess at causes I didn't "
        "mention."
    )),
    WriteUpStyle("design-feedback", "Design feedback", (
        "Write this up as design feedback on the screens I'm showing. Give it a "
        "short title, then one section per change I'm suggesting: what's there "
        "now, what I'd change and why, with the screenshot that shows it. End "
        "with a short list of the proposed changes. Tidy my wording and keep my "
        "reasoning, but don't add ideas I didn't have."
    )),
    WriteUpStyle("agent-task", "Task for an agent", (
        "Write this up as a brief for a coding agent to act on. Give it a short "
        "title, then the goal in a sentence or two, what to change and where, "
        "as I described it, and how to tell it's done. Put each screenshot "
        "beside the part it explains. Keep the exact names of screens, buttons "
        "and files, and don't add requirements I didn't state."
    )),
)


def saved_styles(settings: dict) -> list[WriteUpStyle]:
    """The user's styles, without the default; the starters until any is saved."""
    raw = (settings or {}).get(SettingsKey.WALKTHROUGH_WRITE_UP_STYLES)
    if raw is None:
        return list(STARTER_STYLES)
    if not isinstance(raw, list):
        return []
    styles: list[WriteUpStyle] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        values = [item.get(key) for key in ("id", "name", "instructions")]
        if any(not isinstance(value, str) or not value.strip() for value in values):
            continue
        style = WriteUpStyle(*(value.strip() for value in values))
        if style.id in seen or style.id == DEFAULT_STYLE.id:
            continue
        seen.add(style.id)
        styles.append(style)
    return styles[:MAX_STYLES]


def load_styles(settings: dict) -> list[WriteUpStyle]:
    """Every style to pick from: the default, then the user's."""
    return [DEFAULT_STYLE, *saved_styles(settings)]


def style_named(name: str, styles) -> WriteUpStyle | None:
    folded = name.strip().casefold()
    return next((style for style in styles if style.name.casefold() == folded), None)


def style_for(instructions: str, styles) -> WriteUpStyle | None:
    """The style these instructions are, word for word, or None for the user's own.

    "" instructions are the default's, as everywhere else.
    """
    text = instructions.strip() or DEFAULT_INSTRUCTIONS
    return next((style for style in styles if style.instructions == text), None)


def save_style(settings: dict, name: str, instructions: str) -> WriteUpStyle:
    """Save ``instructions`` as the style ``name``, replacing one by that name.

    Mutate inside SettingsManager.mutate_settings to keep concurrent edits.

    Raises:
        ValueError: Saying what to fix, for the user.
    """
    name, instructions = name.strip(), instructions.strip()
    if not name:
        raise ValueError("Give the style a name.")
    if len(name) > MAX_NAME:
        raise ValueError(f"Use a name with {MAX_NAME} characters or fewer.")
    if not instructions:
        raise ValueError("Add the instructions for the write-up you want.")
    if name.casefold() == DEFAULT_STYLE.name.casefold():
        raise ValueError(f"\"{DEFAULT_STYLE.name}\" is the built-in style. Choose another name.")
    styles = saved_styles(settings)
    existing = style_named(name, styles)
    if existing is None and len(styles) >= MAX_STYLES:
        raise ValueError(f"You can keep up to {MAX_STYLES} styles. Delete one first.")
    style = WriteUpStyle(existing.id if existing else uuid.uuid4().hex[:12], name, instructions)
    if existing is None:
        styles.append(style)
    else:
        styles[styles.index(existing)] = style
    settings[SettingsKey.WALKTHROUGH_WRITE_UP_STYLES] = [each.to_dict() for each in styles]
    return style


def delete_style(settings: dict, style_id: str) -> None:
    """Mutate inside SettingsManager.mutate_settings; the default stays."""
    settings[SettingsKey.WALKTHROUGH_WRITE_UP_STYLES] = [
        style.to_dict() for style in saved_styles(settings) if style.id != style_id
    ]

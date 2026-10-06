"""Saved rewrite instructions applied to selected text, optionally by shortcut."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping

from config import config
from services.cleanup_profiles import normalize_hotkey
from services.settings import SettingsKey

NAME_MAX_CHARS = 80


@dataclass(frozen=True)
class Transform:
    id: str
    name: str
    instruction: str
    hotkey: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


STARTER_TRANSFORMS: tuple[Transform, ...] = (
    Transform(
        "polish",
        "Polish",
        "Improve the flow and clarity of the text while keeping its meaning, "
        "tone, and length about the same. Fix grammar and punctuation.",
    ),
    Transform(
        "make-concise",
        "Make concise",
        "Make the text shorter and more direct. Keep every fact, request, and "
        "name; remove filler, repetition, and hedging.",
    ),
    Transform(
        "fix-grammar",
        "Fix grammar",
        "Fix spelling, grammar, and punctuation only. Do not change wording, "
        "tone, or structure beyond what a correction needs.",
    ),
    Transform(
        "prompt-engineer",
        "Prompt engineer",
        "Rewrite the text as a clear, well-structured prompt for an AI "
        "assistant: state the goal, the relevant context, constraints, and "
        "the expected output format. Do not answer the prompt.",
    ),
)


def _hotkey(value) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        return normalize_hotkey(value)
    except Exception:
        return ""


def load_transforms(settings: Mapping) -> list[Transform]:
    """The saved transforms; the starters before any library is saved.

    Malformed rows, repeated ids and anything past
    ``config.MAX_TEXT_TRANSFORMS`` are skipped rather than failing the list.
    """
    raw = (settings or {}).get(SettingsKey.TEXT_TRANSFORMS)
    if raw is None:
        return list(STARTER_TRANSFORMS)
    if not isinstance(raw, list):
        return []
    transforms = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        values = [item.get(key) for key in ("id", "name", "instruction")]
        if any(not isinstance(value, str) or not value.strip() for value in values):
            continue
        transform_id, name, instruction = (value.strip() for value in values)
        if transform_id in seen:
            continue
        seen.add(transform_id)
        transforms.append(
            Transform(transform_id, name, instruction, _hotkey(item.get("hotkey")))
        )
        if len(transforms) >= config.MAX_TEXT_TRANSFORMS:
            break
    return transforms


def find_transform(settings: Mapping, transform_id: str) -> Transform | None:
    return next((t for t in load_transforms(settings) if t.id == transform_id), None)


def transform_hotkey_conflict(
    hotkey: str, settings: Mapping, *, exclude_id: str = "",
) -> str:
    """The action, profile or other transform already using ``hotkey``, or ""."""
    # hotkey_conflicts reads this module's transforms, so import on use.
    from services.hotkey_conflicts import TRANSFORM, hotkey_conflict

    return hotkey_conflict(hotkey, settings, exclude=(TRANSFORM, exclude_id))


def validate_transform(transform: Transform, settings: Mapping) -> None:
    """Raise ValueError with a user-facing reason ``transform`` can't be saved."""
    name = transform.name.strip()
    if not name:
        raise ValueError("Give the transform a name.")
    if len(name) > NAME_MAX_CHARS:
        raise ValueError(f"Use a name with {NAME_MAX_CHARS} characters or fewer.")
    if not transform.instruction.strip():
        raise ValueError("Add an instruction for how to change the text.")
    existing = load_transforms(settings)
    if any(t.id != transform.id and t.name.casefold() == name.casefold() for t in existing):
        raise ValueError("A transform with that name already exists.")
    if (
        all(t.id != transform.id for t in existing)
        and len(existing) >= config.MAX_TEXT_TRANSFORMS
    ):
        raise ValueError(
            f"You can keep up to {config.MAX_TEXT_TRANSFORMS} transforms. "
            "Delete one to add another."
        )
    conflict = transform_hotkey_conflict(transform.hotkey, settings, exclude_id=transform.id)
    if conflict:
        raise ValueError(f"That shortcut is already used by {conflict}. Choose another.")


def save_transform(settings: dict, transform: Transform) -> None:
    """Add or replace ``transform``; a ``mutate_settings`` mutator.

    Raises:
        ValueError: With a user-facing reason; nothing is changed.
    """
    transform = Transform(
        transform.id.strip(),
        transform.name.strip(),
        transform.instruction.strip(),
        _hotkey(transform.hotkey),
    )
    if not transform.id:
        raise ValueError("This transform has no id.")
    validate_transform(transform, settings)
    transforms = load_transforms(settings)
    index = next(
        (i for i, t in enumerate(transforms) if t.id == transform.id), len(transforms)
    )
    transforms[index : index + 1] = [transform]
    settings[SettingsKey.TEXT_TRANSFORMS] = [t.to_dict() for t in transforms]


def delete_transform(settings: dict, transform_id: str) -> None:
    """Remove a transform; a ``mutate_settings`` mutator."""
    settings[SettingsKey.TEXT_TRANSFORMS] = [
        t.to_dict() for t in load_transforms(settings) if t.id != transform_id
    ]

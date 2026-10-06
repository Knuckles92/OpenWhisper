"""Saved rewrite instructions applied to selected text, optionally by shortcut."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping


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


def load_transforms(settings: Mapping) -> list[Transform]:
    """The saved transforms; the starters before any library is saved."""
    return []


def save_transform(settings: dict, transform: Transform) -> None:
    """Add or replace ``transform``; a ``mutate_settings`` mutator."""


def delete_transform(settings: dict, transform_id: str) -> None:
    """Remove a transform; a ``mutate_settings`` mutator."""


def find_transform(settings: Mapping, transform_id: str) -> Transform | None:
    return next((t for t in load_transforms(settings) if t.id == transform_id), None)

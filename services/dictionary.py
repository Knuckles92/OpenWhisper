"""The personal dictionary: words OpenWhisper should always get right.

Terms reach the speech model where an engine supports hints, replace their
"sounds like" variants in every transcript, and go into the cleanup prompt.
Terms are user content: never log them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True)
class DictionaryTerm:
    id: str
    term: str
    starred: bool = False
    #: User-entered "sounds like" variants replaced by ``term``.
    heard: tuple[str, ...] = ()
    learned: bool = False
    #: Added since the user last looked at the Dictionary page.
    new: bool = False


def load_dictionary(settings: Mapping) -> list[DictionaryTerm]:
    """The valid saved terms, in their saved order."""
    return []


def recognition_phrases(settings: Mapping) -> tuple[str, ...]:
    """Speech-model hints: starred terms first, cut to the engine budget."""
    return ()


def apply_replacements(text: str, terms: Sequence[DictionaryTerm]) -> str:
    """``text`` with every "sounds like" variant replaced by its term."""
    return text


def prompt_block(terms: Sequence[DictionaryTerm]) -> str:
    """The cleanup-prompt block listing the terms, or ""."""
    return ""


def schedule_learning(job, pasted_text: str) -> None:
    """Look for a correction of the pasted text later; Qt thread, never blocks."""

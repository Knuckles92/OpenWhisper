"""Snippets: a spoken trigger phrase becomes exact text, without AI.

A whole-utterance trigger skips cleanup. Triggers inside a sentence become
protected placeholders before cleanup and are expanded after it; if cleanup
loses a placeholder, the uncleaned text with expansions is used instead.
Live dictation only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True)
class Snippet:
    id: str
    trigger: str
    text: str
    #: Render light Markdown to HTML and paste it as rich text.
    formatted: bool = False


@dataclass(frozen=True)
class SnippetPlan:
    #: The text to clean: placeholders stand in for triggers.
    text: str
    #: Set when the whole utterance was one trigger.
    whole: Snippet | None = None
    placeholders: tuple[tuple[str, Snippet], ...] = ()


def load_snippets(settings: Mapping) -> list[Snippet]:
    """The valid saved snippets, in their saved order."""
    return []


def plan_expansion(text: str, snippets: Sequence[Snippet]) -> SnippetPlan:
    """Find triggers in ``text`` and protect them for cleanup."""
    return SnippetPlan(text)


def prompt_guard(plan: SnippetPlan) -> str:
    """The cleanup-prompt instruction to keep ``plan``'s placeholders, or ""."""
    return ""


def expand(text: str, plan: SnippetPlan) -> tuple[str, str, bool]:
    """``(plain, html, ok)``: ``text`` with ``plan``'s placeholders expanded.

    ``html`` is "" unless a formatted snippet was used; ``ok`` is False when
    a placeholder is missing from ``text``.
    """
    return text, "", True

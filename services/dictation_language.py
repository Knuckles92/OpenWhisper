"""The languages a user dictates in, and the one the next dictation uses.

Switching never reloads the engine and never writes local_asr_language; the
active language is read when a dictation's final pass starts.
"""

from __future__ import annotations

from typing import Mapping

from services.settings import MeetingLanguage


def language_choices(settings: Mapping) -> list[str]:
    """Codes from dictation_languages that the current engine accepts."""
    return []


def job_language(settings: Mapping) -> str:
    """The language for a dictation starting now; "" for the engine's own setting."""
    return ""


def cycle(settings: dict) -> None:
    """Make the next choice active; a ``mutate_settings`` mutator."""


def label(code: str) -> str:
    """Display name for a language code, such as "English"."""
    return dict(MeetingLanguage.CHOICES).get(code, code)


def short_label(code: str) -> str:
    """Chip text for a language code, such as "EN"."""
    return code.upper()

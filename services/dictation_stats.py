"""Local-only dictation statistics for the Stats window.

Rows live in the dictation_stats table, written when history is saved, so
they survive moving history to a paired host and Clear history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, tzinfo


@dataclass(frozen=True)
class StatsSummary:
    words_this_week: int = 0
    words_all_time: int = 0
    average_wpm: float = 0.0
    words_cleaned_up: int = 0
    day_streak: int = 0
    #: ``(app_name, words)``, most words first.
    top_apps: tuple[tuple[str, int], ...] = ()
    #: ``(day, words)`` for the last 14 days, oldest first.
    daily_words: tuple[tuple[date, int], ...] = ()


def record(entry_fields: dict, row) -> None:
    """Add a stats row for a saved history entry; runs on the history-save worker."""


def load_summary(today: date | None = None, tz: tzinfo | None = None) -> StatsSummary:
    return StatsSummary()


def reset() -> None:
    """Delete every stats row; history is untouched."""

"""The "New in OpenWhisper" note on Settings → Home.

This file is the only place the note lives. To refresh it:

1. Rewrite ``text`` — one or two sentences naming the headline features of
   the current release line, taken from ``CHANGELOG.md``.
2. Set ``updated`` to today's date. Anyone who dismissed the previous note
   sees the new one once, because dismissal remembers the ``updated`` it hid.
3. Keep ``release_line`` equal to the app's major.minor.

``tests/test_whats_new.py`` fails when ``release_line`` falls behind
``_version.py``, so a milestone release cannot ship last line's note, and when
the text outgrows the tile. Fixing a typo needs no new ``updated``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class WhatsNew:
    """One note: what it says, and what identifies it as seen."""

    # major.minor of the releases the text describes.
    release_line: str
    # ISO date the text was last rewritten; stored when the note is dismissed.
    updated: str
    text: str


WHATS_NEW: Final[WhatsNew] = WhatsNew(
    release_line="2.9",
    updated="2026-11-25",
    text=(
        "Fourier 2B is now available in Downloads as an optional Qwen3-ASR "
        "model on Windows and Linux. Send walkthroughs directly to your coding "
        "agent, browse models by family, and enjoy leak-free Linux/X11 "
        "shortcut refreshes."
    ),
)

# Longest text the Home tile shows without crowding the cards below it.
MAX_TEXT_LENGTH: Final[int] = 280


def is_seen(seen: object) -> bool:
    """Whether the stored dismissal is for the current note."""
    return seen == WHATS_NEW.updated

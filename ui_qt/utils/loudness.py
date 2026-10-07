"""Microphone level to how loud it sounds, for live level displays."""
from __future__ import annotations

import math
from typing import Final

#: Levels are RMS fractions of full scale. Speech on a desk microphone sits
#: around -40 to -15 dBFS, so this window gives it most of the range while
#: room noise stays near zero.
FLOOR_DB: Final[float] = -60.0
CEILING_DB: Final[float] = -12.0


def level_to_height(level: float) -> float:
    """Map an RMS level (a 0-1 fraction of full scale) to a 0-1 loudness.

    The scale is logarithmic, as loudness is heard, so quiet speech still
    moves a display and a shout does not flatten everything else.
    """
    if level <= 0.0:
        return 0.0
    db = 20.0 * math.log10(level)
    return max(0.0, min(1.0, (db - FLOOR_DB) / (CEILING_DB - FLOOR_DB)))

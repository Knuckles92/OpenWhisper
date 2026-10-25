"""Cleaning up and Rewriting: scribbled lines a caret combs straight.

Three lines of word-shaped strokes wobble out of place. A purple caret runs
along them; behind it each word settles onto its line at its full length,
evenly spaced, and turns from faint to ink. Once the caret is through the
lines loosen again and it starts over, for as long as the text takes.
"""
from __future__ import annotations

import math
import random
from typing import Final

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPen

from ui_qt.overlays.recording_looks import smooth
from ui_qt.utils.palette import token_color

#: One pass of the caret, then the lines hold, loosen, and it goes again.
PERIOD_S: Final[float] = 2.1
COMB_S: Final[float] = 1.35
LOOSEN_AT_S: Final[float] = 1.75
LOOSEN_S: Final[float] = 0.35
#: How long the lines take to appear.
APPEAR_S: Final[float] = 0.35
HALF_SPAN: Final[float] = 66.0
LINE_PITCH: Final[float] = 9.0
WORD_GAP: Final[float] = 5.0
STROKE: Final[float] = 3.0


def _scribble(seed: int = 7) -> tuple:
    """Per line, its words as ``(length, dy, dx, wobble phase)``, fitted to the span."""
    rng = random.Random(seed)
    lines = []
    for _ in range(3):
        words, x = [], 0.0
        while True:
            length = rng.uniform(8, 24)
            if x + length > 2 * HALF_SPAN:
                break
            words.append((length, rng.uniform(-3.5, 3.5), rng.uniform(-4, 4), rng.uniform(0, math.tau)))
            x += length + WORD_GAP
        lines.append(tuple(words))
    return tuple(lines)


LINES: Final[tuple] = _scribble()


def _mix(a: QColor, b: QColor, f: float) -> QColor:
    return QColor(
        int(a.red() + (b.red() - a.red()) * f),
        int(a.green() + (b.green() - a.green()) * f),
        int(a.blue() + (b.blue() - a.blue()) * f),
        int(a.alpha() + (b.alpha() - a.alpha()) * f),
    )


def draw(painter: QPainter, cx: float, cy: float, now: float) -> None:
    """The lines around ``(cx, cy)``, ``now`` seconds into the stage."""
    appear = smooth((now - 0.1) / APPEAR_S)
    if appear <= 0.0:
        return
    cyc = now % PERIOD_S
    left = cx - HALF_SPAN
    caret = left - 10 + (2 * HALF_SPAN + 20) * smooth(cyc / COMB_S)
    loosen = smooth((cyc - LOOSEN_AT_S) / LOOSEN_S)
    purple = token_color("purple", int(255 * appear))
    painter.setPen(Qt.PenStyle.NoPen)
    for row, words in enumerate(LINES):
        y = cy - LINE_PITCH + LINE_PITCH * row
        x = left
        for length, dy, dx, phase in words:
            combed = smooth((caret - (x + length)) / 14) * (1.0 - loosen)
            loose = 1.0 - combed
            ink = token_color("overlay-text", int((70 + 120 * combed) * appear))
            lit = max(0.0, 1.0 - abs(caret - (x + length / 2)) / 18) * (1.0 - loosen)
            painter.setBrush(_mix(ink, purple, lit))
            height = STROKE + (0.8 * loose if combed <= 0.5 else 0.0)
            painter.drawRoundedRect(
                QRectF(x + loose * dx, y + loose * (dy + 1.2 * math.sin(now * 5 + phase)) - height / 2,
                       length * (0.85 + 0.15 * combed), height),
                STROKE / 2, STROKE / 2,
            )
            x += length + WORD_GAP
    if cyc < COMB_S + 0.1:
        alpha = int(230 * appear * (1.0 - smooth((cyc - COMB_S + 0.05) / 0.15)))
        painter.setPen(QPen(token_color("purple", alpha), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(QPointF(caret, cy - 15), QPointF(caret, cy + 15))

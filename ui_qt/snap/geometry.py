"""Where things go on the snap overlay, and how logical pixels map to the image.

The overlay works in logical pixels, the units the user's mouse moves in;
the frozen screenshot is kept in device pixels, the units it was captured
in. These helpers are the only place the two meet.
"""

from __future__ import annotations

import math

from PyQt6.QtCore import QPoint, QRect, QSize

#: Space between the selection and the toolbar.
TOOLBAR_GAP = 10
#: Floating chrome never comes closer than this to a screen edge.
EDGE_MARGIN = 8
_EPSILON = 1e-6


def span(a: QPoint, b: QPoint) -> QRect:
    """The rectangle a drag from ``a`` to ``b`` covers: ``|dx|`` by ``|dy|`` pixels."""
    return QRect(
        min(a.x(), b.x()), min(a.y(), b.y()), abs(b.x() - a.x()), abs(b.y() - a.y())
    )


def device_rect(selection: QRect, dpr: float, image_rect: QRect) -> QRect:
    """``selection`` in the frozen image's device pixels, kept inside the image.

    Edges round outward, so a fractional ratio (1.25, 1.5) never shaves off a
    row of pixels the user saw inside the frame.
    """
    left = math.floor(selection.x() * dpr + _EPSILON)
    top = math.floor(selection.y() * dpr + _EPSILON)
    right = math.ceil((selection.x() + selection.width()) * dpr - _EPSILON)
    bottom = math.ceil((selection.y() + selection.height()) * dpr - _EPSILON)
    return QRect(left, top, right - left, bottom - top).intersected(image_rect)


def _clamp(value: int, low: int, high: int) -> int:
    """``value`` within ``[low, high]``; ``low`` wins when the range is empty."""
    return max(low, min(value, high))


def place_toolbar(
    size: QSize,
    selection: QRect,
    bounds: QRect,
    *,
    gap: int = TOOLBAR_GAP,
    margin: int = EDGE_MARGIN,
) -> QPoint:
    """Top-left of a ``size`` toolbar: centred under ``selection``, all on ``bounds``.

    It flips above the selection when there is no room below, and sits inside
    the selection's bottom edge when the selection fills the screen's height.
    """
    x = _clamp(
        selection.x() + (selection.width() - size.width()) // 2,
        bounds.left() + margin,
        bounds.right() + 1 - margin - size.width(),
    )
    floor = bounds.bottom() + 1 - margin - size.height()
    below = selection.bottom() + 1 + gap
    if below <= floor:
        return QPoint(x, below)
    above = selection.top() - gap - size.height()
    if above >= bounds.top() + margin:
        return QPoint(x, above)
    inside = selection.bottom() + 1 - gap - size.height()
    return QPoint(x, _clamp(inside, bounds.top() + margin, floor))


def badge_rect(size: QSize, selection: QRect, bounds: QRect, *, gap: int = 6) -> QRect:
    """The size readout: just above the selection's top-left, else just inside."""
    y = selection.top() - gap - size.height()
    if y < bounds.top() + 4:
        y = selection.top() + gap
    x = _clamp(
        selection.left(), bounds.left() + 4, bounds.right() + 1 - 4 - size.width()
    )
    return QRect(QPoint(x, y), size)

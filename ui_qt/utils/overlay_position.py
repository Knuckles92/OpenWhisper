"""Screen-edge positioning helpers for floating overlays."""

from __future__ import annotations

from typing import Tuple

from PyQt6.QtCore import QPoint, QRect


def clamp_rect_to_available(
    x: int,
    y: int,
    width: int,
    height: int,
    available: QRect,
    margin: int = 8,
) -> Tuple[int, int]:
    """Clamp a top-left position so the rect stays inside available geometry.

    Args:
        x: Preferred left edge.
        y: Preferred top edge.
        width: Overlay width in pixels.
        height: Overlay height in pixels.
        available: Screen available geometry (excludes taskbars).
        margin: Minimum inset from each screen edge.

    Returns:
        Clamped ``(x, y)`` top-left position.
    """
    if width <= 0 or height <= 0 or available.isNull() or available.width() <= 0:
        return x, y

    min_x = available.x() + margin
    min_y = available.y() + margin
    max_x = available.x() + available.width() - width - margin
    max_y = available.y() + available.height() - height - margin

    # If the overlay is larger than the usable area, pin to the top-left inset.
    if max_x < min_x:
        max_x = min_x
    if max_y < min_y:
        max_y = min_y

    return max(min_x, min(x, max_x)), max(min_y, min(y, max_y))


def preferred_overlay_position(
    anchor: QPoint,
    width: int,
    height: int,
    available: QRect,
    offset: int = 10,
    margin: int = 8,
) -> Tuple[int, int]:
    """Pick an on-screen position near an anchor, flipping when needed.

    Prefers below-right of the anchor. If that overflows the right or bottom
    edge, flips above and/or left, then clamps into available geometry.

    Args:
        anchor: Cursor or caret point in global coordinates.
        width: Overlay width in pixels.
        height: Overlay height in pixels.
        available: Screen available geometry for the anchor's display.
        offset: Gap between the anchor and the overlay.
        margin: Minimum inset from each screen edge.

    Returns:
        ``(x, y)`` top-left position for the overlay.
    """
    x = anchor.x() + offset
    y = anchor.y() + offset

    right_limit = available.x() + available.width() - margin
    bottom_limit = available.y() + available.height() - margin

    if x + width > right_limit:
        x = anchor.x() - width - offset
    if y + height > bottom_limit:
        y = anchor.y() - height - offset

    return clamp_rect_to_available(x, y, width, height, available, margin)


def max_height_for_anchor(
    anchor: QPoint,
    available: QRect,
    configured_max: int,
    offset: int = 10,
    margin: int = 8,
) -> int:
    """Return the tallest overlay height that can fit near an anchor.

    Considers both below-anchor and above-anchor placement so growth can flip
    to the side with more room.

    Args:
        anchor: Cursor or caret point in global coordinates.
        available: Screen available geometry for the anchor's display.
        configured_max: Soft maximum from app config.
        offset: Gap between the anchor and the overlay.
        margin: Minimum inset from each screen edge.

    Returns:
        Effective max height in pixels (at least 1).
    """
    if available.isNull() or available.height() <= 0:
        return max(1, configured_max)

    below = available.y() + available.height() - margin - (anchor.y() + offset)
    above = anchor.y() - offset - (available.y() + margin)
    room = max(below, above)
    return max(1, min(configured_max, room))

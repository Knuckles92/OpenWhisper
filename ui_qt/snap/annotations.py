"""What Snap draws: arrows, boxes, pen and highlighter strokes, labels, steps.

Annotations are small vector objects in logical pixels relative to the
selection's top-left corner. The overlay paints them on screen, and the
export paints the very same objects onto the device-pixel crop with the
painter scaled by the device-pixel ratio, so what the user drew is what they
keep, crisp at any scale.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from PyQt6.QtCore import QPointF, QRectF, QSizeF, Qt
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPolygonF,
)


class Tool(StrEnum):
    ARROW = "arrow"
    BOX = "box"
    PEN = "pen"
    HIGHLIGHTER = "highlighter"
    TEXT = "text"
    STEP = "step"


@dataclass(frozen=True)
class Swatch:
    name: str
    color: str


#: Saturated mid-tones that hold up on light and dark apps alike; coral first.
SWATCHES: tuple[Swatch, ...] = (
    Swatch("Coral", "#FF4F3A"),
    Swatch("Amber", "#F5A524"),
    Swatch("Blue", "#3A7BFD"),
    Swatch("Green", "#1FA463"),
)

#: Drags shorter than this are clicks, not shapes.
MIN_DRAG = 6.0
ARROW_WIDTH = 4.0
BOX_WIDTH = 3.0
PEN_WIDTH = 4.0
HIGHLIGHTER_WIDTH = 18.0
HIGHLIGHTER_ALPHA = 120
BOX_FILL_ALPHA = 24
BOX_RADIUS = 6.0
STEP_RADIUS = 13.0
STEP_RING = 2.0
LABEL_PAD_X = 8.0
LABEL_PAD_Y = 3.0
LABEL_RADIUS = 6.0
#: How far an arrow bows, as a share of its length, and the most it ever bows.
ARROW_BEND = 0.1
ARROW_MAX_BEND = 26.0
#: A soft drop shadow lifts marks off busy screenshots without an outline.
_SHADOW = QColor(0, 0, 0, 70)
_SHADOW_OFFSET = QPointF(0.0, 1.2)
_LIGHT_INK = QColor("#FFFFFF")
_DARK_INK = QColor("#2A1A06")


def ink_for(color: QColor) -> QColor:
    """Type that reads on a fill of ``color``: white unless the fill is light.

    White stays on coral even though dark type measures a little higher
    contrast there: it is how numbered badges look everywhere else.
    """

    def linear(channel: float) -> float:
        return (
            channel / 12.92
            if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
        )

    luminance = (
        0.2126 * linear(color.redF())
        + 0.7152 * linear(color.greenF())
        + 0.0722 * linear(color.blueF())
    )
    return QColor(_LIGHT_INK if luminance < 0.3 else _DARK_INK)


def grayscale(font: QFont) -> QFont:
    """``font`` without subpixel (ClearType) smoothing.

    Kept images get scaled and viewed on other screens, where subpixel
    smoothing shows as coloured fringes around every letter.
    """
    font = QFont(font)
    font.setStyleStrategy(
        QFont.StyleStrategy.PreferAntialias | QFont.StyleStrategy.NoSubpixelAntialias
    )
    return font


def _app_font() -> QFont:
    return grayscale(QGuiApplication.font())


def label_font(pixel_size: int = 16) -> QFont:
    font = _app_font()
    font.setPixelSize(pixel_size)
    font.setWeight(QFont.Weight.DemiBold)
    return font


def step_font(number: int) -> QFont:
    font = _app_font()
    font.setPixelSize(14 if number < 100 else 11)
    font.setWeight(QFont.Weight.Bold)
    return font


def _distance(a: QPointF, b: QPointF) -> float:
    return math.hypot(b.x() - a.x(), b.y() - a.y())


def _round_pen(color: QColor, width: float) -> QPen:
    return QPen(
        color,
        width,
        Qt.PenStyle.SolidLine,
        Qt.PenCapStyle.RoundCap,
        Qt.PenJoinStyle.RoundJoin,
    )


def _fill_with_shadow(painter: QPainter, path: QPainterPath, color: QColor) -> None:
    painter.fillPath(path.translated(_SHADOW_OFFSET), _SHADOW)
    painter.fillPath(path, color)


def _bulge(ux: float, uy: float) -> tuple[float, float]:
    """Which way an arrow along ``(ux, uy)`` bows: up, or left when vertical.

    Hand-drawn arrows arc the same way whichever way they point, so the
    curve never flips belly-down just because the arrow points left.
    """
    nx, ny = uy, -ux
    if ny > 0.2 or (abs(ny) <= 0.2 and nx > 0):
        return -nx, -ny
    return nx, ny


def arrow_shapes(
    start: QPointF, end: QPointF, width: float
) -> tuple[QPainterPath, QPolygonF]:
    """The shaft (a gently bowed quadratic curve) and the filled head of an arrow.

    The shaft stops inside the head, so its round cap never pokes past the tip.
    """
    dx, dy = end.x() - start.x(), end.y() - start.y()
    length = math.hypot(dx, dy)
    if length < 1e-6:
        return QPainterPath(start), QPolygonF()
    nx, ny = _bulge(dx / length, dy / length)
    bend = min(length * ARROW_BEND, ARROW_MAX_BEND)
    control = QPointF(
        (start.x() + end.x()) / 2 + nx * bend, (start.y() + end.y()) / 2 + ny * bend
    )
    tangent = end - control
    reach = math.hypot(tangent.x(), tangent.y()) or 1.0
    ux, uy = tangent.x() / reach, tangent.y() / reach
    head = min(4.0 + width * 3.2, length * 0.6)
    half = head * 0.62
    base = QPointF(end.x() - ux * head, end.y() - uy * head)
    notch = QPointF(end.x() - ux * head * 0.78, end.y() - uy * head * 0.78)
    left = QPointF(base.x() - uy * half, base.y() + ux * half)
    right = QPointF(base.x() + uy * half, base.y() - ux * half)
    shaft = QPainterPath(start)
    shaft.quadTo(control, QPointF(end.x() - ux * head * 0.6, end.y() - uy * head * 0.6))
    return shaft, QPolygonF([end, left, notch, right])


def arrow_outline(start: QPointF, end: QPointF, width: float) -> QPainterPath:
    """Shaft and head as one outline: no doubled shadow where they meet."""
    shaft, head = arrow_shapes(start, end, width)
    stroker = QPainterPathStroker()
    stroker.setWidth(width)
    stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
    stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    outline = stroker.createStroke(shaft)
    if head.isEmpty():
        return outline
    tip = QPainterPath()
    tip.addPolygon(head)
    tip.closeSubpath()
    return outline.united(tip)


def smooth_path(points: list[QPointF]) -> QPainterPath:
    """A freehand line through ``points``, smoothed by quadratics through midpoints."""
    path = QPainterPath(points[0])
    for current, following in zip(points[1:-1], points[2:], strict=True):
        path.quadTo(current, (current + following) / 2)
    if len(points) > 1:
        path.lineTo(points[-1])
    return path


@dataclass
class Arrow:
    start: QPointF
    end: QPointF
    color: QColor
    width: float = ARROW_WIDTH

    def extend(self, point: QPointF) -> None:
        self.end = QPointF(point)

    def meaningful(self) -> bool:
        return _distance(self.start, self.end) >= MIN_DRAG

    def bounds(self) -> QRectF:
        # From the parts, not the united outline: this runs on every drag step.
        shaft, head = arrow_shapes(self.start, self.end, self.width)
        grow = self.width / 2 + 3
        return (
            shaft.boundingRect()
            .united(head.boundingRect())
            .adjusted(-grow, -grow, grow, grow + 1)
        )

    def paint(self, painter: QPainter) -> None:
        if _distance(self.start, self.end) < 1.0:
            return
        _fill_with_shadow(
            painter, arrow_outline(self.start, self.end, self.width), self.color
        )


@dataclass
class Box:
    start: QPointF
    end: QPointF
    color: QColor
    width: float = BOX_WIDTH

    def rect(self) -> QRectF:
        return QRectF(self.start, self.end).normalized()

    def extend(self, point: QPointF) -> None:
        self.end = QPointF(point)

    def meaningful(self) -> bool:
        rect = self.rect()
        return (
            rect.width() >= MIN_DRAG / 2
            and rect.height() >= MIN_DRAG / 2
            and (_distance(self.start, self.end) >= MIN_DRAG)
        )

    def bounds(self) -> QRectF:
        grow = self.width + 3
        return self.rect().adjusted(-grow, -grow, grow, grow + 1)

    def paint(self, painter: QPainter) -> None:
        rect = self.rect()
        radius = min(BOX_RADIUS, rect.width() / 2, rect.height() / 2)
        fill = QColor(self.color)
        fill.setAlpha(BOX_FILL_ALPHA)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, radius, radius)
        outline = QPainterPath()
        outline.addRoundedRect(rect, radius, radius)
        painter.strokePath(
            outline.translated(_SHADOW_OFFSET), _round_pen(_SHADOW, self.width)
        )
        painter.strokePath(outline, _round_pen(self.color, self.width))


@dataclass
class Stroke:
    """A pen line, or with ``highlighter`` a wide translucent marker swipe."""

    points: list[QPointF]
    color: QColor
    highlighter: bool = False

    @property
    def width(self) -> float:
        return HIGHLIGHTER_WIDTH if self.highlighter else PEN_WIDTH

    def extend(self, point: QPointF) -> None:
        # Sub-pixel jitter only adds bumps to the smoothed curve.
        if _distance(self.points[-1], point) >= 1.5:
            self.points.append(QPointF(point))

    def meaningful(self) -> bool:
        return bool(self.points)

    def bounds(self) -> QRectF:
        # The smoothed curve never leaves the points' box, which is cheap to
        # find on every drag step of a long stroke.
        grow = self.width / 2 + 3
        box = QPolygonF(self.points).boundingRect()
        return box.adjusted(-grow, -grow, grow, grow + 1)

    def paint(self, painter: QPainter) -> None:
        path = smooth_path(self.points)
        if len(self.points) == 1:
            # A click with the pen leaves a dot, as a real pen would.
            path.lineTo(self.points[0] + QPointF(0.01, 0))
        if self.highlighter:
            color = QColor(self.color)
            color.setAlpha(HIGHLIGHTER_ALPHA)
            # One stroke call covers each pixel once, so crossings don't darken.
            painter.strokePath(path, _round_pen(color, self.width))
            return
        painter.strokePath(
            path.translated(_SHADOW_OFFSET), _round_pen(_SHADOW, self.width)
        )
        painter.strokePath(path, _round_pen(self.color, self.width))


@dataclass
class Label:
    """A short note on a rounded tag of the chosen colour, like the step badges."""

    pos: QPointF
    text: str
    color: QColor
    font: QFont = field(default_factory=label_font)

    def extend(self, point: QPointF) -> None:
        return None

    def meaningful(self) -> bool:
        return bool(self.text.strip())

    def rect(self) -> QRectF:
        metrics = QFontMetricsF(self.font)
        size = QSizeF(
            metrics.horizontalAdvance(self.text) + 2 * LABEL_PAD_X,
            metrics.height() + 2 * LABEL_PAD_Y,
        )
        return QRectF(self.pos, size)

    def bounds(self) -> QRectF:
        return self.rect().adjusted(-2, -2, 2, 3)

    def paint(self, painter: QPainter) -> None:
        rect = self.rect()
        tag = QPainterPath()
        tag.addRoundedRect(rect, LABEL_RADIUS, LABEL_RADIUS)
        _fill_with_shadow(painter, tag, self.color)
        painter.setFont(grayscale(self.font))
        painter.setPen(ink_for(self.color))
        painter.drawText(
            rect.adjusted(LABEL_PAD_X, 0, -LABEL_PAD_X / 2, 0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self.text,
        )


@dataclass
class Step:
    """A numbered badge (filled circle, white ring), for "click here, then here"."""

    center: QPointF
    number: int
    color: QColor

    def extend(self, point: QPointF) -> None:
        return None

    def meaningful(self) -> bool:
        return True

    def bounds(self) -> QRectF:
        reach = STEP_RADIUS + STEP_RING + 3
        return QRectF(
            self.center.x() - reach, self.center.y() - reach, 2 * reach, 2 * reach + 1
        )

    def paint(self, painter: QPainter) -> None:
        ring = STEP_RADIUS + STEP_RING
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_SHADOW)
        painter.drawEllipse(self.center + _SHADOW_OFFSET, ring, ring)
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawEllipse(self.center, ring, ring)
        painter.setBrush(self.color)
        painter.drawEllipse(self.center, STEP_RADIUS, STEP_RADIUS)
        painter.setFont(step_font(self.number))
        painter.setPen(ink_for(self.color))
        box = QRectF(
            self.center.x() - STEP_RADIUS,
            self.center.y() - STEP_RADIUS,
            2 * STEP_RADIUS,
            2 * STEP_RADIUS,
        )
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, str(self.number))


Annotation = Arrow | Box | Stroke | Label | Step


_DRAFTS: dict[Tool, Callable[[QPointF, QColor], Annotation]] = {
    Tool.ARROW: lambda point, color: Arrow(QPointF(point), QPointF(point), color),
    Tool.BOX: lambda point, color: Box(QPointF(point), QPointF(point), color),
    Tool.PEN: lambda point, color: Stroke([QPointF(point)], color),
    Tool.HIGHLIGHTER: lambda point, color: Stroke(
        [QPointF(point)], color, highlighter=True
    ),
}


def new_draft(tool: Tool, point: QPointF, color: QColor) -> Annotation | None:
    """The shape a ``tool`` drag starts at ``point``; None for text and steps."""
    make = _DRAFTS.get(tool)
    return make(point, QColor(color)) if make is not None else None


class AnnotationStack:
    """The marks on one snap, in drawing order, with undo and redo."""

    def __init__(self) -> None:
        self._items: list[Annotation] = []
        self._undone: list[Annotation] = []

    @property
    def items(self) -> tuple[Annotation, ...]:
        return tuple(self._items)

    def __len__(self) -> int:
        return len(self._items)

    @property
    def can_undo(self) -> bool:
        return bool(self._items)

    @property
    def can_redo(self) -> bool:
        return bool(self._undone)

    def add(self, item: Annotation) -> None:
        self._items.append(item)
        self._undone.clear()

    def undo(self) -> Annotation | None:
        if not self._items:
            return None
        item = self._items.pop()
        self._undone.append(item)
        return item

    def redo(self) -> Annotation | None:
        if not self._undone:
            return None
        item = self._undone.pop()
        self._items.append(item)
        return item

    def next_step_number(self) -> int:
        """Steps count up from 1; undoing a step gives its number back."""
        return 1 + sum(isinstance(item, Step) for item in self._items)

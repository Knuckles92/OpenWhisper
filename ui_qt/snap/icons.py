"""The toolbar's glyphs, drawn as vector paths.

Drawn rather than loaded so they stay sharp at every scale on every screen,
and so the arrow and step icons are tiny versions of the marks they make.
Each glyph is designed on a 20 x 20 grid and scaled into its button.
"""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen

from ui_qt.snap.annotations import arrow_outline, smooth_path, step_font

GRID = 20.0
_LINE = 1.7


def _line_pen(color: QColor, width: float = _LINE) -> QPen:
    return QPen(
        color,
        width,
        Qt.PenStyle.SolidLine,
        Qt.PenCapStyle.RoundCap,
        Qt.PenJoinStyle.RoundJoin,
    )


def _path(*points: tuple[float, float]) -> QPainterPath:
    path = QPainterPath(QPointF(*points[0]))
    for point in points[1:]:
        path.lineTo(QPointF(*point))
    return path


def _arrow(painter: QPainter, color: QColor) -> None:
    painter.fillPath(arrow_outline(QPointF(3.5, 16.5), QPointF(16.5, 3.5), 1.8), color)


def _box(painter: QPainter, color: QColor) -> None:
    painter.setPen(_line_pen(color))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(QRectF(3.2, 4.6, 13.6, 10.8), 2.6, 2.6)


def _pen(painter: QPainter, color: QColor) -> None:
    points = [
        QPointF(x, y)
        for x, y in ((3, 13), (5.5, 7.5), (8.5, 12.5), (11.5, 6.5), (14, 12), (17, 6.5))
    ]
    painter.strokePath(smooth_path(points), _line_pen(color, 1.9))


def _highlighter(painter: QPainter, color: QColor) -> None:
    # A marker (wide barrel, narrow chisel tip) over the swipe it leaves.
    swipe = QColor(color)
    swipe.setAlphaF(0.45)
    painter.strokePath(_path((3.4, 16.0), (16.6, 16.0)), _line_pen(swipe, 4.4))
    painter.strokePath(_path((9.4, 9.6), (15.2, 3.8)), _line_pen(color, 5.0))
    painter.strokePath(_path((6.6, 12.6), (9.4, 9.6)), _line_pen(color, 2.4))


def _text(painter: QPainter, color: QColor) -> None:
    pen = _line_pen(color, 2.0)
    painter.strokePath(_path((4.5, 4.5), (15.5, 4.5)), pen)
    painter.strokePath(_path((10.0, 4.5), (10.0, 16.0)), pen)
    painter.strokePath(_path((7.5, 16.0), (12.5, 16.0)), _line_pen(color, 1.6))


def _step(painter: QPainter, color: QColor) -> None:
    painter.setPen(_line_pen(color))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(QPointF(10, 10), 7.2, 7.2)
    font = QFont(step_font(1))
    font.setPixelSize(10)
    painter.setFont(font)
    painter.setPen(color)
    painter.drawText(QRectF(2.8, 2.6, 14.4, 14.6), Qt.AlignmentFlag.AlignCenter, "1")


def _undo_path(mirror: bool) -> tuple[QPainterPath, QPainterPath]:
    def x(value: float) -> float:
        return GRID - value if mirror else value

    curve = QPainterPath(QPointF(x(15.5), 15.5))
    curve.cubicTo(QPointF(x(16.5), 9.0), QPointF(x(12.0), 6.8), QPointF(x(5.0), 7.2))
    head = _path((x(8.4), 3.8), (x(5.0), 7.2), (x(8.4), 10.6))
    return curve, head


def _undo(painter: QPainter, color: QColor) -> None:
    for part in _undo_path(mirror=False):
        painter.strokePath(part, _line_pen(color))


def _redo(painter: QPainter, color: QColor) -> None:
    for part in _undo_path(mirror=True):
        painter.strokePath(part, _line_pen(color))


def _check(painter: QPainter, color: QColor) -> None:
    painter.strokePath(
        _path((4.6, 10.4), (8.4, 14.2), (15.6, 6.0)), _line_pen(color, 2.2)
    )


def _cross(painter: QPainter, color: QColor) -> None:
    pen = _line_pen(color, 1.9)
    painter.strokePath(_path((5.6, 5.6), (14.4, 14.4)), pen)
    painter.strokePath(_path((14.4, 5.6), (5.6, 14.4)), pen)


_GLYPHS: dict[str, Callable[[QPainter, QColor], None]] = {
    "arrow": _arrow,
    "box": _box,
    "pen": _pen,
    "highlighter": _highlighter,
    "text": _text,
    "step": _step,
    "undo": _undo,
    "redo": _redo,
    "keep": _check,
    "drop": _cross,
}


def paint_icon(painter: QPainter, name: str, rect: QRectF, color: QColor) -> None:
    """Draw glyph ``name`` scaled into ``rect`` in ``color``."""
    glyph = _GLYPHS[name]
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(rect.topLeft())
        painter.scale(rect.width() / GRID, rect.height() / GRID)
        glyph(painter, color)
    finally:
        painter.restore()

"""The walkthrough column's glyphs, drawn as vector paths on a 24-unit grid.

Drawn rather than loaded so they stay sharp at every scale, and take any
colour the column paints them in.
"""

from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen


def _icon_path(name: str) -> QPainterPath:
    path = QPainterPath()

    def line(*points: tuple[float, float]) -> None:
        path.moveTo(*points[0])
        for point in points[1:]:
            path.lineTo(*point)

    if name == "camera":
        line((4, 8), (7, 8), (9, 5), (15, 5), (17, 8), (20, 8), (20, 19), (4, 19), (4, 8))
        path.addEllipse(QPointF(12, 13), 3.5, 3.5)
    elif name == "up":
        line((6, 15), (12, 9), (18, 15))
    elif name == "down":
        line((6, 9), (12, 15), (18, 9))
    elif name == "x":
        line((6, 6), (18, 18))
        line((18, 6), (6, 18))
    elif name == "undo":
        line((9, 14), (4, 9), (9, 4))
        path.moveTo(4, 9)
        path.lineTo(14, 9)
        path.arcTo(QRectF(8, 9, 12, 12), 90, -180)
        path.lineTo(11, 21)
    elif name == "check":
        line((5, 12.5), (9.5, 17), (19, 7.5))
    elif name == "note":
        line((5, 4), (19, 4), (19, 16), (9, 16), (5, 20), (5, 4))
    return path


#: Stroke width of each icon, in grid units.
_STROKE = {"camera": 2.0, "up": 2.4, "down": 2.4, "x": 2.4, "undo": 2.0, "check": 2.8,
           "note": 2.2}


def paint_icon(painter: QPainter, name: str, center: QPointF, size: float, color: QColor) -> None:
    """Draw icon ``name`` ``size`` pixels square around ``center``."""
    painter.save()
    painter.translate(center.x() - size / 2, center.y() - size / 2)
    painter.scale(size / 24, size / 24)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(color, _STROKE[name], Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                        Qt.PenJoinStyle.RoundJoin))
    painter.drawPath(_icon_path(name))
    painter.restore()

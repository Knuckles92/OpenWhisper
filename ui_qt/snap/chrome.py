"""The overlay's own furniture: the hint pill, the size readout, the selection frame.

None of it is part of the kept image. Colours come from the app palette's
floating-surface roles (the ones the recording overlay uses), so the pill
and toolbar look like OpenWhisper in both themes.
"""

from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import QPointF, QRect, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen

from ui_qt.utils.font_scale import current_ui_font_scale
from ui_qt.utils.palette import Palette

#: The frozen screen outside the selection: a warm smoked glass, not a grey wash.
DIM = QColor(18, 12, 9, 150)
_FRAME = QColor(255, 255, 255, 235)
_FRAME_SHADOW = QColor(0, 0, 0, 90)
_CORNER = 11.0
_KEY_HINT = (
    ("key", "Enter"),
    ("text", "to keep"),
    ("dot", "·"),
    ("key", "Esc"),
    ("text", "to cancel"),
)


def chrome_font(
    base: QFont, pixel_size: float, weight: QFont.Weight = QFont.Weight.Normal
) -> QFont:
    """``base`` at ``pixel_size`` scaled by the user's UI text size."""
    font = QFont(base)
    font.setPixelSize(max(8, round(pixel_size * current_ui_font_scale())))
    font.setWeight(weight)
    return font


def paint_shadow(
    painter: QPainter, rect: QRectF, radius: float, *, strength: float = 1.0
) -> None:
    """A soft drop shadow under a floating panel, from a few widening rounded rects."""
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    for step, alpha in enumerate((34, 22, 14, 8, 4), start=1):
        painter.setBrush(QColor(0, 0, 0, round(alpha * strength)))
        grown = rect.adjusted(-step, -step + 2.5, step, step + 2.5)
        painter.drawRoundedRect(grown, radius + step, radius + step)
    painter.restore()


def paint_panel(
    painter: QPainter,
    rect: QRectF,
    radius: float,
    palette: Palette,
    *,
    shadow: float = 1.0,
) -> None:
    """A floating surface in the app's overlay colours, over a soft shadow.

    Opaque, unlike the recording overlay's glass: under it is a screenshot,
    and text showing through a toolbar reads as clutter. ``shadow`` 0 leaves
    the shadow to whoever paints beneath the panel.
    """
    if shadow:
        paint_shadow(painter, rect, radius, strength=shadow)
    fill = palette.color("overlay-bg")
    fill.setAlpha(255)
    painter.setPen(QPen(palette.color("overlay-border"), 1))
    painter.setBrush(fill)
    painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)


def paint_frame(painter: QPainter, selection: QRect) -> None:
    """A crisp white frame with viewfinder corners; reads on light and dark."""
    rect = QRectF(selection).adjusted(-0.5, -0.5, 0.5, 0.5)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(_FRAME_SHADOW, 1))
    painter.drawRect(QRectF(selection).adjusted(-1.5, -1.5, 1.5, 1.5))
    painter.setPen(QPen(_FRAME, 1))
    painter.drawRect(rect)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    corner = min(_CORNER, rect.width() / 2, rect.height() / 2)
    pen = QPen(
        _FRAME,
        3.0,
        Qt.PenStyle.SolidLine,
        Qt.PenCapStyle.RoundCap,
        Qt.PenJoinStyle.RoundJoin,
    )
    painter.setPen(pen)
    outer = rect.adjusted(-1.5, -1.5, 1.5, 1.5)
    for point, dx, dy in (
        (outer.topLeft(), 1, 1),
        (outer.topRight(), -1, 1),
        (outer.bottomLeft(), 1, -1),
        (outer.bottomRight(), -1, -1),
    ):
        painter.drawPolyline(
            [point + QPointF(0, dy * corner), point, point + QPointF(dx * corner, 0)]
        )
    painter.restore()


def badge_size(text: str, font: QFont) -> QSize:
    metrics = QFontMetrics(font)
    return QSize(metrics.horizontalAdvance(text) + 18, metrics.height() + 8)


def paint_badge(
    painter: QPainter, rect: QRect, text: str, font: QFont, palette: Palette
) -> None:
    """The ``1280 × 720`` readout beside a selection being dragged."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    paint_panel(painter, QRectF(rect), 7, palette, shadow=0.6)
    painter.setFont(font)
    painter.setPen(palette.color("overlay-text"))
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
    painter.restore()


@dataclass(frozen=True)
class _Piece:
    kind: str
    text: str
    width: int


class HintPill:
    """The slim pill at the top of the screen: the caller's hint, then the two keys.

    Keys are drawn as small keycaps, so "Enter" and "Esc" read as keys at a
    glance rather than as words in a sentence.
    """

    PAD_X = 14
    GAP = 7
    KEY_PAD = 6

    def __init__(
        self, hint: str, base_font: QFont, palette: Palette, max_width: int = 0
    ) -> None:
        self._palette = palette
        self._font = chrome_font(base_font, 12.5)
        self._key_font = chrome_font(base_font, 11.5, QFont.Weight.DemiBold)
        keys = [self._piece(kind, text) for kind, text in _KEY_HINT]
        self._pieces = self._hint_pieces(hint.strip(), keys, max_width) + keys
        self._height = (
            max(
                QFontMetrics(self._font).height(),
                QFontMetrics(self._key_font).height() + 4,
            )
            + 12
        )

    def _piece(self, kind: str, text: str) -> _Piece:
        if kind == "key":
            width = (
                QFontMetrics(self._key_font).horizontalAdvance(text) + 2 * self.KEY_PAD
            )
        else:
            width = QFontMetrics(self._font).horizontalAdvance(text)
        return _Piece(kind, text, width)

    def _hint_pieces(
        self, hint: str, keys: list[_Piece], max_width: int
    ) -> list[_Piece]:
        """The caller's hint and a dot, cut short so the keys fit ``max_width``."""
        if not hint:
            return []
        dot = self._piece("dot", "·")
        used = (
            2 * self.PAD_X
            + dot.width
            + sum(key.width + self.GAP for key in keys)
            + self.GAP
        )
        room = max_width - used if max_width > 0 else 1 << 20
        if room < 40:
            return []
        text = QFontMetrics(self._font).elidedText(
            hint, Qt.TextElideMode.ElideRight, room
        )
        return [self._piece("text", text), dot]

    def size(self) -> QSize:
        width = sum(piece.width for piece in self._pieces) + self.GAP * (
            len(self._pieces) - 1
        )
        return QSize(width + 2 * self.PAD_X, self._height)

    def paint(self, painter: QPainter, rect: QRect) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        panel = QRectF(rect)
        paint_panel(painter, panel, panel.height() / 2, self._palette, shadow=0.8)
        x = rect.left() + self.PAD_X
        for piece in self._pieces:
            self._paint_piece(
                painter, piece, QRect(x, rect.top(), piece.width, rect.height())
            )
            x += piece.width + self.GAP
        painter.restore()

    def _paint_piece(self, painter: QPainter, piece: _Piece, cell: QRect) -> None:
        text_color = self._palette.color("overlay-text")
        if piece.kind == "key":
            self._paint_key(painter, piece.text, cell, text_color)
            return
        if piece.kind == "dot":
            text_color = self._palette.color("text-secondary")
        painter.setFont(self._font)
        painter.setPen(text_color)
        painter.drawText(cell, Qt.AlignmentFlag.AlignCenter, piece.text)

    def _paint_key(
        self, painter: QPainter, text: str, cell: QRect, color: QColor
    ) -> None:
        height = QFontMetrics(self._key_font).height() + 4
        cap = QRectF(
            cell.left(), cell.center().y() - height / 2 + 0.5, cell.width(), height
        )
        wash = self._palette.color("overlay-rgb", 26)
        painter.setPen(QPen(self._palette.color("overlay-border"), 1))
        painter.setBrush(wash)
        painter.drawRoundedRect(cap.adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)
        painter.setFont(self._key_font)
        painter.setPen(color)
        painter.drawText(cap, Qt.AlignmentFlag.AlignCenter, text)

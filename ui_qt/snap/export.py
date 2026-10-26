"""Turning the frozen screen, a selection and its annotations into the kept image."""

from __future__ import annotations

from collections.abc import Sequence

from PyQt6.QtCore import QPoint, QRect, QRectF
from PyQt6.QtGui import QImage, QPainter

from ui_qt.snap.annotations import Annotation
from ui_qt.snap.geometry import device_rect


def logical_rect(image: QImage) -> QRect:
    """The whole image in logical pixels: what "the whole screen" selects."""
    return QRect(QPoint(0, 0), image.deviceIndependentSize().toSize())


def render(
    frozen: QImage, selection: QRect, items: Sequence[Annotation] = ()
) -> QImage:
    """``selection`` of ``frozen`` with ``items`` drawn on, in device pixels.

    The crop is taken straight from the captured pixels and the annotations
    are drawn with the painter scaled by the device-pixel ratio, so a 2x
    screen keeps 2x detail and strokes stay crisp rather than upscaled. The
    result carries the screen's ratio: ``size()`` is device pixels and
    ``deviceIndependentSize()`` the size it had on screen.
    """
    dpr = frozen.devicePixelRatio() or 1.0
    crop = device_rect(selection, dpr, frozen.rect())
    image = frozen.copy(crop)
    image.setDevicePixelRatio(1.0)
    if items:
        _paint_items(image, crop, selection, dpr, items)
    image.setDevicePixelRatio(dpr)
    return image


def _paint_items(
    image: QImage,
    crop: QRect,
    selection: QRect,
    dpr: float,
    items: Sequence[Annotation],
) -> None:
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        # Device pixels of the whole screen, then logical pixels: the same
        # space the overlay drew the marks in.
        painter.translate(-crop.x(), -crop.y())
        painter.scale(dpr, dpr)
        painter.setClipRect(QRectF(selection))
        for item in items:
            item.paint(painter)
    finally:
        painter.end()

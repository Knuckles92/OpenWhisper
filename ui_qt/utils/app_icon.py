"""Application icon rendering.

Provides one visual identity for the window, the system tray, and the
installer/exe icon. The artwork is drawn in code so it stays crisp at every
size and matches the microphone glyph on the loading screen; a bundled
``.ico`` is preferred when present so the running app looks identical to the
icon Windows shows in Explorer and the taskbar.
"""

import logging
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap

from config import bundle_root

logger = logging.getLogger(__name__)

# Sizes Windows asks for across Explorer, the taskbar, alt-tab, and tooltips.
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)

# Theme accent gradient (ui_qt/styles/theme.qss).
_GRADIENT_START = QColor("#0a84ff")
_GRADIENT_END = QColor("#64d2ff")

_cached_icon: Optional[QIcon] = None


def icon_file_path() -> Path:
    """Path to the bundled multi-resolution .ico."""
    return Path(bundle_root()) / "ui_qt" / "assets" / "openwhisper.ico"


def render_app_pixmap(size: int) -> QPixmap:
    """Draw the OpenWhisper mark at ``size`` x ``size`` pixels.

    Every coordinate is expressed as a fraction of ``size`` so the glyph keeps
    its proportions from a 16 px tray icon up to a 256 px Explorer tile.

    Args:
        size: Edge length in pixels.

    Returns:
        A transparent-background pixmap containing the mark.
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    # Rounded-square background with the theme accent gradient.
    gradient = QLinearGradient(0, 0, size, size)
    gradient.setColorAt(0, _GRADIENT_START)
    gradient.setColorAt(1, _GRADIENT_END)

    background = QPainterPath()
    background.addRoundedRect(QRectF(0, 0, size, size), size * 0.22, size * 0.22)
    painter.fillPath(background, gradient)

    if size < 32:
        _draw_mic_simplified(painter, size)
    else:
        _draw_mic_detailed(painter, size)

    painter.end()
    return pixmap


def _draw_mic_detailed(painter: QPainter, size: int) -> None:
    """Outlined microphone with cradle, stem, and base.

    Matches the glyph painted on the loading screen. Needs roughly 32 px to
    stay legible.
    """
    pen = QPen(QColor(255, 255, 255), size * 0.055)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    capsule = QRectF(size * 0.37, size * 0.20, size * 0.26, size * 0.38)
    painter.drawRoundedRect(capsule, size * 0.13, size * 0.13)

    # Cradle: the lower half of an ellipse hugging the capsule.
    cradle = QRectF(size * 0.27, size * 0.30, size * 0.46, size * 0.42)
    painter.drawArc(cradle, 180 * 16, 180 * 16)

    # Stem and base.
    painter.drawLine(
        int(size * 0.50), int(size * 0.72), int(size * 0.50), int(size * 0.81)
    )
    painter.drawLine(
        int(size * 0.37), int(size * 0.81), int(size * 0.63), int(size * 0.81)
    )


def _draw_mic_simplified(painter: QPainter, size: int) -> None:
    """Solid microphone for 16-24 px tray and taskbar renderings.

    At these sizes an outlined capsule closes up into a white blob and the
    stem and base disappear into single grey pixels, so the capsule is filled
    instead of stroked, enlarged, and the base dropped. Only the cradle is
    kept, since that silhouette is what makes the shape read as a microphone.
    """
    white = QColor(255, 255, 255)

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(white)
    capsule = QRectF(size * 0.36, size * 0.16, size * 0.28, size * 0.40)
    painter.drawRoundedRect(capsule, size * 0.14, size * 0.14)

    pen = QPen(white, max(1.5, size * 0.10))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    cradle = QRectF(size * 0.22, size * 0.28, size * 0.56, size * 0.48)
    painter.drawArc(cradle, 180 * 16, 180 * 16)


def app_icon() -> QIcon:
    """Return the application icon, cached for the process lifetime.

    Prefers the bundled ``.ico`` so the in-app icon matches what Windows shows
    for the executable; falls back to rendering the mark at every size in
    ``ICON_SIZES`` when the file is missing.
    """
    global _cached_icon
    if _cached_icon is not None:
        return _cached_icon

    ico_path = icon_file_path()
    if ico_path.exists():
        icon = QIcon(str(ico_path))
        if not icon.isNull():
            _cached_icon = icon
            return icon
        logger.warning(f"Icon file at {ico_path} could not be loaded; drawing fallback")

    icon = QIcon()
    for size in ICON_SIZES:
        icon.addPixmap(render_app_pixmap(size))
    _cached_icon = icon
    return icon

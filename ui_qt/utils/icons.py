"""Load the bundled Tabler interface icons (``ui_qt/assets/tabler``).

Icons are cached by file name: rendering the SVG costs ~0.2 ms, and Settings
and its search palette ask for the same few dozen repeatedly. A ``QIcon`` is
a shared value, so callers must not add pixmaps to one they were given.
"""
import functools
from pathlib import Path

from PyQt6.QtCore import QSize
from PyQt6.QtGui import QIcon, QPixmap

from config import bundle_root


def tabler_path(filename: str) -> Path:
    """Path to one bundled Tabler icon file."""
    return Path(bundle_root()) / "ui_qt" / "assets" / "tabler" / filename


@functools.cache
def tabler_icon(filename: str) -> QIcon:
    """Load a bundled Tabler icon."""
    return QIcon(str(tabler_path(filename)))


def tabler_pixmap(filename: str, size: int) -> QPixmap:
    """Render a bundled Tabler icon at ``size`` x ``size``."""
    return tabler_icon(filename).pixmap(QSize(size, size))


@functools.cache
def design_icon(filename: str) -> QIcon:
    """A Tabler icon that keeps its colour on a disabled button.

    Qt greys out disabled icons, which would erase the semantic colour of a
    "current choice" button that is disabled because it is already chosen.
    """
    icon = QIcon(tabler_icon(filename))
    icon.addPixmap(icon.pixmap(24, 24), QIcon.Mode.Disabled, QIcon.State.Off)
    return icon

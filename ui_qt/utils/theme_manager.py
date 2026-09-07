import logging
import re
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication

from config import bundle_root
from ui_qt.utils.palette import (
    DARK,
    Palette,
    palette_for,
    set_current_palette,
)

logger = logging.getLogger(__name__)

# Matches an unquoted, relative url() in the stylesheet, e.g.
# ``image: url(ui_qt/assets/check.svg);``
_RELATIVE_URL_RE = re.compile(r"url\(\s*(?!['\"]?[a-zA-Z]:)(?!['\"]?[:/])([^)]+?)\s*\)")


class ThemeManager(QObject):
    """Owns the stylesheet template and the theme it is resolved against.

    ``theme.qss`` is written once with ``@token`` colours; :meth:`set_theme`
    swaps the palette those resolve to. Applying the result to the application
    is the job of ``ui_qt.utils.font_scale.apply_ui_font_scale``, which also
    re-resolves every widget-local stylesheet, so a theme change goes through
    ``ui_qt.app.QtApplication.set_theme`` rather than this class alone.
    """

    theme_changed = pyqtSignal(str)

    def __init__(self, theme: str = DARK):
        super().__init__()
        self._template = ""
        self._palette: Palette = palette_for(theme)
        set_current_palette(self._palette)
        self._load_stylesheet()

    @property
    def current_theme(self) -> str:
        return self._palette.name

    @property
    def palette(self) -> Palette:
        return self._palette

    def _load_stylesheet(self) -> Optional[str]:
        """Load, rewrite, and cache the stylesheet template.

        Returns:
            The template text, or None when it could not be loaded.
        """
        theme_path = Path(bundle_root()) / "ui_qt" / "styles" / "theme.qss"
        try:
            if not theme_path.exists():
                logger.error(f"Stylesheet not found at {theme_path}; UI will be unstyled")
                return None

            self._template = _absolutize_urls(
                theme_path.read_text(encoding="utf-8"), Path(bundle_root())
            )
            return self._template
        except Exception as e:
            logger.error(f"Error loading stylesheet from {theme_path}: {e}")

        return None

    @property
    def stylesheet(self) -> str:
        """The stylesheet resolved against the current theme."""
        return self._palette.resolve(self._template)

    def scaled_stylesheet(self, scale: float) -> str:
        """The resolved theme with every ``font-size`` multiplied by ``scale``."""
        from ui_qt.utils.font_scale import scale_qss_fonts

        return scale_qss_fonts(self.stylesheet, scale)

    def set_theme(self, theme_name: str) -> bool:
        """Switch the palette. Returns True when the theme actually changed."""
        palette = palette_for(theme_name)
        if palette.name == self._palette.name:
            return False
        self._palette = palette
        set_current_palette(palette)
        self.theme_changed.emit(palette.name)
        return True

    def apply_platform_palette(
        self, app: Optional[QApplication] = None, *, pin_color_scheme: bool = True
    ) -> None:
        """Push the theme into ``QPalette`` and, optionally, the platform colour scheme.

        The stylesheet covers the widgets it names; ``QPalette`` is what the
        rest read (message boxes, calendar popups, item views' selection).
        The colour-scheme hint is what Windows uses for the title bar; it is
        left alone when the caller is following the system scheme, because
        setting it overrides the value that scheme is read from.
        """
        instance = app or QApplication.instance()
        if instance is None:
            return
        instance.setPalette(build_qpalette(self._palette))
        if not pin_color_scheme:
            return
        scheme = Qt.ColorScheme.Dark if self._palette.is_dark else Qt.ColorScheme.Light
        try:
            instance.styleHints().setColorScheme(scheme)
        except AttributeError:  # Qt < 6.8
            pass


def build_qpalette(palette: Palette) -> QPalette:
    qpalette = QPalette()
    roles = {
        QPalette.ColorRole.Window: "bg",
        QPalette.ColorRole.WindowText: "text",
        QPalette.ColorRole.Base: "surface",
        QPalette.ColorRole.AlternateBase: "bg",
        QPalette.ColorRole.Text: "text",
        QPalette.ColorRole.Button: "surface",
        QPalette.ColorRole.ButtonText: "text",
        QPalette.ColorRole.ToolTipBase: "surface-hover",
        QPalette.ColorRole.ToolTipText: "text",
        QPalette.ColorRole.PlaceholderText: "text-secondary",
        QPalette.ColorRole.Highlight: "accent",
        QPalette.ColorRole.HighlightedText: "on-accent",
        QPalette.ColorRole.Link: "accent",
        QPalette.ColorRole.LinkVisited: "accent-soft",
        QPalette.ColorRole.Mid: "border",
        QPalette.ColorRole.Dark: "border-strong",
        QPalette.ColorRole.Light: "surface-hover",
        QPalette.ColorRole.Midlight: "surface-hover",
        QPalette.ColorRole.Shadow: "shadow-rgb",
    }
    for role, token in roles.items():
        qpalette.setColor(role, palette.color(token))
    disabled = palette.color("text-muted")
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        qpalette.setColor(QPalette.ColorGroup.Disabled, role, QColor(disabled))
    return qpalette


def _absolutize_urls(stylesheet: str, root: Path) -> str:
    """Rewrite relative ``url(...)`` references to absolute paths.

    Qt resolves a relative stylesheet URL against the process working
    directory, not the location of the .qss file. That happens to work when
    the app is launched from the repository root, but breaks for an installed
    build started from a Start Menu shortcut. Rewriting the paths up front
    makes asset loading independent of the working directory.

    Args:
        stylesheet: Raw stylesheet text.
        root: Directory the relative URLs are written against.

    Returns:
        The stylesheet with every relative url() made absolute.
    """
    def _replace(match: re.Match) -> str:
        # Qt accepts forward slashes on every platform; backslashes in a
        # stylesheet url() are treated as escapes.
        resolved = (root / match.group(1).strip("'\"")).resolve().as_posix()
        return f"url({resolved})"

    return _RELATIVE_URL_RE.sub(_replace, stylesheet)

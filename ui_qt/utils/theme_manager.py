import logging
import re
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject

from config import bundle_root

logger = logging.getLogger(__name__)

# Matches an unquoted, relative url() in the stylesheet, e.g.
# ``image: url(ui_qt/assets/check.svg);``
_RELATIVE_URL_RE = re.compile(r"url\(\s*(?!['\"]?[a-zA-Z]:)(?!['\"]?[:/])([^)]+?)\s*\)")


class ThemeManager(QObject):
    def __init__(self):
        super().__init__()
        self.current_theme = "dark"
        self._stylesheet = ""
        self._load_stylesheet()

    def _load_stylesheet(self) -> Optional[str]:
        """Load, rewrite, and cache the stylesheet.

        Returns:
            The stylesheet text, or None when it could not be loaded.
        """
        theme_path = Path(bundle_root()) / "ui_qt" / "styles" / "theme.qss"
        try:
            if not theme_path.exists():
                logger.error(f"Stylesheet not found at {theme_path}; UI will be unstyled")
                return None

            self._stylesheet = _absolutize_urls(
                theme_path.read_text(encoding="utf-8"), Path(bundle_root())
            )
            return self._stylesheet
        except Exception as e:
            logger.error(f"Error loading stylesheet from {theme_path}: {e}")

        return None

    @property
    def stylesheet(self) -> str:
        return self._stylesheet

    def scaled_stylesheet(self, scale: float) -> str:
        """The cached theme with every ``font-size`` multiplied by ``scale``."""
        from ui_qt.utils.font_scale import scale_qss_fonts

        return scale_qss_fonts(self._stylesheet, scale)

    def set_theme(self, theme_name: str):
        self.current_theme = theme_name

    def get_color(self, color_name: str) -> str:
        colors = {
            'primary': '#0a84ff',
            'primary_hover': '#007aff',
            'secondary': '#8e8e93',
            'danger': '#ff453a',
            'success': '#30d158',
            'accent': '#64d2ff',
            'background': '#1c1c1e',
            'surface': '#2c2c2e',
            'border': '#3a3a3c',
            'text': '#f5f5f7',
            'text_secondary': '#8e8e93',
        }
        return colors.get(color_name, '#ffffff')


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

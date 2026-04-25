"""
Theme management for PyQt6 UI.
Handles stylesheet loading and theme switching.
"""
from pathlib import Path
import logging
from typing import Optional
from PyQt6.QtCore import QObject, pyqtSignal


class ThemeManager(QObject):
    """Manages application theme and stylesheet."""

    theme_changed = pyqtSignal(str)  # Emitted when theme changes

    def __init__(self):
        """Initialize theme manager."""
        super().__init__()
        self.current_theme = "dark"
        self._load_stylesheet()

    def _load_stylesheet(self) -> Optional[str]:
        """Load and cache the stylesheet."""
        try:
            theme_path = Path(__file__).parent.parent / "styles" / "theme.qss"
            if theme_path.exists():
                with open(theme_path, 'r') as f:
                    self._stylesheet = f.read()
                    return self._stylesheet
        except Exception as e:
            logging.warning(f"Error loading stylesheet: {e}")

        return None

    @property
    def stylesheet(self) -> str:
        """Get the current stylesheet."""
        return getattr(self, '_stylesheet', '')

    def set_theme(self, theme_name: str):
        """Set the application theme."""
        self.current_theme = theme_name
        self.theme_changed.emit(theme_name)

    def get_color(self, color_name: str) -> str:
        """Get a color value from the theme."""
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

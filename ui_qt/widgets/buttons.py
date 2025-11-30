"""
Modern button components for PyQt6 UI.
"""
from PyQt6.QtWidgets import QPushButton
from PyQt6.QtCore import Qt, QPropertyAnimation, QEasingCurve, pyqtSignal
from PyQt6.QtGui import QFont


class ModernButton(QPushButton):
    """Modern button with smooth hover and click animations."""

    clicked_smooth = pyqtSignal()

    def __init__(self, text: str = "", parent=None):
        """Initialize modern button."""
        super().__init__(text, parent)
        self.setMinimumHeight(44) # Increased height
        self.setFont(QFont("Segoe UI", 12)) # Increased font size
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # Remove default focus outline
        self._base_text = text
        self._hotkey_text = ""

    def setText(self, text: str):
        """Override setText to update base text and re-apply hotkey."""
        self._base_text = text
        self._update_text()

    def set_hotkey(self, hotkey: str):
        """Set the hotkey text to display on the button."""
        self._hotkey_text = hotkey
        self._update_text()

    def _update_text(self):
        """Update the button text combining base text and hotkey."""
        if self._hotkey_text:
            # Use a slightly smaller font for the hotkey or just append it
            super().setText(f"{self._base_text} [{self._hotkey_text}]")
        else:
            super().setText(self._base_text)


class PrimaryButton(ModernButton):
    """Primary action button with gradient."""

    def __init__(self, text: str = "", parent=None):
        """Initialize primary button."""
        super().__init__(text, parent)
        self.setObjectName("primaryButton")
        self.setMinimumHeight(48)
        self.setMinimumWidth(140)


class DangerButton(ModernButton):
    """Danger button for destructive actions."""

    def __init__(self, text: str = "", parent=None):
        """Initialize danger button."""
        super().__init__(text, parent)
        self.setObjectName("dangerButton")
        self.setMinimumHeight(48)
        self.setMinimumWidth(140)


class SuccessButton(ModernButton):
    """Success button for positive actions."""

    def __init__(self, text: str = "", parent=None):
        """Initialize success button."""
        super().__init__(text, parent)
        self.setObjectName("successButton")
        self.setMinimumHeight(48)
        self.setMinimumWidth(140)


class WarningButton(ModernButton):
    """Warning button for caution actions (yellow/amber)."""

    def __init__(self, text: str = "", parent=None):
        """Initialize warning button."""
        super().__init__(text, parent)
        self.setObjectName("warningButton")
        self.setMinimumHeight(48)
        self.setMinimumWidth(140)


class IconButton(ModernButton):
    """Small button, typically used for icons."""

    def __init__(self, icon=None, parent=None):
        """Initialize icon button."""
        super().__init__(parent=parent)
        if icon:
            self.setIcon(icon)
        self.setMinimumSize(44, 44)
        self.setMaximumSize(44, 44)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

"""
Modern button components for PyQt6 UI.
"""
from PyQt6.QtWidgets import QPushButton, QSizePolicy, QWidget, QHBoxLayout, QLabel
from PyQt6.QtCore import Qt, pyqtSignal, QPoint, QTimer
from PyQt6.QtGui import QFont


class HotkeyHoverHint(QWidget):
    """Floating shortcut hint shown above a button on hover."""

    _TEXT_STYLE = """
        QLabel {
            color: #f5f5f7;
            font-size: 11px;
            font-weight: 600;
            font-family: "Segoe UI", "Helvetica Neue", sans-serif;
            background: transparent;
            border: none;
            padding: 0;
        }
    """

    def __init__(self, parent=None):
        super().__init__(
            parent,
            Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint,
        )
        self.setObjectName("hotkeyHoverHint")

        outer = QHBoxLayout(self)
        outer.setContentsMargins(8, 5, 8, 5)
        outer.setSpacing(0)

        self._text_label = QLabel()
        self._text_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._text_label.setStyleSheet(self._TEXT_STYLE)
        outer.addWidget(self._text_label)

        self.setStyleSheet("""
            HotkeyHoverHint {
                background-color: rgba(28, 28, 30, 0.94);
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 8px;
            }
        """)

    def set_hotkey(self, hotkey: str) -> None:
        """Update the shortcut text shown in the hover hint."""
        self._text_label.setText(hotkey)
        self.adjustSize()


class Button(QPushButton):
    """Modern button with smooth hover and click animations."""

    clicked_smooth = pyqtSignal()

    def __init__(self, text: str = "", parent=None):
        """Initialize modern button."""
        super().__init__(text, parent)
        self.setMinimumHeight(44)
        self.setFont(QFont("Segoe UI", 12))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        size_policy = self.sizePolicy()
        size_policy.setHorizontalPolicy(QSizePolicy.Policy.Minimum)
        self.setSizePolicy(size_policy)

        self._base_text = text
        self._hotkey_text = ""
        self._active = True
        self._base_min_height = 44
        self._base_min_width = 140
        self._hotkey_hint: HotkeyHoverHint | None = None
        self._hide_hint_timer = QTimer(self)
        self._hide_hint_timer.setSingleShot(True)
        self._hide_hint_timer.setInterval(80)
        self._hide_hint_timer.timeout.connect(self._hide_hotkey_hint)

    def setText(self, text: str):
        """Override setText to update base text."""
        self._base_text = text
        super().setText(text)
        self._refresh_size()

    def set_hotkey(self, hotkey: str):
        """Set the hotkey shown in a hover hint above the button."""
        self._hotkey_text = hotkey
        if self._hotkey_hint is not None:
            self._hotkey_hint.set_hotkey(hotkey)

    def set_active(self, active: bool):
        """Toggle interactivity while keeping the button hover-responsive.

        Unlike ``setEnabled(False)``, the widget stays enabled so it continues
        to receive hover events (and can show its hotkey hint); clicks are
        suppressed and the pointer reverts to the default arrow. This is used
        for buttons that should always advertise their shortcut even when the
        action isn't currently available.
        """
        if self._active == active:
            return
        self._active = active
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if active else Qt.CursorShape.ArrowCursor
        )
        if not active:
            self._hide_hotkey_hint()

    def _refresh_size(self):
        """Size the button to fit its label without horizontal clipping."""
        fm = self.fontMetrics()
        text_width = fm.horizontalAdvance(self.text()) if self.text() else 0
        horizontal_padding = 40
        self.setMinimumWidth(max(self._base_min_width, text_width + horizontal_padding))
        self.setMinimumHeight(self._base_min_height)

    def _ensure_hotkey_hint(self) -> HotkeyHoverHint:
        if self._hotkey_hint is None:
            self._hotkey_hint = HotkeyHoverHint(self.window())
            if self._hotkey_text:
                self._hotkey_hint.set_hotkey(self._hotkey_text)
        return self._hotkey_hint

    def _show_hotkey_hint(self) -> None:
        if not self._hotkey_text or not self.isEnabled():
            return

        self._hide_hint_timer.stop()
        hint = self._ensure_hotkey_hint()
        hint.set_hotkey(self._hotkey_text)

        top_center = self.mapToGlobal(QPoint(self.width() // 2, 0))
        hint_width = hint.sizeHint().width()
        hint_height = hint.sizeHint().height()
        hint.move(
            top_center.x() - hint_width // 2,
            top_center.y() - hint_height - 8,
        )
        hint.show()
        hint.raise_()

    def _hide_hotkey_hint(self) -> None:
        if self._hotkey_hint is not None:
            self._hotkey_hint.hide()

    def mousePressEvent(self, event):
        if not self._active:
            event.ignore()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if not self._active:
            event.ignore()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if not self._active and event.key() in (
            Qt.Key.Key_Space,
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
        ):
            event.ignore()
            return
        super().keyPressEvent(event)

    def enterEvent(self, event):
        super().enterEvent(event)
        self._show_hotkey_hint()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._hide_hint_timer.start()

    def hideEvent(self, event):
        self._hide_hotkey_hint()
        super().hideEvent(event)


class PrimaryButton(Button):
    """Primary action button with gradient."""

    def __init__(self, text: str = "", parent=None):
        """Initialize primary button."""
        super().__init__(text, parent)
        self.setObjectName("primaryButton")
        self._base_min_height = 48
        self._base_min_width = 140
        self.setMinimumHeight(48)
        self.setMinimumWidth(140)


class DangerButton(Button):
    """Danger button for destructive actions."""

    def __init__(self, text: str = "", parent=None):
        """Initialize danger button."""
        super().__init__(text, parent)
        self.setObjectName("dangerButton")
        self._base_min_height = 48
        self._base_min_width = 140
        self.setMinimumHeight(48)
        self.setMinimumWidth(140)


class SuccessButton(Button):
    """Success button for positive actions."""

    def __init__(self, text: str = "", parent=None):
        """Initialize success button."""
        super().__init__(text, parent)
        self.setObjectName("successButton")
        self._base_min_height = 48
        self._base_min_width = 140
        self.setMinimumHeight(48)
        self.setMinimumWidth(140)


class WarningButton(Button):
    """Warning button for caution actions (yellow/amber)."""

    def __init__(self, text: str = "", parent=None):
        """Initialize warning button."""
        super().__init__(text, parent)
        self.setObjectName("warningButton")
        self._base_min_height = 48
        self._base_min_width = 140
        self.setMinimumHeight(48)
        self.setMinimumWidth(140)


class IconButton(Button):
    """Small button, typically used for icons."""

    def __init__(self, icon=None, parent=None):
        """Initialize icon button."""
        super().__init__(parent=parent)
        if icon:
            self.setIcon(icon)
        self.setMinimumSize(44, 44)
        self.setMaximumSize(44, 44)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

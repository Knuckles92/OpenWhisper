"""
Modern button components for PyQt6 UI.
"""
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QWidget,
)
from PyQt6.QtCore import (
    Qt,
    pyqtSignal,
    QEasingCurve,
    QEvent,
    QLineF,
    QObject,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRectF,
    QTimer,
)
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen


class HotkeyHoverHint(QWidget):
    """Floating pill-shaped shortcut hint shown above a button on hover."""

    _BACKGROUND = QColor(58, 58, 60, 248)
    _BORDER = QColor(255, 255, 255, 38)

    _TEXT_STYLE = """
        QLabel {
            color: #f5f5f7;
            font-size: 11px;
            font-weight: 600;
            letter-spacing: 0.5px;
            font-family: "Segoe UI", "Helvetica Neue", sans-serif;
            background: transparent;
            border: none;
            padding: 0;
        }
    """

    def __init__(self, parent=None):
        super().__init__(
            parent,
            Qt.WindowType.ToolTip
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint,
        )
        self.setObjectName("hotkeyHoverHint")
        # The pill is painted in paintEvent against a translucent window,
        # so the rectangular window corners stay invisible. QSS rounding on
        # an opaque ToolTip window leaves square backing blocks, and the
        # native drop shadow re-outlines the rectangle.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(12, 6, 12, 6)
        outer.setSpacing(0)

        self._text_label = QLabel()
        self._text_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._text_label.setStyleSheet(self._TEXT_STYLE)
        outer.addWidget(self._text_label)

        self._fade_in = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade_in.setDuration(140)
        self._fade_in.setStartValue(0.0)
        self._fade_in.setEndValue(1.0)
        self._fade_in.setEasingCurve(QEasingCurve.Type.OutCubic)

    def set_hotkey(self, hotkey: str) -> None:
        """Update the shortcut text shown in the hover hint."""
        self._text_label.setText(hotkey)
        self.adjustSize()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Half-pixel inset keeps the 1px border crisp instead of smeared
        # across two device pixels.
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = rect.height() / 2.0
        painter.setPen(QPen(self._BORDER, 1.0))
        painter.setBrush(self._BACKGROUND)
        painter.drawRoundedRect(rect, radius, radius)

    def showEvent(self, event):
        super().showEvent(event)
        self._fade_in.stop()
        self.setWindowOpacity(0.0)
        self._fade_in.start()


class HotkeyHintFilter(QObject):
    """Shows a ``HotkeyHoverHint`` pill above a button while it is hovered.

    Attachable to any ``QPushButton`` — including plain styled buttons —
    without changing the button's font or sizing the way subclassing
    ``Button`` would. The filter parents itself to the button, so its
    lifetime is managed automatically.
    """

    _HIDE_DELAY_MS = 80

    def __init__(self, button: QPushButton, hotkey: str = ""):
        """Attach the hover hint to a button.

        Args:
            button: The button to watch; also becomes this filter's parent.
            hotkey: Initial shortcut display text (empty disables the hint).
        """
        super().__init__(button)
        self._button = button
        self._hotkey = hotkey
        self._hint: HotkeyHoverHint | None = None
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(self._HIDE_DELAY_MS)
        self._hide_timer.timeout.connect(self.hide_hint)
        button.installEventFilter(self)

    def set_hotkey(self, hotkey: str) -> None:
        """Update the shortcut text shown in the hover hint."""
        self._hotkey = hotkey
        if self._hint is not None:
            self._hint.set_hotkey(hotkey)

    def eventFilter(self, obj, event):
        if obj is self._button:
            event_type = event.type()
            if event_type == QEvent.Type.Enter:
                self.show_hint()
            elif event_type == QEvent.Type.Leave:
                self._hide_timer.start()
            elif event_type == QEvent.Type.Hide:
                self.hide_hint()
        return False

    def show_hint(self) -> None:
        """Show the pill centered above the button."""
        if not self._hotkey or not self._button.isEnabled():
            return

        self._hide_timer.stop()
        if self._hint is None:
            self._hint = HotkeyHoverHint(self._button.window())
        self._hint.set_hotkey(self._hotkey)

        top_center = self._button.mapToGlobal(QPoint(self._button.width() // 2, 0))
        hint_width = self._hint.sizeHint().width()
        hint_height = self._hint.sizeHint().height()
        self._hint.move(
            top_center.x() - hint_width // 2,
            top_center.y() - hint_height - 8,
        )
        self._hint.show()
        self._hint.raise_()

    def hide_hint(self) -> None:
        """Hide the pill immediately."""
        if self._hint is not None:
            self._hint.hide()


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
        self._active = True
        self._base_min_height = 44
        self._base_min_width = 140
        self._hotkey_hint_filter = HotkeyHintFilter(self)

    def setText(self, text: str):
        """Override setText to update base text."""
        self._base_text = text
        super().setText(text)
        self._refresh_size()

    def set_hotkey(self, hotkey: str):
        """Set the hotkey shown in a hover hint above the button."""
        self._hotkey_hint_filter.set_hotkey(hotkey)

    def set_base_minimum_size(self, width: int, height: int) -> None:
        """Set the minimum dimensions used when the button text changes.

        Args:
            width: Minimum button width in pixels.
            height: Minimum button height in pixels.
        """
        self._base_min_width = width
        self._base_min_height = height
        self._refresh_size()

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
            self._hotkey_hint_filter.hide_hint()

    def _refresh_size(self):
        """Size the button to fit its label without horizontal clipping.

        Polish first so fontMetrics match the theme stylesheet (e.g. 14px /
        weight 600). Measuring against the constructor's setFont() fallback
        underestimates width on platforms where Segoe UI is missing.
        """
        self.ensurePolished()
        fm = self.fontMetrics()
        text_width = fm.horizontalAdvance(self.text()) if self.text() else 0
        horizontal_padding = 40
        # sizeHint includes stylesheet padding/borders; fontMetrics alone can
        # still undershoot by a pixel or two on macOS.
        fitted = max(text_width + horizontal_padding, self.sizeHint().width())
        self.setMinimumWidth(max(self._base_min_width, fitted))
        self.setMinimumHeight(self._base_min_height)

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


class SplitButton(Button):
    """Button with an integrated dropdown zone on its right edge.

    Renders as a single seamless rounded button: clicking the label area
    emits ``clicked`` as usual, while the chevron zone on the right pops
    the attached menu below the button. Style it like any other button by
    assigning a themed object name (e.g. ``warningButton``).
    """

    _ZONE_WIDTH = 34
    # Matches the QPushButton border-radius in theme.qss so the hover
    # overlay on the dropdown zone follows the button's rounded corners.
    _CORNER_RADIUS = 8

    def __init__(self, text: str = "", parent=None):
        """Initialize split button."""
        super().__init__(text, parent)
        self._menu: QMenu | None = None
        self._zone_hovered = False
        self.setMouseTracking(True)

    def set_menu(self, menu: QMenu) -> None:
        """Attach the dropdown menu and reserve the chevron zone.

        Args:
            menu: Menu shown when the right-edge zone is clicked.
        """
        self._menu = menu
        # Keep the label centered in the area left of the zone: the base
        # theme uses 20px side padding, so widen only the right side.
        self.setStyleSheet(f"padding-right: {self._ZONE_WIDTH + 20}px;")
        self._refresh_size()
        self.update()

    def menu(self) -> QMenu | None:
        """Return the attached dropdown menu, if any."""
        return self._menu

    def _refresh_size(self):
        super()._refresh_size()
        if self._menu is not None:
            self.setMinimumWidth(self.minimumWidth() + self._ZONE_WIDTH)

    def _zone_rect(self) -> QRect:
        return QRect(
            self.width() - self._ZONE_WIDTH, 0, self._ZONE_WIDTH, self.height()
        )

    def _show_menu(self) -> None:
        if self._menu is None:
            return
        menu_width = self._menu.sizeHint().width()
        anchor = self.mapToGlobal(
            QPoint(self.width() - menu_width, self.height() + 4)
        )
        self._menu.popup(anchor)

    def mousePressEvent(self, event):
        if (
            self._menu is not None
            and self._active
            and event.button() == Qt.MouseButton.LeftButton
            and self._zone_rect().contains(event.position().toPoint())
        ):
            self._show_menu()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        hovered = self._zone_rect().contains(event.position().toPoint())
        if hovered != self._zone_hovered:
            self._zone_hovered = hovered
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        if self._zone_hovered:
            self._zone_hovered = False
            self.update()
        super().leaveEvent(event)

    def keyPressEvent(self, event):
        if (
            self._menu is not None
            and self._active
            and event.key() == Qt.Key.Key_Down
        ):
            self._show_menu()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._menu is None:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        zone = self._zone_rect()

        if self._zone_hovered and self._active and self.isEnabled():
            rounded = QPainterPath()
            rounded.addRoundedRect(
                QRectF(self.rect()), self._CORNER_RADIUS, self._CORNER_RADIUS
            )
            painter.setClipPath(rounded)
            painter.fillRect(zone, QColor(0, 0, 0, 34))
            painter.setClipping(False)

        divider_x = zone.left() + 0.5
        inset = self.height() * 0.28
        painter.setPen(QPen(QColor(255, 255, 255, 60), 1.0))
        painter.drawLine(
            QLineF(divider_x, inset, divider_x, self.height() - inset)
        )

        chevron = QPen(QColor("#ffffff"), 1.6)
        chevron.setCapStyle(Qt.PenCapStyle.RoundCap)
        chevron.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(chevron)
        cx = zone.left() + zone.width() / 2.0
        cy = self.height() / 2.0
        painter.drawLines((
            QLineF(cx - 3.5, cy - 1.75, cx, cy + 1.75),
            QLineF(cx, cy + 1.75, cx + 3.5, cy - 1.75),
        ))


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

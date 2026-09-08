"""Interactive field help that remains open while its links are in use."""
from PyQt6.QtCore import QEvent, QPoint, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QCursor, QPainter, QPen
from PyQt6.QtWidgets import QFrame, QLabel, QPushButton, QSizePolicy, QVBoxLayout

from ui_qt.utils.palette import token_color


_CARD_FLAGS = (
    Qt.WindowType.ToolTip
    | Qt.WindowType.FramelessWindowHint
    | Qt.WindowType.NoDropShadowWindowHint
)


class HelpCard(QFrame):
    """Rounded popover painted against a translucent window.

    QSS ``border-radius`` on an opaque tool window leaves square backing
    blocks. Translucency without a ``paintEvent`` leaves the card invisible,
    which is what made the engine-field hints unreadable over the recording
    buttons.
    """

    RADIUS = 10

    def __init__(self, parent=None):
        super().__init__(parent, _CARD_FLAGS)
        self.setObjectName("fieldHelpCard")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(token_color("border-hover"), 1.0))
        painter.setBrush(token_color("surface-hover"))
        painter.drawRoundedRect(rect, self.RADIUS, self.RADIUS)


class FieldHelp(QPushButton):
    """A plain field caption that opens help on hover or keyboard activation."""

    destination_requested = pyqtSignal(str)

    def __init__(self, caption, description, links, parent=None):
        super().__init__(parent)
        self.caption = caption
        self.setAccessibleName(f"Help for {caption}")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setAutoDefault(False)
        self.setDefault(False)
        self.setFlat(True)
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.setStyleSheet("QPushButton { background: transparent; border: none; padding: 0; }")
        self.card = HelpCard(self)
        self.card.setStyleSheet(
            "QFrame#fieldHelpCard { background: transparent; border: none; }"
            "QLabel { color: @text-secondary; background: transparent; border: none; }"
            "QLabel#fieldHelpTitle { color: @text; font-weight: 600; }"
            "QPushButton { color: @accent; background: transparent; border: none; "
            "text-align: left; padding: 4px 0 0 0; }"
            "QPushButton:hover, QPushButton:focus { text-decoration: underline; }"
        )
        layout = QVBoxLayout(self.card)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)
        title = QLabel(caption)
        title.setObjectName("fieldHelpTitle")
        layout.addWidget(title)
        self._body = QLabel(description)
        self._body.setWordWrap(True)
        layout.addWidget(self._body)
        self.links = []
        for text, destination in links:
            button = QPushButton(text)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda checked=False, d=destination: self._navigate(d))
            layout.addWidget(button)
            self.links.append(button)
            button.installEventFilter(self)
        self.card.installEventFilter(self)
        self.card.hide()
        self._show_timer = QTimer(self)
        self._show_timer.setSingleShot(True)
        self._show_timer.setInterval(450)
        self._show_timer.timeout.connect(self.show_help)
        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setInterval(200)
        self._dismiss_timer.timeout.connect(self._dismiss_if_outside)
        self.clicked.connect(self._open_for_keyboard)

    def show_help(self):
        if not self.isVisible():
            return
        self._host_card()
        self._size_card()
        point = self._card_position()
        self.card.move(point)
        self.card.show()
        self.card.raise_()
        # Windows can ignore the first move() until the native window exists.
        self.card.move(point)
        self._dismiss_timer.start()

    def _host_card(self):
        """Own the card from the top-level window so it stays above siblings.

        Parenting a tool window to this caption (a 20px-tall button inside a
        scroll area) lets the recording controls paint over the hint and can
        place ``move(mapToGlobal(...))`` in the middle of the page.
        """
        host = self.window()
        if self.card.parent() is host and self.card.isWindow():
            return
        self.card.setParent(host, _CARD_FLAGS)
        self.card.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.card.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def _size_card(self):
        from ui_qt.utils.font_scale import current_ui_font_scale

        screen = self.screen().availableGeometry()
        width = min(round(360 * current_ui_font_scale()), max(160, screen.width()))
        inner = max(80, width - 28)
        self._body.setFixedWidth(inner)
        for link in self.links:
            link.setMaximumWidth(inner)
        self.card.setFixedWidth(width)
        self.card.adjustSize()

    def _card_position(self) -> QPoint:
        point = self.mapToGlobal(QPoint(0, self.height() + 4))
        screen = self.screen().availableGeometry()
        point.setX(max(screen.left(), min(point.x(), screen.right() - self.card.width() + 1)))
        if point.y() + self.card.height() > screen.bottom():
            point.setY(self.mapToGlobal(QPoint(0, 0)).y() - self.card.height() - 4)
        point.setY(max(screen.top(), point.y()))
        return point

    def _open_for_keyboard(self):
        self.show_help()
        self.card.activateWindow()
        if self.links:
            self.links[0].setFocus()

    def _navigate(self, destination):
        self.card.hide()
        self._dismiss_timer.stop()
        self.destination_requested.emit(destination)

    def _dismiss_if_outside(self):
        point = QCursor.pos()
        over_caption = self.rect().contains(self.mapFromGlobal(point))
        over_card = self.card.rect().adjusted(-8, -8, 8, 8).contains(self.card.mapFromGlobal(point))
        if not over_caption and not over_card and not self.card.isActiveWindow():
            self.card.hide()
            self._dismiss_timer.stop()

    def sizeHint(self) -> QSize:
        metrics = self.fontMetrics()
        return QSize(metrics.horizontalAdvance(self.caption), metrics.height())

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        active = self.underMouse() or self.hasFocus() or self.isDown()
        color = token_color("accent" if active else "text-secondary")
        metrics = self.fontMetrics()
        rect = QRectF(self.rect())
        text_width = metrics.horizontalAdvance(self.caption)
        painter.setPen(color)
        painter.drawText(
            QRectF(rect.left(), rect.top(), text_width, rect.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            self.caption,
        )

    def enterEvent(self, event):
        self._show_timer.start()
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._show_timer.stop()
        self.update()
        super().leaveEvent(event)

    def focusInEvent(self, event):
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        self.update()
        super().focusOutEvent(event)

    def hideEvent(self, event):
        self._show_timer.stop()
        self._dismiss_timer.stop()
        self.card.hide()
        super().hideEvent(event)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            self.card.hide()
            self._dismiss_timer.stop()
            self.setFocus()
            return True
        if watched is self.card and event.type() == QEvent.Type.WindowDeactivate:
            self.card.hide()
            self._dismiss_timer.stop()
        return super().eventFilter(watched, event)

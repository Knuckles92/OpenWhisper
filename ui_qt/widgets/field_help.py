"""Interactive field help that remains open while its links are in use."""
from PyQt6.QtCore import QEvent, QPoint, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QCursor, QPainter
from PyQt6.QtWidgets import QFrame, QLabel, QPushButton, QVBoxLayout

from ui_qt.widgets.help_glyph import HelpSymbol, glyph_colors, glyph_diameter, paint_help_glyph


class FieldHelp(QPushButton):
    """A field caption with a painted help glyph that opens a help card.

    The caption is painted rather than set as button text so the glyph is a
    shape beside it, not a fallback font character in the string.
    """

    destination_requested = pyqtSignal(str)

    #: Gap between the caption's last letter and the glyph ring.
    GLYPH_GAP = 5

    def __init__(self, caption, description, links, parent=None):
        super().__init__(parent)
        self.caption = caption
        self.setAccessibleName(f"Help for {caption}")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setStyleSheet("QPushButton { background: transparent; border: none; padding: 0; }")
        self.card = QFrame(self, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.card.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        # Without a translucent window the rounded border sits inside a square
        # opaque window and the corners show as notches.
        self.card.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.card.setObjectName("fieldHelpCard")
        self.card.setStyleSheet(
            "QFrame#fieldHelpCard { background: @surface; border: 1px solid @border-hover; "
            "border-radius: 10px; }"
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
        body = QLabel(description)
        body.setWordWrap(True)
        layout.addWidget(body)
        self.links = []
        for text, destination in links:
            button = QPushButton(text)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda checked=False, d=destination: self._navigate(d))
            layout.addWidget(button)
            self.links.append(button)
            button.installEventFilter(self)
        self.card.installEventFilter(self)
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
        from ui_qt.utils.font_scale import current_ui_font_scale
        width = round(360 * current_ui_font_scale())
        self.card.setFixedWidth(min(width, self.screen().availableGeometry().width()))
        self.card.adjustSize()
        point = self.mapToGlobal(QPoint(0, self.height() + 4))
        screen = self.screen().availableGeometry()
        point.setX(max(screen.left(), min(point.x(), screen.right() - self.card.width() + 1)))
        if point.y() + self.card.height() > screen.bottom():
            point.setY(self.mapToGlobal(QPoint(0, 0)).y() - self.card.height() - 4)
        point.setY(max(screen.top(), point.y()))
        self.card.move(point)
        self.card.show()
        self._dismiss_timer.start()

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

    def _glyph_diameter(self) -> int:
        return glyph_diameter(self.fontMetrics())

    def sizeHint(self) -> QSize:
        metrics = self.fontMetrics()
        d = self._glyph_diameter()
        width = metrics.horizontalAdvance(self.caption) + self.GLYPH_GAP + d
        return QSize(width, max(metrics.height(), d))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        active = self.underMouse() or self.hasFocus() or self.isDown()
        color, fill = glyph_colors(active)
        metrics = self.fontMetrics()
        rect = QRectF(self.rect())
        text_width = metrics.horizontalAdvance(self.caption)
        painter.setPen(color)
        painter.drawText(
            QRectF(rect.left(), rect.top(), text_width, rect.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            self.caption,
        )
        # Centre the ring on the caption's cap height rather than its line box,
        # otherwise it rides low against the descender room.
        baseline = (rect.height() - metrics.height()) / 2 + metrics.ascent()
        d = self._glyph_diameter()
        glyph = QRectF(rect.left() + text_width + self.GLYPH_GAP, baseline - metrics.capHeight() / 2 - d / 2, d, d)
        paint_help_glyph(painter, glyph, color, HelpSymbol.INFO, fill)

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

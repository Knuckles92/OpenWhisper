"""Interactive field help that remains open while its links are in use."""
from PyQt6.QtCore import QEvent, QPoint, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QCursor
from PyQt6.QtWidgets import QFrame, QLabel, QPushButton, QVBoxLayout


class FieldHelp(QPushButton):
    destination_requested = pyqtSignal(str)

    def __init__(self, caption, description, links, parent=None):
        super().__init__(f"{caption} ⓘ", parent)
        self.setAccessibleName(f"Help for {caption}")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            "QPushButton { color: @text-secondary; background: transparent; "
            "border: none; text-align: left; padding: 0; }"
            "QPushButton:hover, QPushButton:focus { color: @accent; }"
        )
        self.card = QFrame(self, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.card.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.card.setObjectName("fieldHelpCard")
        self.card.setStyleSheet(
            "QFrame#fieldHelpCard { background: @surface; border: 1px solid @border; "
            "border-radius: 10px; }"
            "QLabel { color: @text; background: transparent; border: none; }"
            "QPushButton { color: @accent; background: transparent; border: none; "
            "text-align: left; padding: 6px 0; }"
            "QPushButton:hover, QPushButton:focus { text-decoration: underline; }"
        )
        layout = QVBoxLayout(self.card)
        layout.setContentsMargins(14, 12, 14, 12)
        title = QLabel(caption)
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

    def enterEvent(self, event):
        self._show_timer.start()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._show_timer.stop()
        super().leaveEvent(event)

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

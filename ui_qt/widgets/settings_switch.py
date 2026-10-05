"""A keyboard-accessible on/off switch shared by every Settings page."""

from PyQt6.QtCore import QEvent, QRectF, QSize, Qt
from PyQt6.QtGui import QPainter, QPen
from PyQt6.QtWidgets import QAbstractButton, QSizePolicy

from ui_qt.utils.font_scale import current_ui_font_scale
from ui_qt.utils.palette import token_color


class SettingsSwitch(QAbstractButton):
    """A keyboard-accessible toggle painted with the current theme's tokens."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setObjectName("basicSettingsSwitch")

    def sizeHint(self):
        scale = current_ui_font_scale()
        return QSize(round(46 * scale), round(28 * scale))

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self.updateGeometry()
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            painter.setOpacity(0.45)
        rect = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(
            token_color("accent" if self.isChecked() else "slate-border-strong")
        )
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        diameter = rect.height() - 6
        x = rect.right() - diameter - 3 if self.isChecked() else rect.left() + 3
        painter.setBrush(token_color("on-accent"))
        painter.drawEllipse(QRectF(x, rect.top() + 3, diameter, diameter))
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(token_color("accent"), 1))
            painter.drawRoundedRect(
                QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 14, 14
            )

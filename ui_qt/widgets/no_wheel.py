"""
Widgets that ignore mouse-wheel value changes unless focused.

Prevents accidental selection changes while scrolling a parent page.
"""
from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QPalette, QWheelEvent
from PyQt6.QtWidgets import (
    QComboBox,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QStyleOptionComboBox,
    QStylePainter,
)


class NoWheelComboBox(QComboBox):
    """QComboBox that only scrolls its items when it has keyboard focus."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class ElidingComboBox(NoWheelComboBox):
    """Combo whose width floor ignores how long its item text happens to be.

    A non-editable ``QComboBox`` reports the widest item as its minimum width
    and uses a ``Minimum`` horizontal policy, so a single long model id or
    endpoint name raises the enclosing window's minimum width. These combos hold
    catalog values of unbounded length, so the floor is capped here and the
    closed-state text is elided rather than clipped mid-character. The popup
    still shows every item in full.
    """

    #: Enough for a short value plus the arrow; long values elide.
    MINIMUM_WIDTH = 170

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        return QSize(min(hint.width(), self.MINIMUM_WIDTH), hint.height())

    def paintEvent(self, _event) -> None:
        painter = QStylePainter(self)
        painter.setPen(self.palette().color(QPalette.ColorRole.Text))
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)
        field = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox,
            option,
            QStyle.SubControl.SC_ComboBoxEditField,
            self,
        )
        option.currentText = self.fontMetrics().elidedText(
            option.currentText, Qt.TextElideMode.ElideRight, field.width()
        )
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, option)


class NoWheelSpinBox(QSpinBox):
    """QSpinBox that only changes value on wheel when it has keyboard focus."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()

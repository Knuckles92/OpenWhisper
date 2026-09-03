"""
Widgets that ignore mouse-wheel value changes unless focused.

Prevents accidental selection changes while scrolling a parent page.
"""
from pathlib import Path

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QIcon, QPalette, QWheelEvent
from PyQt6.QtWidgets import (
    QAbstractSpinBox,
    QComboBox,
    QDateEdit,
    QFrame,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QStyleOptionComboBox,
    QStylePainter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from config import bundle_root


def _asset_icon(*parts: str) -> QIcon:
    return QIcon(str(Path(bundle_root()).joinpath("ui_qt", "assets", *parts)))


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
    """QSpinBox that only changes value on wheel when it has keyboard focus.

    A stacked chevron rail on the right steps the value. Native arrows stay
    hidden so the theme cannot draw them as two-border corner marks.
    """

    STEPPER_WIDTH = 28
    STEPPER_GAP = 4
    ICON_SIZE = QSize(16, 10)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)

        self._stepper = QWidget(self)
        self._stepper.setObjectName("spinStepper")
        self._stepper.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        rail = QVBoxLayout(self._stepper)
        rail.setContentsMargins(0, 0, 0, 0)
        rail.setSpacing(0)

        self.up_button = self._make_step_button(
            "spinStepUp", "spin-chevron-up.svg", "Increase"
        )
        self.down_button = self._make_step_button(
            "spinStepDown", "spin-chevron-down.svg", "Decrease"
        )
        self.up_button.clicked.connect(lambda: self._step(1))
        self.down_button.clicked.connect(lambda: self._step(-1))
        divider = QFrame(self._stepper)
        divider.setObjectName("spinStepperDivider")
        divider.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        divider.setFixedHeight(1)
        divider.setFrameShape(QFrame.Shape.NoFrame)
        rail.addWidget(self.up_button)
        rail.addWidget(divider)
        rail.addWidget(self.down_button)

        self.valueChanged.connect(self._sync_step_buttons)
        self._sync_step_buttons()
        self._layout_stepper()

    def _make_step_button(
        self, object_name: str, icon: str, tooltip: str
    ) -> QToolButton:
        button = QToolButton(self._stepper)
        button.setObjectName(object_name)
        button.setIcon(_asset_icon(icon))
        button.setIconSize(self.ICON_SIZE)
        button.setToolTip(tooltip)
        button.setAutoRepeat(True)
        button.setAutoRepeatDelay(400)
        button.setAutoRepeatInterval(80)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setMinimumSize(0, 0)
        button.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        return button

    def _step(self, steps: int) -> None:
        if not self.hasFocus():
            self.setFocus(Qt.FocusReason.MouseFocusReason)
        self.stepBy(steps)

    def _sync_step_buttons(self, *_args) -> None:
        flags = self.stepEnabled()
        self.up_button.setEnabled(
            bool(flags & QAbstractSpinBox.StepEnabledFlag.StepUpEnabled)
        )
        self.down_button.setEnabled(
            bool(flags & QAbstractSpinBox.StepEnabledFlag.StepDownEnabled)
        )

    def _layout_stepper(self) -> None:
        frame = 1
        width = self.STEPPER_WIDTH
        self._stepper.setGeometry(
            max(0, self.width() - width - frame),
            frame,
            width,
            max(0, self.height() - 2 * frame),
        )
        self._stepper.raise_()
        edit = self.lineEdit()
        if edit is not None:
            edit.setTextMargins(0, 0, width + self.STEPPER_GAP, 0)

    def _stepper_reserve(self) -> int:
        return self.STEPPER_WIDTH + self.STEPPER_GAP

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        return QSize(hint.width() + self._stepper_reserve(), hint.height())

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        return QSize(hint.width() + self._stepper_reserve(), hint.height())

    def setSpecialValueText(self, text: str) -> None:
        super().setSpecialValueText(text)
        self.updateGeometry()

    def setMinimum(self, min_val: int) -> None:
        super().setMinimum(min_val)
        self._sync_step_buttons()

    def setMaximum(self, max_val: int) -> None:
        super().setMaximum(max_val)
        self._sync_step_buttons()

    def setRange(self, min_val: int, max_val: int) -> None:
        super().setRange(min_val, max_val)
        self._sync_step_buttons()

    def setWrapping(self, wrapping: bool) -> None:
        super().setWrapping(wrapping)
        self._sync_step_buttons()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._layout_stepper()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._layout_stepper()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class NoWheelDateEdit(QDateEdit):
    """QDateEdit that only changes date on wheel when it has keyboard focus."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCalendarPopup(True)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()

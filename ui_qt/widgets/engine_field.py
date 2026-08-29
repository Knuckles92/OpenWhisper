"""Building blocks for the engine card's field row.

The card lays four labeled fields across its width, so each one has to survive
being narrow: combos elide instead of reporting their longest item as a minimum
width, which is what lets Device and Quant sit in a quarter of the row.
"""
from enum import Enum
from typing import Iterable

from PyQt6.QtWidgets import QComboBox, QLabel, QVBoxLayout, QWidget

from ui_qt.widgets.no_wheel import ElidingComboBox

#: A non-editable QComboBox reports its widest item as a minimum width, which
#: would let one long model id set the whole window's floor.
FIELD_MINIMUM_WIDTH = 96

DOT_DIAMETER = 8


class EngineStatus(Enum):
    """What the status dots report about the engine.

    ``UNKNOWN`` exists because nothing has loaded yet when the window first
    opens, and the API backends have no local engine to report on: a green dot
    in either case would be a guess rather than a reading.
    """

    READY = "ready"
    ATTENTION = "attention"
    UNKNOWN = "unknown"


_DOT_COLORS = {
    EngineStatus.READY: "#30d158",
    EngineStatus.ATTENTION: "#ff9f0a",
    EngineStatus.UNKNOWN: "#636366",
}


class StatusDot(QLabel):
    """A filled dot reporting one :class:`EngineStatus`.

    Styles itself rather than relying on the application stylesheet, because one
    of these is parented to a QComboBox to sit inside the closed field and an
    application stylesheet never reaches a QComboBox's children.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("engineStatusDot")
        self.setFixedSize(DOT_DIAMETER, DOT_DIAMETER)
        self._status = EngineStatus.UNKNOWN
        self.set_status(EngineStatus.UNKNOWN)

    def status(self) -> EngineStatus:
        return self._status

    def set_status(self, status: EngineStatus):
        self._status = status
        radius = DOT_DIAMETER // 2
        self.setStyleSheet(
            f"background-color: {_DOT_COLORS[status]};"
            f"border: none; border-radius: {radius}px;"
        )


class StatusFieldCombo(ElidingComboBox):
    """A field combo carrying a status dot inside its left padding.

    The dot describes the engine behind the current value, not the values
    themselves, so it cannot be an item icon — Qt would repeat it down the
    popup as though every backend had its own status.
    """

    #: Matches the left padding reserved for the dot in the QSS.
    DOT_LEFT = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dot = StatusDot(self)
        self._position_dot()

    def set_status(self, status: EngineStatus):
        self._dot.set_status(status)

    def set_status_visible(self, visible: bool):
        """Show or hide the dot, reclaiming its indent when hidden.

        The indent is a QSS property rather than a layout, so it has to be
        switched explicitly or the value stays pushed right off an absent dot.
        """
        self._dot.setVisible(visible)
        self.setProperty("dot", "true" if visible else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_dot()

    def _position_dot(self):
        self._dot.move(
            self.DOT_LEFT, (self.height() - self._dot.height()) // 2
        )


def engine_combo(items: Iterable[str], primary: bool = False) -> QComboBox:
    """A recessed combo for one field of the engine card.

    Args:
        items: Choices, in display order.
        primary: True for the Backend field, which carries the status dot and
            heavier text because it governs the other three.
    """
    combo = StatusFieldCombo() if primary else ElidingComboBox()
    combo.MINIMUM_WIDTH = FIELD_MINIMUM_WIDTH
    combo.addItems(list(items))
    combo.setObjectName("engineFieldPrimary" if primary else "engineField")
    return combo


def engine_field(caption: str, field: QWidget) -> QWidget:
    """Stack a dim caption above ``field`` as one column of the row."""
    wrapper = QWidget()
    wrapper.setObjectName("engineFieldGroup")
    column = QVBoxLayout(wrapper)
    column.setContentsMargins(0, 0, 0, 0)
    column.setSpacing(4)

    label = QLabel(caption)
    label.setObjectName("engineFieldLabel")
    column.addWidget(label)
    column.addWidget(field)
    return wrapper

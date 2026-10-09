"""A row of buttons that wraps into balanced rows instead of painting over each other."""
from math import lcm

from PyQt6.QtCore import QEvent, QTimer
from PyQt6.QtWidgets import QGridLayout, QSizePolicy, QWidget


class ButtonRow(QWidget):
    """Buttons share one row while their labels fit, then wrap into more rows.

    A plain ``QHBoxLayout`` lets a stretchy button shrink below its label when the
    column is narrow (a 300 px inspector at 130% font scale), so its neighbours paint
    over it. Here every button keeps its own label width as a floor and the row count
    grows until each row fits. The short row goes first, so three links wrap as 1 + 2
    with the main link on top, and each row's buttons share its full width.
    """

    def __init__(self, buttons, *, spacing=8, parent=None):
        super().__init__(parent)
        self._buttons = list(buttons)
        self._spacing = spacing
        self._shape = None
        # Width comes from the column this sits in; rows are chosen to fit it.
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(spacing)
        self._reflow_timer = QTimer(self)
        self._reflow_timer.setSingleShot(True)
        self._reflow_timer.timeout.connect(self.refresh)
        # Adopt the buttons now: a parentless widget reports itself hidden until it is
        # shown, and showing one before it is placed would open it as its own window.
        for button in self._buttons:
            button.setParent(self)
        self.refresh()

    def refresh(self) -> None:
        """Re-pick the rows; call after changing a label or showing/hiding a button."""
        visible = [button for button in self._buttons if not button.isHidden()]
        for button in visible:
            # ``Button.setText`` sets its own floor; compact buttons clear it, so raise it.
            button.setMinimumWidth(max(button.minimumWidth(), button.sizeHint().width()))
        # Even a row of one needs the widest label, so a column that would
        # squeeze it narrower (a two-column panel) stacks instead.
        self.setMinimumWidth(max((button.minimumWidth() for button in visible), default=0))
        sizes = self._row_sizes([button.minimumWidth() for button in visible])
        shape = (tuple(sizes), tuple(id(button) for button in visible))
        if shape == self._shape:
            return
        self._shape = shape
        layout = self.layout()
        for button in self._buttons:
            layout.removeWidget(button)
        # Every row spans the same grid, so a row of one is as wide as a row of three.
        columns = lcm(*sizes)
        placed = 0
        for row, size in enumerate(sizes):
            span = columns // size
            for slot in range(size):
                layout.addWidget(visible[placed], row, slot * span, 1, span)
                placed += 1
        for column in range(max(columns, layout.columnCount())):
            layout.setColumnStretch(column, 1 if column < columns else 0)
        self.updateGeometry()

    def _row_sizes(self, widths):
        """How many buttons go in each row: fewest rows where every row fits."""
        count = len(widths)
        for rows in range(1, count + 1):
            base, extra = divmod(count, rows)
            sizes = [base] * (rows - extra) + [base + 1] * extra
            start = 0
            for size in sizes:
                taken = widths[start:start + size]
                if sum(taken) + self._spacing * (size - 1) > self.width():
                    break
                start += size
            else:
                return sizes
        return [1] * count

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.refresh()

    def event(self, event):
        # A label or visibility change on a child is a layout request for this widget.
        if event.type() in (QEvent.Type.LayoutRequest, QEvent.Type.FontChange, QEvent.Type.StyleChange):
            if hasattr(self, "_reflow_timer"):
                self._reflow_timer.start(0)
        return super().event(event)

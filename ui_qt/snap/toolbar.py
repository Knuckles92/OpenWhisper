"""The compact annotate toolbar that rides under the selection.

One painted widget rather than a row of QToolButtons: the app stylesheet
restyles every QToolButton, and here each control needs its own look (tool
icons tinted with the chosen colour, round swatches, a filled Keep). It
never takes keyboard focus, so typing and shortcuts stay with the overlay.
"""

from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QToolTip, QWidget

from ui_qt.snap.annotations import SWATCHES, Tool
from ui_qt.snap.chrome import paint_panel
from ui_qt.snap.icons import paint_icon
from ui_qt.utils.palette import Palette

BUTTON = 34
SWATCH = 26
ICON = 20
PAD = 6
SEPARATOR = 13
RADIUS = 12.0
#: Room around the panel for its drop shadow.
SHADOW = 10

_TOOLS = (
    (Tool.ARROW, "Arrow (A)"),
    (Tool.BOX, "Box (B)"),
    (Tool.PEN, "Pen (P)"),
    (Tool.HIGHLIGHTER, "Highlighter (H)"),
    (Tool.TEXT, "Text (T)"),
    (Tool.STEP, "Step number (N)"),
)


@dataclass(frozen=True)
class _Button:
    kind: str  # "tool", "undo", "redo", "color", "drop" or "keep"
    value: str
    rect: QRect
    tip: str


def _layout() -> tuple[list[_Button], list[int]]:
    """Buttons left to right, and the x of each group separator, inside the panel."""
    buttons: list[_Button] = []
    separators: list[int] = []
    x = SHADOW + PAD
    top = SHADOW + PAD

    def add(kind: str, value: str, tip: str, width: int = BUTTON) -> None:
        nonlocal x
        buttons.append(_Button(kind, value, QRect(x, top, width, BUTTON), tip))
        x += width

    def separate() -> None:
        nonlocal x
        separators.append(x + SEPARATOR // 2)
        x += SEPARATOR

    for tool, tip in _TOOLS:
        add("tool", tool.value, tip)
    separate()
    add("undo", "undo", "Undo (Ctrl+Z)")
    add("redo", "redo", "Redo (Ctrl+Shift+Z)")
    separate()
    for index, swatch in enumerate(SWATCHES):
        add("color", str(index), f"{swatch.name} ({index + 1})", SWATCH)
    separate()
    add("drop", "drop", "Cancel (Esc)")
    add("keep", "keep", "Keep (Enter)")
    return buttons, separators


class SnapToolbar(QWidget):
    """Tools, undo and redo, colours, then Cancel and Keep."""

    tool_chosen = pyqtSignal(str)
    color_chosen = pyqtSignal(int)
    undo_clicked = pyqtSignal()
    redo_clicked = pyqtSignal()
    drop_clicked = pyqtSignal()
    keep_clicked = pyqtSignal()

    def __init__(self, parent: QWidget, palette: Palette) -> None:
        super().__init__(parent)
        self.setObjectName("snapToolbar")
        self._palette = palette
        self._buttons, self._separators = _layout()
        width = self._buttons[-1].rect.right() + 1 + PAD + SHADOW
        self.setFixedSize(width, BUTTON + 2 * PAD + 2 * SHADOW)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self._tool = Tool.ARROW
        self._color = 0
        self._can_undo = False
        self._can_redo = False
        self._hover: _Button | None = None
        self._pressed: _Button | None = None
        self._signals = {
            "undo": self.undo_clicked,
            "redo": self.redo_clicked,
            "drop": self.drop_clicked,
            "keep": self.keep_clicked,
        }

    # ---- state ----

    def panel_rect(self) -> QRect:
        """The visible panel, inside the shadow margin."""
        return self.rect().adjusted(SHADOW, SHADOW, -SHADOW, -SHADOW)

    def panel_size(self) -> QSize:
        return self.panel_rect().size()

    def set_state(self, tool: Tool, color: int, can_undo: bool, can_redo: bool) -> None:
        state = (Tool(tool), color, can_undo, can_redo)
        if state != (self._tool, self._color, self._can_undo, self._can_redo):
            self._tool, self._color, self._can_undo, self._can_redo = state
            self.update()

    def button_rect(self, kind: str, value: str = "") -> QRect:
        """Where a control is, for tests and for anchoring popups; empty if unknown."""
        for button in self._buttons:
            if button.kind == kind and (not value or button.value == value):
                return QRect(button.rect)
        return QRect()

    def _enabled(self, button: _Button) -> bool:
        return {"undo": self._can_undo, "redo": self._can_redo}.get(button.kind, True)

    def _button_at(self, pos: QPoint) -> _Button | None:
        return next(
            (b for b in self._buttons if b.rect.contains(pos) and self._enabled(b)),
            None,
        )

    # ---- input ----

    def mouseMoveEvent(self, event) -> None:
        hover = self._button_at(event.position().toPoint())
        if hover is not self._hover:
            self._hover = hover
            shape = (
                Qt.CursorShape.PointingHandCursor
                if hover
                else Qt.CursorShape.ArrowCursor
            )
            self.setCursor(shape)
            self.update()
        event.accept()

    def leaveEvent(self, event) -> None:
        self._hover = None
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = self._button_at(event.position().toPoint())
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        pressed, self._pressed = self._pressed, None
        if pressed is not None and pressed is self._button_at(
            event.position().toPoint()
        ):
            self._activate(pressed)
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        # A quick second click (two undos) is still a click, not a double-click.
        self.mousePressEvent(event)

    def _activate(self, button: _Button) -> None:
        if button.kind == "tool":
            self.tool_chosen.emit(button.value)
        elif button.kind == "color":
            self.color_chosen.emit(int(button.value))
        else:
            self._signals[button.kind].emit()

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.ToolTip:
            button = self._button_at(event.pos())
            if button is None:
                QToolTip.hideText()
            else:
                QToolTip.showText(event.globalPos(), button.tip, self, button.rect)
            return True
        return super().event(event)

    # ---- painting ----

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        paint_panel(painter, QRectF(self.panel_rect()), RADIUS, self._palette)
        self._paint_separators(painter)
        for button in self._buttons:
            self._paint_button(painter, button)
        painter.end()

    def _paint_separators(self, painter: QPainter) -> None:
        painter.setPen(QPen(self._palette.color("overlay-border"), 1))
        top = SHADOW + PAD + 8
        for x in self._separators:
            painter.drawLine(QPointF(x + 0.5, top), QPointF(x + 0.5, top + BUTTON - 16))

    def _paint_button(self, painter: QPainter, button: _Button) -> None:
        if button.kind == "color":
            self._paint_swatch(painter, button)
            return
        rect = QRectF(button.rect).adjusted(2, 2, -2, -2)
        background, icon = self._button_colors(button)
        if background is not None:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(background)
            painter.drawRoundedRect(rect, 8, 8)
        inset = (rect.width() - ICON) / 2
        paint_icon(
            painter,
            button.value if button.kind == "tool" else button.kind,
            rect.adjusted(inset, inset, -inset, -inset),
            icon,
        )

    def _button_colors(self, button: _Button) -> tuple[QColor | None, QColor]:
        """The fill behind a button (None for none) and its icon colour."""
        text = self._palette.color("overlay-text")
        hovered = button is self._hover
        if button.kind == "keep":
            return self._palette.color("accent-hover" if hovered else "accent"), QColor(
                "#FFFFFF"
            )
        if button.kind == "tool" and button.value == self._tool.value:
            color = QColor(SWATCHES[self._color].color)
            tint = QColor(color)
            tint.setAlpha(46 if hovered else 34)
            return tint, color
        if not self._enabled(button):
            faded = QColor(text)
            faded.setAlphaF(0.32)
            return None, faded
        return (self._palette.color("overlay-rgb", 24) if hovered else None), text

    def _paint_swatch(self, painter: QPainter, button: _Button) -> None:
        center = QRectF(button.rect).center()
        color = QColor(SWATCHES[int(button.value)].color)
        selected = int(button.value) == self._color
        if selected or button is self._hover:
            ring = QColor(self._palette.color("overlay-text"))
            ring.setAlphaF(1.0 if selected else 0.35)
            painter.setPen(QPen(ring, 2.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(center, 11.0, 11.0)
        painter.setPen(QPen(QColor(0, 0, 0, 60), 1.0))
        painter.setBrush(color)
        painter.drawEllipse(center, 8.0, 8.0)

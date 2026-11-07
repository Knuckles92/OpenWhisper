"""The compact annotate toolbar that rides under the selection.

One painted widget rather than a row of QToolButtons: the app stylesheet
restyles every QToolButton, and here each control needs its own look (tool
icons tinted with the chosen colour, round swatches, a filled Keep). It
never takes keyboard focus, so typing and shortcuts stay with the overlay.
The widget is exactly the panel; the overlay paints its shadow, so no
margin around it swallows clicks meant for the screenshot.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from PyQt6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFontMetrics, QPainter, QPen
from PyQt6.QtWidgets import QToolTip, QWidget

from ui_qt.snap.annotations import SWATCHES, Tool
from ui_qt.snap.chrome import chrome_font, paint_panel
from ui_qt.snap.icons import paint_icon
from ui_qt.utils.palette import Palette
from ui_qt.widgets.capture_delay import CaptureDelay

BUTTON = 34
SWATCH = 26
ICON = 20
PAD = 6
SEPARATOR = 13
RADIUS = 12.0
LABEL_PAD = 6
LABEL_GAP = 4

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
    kind: str
    value: str
    rect: QRect
    tip: str
    label: str


def _layout(
    live: bool, label_metrics: QFontMetrics, mark_shortcuts: dict[str, str],
    *, wrap: bool = False,
) -> tuple[list[_Button], list[tuple[int, int]]]:
    """Buttons left to right, and the x of each group separator, inside the panel."""
    buttons: list[_Button] = []
    separators: list[tuple[int, int]] = []
    x = PAD
    top = PAD

    def add(kind: str, value: str, tip: str, width: int = BUTTON, *, label: str = "") -> None:
        nonlocal x
        if label:
            width = ICON + LABEL_GAP + label_metrics.horizontalAdvance(label) + 2 * LABEL_PAD + 4
        buttons.append(_Button(kind, value, QRect(x, top, width, BUTTON), tip, label))
        x += width

    def separate() -> None:
        nonlocal x
        separators.append((x + SEPARATOR // 2, top))
        x += SEPARATOR

    for tool, tip in _TOOLS:
        if live:
            tip += f" · Hold {'Cmd' if sys.platform == 'darwin' else 'Ctrl'} to pin"
        add("tool", tool.value, tip)
    separate()
    if not live:
        add("undo", "undo", "Undo (Ctrl+Z)")
        add("redo", "redo", "Redo (Ctrl+Shift+Z)")
        separate()
    for index, swatch in enumerate(SWATCHES):
        add("color", str(index), f"{swatch.name} ({index + 1})", SWATCH)
    separate()
    add("snap", "snap", "Snap to corners and window edges (S)")
    if live:
        if wrap:
            x, top = PAD, PAD + BUTTON + 4
        add("region", "region", "Select an area for automatic screenshots; close its × to capture the whole screen", label="Region")
        separate()
        add("delay", "delay", "Screenshot delay", max(148, label_metrics.horizontalAdvance("Delay · Instant") + 61))
        separate()
        keep_key = mark_shortcuts.get("keep_marks", "K while drawing")
        clear_key = mark_shortcuts.get("clear", "C while drawing")
        add("keep_marks", "keep_marks", f"Keep marks ({keep_key})", label="Keep marks")
        add("clear", "clear", f"Clear marks ({clear_key})", label="Clear marks")
        add("drop", "drop", "Return to the desktop (Esc)")
    else:
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
    snap_clicked = pyqtSignal()
    keep_marks_clicked = pyqtSignal()
    clear_clicked = pyqtSignal()
    region_clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None, palette: Palette, *, live: bool = False,
                 flags: Qt.WindowType = Qt.WindowType.Widget,
                 mark_shortcuts: dict[str, str] | None = None) -> None:
        super().__init__(parent, flags)
        if live:
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setObjectName("snapToolbar")
        self._palette = palette
        self._label_font = chrome_font(self.font(), 12)
        self._live = live
        self._mark_shortcuts = mark_shortcuts or {}
        self._buttons, self._separators = _layout(
            live, QFontMetrics(self._label_font), self._mark_shortcuts
        )
        self.setFixedSize(self._buttons[-1].rect.right() + 1 + PAD, BUTTON + 2 * PAD)
        self.capture_delay = CaptureDelay(self) if live else None
        if self.capture_delay is not None:
            self.capture_delay.move(self.button_rect("delay").topLeft())
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self._tool = Tool.ARROW
        self._color = 0
        self._can_undo = False
        self._can_redo = False
        self._snap_enabled = False
        self._keep_marks = False
        self._region_active = False
        self._hover: _Button | None = None
        self._pressed: _Button | None = None
        self._signals = {
            "undo": self.undo_clicked,
            "redo": self.redo_clicked,
            "drop": self.drop_clicked,
            "keep": self.keep_clicked,
            "snap": self.snap_clicked,
            "keep_marks": self.keep_marks_clicked,
            "clear": self.clear_clicked,
            "region": self.region_clicked,
        }

    # ---- state ----

    def fit_to_width(self, width: int) -> None:
        buttons, separators = _layout(self._live, QFontMetrics(self._label_font), self._mark_shortcuts)
        if self._live and buttons[-1].rect.right() + 1 + PAD > width:
            buttons, separators = _layout(True, QFontMetrics(self._label_font), self._mark_shortcuts, wrap=True)
        self._buttons, self._separators = buttons, separators
        self._hover = self._pressed = None
        self.setFixedSize(max(b.rect.right() for b in buttons) + 1 + PAD,
                          max(b.rect.bottom() for b in buttons) + 1 + PAD)
        if self.capture_delay is not None:
            self.capture_delay.move(self.button_rect("delay").topLeft())

    def set_state(self, tool: Tool | None, color: int, can_undo: bool, can_redo: bool) -> None:
        state = (tool, color, can_undo, can_redo)
        if state != (self._tool, self._color, self._can_undo, self._can_redo):
            self._tool, self._color, self._can_undo, self._can_redo = state
            self.update()

    def set_snap_enabled(self, enabled: bool) -> None:
        self._snap_enabled = enabled
        self.update()

    def set_keep_marks(self, enabled: bool) -> None:
        self._keep_marks = enabled
        self.update()

    def set_region_active(self, active: bool) -> None:
        self._region_active = active
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
        paint_panel(painter, QRectF(self.rect()), RADIUS, self._palette, shadow=0)
        self._paint_separators(painter)
        for button in self._buttons:
            self._paint_button(painter, button)
        painter.end()

    def _paint_separators(self, painter: QPainter) -> None:
        painter.setPen(QPen(self._palette.color("overlay-border"), 1))
        for x, top in self._separators:
            painter.drawLine(QPointF(x + 0.5, top + 8), QPointF(x + 0.5, top + BUTTON - 8))

    def _paint_button(self, painter: QPainter, button: _Button) -> None:
        if button.kind == "delay":
            return  # The delay chip paints itself and owns its preset picker.
        if button.kind == "color":
            self._paint_swatch(painter, button)
            return
        rect = QRectF(button.rect).adjusted(2, 2, -2, -2)
        background, icon = self._button_colors(button)
        if background is not None:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(background)
            painter.drawRoundedRect(rect, 8, 8)
        if button.label:
            self._paint_labeled_button(painter, button, rect, icon)
            return
        inset = (rect.width() - ICON) / 2
        paint_icon(
            painter,
            button.value if button.kind == "tool" else button.kind,
            rect.adjusted(inset, inset, -inset, -inset),
            icon,
        )

    def _paint_labeled_button(self, painter: QPainter, button: _Button, rect: QRectF,
                              icon: QColor) -> None:
        icon_rect = QRectF(rect.left() + LABEL_PAD, rect.center().y() - ICON / 2, ICON, ICON)
        if button.kind == "keep_marks":
            painter.setPen(QPen(icon, 1.3))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(icon_rect.adjusted(3, 3, -3, -3), 3, 3)
            if self._keep_marks:
                paint_icon(painter, "keep", icon_rect, icon)
        else:
            paint_icon(painter, button.kind, icon_rect, icon)
        painter.setFont(self._label_font)
        painter.setPen(self._palette.color("overlay-text"))
        label_rect = QRectF(rect)
        label_rect.setLeft(icon_rect.right() + LABEL_GAP)
        painter.drawText(label_rect, Qt.AlignmentFlag.AlignVCenter, button.label)

    def _button_colors(self, button: _Button) -> tuple[QColor | None, QColor]:
        """The fill behind a button (None for none) and its icon colour."""
        text = self._palette.color("overlay-text")
        hovered = button is self._hover
        if button.kind == "keep":
            return self._palette.color("accent-hover" if hovered else "accent"), QColor(
                "#FFFFFF"
            )
        if ((button.kind == "snap" and self._snap_enabled)
                or (button.kind == "keep_marks" and self._keep_marks)
                or (button.kind == "region" and self._region_active)):
            return self._palette.color("overlay-rgb", 36), self._palette.color("accent")
        if button.kind == "tool" and button.value == self._tool:
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

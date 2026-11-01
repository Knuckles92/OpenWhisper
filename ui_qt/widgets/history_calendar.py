"""The pieces of the History calendar: month grid, year strip, chips and cards.

The grid paints its day cells itself instead of hosting a widget per day. A
busy month holds hundreds of lines, and painting is what lets the selection
ring glide between days and a new month slide in from the side it came from.
Colours come from the palette at paint time, so a theme change needs only a
repaint, and every painted size follows the UI font scale.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence

from PyQt6.QtCore import (
    QEasingCurve, QEvent, QPoint, QPointF, QPropertyAnimation, QRectF, QSequentialAnimationGroup,
    QSize, Qt, QVariantAnimation, pyqtSignal,
)
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QMenu, QPushButton, QSizePolicy, QStyle,
    QStyleOptionButton, QToolTip, QVBoxLayout, QWidget,
)

from services.format_utils import format_audio_duration
from services.history_calendar import (
    DICTATION, FILE, KIND_LABELS, KIND_PLURALS, KINDS, MEETING, CalendarItem, DaySummary,
    MonthKey, milestone_text, month_key, month_weeks,
)
from ui_qt.utils.font_scale import current_ui_font_scale
from ui_qt.utils.palette import current_palette, token_color
from ui_qt.widgets.wrapped_label import WrappedLabel

#: Palette roles per kind: fills and dots, and type on the surface.
KIND_COLOR = {DICTATION: "accent", FILE: "purple", MEETING: "warning"}
KIND_TEXT = {DICTATION: "accent-soft", FILE: "purple-text", MEETING: "warning-text"}
KIND_RGB = {DICTATION: "accent-rgb", FILE: "purple-rgb", MEETING: "warning-rgb"}

MENU_STYLESHEET = """
    QMenu {
        background-color: rgba(@surface-rgb, 0.95);
        color: @text;
        border: 1px solid rgba(@overlay-rgb, 0.1);
        border-radius: 10px;
        padding: 6px;
    }
    QMenu::item {
        padding: 8px 28px 8px 14px;
        border-radius: 6px;
        font-size: 13px;
    }
    QMenu::item:selected {
        background-color: @accent;
        color: @on-accent;
    }
"""


def _px(size: float) -> int:
    """A designed pixel size at the current UI font scale."""
    return max(1, int(round(size * current_ui_font_scale())))


def _font(base: QFont, size: float, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    font = QFont(base)
    font.setPixelSize(_px(size))
    font.setWeight(weight)
    return font


def clock(when) -> str:
    """``9:02 AM``."""
    return when.strftime("%I:%M %p").lstrip("0")


def plural(count: int, one: str, many: Optional[str] = None) -> str:
    return f"{count:,} {one if count == 1 else (many or one + 's')}"


def kind_breakdown(by_kind) -> str:
    """``4 dictations · 1 file · 1 meeting``."""
    parts = []
    for kind in KINDS:
        count = by_kind.get(kind, 0)
        if count:
            parts.append(plural(count, KIND_LABELS[kind].lower(), KIND_PLURALS[kind]))
    return " · ".join(parts)


def pick_lines(items: Sequence[CalendarItem], capacity: int) -> List[CalendarItem]:
    """The ``capacity`` items a crowded day cell shows, in time order.

    Each kind gets a line before any kind gets a second, so a day of twenty
    dictations and one meeting still shows the meeting.
    """
    if len(items) <= capacity:
        return list(items)
    picked: set = set()
    kinds = set()
    for index, item in enumerate(items):
        if item.kind not in kinds and len(picked) < capacity:
            picked.add(index)
            kinds.add(item.kind)
    for index in range(len(items)):
        if len(picked) >= capacity:
            break
        picked.add(index)
    return [items[index] for index in sorted(picked)]


def day_tooltip(day: date, summary: Optional[DaySummary], milestone: Optional[int]) -> str:
    heading = day.strftime("%A, %B ") + str(day.day)
    if not summary or not summary.count:
        return f"{heading}\nNothing recorded"
    lines = [heading, plural(summary.count, "recording")]
    if summary.seconds >= 1:
        lines[-1] += f" · {format_audio_duration(summary.seconds)} of audio"
    breakdown = kind_breakdown(summary.by_kind)
    if breakdown and len(summary.by_kind) > 1:
        lines.append(breakdown)
    if milestone:
        lines.append(f"✦ {milestone_text(milestone)}")
    return "\n".join(lines)


def _rounded(rect: QRectF, radius: float) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    return path


def _mix(a: QColor, b: QColor, amount: float) -> QColor:
    amount = max(0.0, min(1.0, amount))
    return QColor.fromRgbF(*(x + (y - x) * amount for x, y in zip(
        a.getRgbF(), b.getRgbF(), strict=True)))


def _heat(count: int, peak: int) -> QColor:
    """The picker's tint for ``count`` recordings, brighter as it nears ``peak``.

    Gentler than the grid's days: twelve neighbouring tiles of a steady year
    would otherwise read as one block of blue.
    """
    level = (count / max(1, peak)) ** 1.4
    base, span = (0.07, 0.24) if current_palette().is_dark else (0.05, 0.16)
    return token_color("accent-rgb", int(255 * (base + span * level)))


def paint_chevron(painter: QPainter, center: QPointF, size: float, pointing: str,
                  color: QColor, width: float = 1.6) -> None:
    """A thin open chevron pointing ``left``, ``right``, ``up`` or ``down``."""
    x, y, s, h = center.x(), center.y(), size, size / 2
    points = {
        "left": ((x + h, y - s), (x - h, y), (x + h, y + s)),
        "right": ((x - h, y - s), (x + h, y), (x - h, y + s)),
        "up": ((x - s, y + h), (x, y - h), (x + s, y + h)),
        "down": ((x - s, y - h), (x, y + h), (x + s, y - h)),
    }[pointing]
    path = QPainterPath(QPointF(*points[0]))
    for point in points[1:]:
        path.lineTo(QPointF(*point))
    pen = QPen(color, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.save()
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(path)
    painter.restore()


class MonthGrid(QWidget):
    """One month of days: heat for how much was recorded, a line per recording.

    Emits ``day_selected`` for a click or arrow key (the day may belong to
    a neighbouring month), ``day_activated`` for a double-click or Enter,
    ``month_step`` for Page Up/Down, ``today_requested`` for T or Home, and
    ``picker_requested`` for M.
    """

    day_selected = pyqtSignal(object)
    day_activated = pyqtSignal(object)
    month_step = pyqtSignal(int)
    today_requested = pyqtSignal()
    picker_requested = pyqtSignal()

    GAP = 6
    RADIUS = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("calendarMonthGrid")
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(460, 360)
        self.setAccessibleName("Month calendar")
        today = date.today()
        self._year, self._month = today.year, today.month
        self._first_weekday = calendar.SUNDAY
        self._weeks = month_weeks(self._year, self._month, self._first_weekday)
        self._days: Dict[date, DaySummary] = {}
        self._items: Dict[date, List[CalendarItem]] = {}
        self._milestones: Dict[date, int] = {}
        self._today = today
        self._peak = 1
        self._selected: Optional[date] = None
        self._hover: Optional[date] = None

        self._selection_rect: Optional[QRectF] = None
        self._selection_anim = QVariantAnimation(self)
        self._selection_anim.setDuration(190)
        self._selection_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._selection_anim.valueChanged.connect(self._on_selection_moved)

        self._hover_level = 1.0
        self._hover_anim = QVariantAnimation(self)
        self._hover_anim.setDuration(130)
        self._hover_anim.setStartValue(0.0)
        self._hover_anim.setEndValue(1.0)
        self._hover_anim.valueChanged.connect(self._on_hover_level)

        self._slide_from = None
        self._slide_direction = 0
        self._slide_progress = 1.0
        self._slide_anim = QVariantAnimation(self)
        self._slide_anim.setDuration(280)
        self._slide_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._slide_anim.setStartValue(0.0)
        self._slide_anim.setEndValue(1.0)
        self._slide_anim.valueChanged.connect(self._on_slide)
        self._slide_anim.finished.connect(self._on_slide_finished)

        self._pulse: Optional[float] = None
        self._pulse_anim = QVariantAnimation(self)
        self._pulse_anim.setDuration(1100)
        self._pulse_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._pulse_anim.setStartValue(0.0)
        self._pulse_anim.setEndValue(1.0)
        self._pulse_anim.valueChanged.connect(self._on_pulse)
        self._pulse_anim.finished.connect(self._on_pulse_finished)

    # ---- state ----

    @property
    def month(self) -> MonthKey:
        return self._year, self._month

    @property
    def weeks(self) -> List[List[date]]:
        return self._weeks

    def set_month(self, year: int, month: int, *, days: Dict[date, DaySummary],
                  items: Dict[date, List[CalendarItem]], milestones: Dict[date, int],
                  today: date, first_weekday: int = calendar.SUNDAY,
                  direction: int = 0) -> None:
        """Show a month. A non-zero ``direction`` slides it in (1: from the right)."""
        changed = (year, month) != (self._year, self._month)
        slide = bool(direction) and changed and self.isVisible() and self.width() > 0
        if slide:
            self._slide_from = self.grab(self._grid_rect().toAlignedRect())
            self._slide_direction = 1 if direction > 0 else -1
        self._year, self._month = year, month
        self._first_weekday = first_weekday
        self._weeks = month_weeks(year, month, first_weekday)
        self._today = today
        self._hover = None
        self.set_details(days, items, milestones)
        self._sync_selection(animate=False)
        if slide:
            self._slide_anim.stop()
            self._slide_progress = 0.0
            self._slide_anim.start()
        self.update()

    def set_details(self, days: Dict[date, DaySummary], items: Dict[date, List[CalendarItem]],
                    milestones: Dict[date, int]) -> None:
        """New counts or lines for the month already shown."""
        self._days = dict(days)
        self._items = dict(items)
        self._milestones = dict(milestones)
        self._peak = max((summary.count for summary in self._days.values()), default=1) or 1
        self.update()

    def set_selected(self, day: Optional[date], animate: bool = True) -> None:
        self._selected = day
        self._sync_selection(animate)
        self.update()

    def selected(self) -> Optional[date]:
        return self._selected

    def pulse_today(self) -> None:
        """A ring spreads once from today's date, to show where today is."""
        if self._day_rect(self._today) is None:
            return
        self._pulse_anim.stop()
        self._pulse_anim.start()

    # ---- geometry ----

    def _header_height(self) -> int:
        return _px(22)

    def _grid_rect(self) -> QRectF:
        top = self._header_height() + 6
        return QRectF(0, top, self.width(), max(0, self.height() - top))

    def _cell_rect(self, row: int, column: int) -> QRectF:
        grid = self._grid_rect()
        rows = max(1, len(self._weeks))
        width = (grid.width() - self.GAP * 6) / 7
        height = (grid.height() - self.GAP * (rows - 1)) / rows
        return QRectF(grid.left() + column * (width + self.GAP),
                      grid.top() + row * (height + self.GAP), width, height)

    def _day_rect(self, day: Optional[date]) -> Optional[QRectF]:
        if day is None:
            return None
        for row, week in enumerate(self._weeks):
            if day in week:
                return self._cell_rect(row, week.index(day))
        return None

    def day_at(self, pos: QPointF) -> Optional[date]:
        for row, week in enumerate(self._weeks):
            for column, day in enumerate(week):
                if self._cell_rect(row, column).contains(pos):
                    return day
        return None

    def _sync_selection(self, animate: bool) -> None:
        target = self._day_rect(self._selected)
        self._selection_anim.stop()
        if target is None:
            self._selection_rect = None
        elif animate and self._selection_rect is not None and self.isVisible():
            self._selection_anim.setStartValue(self._selection_rect)
            self._selection_anim.setEndValue(target)
            self._selection_anim.start()
        else:
            self._selection_rect = target

    # ---- animation ticks ----

    def _on_selection_moved(self, value) -> None:
        self._selection_rect = QRectF(value)
        self.update()

    def _on_hover_level(self, value) -> None:
        self._hover_level = float(value)
        self.update()

    def _on_slide(self, value) -> None:
        self._slide_progress = float(value)
        self.update()

    def _on_slide_finished(self) -> None:
        self._slide_from = None
        self._slide_progress = 1.0
        self.update()

    def _on_pulse(self, value) -> None:
        self._pulse = float(value)
        self.update()

    def _on_pulse_finished(self) -> None:
        self._pulse = None
        self.update()

    # ---- painting ----

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        self._paint_weekdays(painter)
        grid = self._grid_rect()
        painter.save()
        painter.setClipRect(grid.adjusted(-2, -2, 2, 2))
        if self._slide_from is not None and self._slide_progress < 1.0:
            progress = self._slide_progress
            shift = grid.width() * progress
            painter.save()
            painter.setOpacity(1.0 - progress)
            painter.drawPixmap(QPointF(grid.left() - self._slide_direction * shift, grid.top()),
                               self._slide_from)
            painter.restore()
            painter.save()
            painter.translate(self._slide_direction * (grid.width() - shift), 0)
            painter.setOpacity(0.3 + 0.7 * progress)
            self._paint_cells(painter)
            painter.restore()
        else:
            self._paint_cells(painter)
            self._paint_selection(painter)
            self._paint_pulse(painter)
        painter.restore()

    def _paint_weekdays(self, painter: QPainter) -> None:
        painter.setFont(_font(self.font(), 11, QFont.Weight.DemiBold))
        painter.setPen(token_color("text-secondary"))
        for column in range(7):
            weekday = (self._first_weekday + column) % 7
            cell = self._cell_rect(0, column)
            label = QRectF(cell.left() + 8, 0, cell.width() - 8, self._header_height())
            painter.drawText(label, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                             calendar.day_abbr[weekday])

    def _paint_cells(self, painter: QPainter) -> None:
        for row, week in enumerate(self._weeks):
            for column, day in enumerate(week):
                self._paint_cell(painter, self._cell_rect(row, column), day)

    def _number_rect(self, rect: QRectF) -> QRectF:
        size = _px(22)
        return QRectF(rect.left() + 5, rect.top() + 5, size, size)

    def _paint_cell(self, painter: QPainter, rect: QRectF, day: date) -> None:
        in_month = day.month == self._month
        summary = self._days.get(day) if in_month else None
        path = QPainterPath()
        path.addRoundedRect(rect, self.RADIUS, self.RADIUS)
        painter.fillPath(path, token_color("surface-rgb", int(255 * (0.55 if in_month else 0.14))))
        if summary and summary.count:
            # Busier days glow brighter; light surfaces need less to read as blue.
            level = (summary.count / self._peak) ** 0.8
            span = 0.44 if current_palette().is_dark else 0.30
            painter.fillPath(path, token_color("accent-rgb", int(255 * (0.06 + span * level))))
        if day == self._hover:
            painter.fillPath(path, token_color("overlay-rgb", int(255 * 0.05 * self._hover_level)))
            painter.setPen(QPen(token_color("accent-rgb", int(255 * 0.5 * self._hover_level)), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), self.RADIUS, self.RADIUS)

        number = self._number_rect(rect)
        painter.setFont(_font(self.font(), 12.5, QFont.Weight.DemiBold))
        if day == self._today:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(token_color("accent"))
            painter.drawEllipse(number)
            painter.setPen(token_color("on-accent"))
        else:
            painter.setPen(token_color("text" if in_month else "text-muted"))
        painter.drawText(number, int(Qt.AlignmentFlag.AlignCenter), str(day.day))

        milestone = self._milestones.get(day) if in_month else None
        if milestone:
            painter.setFont(_font(self.font(), 12, QFont.Weight.Bold))
            painter.setPen(token_color("warning"))
            star = QRectF(number.right() + 2, number.top(), _px(16), number.height())
            painter.drawText(star, int(Qt.AlignmentFlag.AlignCenter), "✦")

        if not summary:
            return
        painter.setFont(_font(self.font(), 10.5, QFont.Weight.DemiBold))
        painter.setPen(token_color("text-secondary-strong"))
        count_rect = QRectF(rect.right() - _px(40) - 8, number.top(), _px(40), number.height())
        painter.drawText(count_rect, int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                         f"{summary.count:,}")
        self._paint_lines(painter, rect, number.bottom() + 3, self._items.get(day) or [])
        self._paint_kind_bar(painter, rect, summary)

    def _paint_lines(self, painter: QPainter, rect: QRectF, top: float,
                     items: Sequence[CalendarItem]) -> None:
        if not items:
            return
        line_height = _px(16)
        bottom = rect.bottom() - 9
        capacity = int((bottom - top) // line_height)
        if capacity <= 0:
            return
        left = rect.left() + 9
        text_left = left + _px(10)
        width = max(0.0, rect.right() - 8 - text_left)
        if width < _px(46):
            self._paint_dots(painter, QRectF(left, top + 2, rect.right() - 7 - left, bottom - top - 2),
                             items)
            return
        overflow = len(items) > capacity
        shown = pick_lines(items, capacity - 1 if overflow else capacity)
        font = _font(self.font(), 11)
        metrics = QFontMetrics(font)
        painter.setFont(font)
        for index, item in enumerate(shown):
            y = top + index * line_height
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(token_color(KIND_COLOR[item.kind]))
            radius = max(2.5, _px(3))
            painter.drawEllipse(QPointF(left + radius, y + line_height / 2), radius, radius)
            painter.setPen(token_color("text-body"))
            text = metrics.elidedText(" ".join(item.label().split()), Qt.TextElideMode.ElideRight,
                                      int(width))
            painter.drawText(QRectF(text_left, y, width, line_height),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)
        if overflow:
            y = top + len(shown) * line_height
            painter.setPen(token_color("text-secondary"))
            painter.setFont(_font(self.font(), 10.5, QFont.Weight.DemiBold))
            painter.drawText(QRectF(text_left, y, width, line_height),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                             f"+{len(items) - len(shown)} more")

    def _paint_dots(self, painter: QPainter, area: QRectF, items: Sequence[CalendarItem]) -> None:
        """A narrow cell's lines: a dot per recording in its kind's colour, rows as room allows."""
        radius = max(2.5, _px(3))
        step = radius * 2 + 3
        columns = max(1, int((area.width() + 3) // step))
        rows = max(1, int((area.height() + 3) // step))
        room = columns * rows
        shown = pick_lines(items, room - 1 if len(items) > room else room)
        painter.setPen(Qt.PenStyle.NoPen)
        for index, item in enumerate(shown):
            row, column = divmod(index, columns)
            painter.setBrush(token_color(KIND_COLOR[item.kind]))
            painter.drawEllipse(QPointF(area.left() + radius + column * step,
                                        area.top() + radius + row * step), radius, radius)
        if len(items) > room:
            row, column = divmod(len(shown), columns)
            painter.setPen(token_color("text-secondary"))
            painter.setFont(_font(self.font(), 9, QFont.Weight.Bold))
            painter.drawText(QRectF(area.left() + column * step - 1, area.top() + row * step - 2,
                                    step + 2, step + 2), int(Qt.AlignmentFlag.AlignCenter), "+")

    def _paint_kind_bar(self, painter: QPainter, rect: QRectF, summary: DaySummary) -> None:
        """A thin bar along the bottom: how the day splits between kinds."""
        bar = QRectF(rect.left() + 9, rect.bottom() - 7, rect.width() - 18, 3)
        clip = QPainterPath()
        clip.addRoundedRect(bar, 1.5, 1.5)
        painter.save()
        painter.setClipPath(clip, Qt.ClipOperation.IntersectClip)
        x = bar.left()
        for kind in KINDS:
            count = summary.by_kind.get(kind, 0)
            if not count:
                continue
            width = bar.width() * count / summary.count
            painter.fillRect(QRectF(x, bar.top(), width, bar.height()), token_color(KIND_COLOR[kind]))
            x += width
        painter.restore()

    def _paint_selection(self, painter: QPainter) -> None:
        rect = self._selection_rect
        if rect is None:
            return
        inner = rect.adjusted(1, 1, -1, -1)
        painter.setBrush(token_color("accent-rgb", 22))
        painter.setPen(QPen(token_color("accent"), 2))
        painter.drawRoundedRect(inner, self.RADIUS - 1, self.RADIUS - 1)

    def _paint_pulse(self, painter: QPainter) -> None:
        if self._pulse is None:
            return
        rect = self._day_rect(self._today)
        if rect is None:
            return
        number = self._number_rect(rect)
        radius = number.width() / 2 + _px(20) * self._pulse
        color = token_color("accent")
        color.setAlphaF(0.6 * (1.0 - self._pulse))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(color, 2))
        painter.drawEllipse(number.center(), radius, radius)

    # ---- input ----

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.ToolTip:
            day = self.day_at(QPointF(event.pos()))
            if day is None:
                QToolTip.hideText()
            elif day.month != self._month:
                QToolTip.showText(event.globalPos(), day.strftime("Go to %B %Y"), self)
            else:
                QToolTip.showText(event.globalPos(), day_tooltip(
                    day, self._days.get(day), self._milestones.get(day)
                ), self)
            return True
        return super().event(event)

    def mouseMoveEvent(self, event) -> None:
        day = self.day_at(event.position())
        if day != self._hover:
            self._hover = day
            self._hover_anim.stop()
            self._hover_level = 0.0
            if day is not None:
                self._hover_anim.start()
            self.setCursor(Qt.CursorShape.PointingHandCursor if day else Qt.CursorShape.ArrowCursor)
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = None
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            day = self.day_at(event.position())
            if day is not None:
                self.day_selected.emit(day)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            day = self.day_at(event.position())
            if day is not None:
                self.day_activated.emit(day)
        super().mouseDoubleClickEvent(event)

    _STEPS = {Qt.Key.Key_Left: -1, Qt.Key.Key_Right: 1, Qt.Key.Key_Up: -7, Qt.Key.Key_Down: 7}

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key in self._STEPS:
            base = self._selected or self._today
            self.day_selected.emit(base + timedelta(days=self._STEPS[key]))
        elif key in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            self.month_step.emit(-1 if key == Qt.Key.Key_PageUp else 1)
        elif key in (Qt.Key.Key_Home, Qt.Key.Key_T):
            self.today_requested.emit()
        elif key == Qt.Key.Key_M:
            self.picker_requested.emit()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self._selected is not None:
            self.day_activated.emit(self._selected)
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_selection(animate=False)


class YearStrip(QWidget):
    """A bar per month for the year around the one shown; click one to go there.

    Bars grow in the first time counts arrive and ease to new heights when a
    filter or the window of months changes.
    """

    month_clicked = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("calendarYearStrip")
        self.setMouseTracking(True)
        self.setFixedHeight(_px(50))
        self.setMinimumWidth(12 * 20)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._months: List[MonthKey] = []
        self._counts: Dict[MonthKey, int] = {}
        self._current: Optional[MonthKey] = None
        self._today: Optional[MonthKey] = None
        self._from: List[float] = []
        self._target: List[float] = []
        self._progress = 1.0
        self._hover: Optional[int] = None
        self._grow = QVariantAnimation(self)
        self._grow.setDuration(520)
        self._grow.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._grow.setStartValue(0.0)
        self._grow.setEndValue(1.0)
        self._grow.valueChanged.connect(self._on_grow)

    def sizeHint(self) -> QSize:
        return QSize(12 * _px(24), _px(50))

    def set_data(self, months: Sequence[MonthKey], counts: Dict[MonthKey, int],
                 current: MonthKey, today: MonthKey) -> None:
        months = list(months)
        peak = max((counts.get(key, 0) for key in months), default=0) or 1
        target = [counts.get(key, 0) / peak for key in months]
        shown = self._heights()
        if not self._target or len(shown) != len(target):
            shown = [0.0] * len(target)
        self._months, self._counts, self._current, self._today = months, dict(counts), current, today
        if target != self._target:
            self._from, self._target = shown, target
            self._grow.stop()
            self._progress = 0.0
            self._grow.start()
        self.update()

    def _heights(self) -> List[float]:
        if not self._from or len(self._from) != len(self._target):
            return list(self._target)
        return [a + (b - a) * self._progress for a, b in zip(self._from, self._target, strict=True)]

    def _on_grow(self, value) -> None:
        self._progress = float(value)
        self.update()

    def _slot(self, index: int) -> QRectF:
        width = self.width() / max(1, len(self._months))
        return QRectF(index * width, 0, width, self.height())

    def _index_at(self, pos: QPointF) -> Optional[int]:
        for index in range(len(self._months)):
            if self._slot(index).contains(pos):
                return index
        return None

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        label_height = _px(15)
        chart_bottom = self.height() - label_height - 3
        heights = self._heights()
        painter.setFont(_font(self.font(), 10, QFont.Weight.DemiBold))
        for index, key in enumerate(self._months):
            slot = self._slot(index)
            bar_width = max(6.0, slot.width() * 0.56)
            x = slot.center().x() - bar_width / 2
            level = heights[index] if index < len(heights) else 0.0
            count = self._counts.get(key, 0)
            height = max(2.0, (chart_bottom - 2) * level) if count else 2.0
            bar = QRectF(x, chart_bottom - height, bar_width, height)
            if key == self._current:
                color = token_color("accent")
            elif index == self._hover:
                color = token_color("accent-soft")
            elif count:
                color = token_color("accent-rgb", 105)
            else:
                color = token_color("overlay-rgb", 30)
            path = QPainterPath()
            path.addRoundedRect(bar, min(3.0, bar_width / 2), min(3.0, height / 2))
            painter.fillPath(path, color)
            if key == self._current:
                painter.setPen(token_color("text"))
            elif key == self._today:
                painter.setPen(token_color("accent-soft"))
            else:
                painter.setPen(token_color("text-secondary"))
            label = calendar.month_abbr[key[1]][0]
            painter.drawText(QRectF(slot.left(), chart_bottom + 3, slot.width(), label_height),
                             int(Qt.AlignmentFlag.AlignCenter), label)

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.ToolTip:
            index = self._index_at(QPointF(event.pos()))
            if index is None:
                QToolTip.hideText()
            else:
                year, month = self._months[index]
                count = self._counts.get((year, month), 0)
                QToolTip.showText(event.globalPos(),
                                  f"{calendar.month_name[month]} {year} · "
                                  + (plural(count, "recording") if count else "nothing recorded"),
                                  self)
            return True
        return super().event(event)

    def mouseMoveEvent(self, event) -> None:
        index = self._index_at(event.position())
        if index != self._hover:
            self._hover = index
            self.setCursor(Qt.CursorShape.PointingHandCursor if index is not None
                           else Qt.CursorShape.ArrowCursor)
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = None
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        index = self._index_at(event.position())
        if event.button() == Qt.MouseButton.LeftButton and index is not None:
            self.month_clicked.emit(self._months[index])
        super().mousePressEvent(event)


class MonthTitleButton(QPushButton):
    """The month's name, which opens the month picker.

    It reads as a heading until the pointer finds it: a faint chevron says it
    opens something, and a soft pill eases in on hover and stays while the
    picker is open.

    Given a short form with ``set_title``, it shows that when the full one
    doesn't fit, so a long month name ("September") can't hold a narrow
    page wider than its window.
    """

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setObjectName("calendarMonthTitle")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._full = self._short = ""
        self._open = False
        self._lift = 0.0
        self._lift_anim = QVariantAnimation(self)
        self._lift_anim.setDuration(150)
        self._lift_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._lift_anim.valueChanged.connect(self._on_lift)

    def set_title(self, full: str, short: str) -> None:
        self._full, self._short = full, short
        self.setAccessibleName(full)
        # A button's policy floors it at its size hint; this one may shrink to the short form.
        self.setSizePolicy(QSizePolicy.Policy.Preferred, self.sizePolicy().verticalPolicy())
        self.updateGeometry()
        self._fit_title()

    def _size_for(self, text: str) -> QSize:
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = text
        metrics = self.fontMetrics()
        contents = QSize(metrics.horizontalAdvance(text), metrics.height())
        return self.style().sizeFromContents(QStyle.ContentsType.CT_PushButton, option, contents, self)

    # The hints come from the stored titles, not the one showing, so swapping
    # them never changes what the layout asks for.
    def sizeHint(self) -> QSize:
        return self._size_for(self._full) if self._full else super().sizeHint()

    def minimumSizeHint(self) -> QSize:
        return self._size_for(self._short) if self._short else super().minimumSizeHint()

    def _fit_title(self) -> None:
        if self._full:
            fits = self.width() >= self._size_for(self._full).width()
            self.setText(self._full if fits else self._short)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_title()

    def set_open(self, is_open: bool) -> None:
        self._open = is_open
        self._lift_to(1.0 if is_open or self.underMouse() else 0.0)
        self.update()

    def _lift_to(self, value: float) -> None:
        self._lift_anim.stop()
        self._lift_anim.setStartValue(self._lift)
        self._lift_anim.setEndValue(value)
        self._lift_anim.start()

    def _on_lift(self, value) -> None:
        self._lift = float(value)
        self.update()

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        self._lift_to(1.0)

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        if not self._open:
            self._lift_to(0.0)

    def paintEvent(self, event) -> None:
        if self._lift > 0.0:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.fillPath(_rounded(QRectF(self.rect()), 10),
                             token_color("overlay-rgb", int(255 * 0.06 * self._lift)))
            painter.end()
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = _mix(token_color("text-muted"), token_color("accent-soft"), self._lift)
        # Placed after the text, not from the right edge: the stylesheet's padding
        # doesn't grow with the UI font scale, but the title does.
        size = _px(4.5)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        contents = self.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, self)
        text_end = contents.left() + self.fontMetrics().horizontalAdvance(self.text())
        x = min(text_end + _px(8) + size, self.width() - size - 4)
        paint_chevron(painter, QPointF(x, self.height() / 2 + 1), size,
                      "up" if self._open else "down", color, 2.0)


class MonthPicker(QWidget):
    """A popup for leaping to any month: a year's months, tinted by how much each holds.

    The year between the arrows opens the years themselves, newest first, each
    with a bar per month, for leaping further. Arrow keys move, Page Up and
    Page Down (or the wheel) change the year, Y switches between months and
    years, Enter picks, T picks this month, and Escape steps back out.
    Picking a month emits ``month_chosen``; ``closed`` follows every close.
    """

    month_chosen = pyqtSignal(object)
    closed = pyqtSignal()

    MONTHS, YEARS = "months", "years"
    #: Years on one page of the years view; fewer share at least this many rows' height.
    YEAR_PAGE = 4
    YEAR_ROWS_MIN = 3
    #: Room around the panel for its painted shadow; it falls a little lower.
    SHADOW = 16
    DROP = 5
    RADIUS = 14

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.setObjectName("calendarMonthPicker")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Choose a month")
        today = month_key(date.today())
        self._anchor: Optional[QWidget] = None
        self._counts: Dict[MonthKey, int] = {}
        self._current: MonthKey = today
        self._today: MonthKey = today
        self._mode = self.MONTHS
        self._year = today[0]
        #: The newest year on the years page shown.
        self._page_end = today[0]
        #: The tile under the pointer or the arrow keys: a month index or a page row.
        self._focus: Optional[int] = None
        self._hover = None
        self._pressed = None
        self._wheel = 0

        self._shown = 1.0
        self._open_anim = QVariantAnimation(self)
        self._open_anim.setDuration(170)
        self._open_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._open_anim.setStartValue(0.0)
        self._open_anim.setEndValue(1.0)
        self._open_anim.valueChanged.connect(self._on_shown)

        #: The tiles as they were, sliding or zooming away under the new ones.
        self._change_from = None
        #: -1 or 1: the new tiles come from that side; 2 zooms in, -2 out.
        self._change_kind = 0
        self._change = 1.0
        self._change_anim = QVariantAnimation(self)
        self._change_anim.setDuration(240)
        self._change_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._change_anim.setStartValue(0.0)
        self._change_anim.setEndValue(1.0)
        self._change_anim.valueChanged.connect(self._on_change)
        self._change_anim.finished.connect(self._on_change_finished)

    # ---- state ----

    def set_data(self, counts: Dict[MonthKey, int], current: MonthKey, today: MonthKey) -> None:
        """Recordings per month, the month the calendar shows, and this month."""
        self._counts = {key: count for key, count in counts.items() if count}
        self._current = (int(current[0]), int(current[1]))
        self._today = (int(today[0]), int(today[1]))
        self.update()

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def year(self) -> int:
        return self._year

    @property
    def first_year(self) -> int:
        return min([key[0] for key in self._counts] + [self._current[0], self._today[0]])

    @property
    def last_year(self) -> int:
        return max(self._current[0], self._today[0])

    def page_years(self) -> List[int]:
        """The years on the page shown, newest first."""
        oldest = max(self.first_year, self._page_end - self.YEAR_PAGE + 1)
        return list(range(self._page_end, oldest - 1, -1))

    def _many_years(self) -> bool:
        return self.first_year < self.last_year

    def _year_total(self, year: int) -> int:
        return sum(self._counts.get((year, month), 0) for month in range(1, 13))

    def _tile_count(self) -> int:
        return 12 if self._mode == self.MONTHS else len(self.page_years())

    def _can_step(self, step: int) -> bool:
        if self._mode == self.MONTHS:
            return self.first_year <= self._year + step <= self.last_year
        if step < 0:
            return self._page_end - self.YEAR_PAGE >= self.first_year
        return self._page_end + self.YEAR_PAGE <= self.last_year

    # ---- opening ----

    def popup(self, anchor: QWidget) -> None:
        """Open under ``anchor`` on the year shown, with its month in focus."""
        self._anchor = anchor
        # Set again only when a press on the anchor closes it (see mousePressEvent).
        self.setAttribute(Qt.WidgetAttribute.WA_NoMouseReplay, False)
        self._mode = self.MONTHS
        self._year = self._current[0]
        self._focus = self._current[1] - 1
        self._hover = self._pressed = None
        self._change_anim.stop()
        self._change_from, self._change = None, 1.0
        self.setFixedSize(self.sizeHint())
        self.move(self._place_by(anchor))
        self._shown = 0.0
        self.show()
        self.setFocus()
        self._open_anim.stop()
        self._open_anim.start()

    def _place_by(self, anchor: QWidget) -> QPoint:
        """Below the anchor, or above it when the screen ends first; never off the side."""
        top_room = self.SHADOW - self.DROP
        bottom_room = self.SHADOW + self.DROP
        gap = _px(4)
        below = anchor.mapToGlobal(QPoint(0, anchor.height() + gap))
        x, y = below.x() - self.SHADOW, below.y() - top_room
        screen = anchor.screen()
        if screen is not None:
            room = screen.availableGeometry()
            x = max(room.left() - self.SHADOW, min(x, room.right() + 1 + self.SHADOW - self.width()))
            if y + self.height() - bottom_room > room.bottom():
                y = anchor.mapToGlobal(QPoint(0, 0)).y() - gap - self.height() + bottom_room
        return QPoint(x, y)

    def _choose(self, key: MonthKey) -> None:
        self.hide()
        self.month_chosen.emit(key)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._open_anim.stop()
        self._change_anim.stop()
        self._change_from, self._change, self._shown = None, 1.0, 1.0
        self.closed.emit()

    # ---- moving about ----

    def step(self, step: int) -> bool:
        """Back (-1) or on (1) a year, or a page of years. False at either end."""
        if not self._can_step(step):
            return False
        self._begin_change(step)
        if self._mode == self.MONTHS:
            self._year += step
        else:
            self._page_end += step * self.YEAR_PAGE
            self._focus = min(self._focus or 0, self._tile_count() - 1)
        self.update()
        return True

    def toggle_years(self) -> None:
        """Between a year's months and every year."""
        if self._mode == self.MONTHS:
            if not self._many_years():
                return
            self._begin_change(-2)
            self._mode = self.YEARS
            # Pages count back from the newest year, so the newest always leads.
            self._page_end = self.last_year - (self.last_year - self._year) // self.YEAR_PAGE * self.YEAR_PAGE
            self._focus = self.page_years().index(self._year)
        else:
            self._open_year(self._year)
        self.update()

    def _open_year(self, year: int) -> None:
        self._begin_change(2)
        self._mode = self.MONTHS
        self._year = year
        if year == self._current[0]:
            self._focus = self._current[1] - 1
        else:
            busy = [month for month in range(1, 13) if self._counts.get((year, month))]
            self._focus = busy[-1] - 1 if busy else 0
        self.update()

    def pick(self, index: int) -> None:
        """What a click on a tile does: choose its month, or open its year."""
        if self._mode == self.MONTHS:
            self._choose((self._year, index + 1))
        else:
            years = self.page_years()
            if 0 <= index < len(years):
                self._open_year(years[index])

    def _move_focus(self, step: int) -> None:
        count = self._tile_count()
        focus = self._focus if self._focus is not None else 0
        target = focus + step
        if 0 <= target < count:
            self._focus = target
        elif self._mode == self.MONTHS:
            # Left and right run on through the year's ends; up and down stop there.
            if abs(step) == 1 and self.step(step):
                self._focus = target % 12
        else:
            # Newest first, so past the bottom row lie older years.
            older = target >= count
            if self.step(-1 if older else 1):
                self._focus = 0 if older else self._tile_count() - 1
        self.update()

    def _act(self, target) -> None:
        kind, index = target
        if kind == "prev":
            self.step(-1)
        elif kind == "next":
            self.step(1)
        elif kind == "title":
            self.toggle_years()
        elif kind == "today":
            self._choose(self._today)
        elif kind == "tile":
            self.pick(index)

    def _begin_change(self, kind: int) -> None:
        if not self.isVisible():
            return
        self._change_anim.stop()
        self._change, self._shown = 1.0, 1.0
        self._change_from = self.grab(self._tiles_rect().adjusted(-3, -3, 3, 3).toAlignedRect())
        self._change_kind = kind
        self._change = 0.0
        self._change_anim.start()

    def _on_shown(self, value) -> None:
        self._shown = float(value)
        self.update()

    def _on_change(self, value) -> None:
        self._change = float(value)
        self.update()

    def _on_change_finished(self) -> None:
        self._change_from, self._change = None, 1.0
        self.update()

    # ---- geometry ----

    def _pad(self) -> int:
        return _px(10)

    def _gap(self) -> int:
        return _px(6)

    def _tiles_size(self) -> QSize:
        gap = self._gap()
        return QSize(3 * _px(78) + 2 * gap, 4 * _px(52) + 3 * gap)

    def sizeHint(self) -> QSize:
        tiles, pad = self._tiles_size(), self._pad()
        return QSize(tiles.width() + 2 * pad + 2 * self.SHADOW,
                     pad + _px(34) + self._gap() + tiles.height() + _px(4) + _px(28) + _px(6)
                     + 2 * self.SHADOW)

    def _panel(self) -> QRectF:
        return QRectF(self.SHADOW, self.SHADOW - self.DROP,
                      self.width() - 2 * self.SHADOW, self.height() - 2 * self.SHADOW)

    def _header_rect(self) -> QRectF:
        panel, pad = self._panel(), self._pad()
        return QRectF(panel.left() + pad, panel.top() + pad, panel.width() - 2 * pad, _px(34))

    def _prev_rect(self) -> QRectF:
        header = self._header_rect()
        return QRectF(header.left(), header.top(), header.height(), header.height())

    def _next_rect(self) -> QRectF:
        header = self._header_rect()
        return QRectF(header.right() - header.height(), header.top(), header.height(), header.height())

    def _title_text(self) -> str:
        if self._mode == self.MONTHS:
            return str(self._year)
        years = self.page_years()
        return f"{years[-1]} – {years[0]}" if len(years) > 1 else str(years[0])

    def _title_font(self) -> QFont:
        return _font(self.font(), 14.5, QFont.Weight.Bold)

    def _title_rect(self) -> QRectF:
        header = self._header_rect()
        width = QFontMetrics(self._title_font()).horizontalAdvance(self._title_text()) + _px(20)
        if self._many_years():
            width += _px(14)
        return QRectF(header.center().x() - width / 2, header.top() + _px(3), width,
                      header.height() - _px(6))

    def _tiles_rect(self) -> QRectF:
        header, tiles = self._header_rect(), self._tiles_size()
        return QRectF(header.left(), header.bottom() + self._gap(), tiles.width(), tiles.height())

    def _tile_rect(self, index: int) -> QRectF:
        tiles, gap = self._tiles_rect(), self._gap()
        if self._mode == self.MONTHS:
            row, column = divmod(index, 3)
            width, height = (tiles.width() - 2 * gap) / 3, (tiles.height() - 3 * gap) / 4
            return QRectF(tiles.left() + column * (width + gap), tiles.top() + row * (height + gap),
                          width, height)
        rows = max(self.YEAR_ROWS_MIN, len(self.page_years()))
        height = (tiles.height() - (rows - 1) * gap) / rows
        return QRectF(tiles.left(), tiles.top() + index * (height + gap), tiles.width(), height)

    def _footer_rect(self) -> QRectF:
        tiles = self._tiles_rect()
        return QRectF(tiles.left(), tiles.bottom() + _px(4), tiles.width(), _px(28))

    def _today_rect(self) -> QRectF:
        footer = self._footer_rect()
        width = QFontMetrics(self._footer_font()).horizontalAdvance("This month") + _px(16)
        return QRectF(footer.left() - _px(4), footer.top() + _px(2), width, footer.height() - _px(4))

    def _footer_font(self) -> QFont:
        return _font(self.font(), 11.5, QFont.Weight.DemiBold)

    def tile_center(self, index: int) -> QPoint:
        return self._tile_rect(index).center().toPoint()

    def _target_at(self, pos: QPointF):
        if self._prev_rect().contains(pos):
            return ("prev", 0)
        if self._next_rect().contains(pos):
            return ("next", 0)
        if self._title_rect().contains(pos) and (self._many_years() or self._mode == self.YEARS):
            return ("title", 0)
        if self._today_rect().contains(pos):
            return ("today", 0)
        for index in range(self._tile_count()):
            if self._tile_rect(index).contains(pos):
                return ("tile", index)
        return None

    # ---- painting ----

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        # Opening: it fades in while settling down the last few pixels.
        painter.setOpacity(self._shown)
        painter.translate(0, -(1.0 - self._shown) * _px(6))
        panel = self._panel()
        self._paint_shadow(painter, panel)
        painter.fillPath(_rounded(panel, self.RADIUS), token_color("surface"))
        painter.setPen(QPen(token_color("overlay-rgb", 26), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(panel.adjusted(0.5, 0.5, -0.5, -0.5), self.RADIUS, self.RADIUS)
        self._paint_header(painter)
        self._paint_tiles_area(painter)
        self._paint_footer(painter)

    def _paint_shadow(self, painter: QPainter, panel: QRectF) -> None:
        strength = 22 if current_palette().is_dark else 9
        layers = 10
        for layer in range(layers, 0, -1):
            spread = layer * (self.SHADOW - 2) / layers
            alpha = int(strength * (1.0 - layer / (layers + 1)) ** 2)
            rect = panel.adjusted(-spread, -spread + self.DROP, spread, spread + self.DROP / 2)
            painter.fillPath(_rounded(rect, self.RADIUS + spread), QColor(0, 0, 0, alpha))

    def _paint_header(self, painter: QPainter) -> None:
        for target, rect, pointing in ((("prev", 0), self._prev_rect(), "left"),
                                       (("next", 0), self._next_rect(), "right")):
            enabled = self._can_step(-1 if pointing == "left" else 1)
            if enabled and self._hover == target:
                painter.fillPath(_rounded(rect.adjusted(3, 3, -3, -3), rect.height() / 2 - 3),
                                 token_color("overlay-rgb", 22))
            color = token_color(("accent-soft" if self._hover == target else "text-secondary")
                                if enabled else "text-faint")
            paint_chevron(painter, rect.center(), _px(4.5), pointing, color, 1.8)

        title = self._title_rect()
        clickable = self._many_years() or self._mode == self.YEARS
        hovered = clickable and self._hover == ("title", 0)
        if hovered:
            painter.fillPath(_rounded(title, title.height() / 2), token_color("overlay-rgb", 20))
        painter.setFont(self._title_font())
        painter.setPen(token_color("accent-soft" if hovered else "text-heading"))
        text_rect = title.adjusted(_px(10), 0, -(_px(10) + (_px(14) if self._many_years() else 0)), 0)
        painter.drawText(text_rect, int(Qt.AlignmentFlag.AlignCenter), self._title_text())
        if self._many_years():
            paint_chevron(painter, QPointF(title.right() - _px(14), title.center().y() + 1), _px(3.5),
                          "up" if self._mode == self.YEARS else "down",
                          token_color("accent-soft" if hovered else "text-muted"), 1.6)

    def _paint_tiles_area(self, painter: QPainter) -> None:
        area = self._tiles_rect()
        painter.save()
        painter.setClipRect(area.adjusted(-3, -3, 3, 3))
        if self._change_from is None or self._change >= 1.0:
            self._paint_tiles(painter)
            painter.restore()
            return
        progress, kind = self._change, self._change_kind
        opacity = painter.opacity()
        origin = area.adjusted(-3, -3, 3, 3).topLeft()

        painter.save()
        painter.setOpacity(opacity * (1.0 - progress))
        if kind in (-1, 1):
            painter.translate(-kind * area.width() * 0.3 * progress, 0)
        else:
            self._scale_about(painter, area.center(), 1.0 + (0.08 if kind > 0 else -0.08) * progress)
        painter.drawPixmap(origin, self._change_from)
        painter.restore()

        painter.save()
        painter.setOpacity(opacity * progress)
        if kind in (-1, 1):
            painter.translate(kind * area.width() * 0.3 * (1.0 - progress), 0)
        else:
            self._scale_about(painter, area.center(),
                              1.0 - (0.08 if kind > 0 else -0.08) * (1.0 - progress))
        self._paint_tiles(painter)
        painter.restore()
        painter.restore()

    @staticmethod
    def _scale_about(painter: QPainter, center: QPointF, scale: float) -> None:
        painter.translate(center)
        painter.scale(scale, scale)
        painter.translate(-center)

    def _paint_tiles(self, painter: QPainter) -> None:
        if self._mode == self.MONTHS:
            keys = [(self._year, month) for month in range(1, 13)]
            peak = max((self._counts.get(key, 0) for key in keys), default=0)
            for index, key in enumerate(keys):
                self._paint_month(painter, self._tile_rect(index), key, peak, index == self._focus)
            return
        years = self.page_years()
        peak = max((self._year_total(year) for year in years), default=0)
        for index, year in enumerate(years):
            self._paint_year(painter, self._tile_rect(index), year, peak, index == self._focus)

    def _paint_tile_state(self, painter: QPainter, rect: QRectF, current: bool, focused: bool) -> None:
        """The shown month or year wears the grid's selection ring; focus gets its hover."""
        if focused:
            painter.fillPath(_rounded(rect, 10), token_color("overlay-rgb", 14))
        if current:
            painter.setBrush(token_color("accent-rgb", 22))
            painter.setPen(QPen(token_color("accent"), 2))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 9, 9)
        elif focused:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(token_color("accent-rgb", 128), 1))
            painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)

    def _paint_month(self, painter: QPainter, rect: QRectF, key: MonthKey, peak: int,
                     focused: bool) -> None:
        count = self._counts.get(key, 0)
        path = _rounded(rect, 10)
        painter.fillPath(path, token_color("overlay-rgb", 9))
        if count:
            painter.fillPath(path, _heat(count, peak))
        self._paint_tile_state(painter, rect, key == self._current, focused)

        name = calendar.month_abbr[key[1]]
        painter.setFont(_font(self.font(), 13, QFont.Weight.DemiBold))
        painter.setPen(token_color("text-muted" if key > self._today else "text"))
        if count:
            split = rect.top() + rect.height() * 0.56
            painter.drawText(QRectF(rect.left(), rect.top(), rect.width(), split - rect.top()),
                             int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom), name)
            painter.setFont(_font(self.font(), 10.5, QFont.Weight.DemiBold))
            painter.setPen(token_color("accent-soft" if key == self._current else "text-secondary-strong"))
            painter.drawText(QRectF(rect.left(), split + 1, rect.width(), rect.bottom() - split - 1),
                             int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                             f"{count:,}")
        else:
            painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), name)
        if key == self._today:
            radius = max(2.5, _px(3))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(token_color("accent"))
            painter.drawEllipse(QPointF(rect.right() - 9, rect.top() + 9), radius, radius)

    def _paint_year(self, painter: QPainter, rect: QRectF, year: int, peak: int,
                    focused: bool) -> None:
        total = self._year_total(year)
        path = _rounded(rect, 10)
        painter.fillPath(path, token_color("overlay-rgb", 9))
        if total:
            painter.fillPath(path, _heat(total, peak))
        self._paint_tile_state(painter, rect, year == self._current[0], focused)

        label_width, count_width = _px(56), _px(52)
        painter.setFont(_font(self.font(), 13, QFont.Weight.DemiBold))
        painter.setPen(token_color("text"))
        painter.drawText(QRectF(rect.left() + _px(12), rect.top(), label_width, rect.height()),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), str(year))
        painter.setFont(_font(self.font(), 10.5, QFont.Weight.DemiBold))
        painter.setPen(token_color("text-secondary-strong" if total else "text-muted"))
        painter.drawText(QRectF(rect.right() - count_width - _px(12), rect.top(), count_width,
                                rect.height()),
                         int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                         f"{total:,}" if total else "—")

        # Its twelve months in miniature, so a year's shape shows before it's opened.
        spark_height = min(rect.height() - _px(18), _px(26))
        spark = QRectF(rect.left() + _px(12) + label_width, rect.center().y() - spark_height / 2,
                       rect.width() - label_width - count_width - _px(36), spark_height)
        months = [self._counts.get((year, month), 0) for month in range(1, 13)]
        busiest = max(months) or 1
        slot = spark.width() / 12
        bar_width = max(2.0, slot * 0.58)
        painter.setPen(Qt.PenStyle.NoPen)
        for month, count in enumerate(months, 1):
            height = max(2.0, spark.height() * count / busiest) if count else 2.0
            bar = QRectF(spark.left() + (month - 1) * slot + (slot - bar_width) / 2,
                         spark.bottom() - height, bar_width, height)
            if (year, month) == self._current:
                color = token_color("accent")
            elif count:
                color = token_color("accent-rgb", 165)
            else:
                color = token_color("overlay-rgb", 30)
            painter.fillPath(_rounded(bar, min(1.5, bar_width / 2)), color)

    def _paint_footer(self, painter: QPainter) -> None:
        link = self._today_rect()
        hovered = self._hover == ("today", 0)
        if hovered:
            painter.fillPath(_rounded(link, link.height() / 2), token_color("accent-rgb", 30))
        painter.setFont(self._footer_font())
        painter.setPen(token_color("accent" if hovered else "accent-soft"))
        painter.drawText(link, int(Qt.AlignmentFlag.AlignCenter), "This month")

        if self._mode == self.MONTHS:
            total = self._year_total(self._year)
            summary = f"{plural(total, 'recording')} in {self._year}" if total else f"Nothing in {self._year}"
        else:
            total = sum(self._counts.values())
            summary = (f"{plural(total, 'recording')} since {self.first_year}" if total
                       else "Nothing recorded yet")
        footer = self._footer_rect()
        room = QRectF(link.right() + _px(8), footer.top(), footer.right() - link.right() - _px(10),
                      footer.height())
        font = _font(self.font(), 11)
        painter.setFont(font)
        painter.setPen(token_color("text-secondary"))
        painter.drawText(room, int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                         QFontMetrics(font).elidedText(summary, Qt.TextElideMode.ElideRight,
                                                       int(room.width())))

    # ---- input ----

    def _tooltip(self, target) -> str:
        kind, index = target
        if kind == "tile":
            if self._mode == self.MONTHS:
                count = self._counts.get((self._year, index + 1), 0)
                name = f"{calendar.month_name[index + 1]} {self._year}"
            else:
                year = self.page_years()[index]
                count, name = self._year_total(year), str(year)
            return f"{name} · " + (plural(count, "recording") if count else "nothing recorded")
        if kind in ("prev", "next"):
            step = -1 if kind == "prev" else 1
            if not self._can_step(step):
                return ""
            if self._mode == self.YEARS:
                return "Earlier years" if step < 0 else "Later years"
            return "Previous year (Page Up)" if step < 0 else "Next year (Page Down)"
        if kind == "title":
            return "Every year (Y)" if self._mode == self.MONTHS else f"Back to {self._year} (Y)"
        return "Go to this month (T)"

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.ToolTip:
            target = self._target_at(QPointF(event.pos()))
            text = self._tooltip(target) if target else ""
            if text:
                QToolTip.showText(event.globalPos(), text, self)
            else:
                QToolTip.hideText()
            return True
        return super().event(event)

    def mouseMoveEvent(self, event) -> None:
        target = self._target_at(event.position())
        if target != self._hover:
            self._hover = target
            if target is not None and target[0] == "tile":
                self._focus = target[1]
            self.setCursor(Qt.CursorShape.PointingHandCursor if target else Qt.CursorShape.ArrowCursor)
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = None
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if not self._panel().contains(event.position()):
            # A press on the title that opened it only closes it: replayed to
            # the title, the press would open it straight back up.
            anchor = self._anchor
            if anchor is not None and anchor.rect().contains(
                    anchor.mapFromGlobal(event.globalPosition().toPoint())):
                self.setAttribute(Qt.WidgetAttribute.WA_NoMouseReplay, True)
            self.hide()
            return
        left = event.button() == Qt.MouseButton.LeftButton
        self._pressed = self._target_at(event.position()) if left else None
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        target = self._target_at(event.position())
        pressed, self._pressed = self._pressed, None
        if event.button() == Qt.MouseButton.LeftButton and target is not None and target == pressed:
            self._act(target)
        event.accept()

    def wheelEvent(self, event) -> None:
        self._wheel += event.angleDelta().y()
        while abs(self._wheel) >= 120:
            step = -1 if self._wheel > 0 else 1
            self._wheel += 120 * step
            self.step(step)
        event.accept()

    def keyPressEvent(self, event) -> None:
        key = event.key()
        vertical = 3 if self._mode == self.MONTHS else 1
        moves = {Qt.Key.Key_Left: -1, Qt.Key.Key_Right: 1,
                 Qt.Key.Key_Up: -vertical, Qt.Key.Key_Down: vertical}
        if key in moves:
            self._move_focus(moves[key])
        elif key in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            self.step(-1 if key == Qt.Key.Key_PageUp else 1)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            if self._focus is not None:
                self.pick(self._focus)
        elif key == Qt.Key.Key_Y:
            self.toggle_years()
        elif key in (Qt.Key.Key_Home, Qt.Key.Key_T):
            self._choose(self._today)
        elif key == Qt.Key.Key_Escape:
            if self._mode == self.YEARS:
                self.toggle_years()
            else:
                self.hide()
        else:
            super().keyPressEvent(event)
            return
        event.accept()


class CalendarGlyphButton(QPushButton):
    """A little calendar page showing today's date; opens the History calendar."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("calendarGlyphBtn")
        self.setFixedSize(28, 28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("History calendar")
        self.setStyleSheet("""
            QPushButton#calendarGlyphBtn {
                background-color: transparent;
                border: none;
                border-radius: 14px;
                padding: 0px;
            }
            QPushButton#calendarGlyphBtn:hover {
                background-color: rgba(@overlay-rgb, 0.1);
            }
        """)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = token_color("text-heading" if self.underMouse() else "text-secondary")
        page = QRectF(6.5, 7.5, 15, 14)
        painter.setPen(QPen(color, 1.4))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(page, 3, 3)
        band = QPainterPath()
        band.addRoundedRect(QRectF(page.left(), page.top(), page.width(), 4.5), 3, 3)
        painter.fillPath(band, color)
        for x in (page.left() + 4, page.right() - 4):
            painter.drawLine(QPointF(x, page.top() - 2), QPointF(x, page.top() + 1.5))
        font = QFont(self.font())
        font.setPixelSize(8)
        font.setWeight(QFont.Weight.Bold)
        painter.setFont(font)
        painter.drawText(page.adjusted(0, 4, 0, 0), int(Qt.AlignmentFlag.AlignCenter),
                         str(date.today().day))


class KindChip(QPushButton):
    """A legend entry that doubles as a filter: its kind's colour dot, name and count."""

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.setObjectName("calendarKindChip")
        self.setCheckable(True)
        self.setChecked(True)
        self.setFixedHeight(30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"Show or hide {KIND_PLURALS[kind]}")
        self.set_count(None)

    def set_count(self, count: Optional[int]) -> None:
        name = {DICTATION: "Dictations", FILE: "Files", MEETING: "Meetings"}[self.kind]
        self.setText(name if count is None else f"{name}  {count:,}")

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = token_color(KIND_COLOR[self.kind])
        if not self.isChecked():
            color.setAlphaF(0.35)
        radius = max(3.0, _px(4))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QPointF(12 + radius, self.height() / 2), radius, radius)


class AgendaCard(QFrame):
    """One recording in the day agenda: time, kind, where it's kept, and what was said."""

    activated = pyqtSignal(object)
    copy_requested = pyqtSignal(object)

    def __init__(self, item: CalendarItem, milestone: Optional[int] = None, parent=None):
        super().__init__(parent)
        self.item = item
        self.setObjectName("calendarAgendaCard")
        self.setProperty("kind", item.kind)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)
        self.setAccessibleName(f"{KIND_LABELS[item.kind]} at {clock(item.when)}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 12, 11)
        layout.setSpacing(6)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
        self.time_label = QLabel(clock(item.when))
        self.time_label.setObjectName("calendarCardTime")
        top.addWidget(self.time_label)
        self.kind_pill = QLabel(KIND_LABELS[item.kind])
        self.kind_pill.setObjectName("calendarKindPill")
        self.kind_pill.setProperty("kind", item.kind)
        top.addWidget(self.kind_pill)
        top.addStretch()
        location, tooltip = item.location()
        if location:
            chip = QLabel(location)
            chip.setObjectName("calendarPlaceChip")
            chip.setToolTip(tooltip)
            top.addWidget(chip)
        if item.seconds >= 1:
            length = QLabel(format_audio_duration(item.seconds))
            length.setObjectName("calendarLengthChip")
            length.setToolTip("Length of the recording")
            top.addWidget(length)
        layout.addLayout(top)

        if item.title:
            self.title_label = WrappedLabel(item.title)
            self.title_label.setObjectName("calendarCardTitle")
            layout.addWidget(self.title_label)
        preview = item.preview if item.preview and item.preview != item.title else ""
        if preview or not item.title:
            self.preview_label = WrappedLabel(preview or "Nothing was transcribed")
            self.preview_label.setObjectName("calendarCardPreview")
            layout.addWidget(self.preview_label)
        if milestone:
            star = QLabel(f"✦ {milestone_text(milestone)}")
            star.setObjectName("calendarMilestone")
            layout.addWidget(star)
        self.progress_label = QLabel("")
        self.progress_label.setObjectName("calendarCardProgress")
        self.progress_label.setWordWrap(True)
        self.progress_label.hide()
        layout.addWidget(self.progress_label)

    def set_progress(self, text: str) -> None:
        self.progress_label.setText(text)
        self.progress_label.setVisible(bool(text))

    def _show_menu(self, pos) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(MENU_STYLESHEET)
        menu.addAction("Open").triggered.connect(lambda: self.activated.emit(self.item))
        copy = menu.addAction("Copy transcript" if self.item.kind == MEETING else "Copy text")
        copy.triggered.connect(lambda: self.copy_requested.emit(self.item))
        menu.exec(self.mapToGlobal(pos))

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit(self.item)
        super().mousePressEvent(event)


def fade_in(widgets: Sequence[QWidget], stagger_ms: int = 35, duration_ms: int = 220) -> None:
    """Fade widgets in one after another; the effect is removed when each is done.

    Only the first few are staggered so a long list doesn't keep appearing.
    """
    for index, widget in enumerate(widgets[:8]):
        effect = QGraphicsOpacityEffect(widget)
        effect.setOpacity(0.0)
        widget.setGraphicsEffect(effect)
        group = QSequentialAnimationGroup(widget)
        if index:
            group.addPause(index * stagger_ms)
        fade = QPropertyAnimation(effect, b"opacity", group)
        fade.setDuration(duration_ms)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        group.addAnimation(fade)
        group.finished.connect(lambda widget=widget: widget.setGraphicsEffect(None))
        group.start(QSequentialAnimationGroup.DeletionPolicy.DeleteWhenStopped)


__all__ = [
    "AgendaCard", "CalendarGlyphButton", "KIND_COLOR", "KIND_RGB", "KIND_TEXT", "KindChip",
    "MENU_STYLESHEET", "MonthGrid", "MonthPicker", "MonthTitleButton", "YearStrip", "clock",
    "day_tooltip", "fade_in", "kind_breakdown", "paint_chevron", "pick_lines", "plural",
]

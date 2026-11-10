"""Painted charts for the Stats window: columns, heat maps and share bars.

Each chart reads its colours from the palette at paint time, so a theme
switch only needs a repaint, and gives screen readers its numbers through
its accessible name and description.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Callable, NamedTuple, Optional, Sequence

from PyQt6.QtCore import QLocale, QPointF, QRectF, Qt
from PyQt6.QtGui import QBrush, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QSizePolicy, QToolTip, QWidget

from services.desktop_session import use_omarchy_ui
from ui_qt.utils.font_scale import current_ui_font_scale
from ui_qt.utils.palette import token_color


def words_text(count: int) -> str:
    return f"{count:,} word" + ("" if count == 1 else "s")


def _scaled(pixels: float) -> int:
    return round(pixels * current_ui_font_scale())


def _small_font(widget: QWidget) -> QFont:
    font = QFont(widget.font())
    font.setPointSizeF(max(6.5, font.pointSizeF() * 0.8))
    return font


def _rounded(painter: QPainter, rect: QRectF, radius: float, brush) -> None:
    path = QPainterPath()
    radius = 1.0 if use_omarchy_ui() else min(radius, rect.width() / 2, rect.height() / 2)
    path.addRoundedRect(rect, radius, radius)
    painter.fillPath(path, brush)


class Bar(NamedTuple):
    #: Read out and shown on hover, as "Tue, Oct 21".
    name: str
    words: int
    #: Drawn under the column; empty for none.
    axis: str = ""


class BarChart(QWidget):
    """Words as rounded columns, with one in full colour and a dashed average."""

    def __init__(self, height: int, tone: str = "accent", parent=None, *,
                 value_text: Callable[[int], str] = words_text):
        """``value_text`` words a column's number for hover and screen readers."""
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._plot_height = _scaled(height)
        self._tone = tone
        self._value_text = value_text
        self._bars: tuple[Bar, ...] = ()
        self._highlight: Optional[int] = None
        self._average = 0.0
        self._hover: Optional[int] = None
        self.setFixedHeight(self._plot_height)

    @property
    def bars(self) -> tuple[Bar, ...]:
        return self._bars

    def set_bars(self, bars: Sequence[Bar], name: str, *, highlight: Optional[int] = None,
                 average: float = 0.0) -> None:
        self._bars = tuple(bars)
        self._highlight = highlight
        self._average = average
        self._hover = None
        axis = any(bar.axis for bar in self._bars)
        self.setFixedHeight(self._plot_height + (self._axis_height() if axis else 0))
        # Screen readers get every column; the chart only shows them on hover.
        self.setAccessibleName(name)
        self.setAccessibleDescription(
            "; ".join(f"{bar.name}: {self._value_text(bar.words)}" for bar in self._bars)
        )
        self.update()

    def _axis_height(self) -> int:
        return self.fontMetrics().height() + 4

    def _slots(self) -> list[QRectF]:
        """Column rects; a few columns stay slim and centred instead of filling the width."""
        count = len(self._bars)
        if not count:
            return []
        gap = max(2.0, min(10.0, self.width() / count * 0.3))
        width = max(2.0, min(_scaled(36), (self.width() - gap * (count - 1)) / count))
        if width == _scaled(36):
            gap = max(gap, (self.width() - width * count) / count)
        left = (self.width() - (width * count + gap * (count - 1))) / 2
        return [
            QRectF(left + index * (width + gap), 0, width, self._plot_height)
            for index in range(count)
        ]

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        peak = max((bar.words for bar in self._bars), default=0)
        painter.setFont(_small_font(self))
        for index, (slot, bar) in enumerate(zip(self._slots(), self._bars)):
            self._paint_column(painter, slot, bar.words, peak, index in (self._highlight, self._hover))
            if bar.axis:
                # Centred under its column, but kept inside the chart at the ends.
                text = painter.fontMetrics().horizontalAdvance(bar.axis) + 2
                x = max(0.0, min(self.width() - text, slot.center().x() - text / 2))
                painter.setPen(token_color("slate-text-4"))
                painter.drawText(QRectF(x, slot.bottom() + 3, text, self._axis_height()),
                                 Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                                 bar.axis)
        if self._average and peak:
            y = self._plot_height - self._plot_height * min(1.0, self._average / peak)
            pen = QPen(token_color("slate-text-3", 150), 1, Qt.PenStyle.DashLine)
            pen.setDashPattern([3, 3])
            painter.setPen(pen)
            painter.drawLine(QPointF(0, y), QPointF(self.width(), y))
        painter.end()

    def _paint_column(self, painter: QPainter, slot: QRectF, words: int, peak: int,
                      strong: bool) -> None:
        if not (words and peak):
            stub_height = min(4.0, slot.height() / 6)
            stub = QRectF(slot.left(), slot.bottom() - stub_height, slot.width(), stub_height)
            _rounded(painter, stub, 2, token_color("overlay-rgb", 30))
            return
        height = max(min(6.0, slot.height() / 3), slot.height() * words / peak)
        column = QRectF(slot.left(), slot.bottom() - height, slot.width(), height)
        fill = QLinearGradient(column.topLeft(), column.bottomLeft())
        fill.setColorAt(0, token_color(self._tone, 255 if strong else 150))
        fill.setColorAt(1, token_color(self._tone, 190 if strong else 80))
        _rounded(painter, column, min(5.0, slot.height() / 8), QBrush(fill))

    def _index_at(self, x: float) -> Optional[int]:
        for index, slot in enumerate(self._slots()):
            if slot.left() - 2 <= x <= slot.right() + 2:
                return index
        return None

    def mouseMoveEvent(self, event) -> None:
        index = self._index_at(event.position().x())
        if index != self._hover:
            self._hover = index
            self.update()
        if index is None:
            QToolTip.hideText()
            return
        bar = self._bars[index]
        QToolTip.showText(event.globalPosition().toPoint(),
                          f"{bar.name} · {self._value_text(bar.words)}", self)

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self._hover = None
        self.update()


#: Shades of the activity map, from no words to the busiest quarter of days.
_LEVEL_ALPHAS = (70, 125, 185, 255)


class ActivityMap(QWidget):
    """A square per day, a column per week, as many weeks as fit; today at the right."""

    WEEKS = 53

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._cell = _scaled(11)
        self._gap = max(2, _scaled(3))
        self._days: tuple[tuple[date, int], ...] = ()
        self._thresholds: tuple[int, ...] = ()
        self._cells: list[tuple[QRectF, date, int]] = []
        self._first_weekday = QLocale().firstDayOfWeek().value - 1
        label = self.fontMetrics().height()
        self.setFixedHeight(label + 4 + 7 * (self._cell + self._gap) + label + 6)
        self.setMinimumWidth(self._gutter() + 8 * (self._cell + self._gap))

    @property
    def days(self) -> tuple[tuple[date, int], ...]:
        return self._days

    def set_days(self, days: Sequence[tuple[date, int]]) -> None:
        self._days = tuple(days)
        busy = sorted(words for _day, words in self._days if words)
        # Quartiles of the days with words, so one huge day doesn't wash out the rest.
        self._thresholds = (
            tuple(busy[(len(busy) - 1) * step // 4] for step in (1, 2, 3)) if busy else ()
        )
        active = len(busy)
        self.setAccessibleName("Activity, a square per day")
        if busy:
            best_day, best = max(self._days, key=lambda item: (item[1], item[0]))
            self.setAccessibleDescription(
                f"{active} days with words; the busiest, {best_day:%B} {best_day.day}, "
                f"had {words_text(best)}"
            )
        else:
            self.setAccessibleDescription("No days with words yet")
        self.update()

    def level(self, words: int) -> int:
        """0 for no words, else 1–4 by quartile of the days with words."""
        if not words:
            return 0
        return 1 + sum(1 for threshold in self._thresholds if words > threshold)

    def _gutter(self) -> int:
        return self.fontMetrics().horizontalAdvance("Wed") + 6

    def _row(self, day: date) -> int:
        return (day.weekday() - self._first_weekday) % 7

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(_small_font(self))
        self._cells = []
        if self._days:
            self._paint_grid(painter)
        self._paint_legend(painter)
        painter.end()

    def _paint_grid(self, painter: QPainter) -> None:
        step = self._cell + self._gap
        gutter = self._gutter()
        weeks = max(1, min(self.WEEKS, (self.width() - gutter + self._gap) // step))
        top = self.fontMetrics().height() + 4
        today = self._days[-1][0]
        this_week = today - timedelta(days=self._row(today))
        left = self.width() - weeks * step + self._gap
        muted = token_color("slate-text-4")
        painter.setPen(muted)
        for row in range(7):
            weekday = (self._first_weekday + row) % 7
            if weekday in (0, 2, 4):
                text = ("Mon", "Wed", "Fri")[weekday // 2]
                painter.drawText(QRectF(0, top + row * step - 2, gutter - 6, self._cell + 4),
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
        last_label = -99
        for day, words in self._days:
            column = weeks - 1 - (this_week - (day - timedelta(days=self._row(day)))).days // 7
            if column < 0:
                continue
            x = left + column * step
            cell = QRectF(x, top + self._row(day) * step, self._cell, self._cell)
            self._paint_cell(painter, cell, self.level(words), day == today)
            self._cells.append((cell, day, words))
            if day.day <= 7 and self._row(day) == 0 and column - last_label >= 3:
                painter.setPen(muted)
                painter.drawText(QRectF(x, 0, 60, top), Qt.AlignmentFlag.AlignLeft, f"{day:%b}")
                last_label = column

    def _paint_cell(self, painter: QPainter, cell: QRectF, level: int, today: bool) -> None:
        colour = (token_color("overlay-rgb", 26) if not level
                  else token_color("accent", _LEVEL_ALPHAS[level - 1]))
        _rounded(painter, cell, 3, colour)
        if today:
            painter.setPen(QPen(token_color("slate-text-3"), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(cell.adjusted(-1.5, -1.5, 1.5, 1.5), 4, 4)

    def _paint_legend(self, painter: QPainter) -> None:
        metrics = self.fontMetrics()
        y = self.height() - metrics.height() - 1
        step = self._cell + self._gap
        right = self.width()
        more = metrics.horizontalAdvance("More")
        painter.setPen(token_color("slate-text-4"))
        painter.drawText(QRectF(right - more, y, more + 2, metrics.height()),
                         Qt.AlignmentFlag.AlignLeft, "More")
        x = right - more - 6 - 5 * step
        for level in range(5):
            cell = QRectF(x + level * step, y + (metrics.height() - self._cell) / 2,
                          self._cell, self._cell)
            self._paint_cell(painter, cell, level, False)
        less = metrics.horizontalAdvance("Less")
        painter.setPen(token_color("slate-text-4"))
        painter.drawText(QRectF(x - less - 6, y, less + 2, metrics.height()),
                         Qt.AlignmentFlag.AlignLeft, "Less")

    def mouseMoveEvent(self, event) -> None:
        position = event.position()
        for cell, day, words in self._cells:
            if cell.adjusted(-1, -1, 1, 1).contains(position):
                QToolTip.showText(event.globalPosition().toPoint(),
                                  f"{day:%a}, {day:%b} {day.day} · {words_text(words)}", self)
                return
        QToolTip.hideText()


def hour_text(hour: int) -> str:
    return f"{(hour % 12) or 12} {'AM' if hour % 24 < 12 else 'PM'}"


#: Monday first; October 5, 2026 was a Monday.
WEEKDAYS = tuple(date(2026, 10, 5 + weekday) for weekday in range(7))


class HourGrid(QWidget):
    """Words by weekday and hour: a row per weekday, a square per hour."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._gap = 2
        self._grid: tuple[tuple[int, ...], ...] = ()
        self._thresholds: tuple[int, ...] = ()
        self._cells: list[tuple[QRectF, int, int, int]] = []
        first = QLocale().firstDayOfWeek().value - 1
        self._order = tuple((first + offset) % 7 for offset in range(7))
        self.setMinimumWidth(self._gutter() + 24 * (3 + self._gap))
        self._fit_height()

    @property
    def grid(self) -> tuple[tuple[int, ...], ...]:
        return self._grid

    def set_grid(self, grid: Sequence[Sequence[int]]) -> None:
        """``grid`` is words by weekday, Monday first, then hour."""
        self._grid = tuple(tuple(day) for day in grid)
        busy = sorted(words for day in self._grid for words in day if words)
        self._thresholds = (
            tuple(busy[(len(busy) - 1) * step // 4] for step in (1, 2, 3)) if busy else ()
        )
        self.setAccessibleName("Words by weekday and hour")
        peak = self.peak()
        if peak is None:
            self.setAccessibleDescription("No words yet")
        else:
            weekday, hour = peak
            self.setAccessibleDescription(
                f"Busiest on {WEEKDAYS[weekday]:%A}s around {hour_text(hour)}, "
                f"with {words_text(self._grid[weekday][hour])}"
            )
        self.update()

    def peak(self) -> Optional[tuple[int, int]]:
        """The ``(weekday, hour)`` with the most words, or None without any."""
        best = max(
            ((words, -weekday, -hour) for weekday, day in enumerate(self._grid)
             for hour, words in enumerate(day)),
            default=(0, 0, 0),
        )
        return (-best[1], -best[2]) if best[0] else None

    def level(self, words: int) -> int:
        if not words:
            return 0
        return 1 + sum(1 for threshold in self._thresholds if words > threshold)

    def _gutter(self) -> int:
        return self.fontMetrics().horizontalAdvance("Wed") + 6

    def _cell(self) -> float:
        room = (self.width() - self._gutter() - 23 * self._gap) / 24
        return max(3.0, min(float(_scaled(20)), room))

    def _fit_height(self) -> None:
        self.setFixedHeight(
            round(7 * (self._cell() + self._gap)) + self.fontMetrics().height() + 4
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_height()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(_small_font(self))
        cell, gutter = self._cell(), self._gutter()
        step = cell + self._gap
        muted = token_color("slate-text-4")
        self._cells = []
        for row, weekday in enumerate(self._order):
            top = row * step
            if row % 2 == 0:
                painter.setPen(muted)
                painter.drawText(QRectF(0, top - 2, gutter - 6, cell + 4),
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                                 f"{WEEKDAYS[weekday]:%a}")
            for hour in range(24):
                words = self._grid[weekday][hour] if self._grid else 0
                rect = QRectF(gutter + hour * step, top, cell, cell)
                level = self.level(words)
                colour = (token_color("overlay-rgb", 26) if not level
                          else token_color("accent", _LEVEL_ALPHAS[level - 1]))
                _rounded(painter, rect, 2, colour)
                self._cells.append((rect, weekday, hour, words))
        painter.setPen(muted)
        bottom = 7 * step + 2
        metrics = painter.fontMetrics()
        for hour in (0, 6, 12, 18):
            text = hour_text(hour)
            x = min(gutter + hour * step, self.width() - metrics.horizontalAdvance(text) - 1)
            painter.drawText(QRectF(x, bottom, metrics.horizontalAdvance(text) + 2,
                                    metrics.height()), Qt.AlignmentFlag.AlignLeft, text)
        painter.end()

    def mouseMoveEvent(self, event) -> None:
        position = event.position()
        for rect, weekday, hour, words in self._cells:
            if rect.adjusted(-1, -1, 1, 1).contains(position):
                QToolTip.showText(
                    event.globalPosition().toPoint(),
                    f"{WEEKDAYS[weekday]:%A}s, {hour_text(hour)} – {hour_text(hour + 1)} · "
                    f"{words_text(words)}", self,
                )
                return
        QToolTip.hideText()


class SplitBar(QWidget):
    """One track cut into shares: the largest in full colour, the rest lighter."""

    _ALPHAS = (255, 175, 120, 80, 50)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFixedHeight(8)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._parts: tuple[tuple[str, int, bool], ...] = ()

    @property
    def parts(self) -> tuple[tuple[str, int, bool], ...]:
        return self._parts

    def set_parts(self, parts: Sequence[tuple[str, int, bool]], name: str) -> None:
        """``parts`` are ``(label, words, named)``; an unnamed share is drawn grey."""
        self._parts = tuple(part for part in parts if part[1] > 0)
        total = sum(words for _label, words, _named in self._parts)
        self.setAccessibleName(name)
        self.setAccessibleDescription("; ".join(
            f"{label}: {round(words * 100 / total)}%" for label, words, _named in self._parts
        ))
        self.update()

    def _rects(self) -> list[QRectF]:
        total = sum(words for _label, words, _named in self._parts)
        if not total:
            return []
        gap = 2.0
        room = max(0.0, self.width() - gap * (len(self._parts) - 1))
        rects, x = [], 0.0
        for _label, words, _named in self._parts:
            width = room * words / total
            rects.append(QRectF(x, 0, width, self.height()))
            x += width + gap
        return rects

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self._parts:
            _rounded(painter, QRectF(self.rect()), 4, token_color("overlay-rgb", 24))
        shade = 0
        for rect, (_label, _words, named) in zip(self._rects(), self._parts):
            if named:
                colour = token_color("accent", self._ALPHAS[min(shade, len(self._ALPHAS) - 1)])
                shade += 1
            else:
                colour = token_color("overlay-rgb", 46)
            _rounded(painter, rect, 3, colour)
        painter.end()

    def mouseMoveEvent(self, event) -> None:
        total = sum(words for _label, words, _named in self._parts)
        for rect, (label, words, _named) in zip(self._rects(), self._parts):
            if rect.left() - 1 <= event.position().x() <= rect.right() + 1:
                QToolTip.showText(
                    event.globalPosition().toPoint(),
                    f"{label} · {words_text(words)} · {round(words * 100 / total)}%", self,
                )
                return
        QToolTip.hideText()


class ShareBar(QWidget):
    """A thin track filled to ``fraction``."""

    def __init__(self, fraction: float, tone: str = "success", height: int = 6, parent=None):
        super().__init__(parent)
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.tone = tone
        self.fraction = 0.0
        self.set_fraction(fraction)

    def set_fraction(self, fraction: float) -> None:
        self.fraction = max(0.0, min(1.0, fraction))
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        _rounded(painter, rect, rect.height() / 2, token_color("overlay-rgb", 24))
        if self.fraction:
            filled = QRectF(rect.left(), rect.top(),
                            max(rect.height(), rect.width() * self.fraction), rect.height())
            fill = QLinearGradient(filled.topLeft(), filled.topRight())
            fill.setColorAt(0, token_color(self.tone, 150))
            fill.setColorAt(1, token_color(self.tone, 255))
            _rounded(painter, filled, rect.height() / 2, QBrush(fill))
        painter.end()

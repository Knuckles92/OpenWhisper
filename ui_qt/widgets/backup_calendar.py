"""A month calendar of the latest backup and the saved automatic schedule.

It is the History calendar's month grid, title button and month picker with
backups in place of recordings: the same painted cells, sliding months, glide
of the selection ring and keyboard moves. Only what a day can hold differs.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import QEvent, QPointF, QRectF, Qt
from PyQt6.QtGui import QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from services.history_calendar import MonthKey, month_distance, month_key, shift_month
from ui_qt.utils.palette import token_color
from ui_qt.widgets.history_calendar import MonthGrid, MonthPicker, MonthTitleButton, _font, _px
from ui_qt.widgets.wrapped_label import WrappedLabel

LATEST_TEXT = "Latest backup saved"
PLANNED_TEXT = "Automatic backup planned"
#: How far ahead the month picker marks planned backups.
PICKER_HORIZON_DAYS = 366


class BackupMonthGrid(MonthGrid):
    """A month whose days can carry the latest backup and planned ones."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("backupCalendarGrid")
        self.setAccessibleName("Backup calendar")
        # The History calendar fills a window; this one shares a settings card.
        self.setMinimumSize(280, 250)
        self.latest: Optional[date] = None
        self.next_due: Optional[date] = None
        self.interval_days = 0

    def is_planned(self, day: date) -> bool:
        if self.next_due is None or self.interval_days <= 0:
            return False
        days = (day - self.next_due).days
        return days >= 0 and days % self.interval_days == 0

    def events(self, day: date) -> List[Tuple[str, str]]:
        """``(short label, long description)`` for what the day holds."""
        found = []
        if day == self.latest:
            found.append(("Saved", LATEST_TEXT))
        if self.is_planned(day):
            found.append(("Planned", PLANNED_TEXT))
        return found

    def _paint_cell(self, painter: QPainter, rect: QRectF, day: date) -> None:
        super()._paint_cell(painter, rect, day)
        found = self.events(day)
        if not found:
            return
        painter.save()
        if day.month != self._month:
            painter.setOpacity(0.5)
        latest = day == self.latest
        planned = self.is_planned(day)
        outline = QPainterPath()
        outline.addRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), self.RADIUS, self.RADIUS)
        if latest:
            painter.fillPath(outline, token_color("success-rgb", int(255 * 0.16)))
            painter.setPen(QPen(token_color("success-rgb", int(255 * 0.55)), 1))
        else:
            pen = QPen(token_color("accent-rgb", int(255 * 0.7)), 1.2)
            pen.setStyle(Qt.PenStyle.CustomDashLine)
            pen.setDashPattern([3.0, 3.0])
            painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(outline)
        self._paint_labels(painter, rect, latest, planned)
        painter.restore()

    def _paint_labels(self, painter: QPainter, rect: QRectF, latest: bool, planned: bool) -> None:
        entries = []
        if latest:
            entries.append(("Saved", "success", "success-text"))
        if planned:
            entries.append(("Planned", "accent", "accent-soft"))
        top = self._number_rect(rect).bottom() + 3
        line = _px(16)
        left = rect.left() + 9
        radius = max(2.5, _px(3))
        text_left = left + _px(10)
        width = rect.right() - 7 - text_left
        room = int((rect.bottom() - 7 - top) // line)
        painter.setPen(Qt.PenStyle.NoPen)
        if width < _px(40) or room < len(entries):
            # Too small for words: a dot per event along the bottom.
            for index, (_text, fill, _ink) in enumerate(entries):
                painter.setBrush(token_color(fill))
                painter.drawEllipse(QPointF(left + radius + index * (radius * 2 + 3),
                                            rect.bottom() - 9 - radius), radius, radius)
            return
        font = _font(self.font(), 11, QFont.Weight.DemiBold)
        metrics = QFontMetrics(font)
        painter.setFont(font)
        for index, (text, fill, ink) in enumerate(entries):
            y = top + index * line
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(token_color(fill))
            painter.drawEllipse(QPointF(left + radius, y + line / 2), radius, radius)
            painter.setPen(token_color(ink))
            painter.drawText(QRectF(text_left, y, width, line),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                             metrics.elidedText(text, Qt.TextElideMode.ElideRight, int(width)))

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.ToolTip:
            day = self.day_at(QPointF(event.pos()))
            if day is None:
                QToolTip.hideText()
            else:
                heading = day.strftime("%A, %B ") + str(day.day)
                found = [long for _short, long in self.events(day)]
                QToolTip.showText(event.globalPos(), "\n".join([heading] + (found or ["No backup"])),
                                  self)
            return True
        return super().event(event)


def _month_counts(latest: Optional[date], next_due: Optional[date], interval_days: int,
                  today: date) -> Dict[MonthKey, int]:
    """Backups per month for the picker's tint: the latest, and planned ones a year ahead."""
    counts: Dict[MonthKey, int] = {}
    if latest is not None:
        counts[month_key(latest)] = 1
    if next_due is not None and interval_days > 0:
        day, end = next_due, today + timedelta(days=PICKER_HORIZON_DAYS)
        while day <= end:
            key = month_key(day)
            counts[key] = counts.get(key, 0) + 1
            day += timedelta(days=interval_days)
    return counts


class BackupCalendar(QWidget):
    """Month grid with a title that opens the month picker, as in History."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("backupCalendar")
        self.latest: Optional[date] = None
        self.next_due: Optional[date] = None
        self.interval_days = 0
        today = date.today()
        self._view: MonthKey = month_key(today)
        self._selected: date = today

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        navigation = QHBoxLayout()
        navigation.setSpacing(6)
        self.title_button = MonthTitleButton("", self)
        self.title_button.setFixedHeight(34)
        self.title_button.setToolTip("Choose a month or year (M)")
        self.title_button.clicked.connect(self.open_month_picker)
        navigation.addWidget(self.title_button)
        navigation.addStretch(1)
        self.today_button = self._nav_button("Today", "Go to today (T)", "backupCalendarToday")
        self.previous_button = self._nav_button("‹", "Previous month", "backupCalendarNav")
        self.next_button = self._nav_button("›", "Next month", "backupCalendarNav")
        for button in (self.today_button, self.previous_button, self.next_button):
            navigation.addWidget(button)
        column.addLayout(navigation)

        self.grid = BackupMonthGrid(self)
        column.addWidget(self.grid, stretch=1)
        self.month_picker = MonthPicker(self)

        legend = QHBoxLayout()
        for text, name in (("● Latest backup", "backupCalendarLatest"),
                           ("┄ Planned", "backupCalendarPlanned")):
            label = QLabel(text)
            label.setObjectName(name)
            legend.addWidget(label)
        legend.addStretch()
        column.addLayout(legend)
        self.detail_label = WrappedLabel("")
        self.detail_label.setObjectName("backupCalendarDetail")
        self.detail_label.setAccessibleName("Selected backup date")
        column.addWidget(self.detail_label)

        self.previous_button.clicked.connect(lambda: self.step_month(-1))
        self.next_button.clicked.connect(lambda: self.step_month(1))
        self.today_button.clicked.connect(self.go_today)
        self.month_picker.month_chosen.connect(self._on_month_picked)
        self.month_picker.closed.connect(lambda: self.title_button.set_open(False))
        self.grid.day_selected.connect(self.select_day)
        self.grid.month_step.connect(self.step_month)
        self.grid.today_requested.connect(self.go_today)
        self.grid.picker_requested.connect(self.open_month_picker)
        self._render(direction=0)

    def _nav_button(self, text: str, tip: str, name: str) -> QToolButton:
        button = QToolButton(self)
        button.setObjectName(name)
        button.setText(text)
        button.setAccessibleName(tip.split(" (")[0])
        button.setToolTip(tip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    # ---- state ----

    @property
    def view(self) -> MonthKey:
        return self._view

    @property
    def selected_date(self) -> date:
        return self._selected

    def is_planned(self, day: date) -> bool:
        return self.grid.is_planned(day)

    def set_dates(self, latest: Optional[date], next_due: Optional[date], interval_days: int) -> None:
        self.latest, self.next_due, self.interval_days = latest, next_due, interval_days
        self.grid.latest, self.grid.next_due, self.grid.interval_days = latest, next_due, interval_days
        self.grid.update()
        self._refresh_picker()
        self._update_detail()

    # ---- moving about ----

    def show_month(self, key: MonthKey, selected: Optional[date] = None) -> None:
        direction = month_distance(self._view, key)
        self._view = key
        if selected is not None and month_key(selected) == key:
            self._selected = selected
        elif month_key(self._selected) != key:
            today = date.today()
            self._selected = today if month_key(today) == key else date(key[0], key[1], 1)
        self._render(direction=(direction > 0) - (direction < 0))

    def step_month(self, step: int) -> None:
        self.show_month(shift_month(self._view, step))

    def select_day(self, day: date) -> None:
        if month_key(day) != self._view:
            self.show_month(month_key(day), day)
            return
        self._selected = day
        self.grid.set_selected(day, animate=True)
        self._update_detail()

    def go_today(self) -> None:
        today = date.today()
        if month_key(today) == self._view:
            self.select_day(today)
        else:
            self.show_month(month_key(today), today)
        self.grid.pulse_today()

    def open_month_picker(self) -> None:
        self._refresh_picker()
        self.title_button.set_open(True)
        self.month_picker.popup(self.title_button)

    def _on_month_picked(self, key: MonthKey) -> None:
        self.show_month(key)
        self.grid.setFocus()

    # ---- rendering ----

    def _render(self, direction: int) -> None:
        year, month = self._view
        self.grid.set_month(year, month, days={}, items={}, milestones={}, today=date.today(),
                            first_weekday=calendar.MONDAY, direction=direction)
        self.grid.set_selected(self._selected, animate=False)
        first = date(year, month, 1)
        self.title_button.set_title(first.strftime("%B %Y"), first.strftime("%b %Y"))
        self._refresh_picker()
        self._update_detail()

    def _refresh_picker(self) -> None:
        today = date.today()
        self.month_picker.set_data(
            _month_counts(self.latest, self.next_due, self.interval_days, today),
            self._view, month_key(today),
        )

    def _update_detail(self) -> None:
        found = [long for _short, long in self.grid.events(self._selected)]
        text = " · ".join(found) if found else "No backup shown for this date"
        day = self._selected
        self.detail_label.setText(f"{day.strftime('%b')} {day.day} — {text}")

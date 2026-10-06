"""A date overview of the latest backup and the saved automatic schedule."""

from PyQt6.QtCore import QDate, QLocale, QRectF, Qt
from PyQt6.QtGui import QPainter, QPen, QTextCharFormat
from PyQt6.QtWidgets import (
    QCalendarWidget,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ui_qt.utils.palette import current_palette
from ui_qt.utils.restyle import repolish
from ui_qt.widgets.wrapped_label import WrappedLabel


class _BackupDates(QCalendarWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("backupCalendarGrid")
        self.setNavigationBarVisible(False)
        self.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self.setHorizontalHeaderFormat(QCalendarWidget.HorizontalHeaderFormat.ShortDayNames)
        self.setFirstDayOfWeek(Qt.DayOfWeek.Monday)
        # Qt marks weekend headings red by default, which implies a warning
        # in a calendar where every day can run a backup.
        for day in (Qt.DayOfWeek.Saturday, Qt.DayOfWeek.Sunday):
            self.setWeekdayTextFormat(day, QTextCharFormat())
        self.setMinimumSize(230, 210)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.latest = QDate()
        self.next_due = QDate()
        self.interval_days = 0

    def is_planned(self, date: QDate) -> bool:
        days = self.next_due.daysTo(date)
        return self.next_due.isValid() and self.interval_days > 0 and days >= 0 and days % self.interval_days == 0

    def paintCell(self, painter: QPainter, rect, date: QDate) -> None:
        palette = current_palette()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        cell = QRectF(rect).adjusted(3, 2, -3, -2)
        selected = date == self.selectedDate()
        today = date == QDate.currentDate()
        painter.setPen(QPen(palette.color("accent-border"), 1) if today else Qt.PenStyle.NoPen)
        painter.setBrush(palette.color("accent-tint") if selected else Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(cell, 7, 7)
        font = painter.font()
        font.setBold(selected or today)
        painter.setFont(font)
        painter.setPen(palette.color("slate-text" if date.month() == self.monthShown() else "slate-text-4"))
        painter.drawText(cell.adjusted(0, 0, 0, -5), Qt.AlignmentFlag.AlignCenter, str(date.day()))
        latest, planned = date == self.latest, self.is_planned(date)
        center = cell.center().x()
        y = cell.bottom() - 5
        if latest:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(palette.color("success-text"))
            painter.drawEllipse(QRectF(center - (6 if planned else 2), y - 2, 4, 4))
        if planned:
            painter.setPen(QPen(palette.color("accent-soft"), 1.3))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QRectF(center + (2 if latest else -2), y - 2, 4, 4))
        painter.restore()


class BackupCalendar(QWidget):
    """Day grid whose title zooms out to a month picker, then a year picker."""

    DAYS, MONTHS, YEARS = range(3)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("backupCalendar")
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        navigation = QHBoxLayout()
        self.title_button = QToolButton(self)
        self.title_button.setObjectName("backupCalendarTitle")
        navigation.addWidget(self.title_button)
        navigation.addStretch(1)
        self.today_button = self._nav_button("Today", "Show today")
        self.previous_button = self._nav_button("‹", "Previous month")
        self.next_button = self._nav_button("›", "Next month")
        for button in (self.today_button, self.previous_button, self.next_button):
            navigation.addWidget(button)
        column.addLayout(navigation)
        self.views = QStackedWidget(self)
        self.views.setObjectName("backupCalendarViews")
        self.grid = _BackupDates(self.views)
        self.grid.setAccessibleName("Backup calendar")
        self.views.addWidget(self.grid)
        self.picker = QWidget(self.views)
        self.picker.setObjectName("backupCalendarPicker")
        cells = QGridLayout(self.picker)
        cells.setContentsMargins(0, 0, 0, 0)
        cells.setSpacing(6)
        self.period_buttons = []
        for index in range(12):
            button = QToolButton(self.picker)
            button.setObjectName("backupCalendarPeriod")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.clicked.connect(lambda _checked=False, i=index: self._pick(i))
            cells.addWidget(button, index // 4, index % 4)
            self.period_buttons.append(button)
        self.views.addWidget(self.picker)
        column.addWidget(self.views)
        legend = QHBoxLayout()
        for text, name in (("● Latest backup", "backupCalendarLatest"),
                           ("○ Planned", "backupCalendarPlanned")):
            label = QLabel(text)
            label.setObjectName(name)
            legend.addWidget(label)
        legend.addStretch()
        column.addLayout(legend)
        self.detail_label = WrappedLabel("")
        self.detail_label.setObjectName("backupCalendarDetail")
        self.detail_label.setAccessibleName("Selected backup date")
        column.addWidget(self.detail_label)
        self._view = self.DAYS
        self._year = self.grid.yearShown()
        self.title_button.clicked.connect(self._zoom_out)
        self.previous_button.clicked.connect(lambda: self._step(-1))
        self.next_button.clicked.connect(lambda: self._step(1))
        self.today_button.clicked.connect(self._show_today)
        self.grid.currentPageChanged.connect(self._refresh)
        self.grid.selectionChanged.connect(self._update_detail)
        self._refresh()
        self._update_detail()

    def _nav_button(self, text: str, accessible_name: str) -> QToolButton:
        button = QToolButton(self)
        button.setObjectName("backupCalendarNav")
        button.setText(text)
        button.setAccessibleName(accessible_name)
        button.setToolTip(accessible_name)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    def set_dates(self, latest: QDate, next_due: QDate, interval_days: int) -> None:
        self.grid.latest = latest
        self.grid.next_due = next_due
        self.grid.interval_days = interval_days
        self.grid.updateCells()
        self._update_detail()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape and self._view != self.DAYS:
            self._show(self.DAYS)
            event.accept()
            return
        super().keyPressEvent(event)

    def _show_today(self) -> None:
        today = QDate.currentDate()
        self.grid.setSelectedDate(today)
        self.grid.setCurrentPage(today.year(), today.month())
        self._show(self.DAYS)

    def _zoom_out(self) -> None:
        if self._view == self.DAYS:
            self._year = self.grid.yearShown()
        self._show(min(self._view + 1, self.YEARS))

    def _step(self, direction: int) -> None:
        if self._view == self.DAYS:
            (self.grid.showNextMonth if direction > 0 else self.grid.showPreviousMonth)()
            return
        self._year += direction * (1 if self._view == self.MONTHS else 10)
        self._refresh()

    def _pick(self, index: int) -> None:
        if self._view == self.YEARS:
            self._year = self._first_year() + index
            self._show(self.MONTHS)
            return
        self.grid.setCurrentPage(self._year, index + 1)
        self._show(self.DAYS)

    def _show(self, view: int) -> None:
        # Hiding the picker would otherwise send keyboard focus to whichever
        # widget follows the calendar in the Settings tab order.
        picker_had_focus = any(button.hasFocus() for button in self.period_buttons)
        self._view = view
        self.views.setCurrentWidget(self.grid if view == self.DAYS else self.picker)
        if picker_had_focus and view == self.DAYS:
            self.grid.setFocus()
        self._refresh()

    def _first_year(self) -> int:
        return self._year // 10 * 10 - 1

    def _refresh(self, *_args) -> None:
        locale = QLocale()
        shown = QDate(self.grid.yearShown(), self.grid.monthShown(), 1)
        today = QDate.currentDate()
        if self._view == self.DAYS:
            title, unit, hint = locale.toString(shown, "MMMM yyyy"), "month", "Choose month"
        elif self._view == self.MONTHS:
            title, unit, hint = str(self._year), "year", "Choose year"
            for index, button in enumerate(self.period_buttons):
                month = QDate(self._year, index + 1, 1)
                self._set_period(
                    button, locale.standaloneMonthName(month.month(), QLocale.FormatType.ShortFormat),
                    locale.toString(month, "MMMM yyyy"),
                    current=month == shown,
                    today=(month.year(), month.month()) == (today.year(), today.month()),
                    outside=False,
                )
        else:
            first = self._first_year()
            title, unit, hint = f"{first + 1} – {first + 10}", "decade", ""
            for index, button in enumerate(self.period_buttons):
                year = first + index
                self._set_period(
                    button, str(year), str(year),
                    current=year == self._year, today=year == today.year(),
                    outside=index in (0, len(self.period_buttons) - 1),
                )
        zoomable = self._view != self.YEARS
        self.title_button.setText(f"{title} ▾" if zoomable else title)
        self.title_button.setAccessibleName(title)
        self.title_button.setToolTip(hint)
        self.title_button.setEnabled(zoomable)
        if zoomable:
            self.title_button.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.title_button.unsetCursor()
        for button, direction in ((self.previous_button, "Previous"), (self.next_button, "Next")):
            button.setAccessibleName(f"{direction} {unit}")
            button.setToolTip(f"{direction} {unit}")

    @staticmethod
    def _set_period(button: QToolButton, text: str, name: str, **states: bool) -> None:
        button.setText(text)
        button.setAccessibleName(name)
        for state, value in states.items():
            button.setProperty(state, value)
        repolish(button)

    def _update_detail(self) -> None:
        date = self.grid.selectedDate()
        events = []
        if date == self.grid.latest:
            events.append("Latest backup saved")
        if self.grid.is_planned(date):
            events.append("Automatic backup planned")
        text = " · ".join(events) if events else "No backup shown for this date"
        self.detail_label.setText(f"{QLocale().toString(date, 'MMM d')} — {text}")

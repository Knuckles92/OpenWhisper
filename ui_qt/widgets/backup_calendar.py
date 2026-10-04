"""A date overview of the latest backup and the saved automatic schedule."""

from PyQt6.QtCore import QDate, QLocale, QRectF, Qt
from PyQt6.QtGui import QPainter, QPen, QTextCharFormat
from PyQt6.QtWidgets import (
    QCalendarWidget,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ui_qt.utils.palette import current_palette
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
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("backupCalendar")
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        navigation = QHBoxLayout()
        self.month_label = QLabel()
        self.month_label.setObjectName("backupCalendarMonth")
        navigation.addWidget(self.month_label, stretch=1)
        self.today_button = self._nav_button("Today", "Show today")
        self.previous_button = self._nav_button("‹", "Previous month")
        self.next_button = self._nav_button("›", "Next month")
        for button in (self.today_button, self.previous_button, self.next_button):
            navigation.addWidget(button)
        column.addLayout(navigation)
        self.grid = _BackupDates(self)
        self.grid.setAccessibleName("Backup calendar")
        column.addWidget(self.grid)
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
        self.previous_button.clicked.connect(self.grid.showPreviousMonth)
        self.next_button.clicked.connect(self.grid.showNextMonth)
        self.today_button.clicked.connect(self._show_today)
        self.grid.currentPageChanged.connect(self._update_month)
        self.grid.selectionChanged.connect(self._update_detail)
        self._update_month()
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

    def _show_today(self) -> None:
        today = QDate.currentDate()
        self.grid.setSelectedDate(today)
        self.grid.setCurrentPage(today.year(), today.month())

    def _update_month(self, *_args) -> None:
        date = QDate(self.grid.yearShown(), self.grid.monthShown(), 1)
        self.month_label.setText(QLocale().toString(date, "MMMM yyyy"))

    def _update_detail(self) -> None:
        date = self.grid.selectedDate()
        events = []
        if date == self.grid.latest:
            events.append("Latest backup saved")
        if self.grid.is_planned(date):
            events.append("Automatic backup planned")
        text = " · ".join(events) if events else "No backup shown for this date"
        self.detail_label.setText(f"{QLocale().toString(date, 'MMM d')} — {text}")

"""History calendar: everything recorded, by month, on the day it happened.

Dictations, transcribed files and meetings share one month grid. Each day is
tinted by how much was recorded and lists what it holds, coloured by kind,
with a bar showing the mix; the agenda beside it shows the selected day in
full. The kind chips double as the legend and a filter, and a Where filter
appears once records are kept on more than one computer.

Reads run off the Qt thread (services/history_calendar.CalendarSource). The
index of every record arrives first and gives each day its heat, then the
month's rows fill in its lines; neighbouring months are read ahead so paging
through them doesn't wait.
"""
from __future__ import annotations

import calendar
import logging
import threading
from datetime import date
from typing import Callable, Dict, FrozenSet, List, Optional

from PyQt6.QtCore import QLocale, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)

from services.format_utils import format_audio_duration
from services.history_calendar import (
    KINDS, MEETING, ON, CalendarIndex, CalendarItem, CalendarSource, MonthKey, Place,
    month_distance, month_key, place_label, shift_month,
)
from ui_qt.widgets.history_calendar import (
    AgendaCard, KindChip, MonthGrid, YearStrip, fade_in, kind_breakdown, plural,
)

logger = logging.getLogger(__name__)

_STYLE = """
    QDialog#historyCalendarDialog {
        background-color: @bg;
    }
    QLabel#calendarMonthTitle {
        color: @text-heading;
        background: transparent;
        font-size: 24px;
        font-weight: 700;
    }
    QPushButton#calendarNavBtn {
        background-color: @surface;
        color: @text;
        border: 1px solid rgba(@overlay-rgb, 0.06);
        border-radius: 16px;
        padding: 0px;
        font-size: 18px;
        font-weight: 600;
    }
    QPushButton#calendarNavBtn:hover {
        background-color: @surface-hover;
        color: @accent-soft;
    }
    QPushButton#calendarTodayBtn {
        background-color: rgba(@accent-rgb, 0.14);
        color: @accent-soft;
        border: 1px solid rgba(@accent-rgb, 0.3);
        border-radius: 14px;
        padding: 4px 14px;
        font-size: 12px;
        font-weight: 600;
    }
    QPushButton#calendarTodayBtn:hover {
        background-color: rgba(@accent-rgb, 0.24);
    }
    QPushButton#calendarKindChip {
        background-color: transparent;
        color: @text-secondary;
        border: 1px solid rgba(@overlay-rgb, 0.1);
        border-radius: 14px;
        padding: 4px 12px 4px 26px;
        font-size: 12px;
        font-weight: 600;
    }
    QPushButton#calendarKindChip:hover {
        background-color: rgba(@overlay-rgb, 0.05);
    }
    QPushButton#calendarKindChip:checked {
        background-color: @surface;
        color: @text;
        border: 1px solid rgba(@overlay-rgb, 0.14);
    }
    QComboBox#calendarPlaceCombo {
        padding: 4px 12px;
        font-size: 12px;
        border-radius: 14px;
    }
    QLabel#calendarStats {
        color: @text-secondary;
        background: transparent;
        font-size: 13px;
    }
    QPushButton#calendarBusiestBtn {
        background: transparent;
        color: @accent-soft;
        border: none;
        padding: 0px 2px;
        font-size: 13px;
        font-weight: 600;
    }
    QPushButton#calendarBusiestBtn:hover {
        color: @accent;
        text-decoration: underline;
    }
    QLabel#calendarStreak {
        background-color: rgba(@warning-rgb, 0.14);
        color: @warning-text;
        border: 1px solid rgba(@warning-rgb, 0.3);
        border-radius: 11px;
        padding: 2px 10px;
        font-size: 12px;
        font-weight: 600;
    }
    QLabel#calendarNotice, QLabel#calendarHint {
        color: @text-muted;
        background: transparent;
        font-size: 11px;
    }
    QFrame#calendarAgenda {
        background-color: rgba(@surface-rgb, 0.45);
        border: 1px solid rgba(@overlay-rgb, 0.06);
        border-radius: 14px;
    }
    QLabel#calendarAgendaWeekday {
        color: @accent-soft;
        background: transparent;
        font-size: 13px;
        font-weight: 600;
    }
    QLabel#calendarTodayBadge {
        color: @on-accent;
        background-color: @accent;
        border-radius: 8px;
        padding: 1px 8px;
        font-size: 10px;
        font-weight: 700;
    }
    QLabel#calendarAgendaDate {
        color: @text-heading;
        background: transparent;
        font-size: 21px;
        font-weight: 700;
    }
    QLabel#calendarAgendaSummary {
        color: @text-secondary;
        background: transparent;
        font-size: 12px;
    }
    QScrollArea#calendarAgendaScroll, QWidget#calendarAgendaList,
    QWidget#calendarEmptyState {
        background: transparent;
        border: none;
    }
    QScrollArea#calendarAgendaScroll QScrollBar:vertical {
        background: transparent;
        width: 8px;
    }
    QScrollArea#calendarAgendaScroll QScrollBar::handle:vertical {
        background: rgba(@overlay-rgb, 0.15);
        border-radius: 4px;
        min-height: 30px;
    }
    QScrollArea#calendarAgendaScroll QScrollBar::add-line:vertical,
    QScrollArea#calendarAgendaScroll QScrollBar::sub-line:vertical {
        height: 0px;
    }
    QFrame#calendarAgendaCard {
        background-color: rgba(@surface-rgb, 0.8);
        border: 1px solid rgba(@overlay-rgb, 0.05);
        border-left: 3px solid @accent;
        border-radius: 10px;
    }
    QFrame#calendarAgendaCard[kind="file"] {
        border-left: 3px solid @purple;
    }
    QFrame#calendarAgendaCard[kind="meeting"] {
        border-left: 3px solid @warning;
    }
    QFrame#calendarAgendaCard:hover {
        background-color: rgba(@surface-hover-rgb, 0.9);
        border-top: 1px solid rgba(@accent-rgb, 0.35);
        border-right: 1px solid rgba(@accent-rgb, 0.35);
        border-bottom: 1px solid rgba(@accent-rgb, 0.35);
    }
    QLabel#calendarCardTime {
        color: @text-secondary-strong;
        background: transparent;
        font-size: 11px;
        font-weight: 600;
    }
    QLabel#calendarKindPill {
        color: @accent-soft;
        background-color: rgba(@accent-rgb, 0.14);
        border-radius: 6px;
        padding: 1px 7px;
        font-size: 10px;
        font-weight: 700;
    }
    QLabel#calendarKindPill[kind="file"] {
        color: @purple-text;
        background-color: rgba(@purple-rgb, 0.14);
    }
    QLabel#calendarKindPill[kind="meeting"] {
        color: @warning-text;
        background-color: rgba(@warning-rgb, 0.14);
    }
    QLabel#calendarPlaceChip {
        color: @success-text;
        background-color: rgba(@success-rgb, 0.10);
        border: 1px solid rgba(@success-rgb, 0.24);
        border-radius: 6px;
        padding: 0px 7px;
        font-size: 10px;
        font-weight: 600;
    }
    QLabel#calendarLengthChip {
        color: @text-tertiary;
        background-color: rgba(@overlay-rgb, 0.06);
        border-radius: 6px;
        padding: 1px 7px;
        font-size: 10px;
        font-weight: 600;
    }
    QLabel#calendarCardTitle {
        color: @text;
        background: transparent;
        font-size: 13px;
        font-weight: 600;
    }
    QLabel#calendarCardPreview {
        color: @text-body;
        background: transparent;
        font-size: 12px;
    }
    QLabel#calendarMilestone {
        color: @warning-text;
        background: transparent;
        font-size: 11px;
        font-weight: 700;
    }
    QLabel#calendarCardProgress {
        color: @text-secondary;
        background: transparent;
        font-size: 11px;
    }
    QLabel#calendarEmptyTitle {
        color: @text-soft;
        background: transparent;
        font-size: 14px;
        font-weight: 600;
    }
    QLabel#calendarEmptyBody {
        color: @text-secondary;
        background: transparent;
        font-size: 12px;
    }
    QPushButton#calendarJumpBtn {
        background-color: @surface;
        color: @accent-soft;
        border: 1px solid rgba(@overlay-rgb, 0.08);
        border-radius: 14px;
        padding: 5px 14px;
        font-size: 12px;
        font-weight: 600;
    }
    QPushButton#calendarJumpBtn:hover {
        background-color: @surface-hover;
    }
"""


def _python_first_weekday() -> int:
    """The locale's first day of the week in Python's numbering (0 is Monday)."""
    try:
        return (QLocale().firstDayOfWeek().value - 1) % 7
    except Exception:
        return calendar.SUNDAY


def _month_title(key: MonthKey) -> str:
    return f"{calendar.month_name[key[1]]} {key[0]}"


class HistoryCalendarDialog(QDialog):
    """A month of recordings: what, when, and where each one is kept.

    Opening an entry or meeting is left to the main window, which already
    knows how: ``entry_requested`` carries the History entry, and
    ``meeting_requested`` the meeting id (after fetching a host-kept one).
    """

    entry_requested = pyqtSignal(object)
    meeting_requested = pyqtSignal(str)
    meeting_copy_requested = pyqtSignal(str)
    entry_copied = pyqtSignal(str)

    _index_loaded = pyqtSignal(int, object, str)
    _month_loaded = pyqtSignal(int, int, int, object)
    _fetch_progress = pyqtSignal(str, str)
    _fetch_done = pyqtSignal(str, str)

    #: Months read ahead on each side of the one shown.
    PREFETCH = 1

    def __init__(self, parent=None, *, source: Optional[CalendarSource] = None,
                 today: Callable[[], date] = date.today,
                 first_weekday: Optional[int] = None, threaded: bool = True):
        super().__init__(parent)
        self.setObjectName("historyCalendarDialog")
        self.setWindowTitle("History Calendar")
        self.setModal(False)
        self.setWindowFlag(Qt.WindowType.WindowMinMaxButtonsHint, True)
        # No explicit minimum: the layout's own keeps the grid and the agenda
        # from ever overlapping, whatever the UI font scale.
        self._fit_to_screen(1180, 800)
        self.setStyleSheet(_STYLE)

        self._source = source or CalendarSource()
        self._today = today
        self._first_weekday = _python_first_weekday() if first_weekday is None else first_weekday
        self._threaded = threaded
        self._index = CalendarIndex()
        self._loaded = False
        self._stale = True
        self._generation = 0
        #: Rows read per month; ``_fresh`` holds those read since the last refresh.
        self._months: Dict[MonthKey, List[CalendarItem]] = {}
        self._fresh: set = set()
        self._loading: set = set()
        self._view: MonthKey = month_key(today())
        self._selected: date = today()
        self._agenda_day: Optional[date] = None
        self._kinds: Optional[FrozenSet[str]] = None
        self._place: Optional[Place] = None
        self._notice = ""
        self._cards: List[AgendaCard] = []
        self._fetching: set = set()
        self._pulse_pending = True
        # Changes elsewhere tend to arrive in bursts (a transcription refreshes
        # History and Past Meetings together), so their refreshes are merged.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(60)
        self._refresh_timer.timeout.connect(self.refresh)
        # Deferred work runs on timers the dialog owns, so none of it can fire
        # after the dialog is gone (a bare singleShot lambda would).
        self._pulse_timer = QTimer(self)
        self._pulse_timer.setSingleShot(True)
        self._pulse_timer.setInterval(220)
        self._pulse_timer.timeout.connect(self._pulse_today)
        self._scroll_to = 0
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.setInterval(0)
        self._scroll_timer.timeout.connect(self._restore_agenda_scroll)

        self._index_loaded.connect(self._on_index_loaded)
        self._month_loaded.connect(self._on_month_loaded)
        self._fetch_progress.connect(self._on_fetch_progress)
        self._fetch_done.connect(self._on_fetch_done)
        self._setup_ui()

    # ---- building ----

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 14)
        root.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(8)
        # Today and the arrows come first so they stay put while the month's
        # name changes length beside them.
        self.today_button = QPushButton("Today")
        self.today_button.setObjectName("calendarTodayBtn")
        self.today_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.today_button.setFixedHeight(30)
        self.today_button.setToolTip("Go to today (T)")
        self.today_button.clicked.connect(self.go_today)
        header.addWidget(self.today_button, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addSpacing(4)
        self.prev_button = QPushButton("‹")
        self.next_button = QPushButton("›")
        for button, tip in ((self.prev_button, "Previous month (Page Up)"),
                            (self.next_button, "Next month (Page Down)")):
            button.setObjectName("calendarNavBtn")
            button.setFixedSize(32, 32)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(tip)
        self.prev_button.setAccessibleName("Previous month")
        self.next_button.setAccessibleName("Next month")
        self.prev_button.clicked.connect(lambda: self.step_month(-1))
        self.next_button.clicked.connect(lambda: self.step_month(1))
        header.addWidget(self.prev_button)
        header.addWidget(self.next_button)
        header.addSpacing(8)
        self.month_title = QLabel(_month_title(self._view))
        self.month_title.setObjectName("calendarMonthTitle")
        header.addWidget(self.month_title)
        header.addSpacing(4)
        self.streak_label = QLabel("")
        self.streak_label.setObjectName("calendarStreak")
        self.streak_label.setToolTip("Days in a row with a recording, up to today")
        self.streak_label.setFixedHeight(24)
        self.streak_label.hide()
        header.addWidget(self.streak_label, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addStretch()

        self.kind_chips: Dict[str, KindChip] = {}
        for kind in KINDS:
            chip = KindChip(kind)
            chip.toggled.connect(self._on_kind_toggled)
            self.kind_chips[kind] = chip
            header.addWidget(chip, 0, Qt.AlignmentFlag.AlignVCenter)
        self.place_combo = QComboBox()
        self.place_combo.setObjectName("calendarPlaceCombo")
        self.place_combo.setToolTip("Where the records are kept")
        self.place_combo.currentIndexChanged.connect(self._on_place_changed)
        self.place_combo.hide()
        header.addWidget(self.place_combo, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(header)

        body = QHBoxLayout()
        body.setSpacing(16)

        month_column = QVBoxLayout()
        month_column.setSpacing(8)
        summary = QHBoxLayout()
        summary.setSpacing(10)
        self.stats_label = QLabel("")
        self.stats_label.setObjectName("calendarStats")
        self.stats_label.setTextFormat(Qt.TextFormat.RichText)
        summary.addWidget(self.stats_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self.busiest_button = QPushButton("")
        self.busiest_button.setObjectName("calendarBusiestBtn")
        self.busiest_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.busiest_button.setToolTip("Show the day with the most recordings")
        self.busiest_button.clicked.connect(self._show_busiest)
        self.busiest_button.hide()
        summary.addWidget(self.busiest_button, 0, Qt.AlignmentFlag.AlignVCenter)
        summary.addStretch()
        month_column.addLayout(summary)
        self.notice_label = QLabel("")
        self.notice_label.setObjectName("calendarNotice")
        self.notice_label.setWordWrap(True)
        self.notice_label.hide()
        month_column.addWidget(self.notice_label)
        self.grid = MonthGrid()
        self.grid.day_selected.connect(self.select_day)
        self.grid.day_activated.connect(self._activate_day)
        self.grid.month_step.connect(self.step_month)
        self.grid.today_requested.connect(self.go_today)
        month_column.addWidget(self.grid, stretch=1)
        body.addLayout(month_column, stretch=1)

        day_column = QVBoxLayout()
        day_column.setSpacing(8)
        self.year_strip = YearStrip()
        self.year_strip.month_clicked.connect(self.show_month)
        day_column.addWidget(self.year_strip)
        day_column.addWidget(self._build_agenda(), stretch=1)
        body.addLayout(day_column)
        root.addLayout(body, stretch=1)

        self.hint_label = QLabel(
            "Arrow keys move between days · Page Up and Page Down change the month · "
            "T goes to today · Enter opens the day's first recording"
        )
        self.hint_label.setObjectName("calendarHint")
        root.addWidget(self.hint_label)

    def _build_agenda(self) -> QFrame:
        self.agenda = QFrame()
        self.agenda.setObjectName("calendarAgenda")
        self.agenda.setFixedWidth(348)
        self.agenda.setMinimumHeight(240)
        layout = QVBoxLayout(self.agenda)
        layout.setContentsMargins(16, 14, 8, 12)
        layout.setSpacing(4)

        weekday_row = QHBoxLayout()
        weekday_row.setContentsMargins(0, 0, 8, 0)
        weekday_row.setSpacing(8)
        self.agenda_weekday = QLabel("")
        self.agenda_weekday.setObjectName("calendarAgendaWeekday")
        weekday_row.addWidget(self.agenda_weekday)
        self.today_badge = QLabel("Today")
        self.today_badge.setObjectName("calendarTodayBadge")
        self.today_badge.hide()
        weekday_row.addWidget(self.today_badge)
        weekday_row.addStretch()
        layout.addLayout(weekday_row)
        self.agenda_date = QLabel("")
        self.agenda_date.setObjectName("calendarAgendaDate")
        layout.addWidget(self.agenda_date)
        self.agenda_summary = QLabel("")
        self.agenda_summary.setObjectName("calendarAgendaSummary")
        self.agenda_summary.setWordWrap(True)
        layout.addWidget(self.agenda_summary)
        layout.addSpacing(8)

        self.agenda_scroll = QScrollArea()
        self.agenda_scroll.setObjectName("calendarAgendaScroll")
        self.agenda_scroll.setWidgetResizable(True)
        self.agenda_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.agenda_list = QWidget()
        self.agenda_list.setObjectName("calendarAgendaList")
        self.agenda_list.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.agenda_layout = QVBoxLayout(self.agenda_list)
        self.agenda_layout.setContentsMargins(0, 0, 8, 0)
        self.agenda_layout.setSpacing(10)
        self.agenda_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.agenda_scroll.setWidget(self.agenda_list)
        layout.addWidget(self.agenda_scroll, stretch=1)
        return self.agenda

    def _fit_to_screen(self, width: int, height: int) -> None:
        """Open at ``width`` x ``height``, or smaller on a screen that can't hold it."""
        parent = self.parentWidget()
        screen = parent.screen() if parent is not None else QApplication.primaryScreen()
        if screen is not None:
            room = screen.availableGeometry()
            width = min(width, room.width() - 40)
            height = min(height, room.height() - 60)
        self.resize(width, height)

    # ---- loading ----

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not event.spontaneous():
            # Opened again: back to today, with the pulse that shows where it
            # is. Restoring a minimized window keeps its place.
            today = self._today()
            if self._loaded and (self._view, self._selected) != (month_key(today), today):
                self.show_month(month_key(today), today)
            self._pulse_pending = True
        if self._stale:
            self.refresh()
        elif self._pulse_pending:
            self._pulse_pending = False
            self._pulse_timer.start()
        self.grid.setFocus()

    def refresh(self) -> None:
        """Read everything again: after a recording, delete or edit elsewhere."""
        self._refresh_timer.stop()
        self._stale = False
        self._generation += 1
        generation = self._generation
        self._fresh = set()
        view = self._view
        # _load_all reads these; _render mustn't ask for them again meanwhile.
        self._loading = {view} | {shift_month(view, step)
                                  for step in range(-self.PREFETCH, self.PREFETCH + 1)}
        self._run(lambda: self._load_all(generation, view))

    def refresh_if_visible(self) -> None:
        """Refresh soon when shown, or the next time it is."""
        if self.isVisible():
            self._refresh_timer.start()
        else:
            self._stale = True

    def _run(self, work: Callable[[], None]) -> None:
        if not self._threaded:
            work()
            return
        threading.Thread(target=work, name="history-calendar-load", daemon=True).start()

    def _load_all(self, generation: int, view: MonthKey) -> None:
        try:
            marks = self._source.load_index()
        except Exception as exc:
            logger.error("Couldn't read the History calendar: %s", exc)
            self._index_loaded.emit(generation, None, "History could not be loaded.")
            return
        self._index_loaded.emit(generation, marks, "")
        self._load_months(generation, [view])
        if self._source.remote_wanted():
            remote = self._source.load_remote(mark.record_id for mark in marks)
            self._index_loaded.emit(generation, marks + remote, self._source.notice)
            if remote:
                self._load_months(generation, [view])
        self._load_months(generation, [shift_month(view, step)
                                       for step in range(-self.PREFETCH, self.PREFETCH + 1) if step])

    def _load_months(self, generation: int, keys: List[MonthKey]) -> None:
        for key in keys:
            try:
                items = self._source.load_month(*key)
            except Exception as exc:
                logger.error("Couldn't read %s for the History calendar: %s", _month_title(key), exc)
                items = []
            self._month_loaded.emit(generation, key[0], key[1], items)

    def _request_months(self, center: MonthKey) -> None:
        """Read the shown month and its neighbours, unless already read or on the way."""
        wanted = [shift_month(center, step) for step in (0, 1, -1)[: 1 + 2 * self.PREFETCH]]
        missing = [key for key in wanted if key not in self._fresh and key not in self._loading]
        if not missing:
            return
        self._loading.update(missing)
        generation = self._generation
        self._run(lambda: self._load_months(generation, missing))

    def _on_index_loaded(self, generation: int, marks, notice: str) -> None:
        if generation != self._generation:
            return
        if marks is None:
            # Nothing is on its way now; let navigation ask again.
            self._loading = set()
        self._loaded = True
        self._notice = notice
        self._index = CalendarIndex(marks or ())
        self._sync_place_filter()
        self._render(direction=0)
        if self._pulse_pending and self.isVisible():
            self._pulse_pending = False
            self._pulse_timer.start()

    def _on_month_loaded(self, generation: int, year: int, month: int, items) -> None:
        if generation != self._generation:
            return
        key = (year, month)
        self._loading.discard(key)
        self._fresh.add(key)
        self._months[key] = list(items or [])
        if key == self._view:
            self._render_details()

    # ---- filters ----

    def _on_kind_toggled(self, _checked: bool) -> None:
        checked = [kind for kind, chip in self.kind_chips.items() if chip.isChecked()]
        if not checked:
            # One kind always stays shown; turning off the last shows it again.
            chip = self.sender()
            if isinstance(chip, KindChip):
                chip.blockSignals(True)
                chip.setChecked(True)
                chip.blockSignals(False)
            return
        self._kinds = None if len(checked) == len(KINDS) else frozenset(checked)
        self._render(direction=0)

    def _sync_place_filter(self) -> None:
        places = self._index.places()
        self.place_combo.blockSignals(True)
        self.place_combo.clear()
        self.place_combo.addItem(place_label(None), None)
        for place in places:
            self.place_combo.addItem(place_label(place), place)
        if self._place not in places:
            self._place = None
        index = next((row for row in range(1, self.place_combo.count())
                      if self.place_combo.itemData(row) == self._place), 0)
        self.place_combo.setCurrentIndex(index)
        self.place_combo.blockSignals(False)
        self.place_combo.setVisible(len(places) > 1)

    def _on_place_changed(self, index: int) -> None:
        self._place = self.place_combo.itemData(index) if index > 0 else None
        self._render(direction=0)

    def _visible(self, item: CalendarItem) -> bool:
        return ((self._kinds is None or item.kind in self._kinds)
                and (self._place is None or item.place == self._place))

    # ---- navigation ----

    def show_month(self, key: MonthKey, day: Optional[date] = None) -> None:
        """Go to a month, selecting ``day`` or the month's most recent active day."""
        key = (int(key[0]), int(key[1]))
        direction = month_distance(self._view, key)
        self._view = key
        self._selected = day if day is not None and month_key(day) == key else self._default_day(key)
        self._render(direction=direction)

    def step_month(self, step: int) -> None:
        self.show_month(shift_month(self._view, step))

    def go_today(self) -> None:
        today = self._today()
        if month_key(today) == self._view:
            self.select_day(today)
        else:
            self.show_month(month_key(today), today)
        self.grid.pulse_today()

    def select_day(self, day: date) -> None:
        if month_key(day) != self._view:
            self.show_month(month_key(day), day)
            return
        self._selected = day
        self.grid.set_selected(day, animate=True)
        self._render_agenda()

    def _default_day(self, key: MonthKey) -> date:
        days = self._index.days(*key, self._kinds, self._place)
        today = self._today()
        if month_key(today) == key:
            if today in days or not days:
                return today
            earlier = [day for day in days if day <= today]
            return max(earlier) if earlier else min(days)
        return max(days) if days else date(key[0], key[1], 1)

    def _show_busiest(self) -> None:
        stats = self._index.month_stats(*self._view, self._kinds, self._place)
        if stats.busiest:
            self.select_day(stats.busiest[0])

    def _activate_day(self, day: date) -> None:
        if month_key(day) != self._view:
            return
        items = self._day_items(day)
        if items:
            self._open(items[0])

    # ---- rendering ----

    def _items_by_day(self) -> Dict[date, List[CalendarItem]]:
        result: Dict[date, List[CalendarItem]] = {}
        for item in self._months.get(self._view) or []:
            if self._visible(item):
                result.setdefault(item.day, []).append(item)
        return result

    def _day_items(self, day: date) -> List[CalendarItem]:
        return self._items_by_day().get(day, [])

    def _render(self, direction: int) -> None:
        year, month = self._view
        self.month_title.setText(_month_title(self._view))
        if month_key(self._selected) != self._view:
            self._selected = self._default_day(self._view)
        self.grid.set_month(
            year, month,
            days=self._index.days(year, month, self._kinds, self._place),
            items=self._items_by_day(),
            milestones=self._index.milestone_days(year, month),
            today=self._today(),
            first_weekday=self._first_weekday,
            direction=direction,
        )
        self.grid.set_selected(self._selected, animate=direction == 0)
        self._render_header()
        self._render_agenda(animate=True)
        if self._loaded:
            self._request_months(self._view)

    def _render_details(self) -> None:
        """The shown month's rows arrived: fill in its lines and agenda."""
        year, month = self._view
        self.grid.set_details(
            self._index.days(year, month, self._kinds, self._place),
            self._items_by_day(),
            self._index.milestone_days(year, month),
        )
        self._render_agenda()

    def _render_header(self) -> None:
        index, (year, month) = self._index, self._view
        stats = index.month_stats(year, month, self._kinds, self._place)
        kind_counts = index.month_stats(year, month, None, self._place).by_kind
        for kind, chip in self.kind_chips.items():
            chip.set_count(kind_counts.get(kind, 0) if self._loaded else None)

        if not self._loaded:
            self.stats_label.setText("Loading…")
        elif stats.count:
            parts = [f"<b>{stats.count:,}</b> {'recording' if stats.count == 1 else 'recordings'}"]
            if stats.seconds >= 1:
                parts.append(f"<b>{format_audio_duration(stats.seconds)}</b> of audio")
            parts.append(f"<b>{stats.active_days:,}</b> {'day' if stats.active_days == 1 else 'days'}")
            self.stats_label.setText(" · ".join(parts))
        else:
            self.stats_label.setText(f"Nothing recorded in {calendar.month_name[month]}")

        busiest = stats.busiest
        show_busiest = bool(busiest and stats.active_days > 1)
        if show_busiest:
            day, count = busiest
            self.busiest_button.setText(f"Busiest: {day.strftime('%a, %b')} {day.day} · {count:,}")
        self.busiest_button.setVisible(show_busiest)

        today = self._today()
        streak = index.streak(today) if month_key(today) == self._view else 0
        if streak >= 2:
            self.streak_label.setText(f"{streak}-day streak" + (" · keep it going" if streak >= 7 else ""))
        self.streak_label.setVisible(streak >= 2)

        self.year_strip.set_data(self._strip_months(), index.month_counts(self._kinds, self._place),
                                 self._view, month_key(today))

        notice = self._notice
        if notice and "reachable" in notice:
            notice = f"{notice} Records kept there show when it is."
        self.notice_label.setText(notice)
        self.notice_label.setVisible(bool(notice))

    def _strip_months(self) -> List[MonthKey]:
        """Twelve months: the year up to now, or centred on the month shown when older."""
        current = month_key(self._today())
        if month_distance(self._view, current) <= 11 and month_distance(current, self._view) <= 0:
            end = current
        else:
            end = shift_month(self._view, 6)
        return [shift_month(end, step) for step in range(-11, 1)]

    def _pulse_today(self) -> None:
        self.grid.pulse_today()

    def _restore_agenda_scroll(self) -> None:
        self.agenda_scroll.verticalScrollBar().setValue(self._scroll_to)

    def _clear_agenda(self) -> None:
        while self.agenda_layout.count():
            item = self.agenda_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # Hidden now: deleteLater waits for the event loop, and until
                # then the old cards would still show under the new ones.
                widget.hide()
                widget.deleteLater()
        self._cards = []

    def _render_agenda(self, animate: bool = True) -> None:
        day = self._selected
        today = self._today()
        self.agenda_weekday.setText(day.strftime("%A"))
        self.today_badge.setVisible(day == today)
        self.agenda_date.setText(f"{day.strftime('%B')} {day.day}"
                                 + ("" if day.year == today.year else f", {day.year}"))
        items = self._day_items(day)
        summary = self._index.days(*self._view, self._kinds, self._place).get(day)
        if summary and summary.count:
            text = plural(summary.count, "recording")
            if summary.seconds >= 1:
                text += f" · {format_audio_duration(summary.seconds)} of audio"
            if len(summary.by_kind) > 1:
                text += "\n" + kind_breakdown(summary.by_kind)
            self.agenda_summary.setText(text)
        else:
            self.agenda_summary.setText("Nothing recorded" if self._loaded else "")

        same_day = self._agenda_day == day
        had_cards = bool(self._cards)
        self._agenda_day = day
        scroll = self.agenda_scroll.verticalScrollBar().value() if same_day else 0
        self._clear_agenda()
        if items:
            for item in items:
                card = AgendaCard(item, self._index.milestone(item.record_id))
                card.activated.connect(self._open)
                card.copy_requested.connect(self._copy)
                if item.record_id in self._fetching:
                    card.set_progress(f"Getting it from {item.place[1]}…")
                self.agenda_layout.addWidget(card)
                self._cards.append(card)
            if animate and not (same_day and had_cards):
                fade_in(self._cards)
            self._scroll_to = scroll
            self._scroll_timer.start()
            return
        if summary and summary.count:
            self.agenda_layout.addWidget(self._empty_state("Loading this day…", ""))
            return
        self.agenda_layout.addWidget(self._empty_state_for(day))

    def _empty_state_for(self, day: date) -> QWidget:
        if not self._loaded:
            return self._empty_state("Loading…", "")
        if not len(self._index):
            return self._empty_state(
                "Nothing recorded yet",
                "Dictations, files you transcribe and meetings appear here on the day "
                "they happen, tinted brighter on busier days.",
            )
        stats = self._index.month_stats(*self._view, self._kinds, self._place)
        if stats.count:
            return self._empty_state(
                "A quiet day",
                "Nothing was recorded on this day. Days with recordings are tinted; "
                "the brighter the tint, the more was recorded.",
            )
        nearest = self._index.nearest_month(self._view, self._kinds, self._place)
        if nearest is not None and nearest != self._view:
            body = f"The closest month with recordings is {_month_title(nearest)}."
        elif self._filtered():
            body = "Nothing recorded yet matches these filters."
        else:
            body = ""
        widget = self._empty_state(
            f"Nothing recorded in {calendar.month_name[self._view[1]]}", body,
        )
        if nearest is not None and nearest != self._view:
            jump = QPushButton(("← " if month_distance(self._view, nearest) < 0 else "")
                               + f"Go to {_month_title(nearest)}"
                               + (" →" if month_distance(self._view, nearest) > 0 else ""))
            jump.setObjectName("calendarJumpBtn")
            jump.setFixedHeight(30)
            jump.setCursor(Qt.CursorShape.PointingHandCursor)
            jump.clicked.connect(lambda: self.show_month(nearest))
            widget.layout().addWidget(jump, 0, Qt.AlignmentFlag.AlignLeft)
        return widget

    def _filtered(self) -> bool:
        return self._kinds is not None or self._place is not None

    @staticmethod
    def _empty_state(title: str, body: str) -> QWidget:
        widget = QWidget()
        widget.setObjectName("calendarEmptyState")
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(2, 18, 10, 0)
        layout.setSpacing(6)
        heading = QLabel(title)
        heading.setObjectName("calendarEmptyTitle")
        heading.setWordWrap(True)
        layout.addWidget(heading)
        if body:
            text = QLabel(body)
            text.setObjectName("calendarEmptyBody")
            text.setWordWrap(True)
            layout.addWidget(text)
        layout.addSpacing(6)
        return widget

    # ---- opening and copying ----

    def _open(self, item: CalendarItem) -> None:
        if item.kind != MEETING:
            self.entry_requested.emit(item.source)
            return
        if item.place[0] == ON:
            self._fetch_meeting(item)
            return
        self.meeting_requested.emit(item.record_id)

    def _copy(self, item: CalendarItem) -> None:
        if item.kind == MEETING:
            self.meeting_copy_requested.emit(item.record_id)
            return
        try:
            QApplication.clipboard().setText(getattr(item.source, "text", "") or "")
        except Exception as exc:
            logger.error("Couldn't copy a calendar entry: %s", exc)
            return
        self.entry_copied.emit(item.record_id)

    def _fetch_meeting(self, item: CalendarItem) -> None:
        """Download a host-kept meeting here, then open it, as Past Meetings does."""
        meeting_id, host = item.record_id, item.place[1]
        if meeting_id in self._fetching:
            return
        self._fetching.add(meeting_id)
        self._on_fetch_progress(meeting_id, f"Getting it from {host}…")

        def work() -> None:
            from services.remote_records.sync import record_sync

            def progress(got: int, total: int) -> None:
                if total:
                    self._fetch_progress.emit(meeting_id, f"Getting it from {host}… {got * 100 // total}%")

            try:
                record_sync.check_out("meeting", meeting_id, progress)
            except Exception as exc:
                logger.warning("Couldn't get a meeting from the host: %s", exc)
                self._fetch_done.emit(meeting_id, str(exc) or type(exc).__name__)
                return
            self._fetch_done.emit(meeting_id, "")

        threading.Thread(target=work, name="history-calendar-fetch", daemon=True).start()

    def _card(self, record_id: str) -> Optional[AgendaCard]:
        for card in self._cards:
            if card.item.record_id == record_id:
                return card
        return None

    def _on_fetch_progress(self, meeting_id: str, text: str) -> None:
        card = self._card(meeting_id)
        if card is not None:
            card.set_progress(text)

    def _on_fetch_done(self, meeting_id: str, error: str) -> None:
        self._fetching.discard(meeting_id)
        if error:
            card = self._card(meeting_id)
            if card is not None:
                card.set_progress(f"Couldn't open it: {error}")
            return
        self.refresh()
        self.meeting_requested.emit(meeting_id)

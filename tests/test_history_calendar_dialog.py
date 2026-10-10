"""History calendar window: navigation, filters, the day agenda, and opening records."""
import gc
import threading
import weakref
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from services.history_calendar import (
    DICTATION, FILE, HERE, MEETING, ON, CalendarItem, Mark,
)
from ui_qt.dialogs.history_calendar_dialog import HistoryCalendarDialog
from ui_qt.widgets.history_calendar import AgendaCard, MonthGrid, pick_lines

TODAY = date(2026, 9, 27)


def entry(record_id, text="hello"):
    return SimpleNamespace(id=record_id, text=text)


def item(record_id, when, kind=DICTATION, place=HERE, title="", preview="said something",
         seconds=4.0, source=None):
    return CalendarItem(kind=kind, record_id=record_id, when=when, title=title, preview=preview,
                        seconds=seconds, place=place,
                        source=source if source is not None else entry(record_id, preview))


class FakeSource:
    """The calendar's reads, from a fixed list of items."""

    def __init__(self, items, remote=()):
        self.items = list(items)
        self.remote = list(remote)
        self.notice = ""
        self.month_reads = []

    def _mark(self, it):
        return Mark(it.when, it.kind, it.place, it.record_id, it.seconds)

    def load_index(self):
        return [self._mark(it) for it in self.items]

    def remote_wanted(self):
        return bool(self.remote)

    def load_remote(self, local_ids):
        return [self._mark(it) for it in self.remote]

    def load_month(self, year, month):
        self.month_reads.append((year, month))
        return sorted((it for it in self.items + self.remote
                       if (it.when.year, it.when.month) == (year, month)),
                      key=lambda it: it.when)


def september():
    return [
        item("d1", datetime(2026, 9, 27, 9, 5), preview="Morning notes"),
        item("d2", datetime(2026, 9, 27, 10, 40), preview="Second thought"),
        item("m1", datetime(2026, 9, 27, 14, 0), kind=MEETING, title="Standup", seconds=900),
        item("f1", datetime(2026, 9, 26, 16, 0), kind=FILE, title="interview.wav"),
        item("d3", datetime(2026, 9, 12, 8, 0)),
        item("d4", datetime(2026, 5, 4, 8, 0)),
    ]


@pytest.fixture
def dialog():
    def make(items=None, remote=()):
        source = FakeSource(september() if items is None else items, remote)
        window = HistoryCalendarDialog(source=source, today=lambda: TODAY, first_weekday=6,
                                       threaded=False)
        window.resize(1180, 800)
        window.show()
        QApplication.processEvents()
        window.source = source
        return window
    return make


def card_ids(window):
    return [card.item.record_id for card in window._cards]


def test_opens_on_today_with_its_recordings(dialog):
    window = dialog()
    assert window.grid.month == (2026, 9)
    assert window.month_title.text() == "September 2026"
    assert window.grid.selected() == TODAY
    assert card_ids(window) == ["d1", "d2", "m1"]
    assert window.today_badge.isVisibleTo(window)
    assert "3 recordings" in window.agenda_summary.text()
    assert "2 dictations · 1 meeting" in window.agenda_summary.text()
    stats = window.stats_label.text()
    assert "<b>5</b> recordings" in stats and "<b>3</b> days" in stats
    assert window.kind_chips[DICTATION].text() == "Dictations  3"
    assert window.kind_chips[MEETING].text() == "Meetings  1"
    # The 26th and 27th in a row.
    assert window.streak_label.isVisibleTo(window)
    assert window.streak_label.text() == "2-day streak"
    assert window.busiest_button.text() == "Busiest: Sun, Sep 27 · 3"


def test_months_are_read_ahead_once(dialog):
    window = dialog()
    assert sorted(set(window.source.month_reads)) == [(2026, 8), (2026, 9), (2026, 10)]
    reads = len(window.source.month_reads)
    window.step_month(-1)
    QApplication.processEvents()
    # August was read ahead; only July is new.
    assert window.source.month_reads[reads:] == [(2026, 7)]


def test_selecting_days_and_months(dialog):
    window = dialog()
    window.select_day(date(2026, 9, 26))
    assert card_ids(window) == ["f1"]
    assert window.agenda_weekday.text() == "Saturday"
    window.select_day(date(2026, 9, 28))
    assert card_ids(window) == []

    # A day in another month goes there.
    window.select_day(date(2026, 5, 4))
    assert window.grid.month == (2026, 5)
    assert card_ids(window) == ["d4"]
    assert window.agenda_date.text() == "May 4"
    assert not window.streak_label.isVisibleTo(window)

    window.go_today()
    assert window.grid.month == (2026, 9)
    assert window.grid.selected() == TODAY


def test_a_new_month_selects_its_latest_active_day(dialog):
    window = dialog()
    window.show_month((2026, 5))
    assert window.grid.selected() == date(2026, 5, 4)
    window.show_month((2026, 6))
    assert window.grid.selected() == date(2026, 6, 1)


def test_an_empty_month_offers_the_closest_one(dialog):
    window = dialog()
    window.show_month((2026, 7))
    # A layout shows new children of a visible parent on the next event loop pass.
    QApplication.processEvents()
    assert "Nothing recorded in July" in window.stats_label.text()
    jump = [button for button in window.agenda_list.findChildren(type(window.today_button))
            if button.objectName() == "calendarJumpBtn" and button.isVisibleTo(window)]
    assert [button.text() for button in jump] == ["← Go to May 2026"]
    jump[0].click()
    assert window.grid.month == (2026, 5)


def test_nothing_recorded_yet(dialog):
    window = dialog(items=[])
    labels = [label.text() for label in window.agenda_list.findChildren(type(window.stats_label))]
    assert "Nothing recorded yet" in labels


def test_kind_chips_filter_and_one_always_stays_on(dialog):
    window = dialog()
    window.kind_chips[MEETING].setChecked(False)
    assert card_ids(window) == ["d1", "d2"]
    assert window.grid._days[TODAY].count == 2
    window.kind_chips[DICTATION].setChecked(False)
    window.select_day(date(2026, 9, 26))
    assert card_ids(window) == ["f1"]
    window.kind_chips[FILE].setChecked(False)
    assert window.kind_chips[FILE].isChecked()
    assert window._kinds == frozenset({FILE})


def test_where_filter_appears_with_more_than_one_place(dialog):
    window = dialog()
    assert not window.place_combo.isVisibleTo(window)
    hosted = item("h1", datetime(2026, 9, 27, 18, 0), place=(ON, "jed"))
    window = dialog(remote=[hosted])
    assert window.place_combo.isVisibleTo(window)
    labels = [window.place_combo.itemText(row) for row in range(window.place_combo.count())]
    assert labels == ["Everywhere", "This computer", "Kept on jed"]
    assert card_ids(window) == ["d1", "d2", "m1", "h1"]
    window.place_combo.setCurrentIndex(2)
    assert card_ids(window) == ["h1"]
    assert window._cards[0].findChild(type(window.stats_label), "calendarPlaceChip").text() == "On jed"


def test_opening_and_copying_records(dialog):
    window = dialog()
    opened, meetings, copies = [], [], []
    window.entry_requested.connect(opened.append)
    window.meeting_requested.connect(meetings.append)
    window.meeting_copy_requested.connect(copies.append)

    cards = {card.item.record_id: card for card in window._cards}
    QTest.mouseClick(cards["d1"], Qt.MouseButton.LeftButton)
    assert [e.id for e in opened] == ["d1"]
    cards["m1"].activated.emit(cards["m1"].item)
    assert meetings == ["m1"]
    cards["m1"].copy_requested.emit(cards["m1"].item)
    assert copies == ["m1"]

    clipboard = QApplication.clipboard()
    saved = clipboard.text()
    try:
        copied = []
        window.entry_copied.connect(copied.append)
        cards["d2"].copy_requested.emit(cards["d2"].item)
        assert clipboard.text() == "Second thought"
        assert copied == ["d2"]
    finally:
        clipboard.setText(saved)

    # Enter (or a double-click) opens the day's first recording.
    window._activate_day(TODAY)
    assert [e.id for e in opened] == ["d1", "d1"]


def test_a_host_kept_meeting_is_fetched_before_it_opens(dialog):
    hosted = item("mh", datetime(2026, 9, 27, 19, 0), kind=MEETING, place=(ON, "jed"),
                  title="Remote sync", source={"id": "mh"})
    window = dialog(remote=[hosted])
    meetings = []
    window.meeting_requested.connect(meetings.append)
    calls = []

    def check_out(kind, record_id, progress):
        calls.append((kind, record_id))
        progress(50, 100)

    with patch("services.remote_records.sync.record_sync.check_out", side_effect=check_out), \
            patch("threading.Thread") as thread:
        thread.side_effect = lambda target, **kwargs: SimpleNamespace(start=target)
        window._open(window._card("mh").item)
    QApplication.processEvents()
    assert calls == [("meeting", "mh")]
    assert meetings == ["mh"]


def test_a_calendar_closed_mid_read_is_not_held_by_its_workers(monkeypatch):
    # The reads and the download used to run as the dialog's own methods: the
    # worker kept a closed dialog alive, then dropped the last reference
    # (deleting a QWidget) or emitted on it from its own thread.
    from services.remote_records.sync import record_sync

    gate = threading.Event()

    class SlowSource(FakeSource):
        def load_index(self):
            gate.wait(5)
            return super().load_index()

    def check_out(kind, record_id, progress):
        gate.wait(5)
        progress(50, 100)

    monkeypatch.setattr(record_sync, "_instance", SimpleNamespace(check_out=check_out))
    hosted = item("mh", datetime(2026, 9, 27, 19, 0), kind=MEETING, place=(ON, "jed"),
                  title="Remote sync", source={"id": "mh"})
    window = HistoryCalendarDialog(source=SlowSource(september()), today=lambda: TODAY,
                                   first_weekday=6)
    window.refresh()
    window._fetch_meeting(hosted)
    names = {"history-calendar-load", "history-calendar-fetch"}
    workers = [t for t in threading.enumerate() if t.name in names]
    assert {t.name for t in workers} == names
    ref = weakref.ref(window)
    window.deleteLater()
    del window
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    # The month arrows' lambdas hold the dialog until Qt frees their
    # connections, a pass after the dialog goes.
    QApplication.processEvents()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    gc.collect()
    assert ref() is None
    gate.set()
    for worker in workers:
        worker.join(5)
        assert not worker.is_alive()
    QApplication.processEvents()


def test_refresh_merges_bursts_and_waits_while_hidden(dialog):
    window = dialog()
    window.hide()
    window.refresh_if_visible()
    assert window._stale
    reads = len(window.source.month_reads)
    window.show()
    QApplication.processEvents()
    assert not window._stale
    assert len(window.source.month_reads) > reads
    window.refresh_if_visible()
    window.refresh_if_visible()
    assert window._refresh_timer.isActive()


# ---- the painted grid ----

def test_grid_maps_positions_and_keys_to_days(dialog):
    window = dialog()
    grid = window.grid
    first = grid.weeks[0][0]
    rect = grid._cell_rect(0, 0)
    assert grid.day_at(QPointF(rect.center())) == first
    assert grid.day_at(QPointF(rect.right() + MonthGrid.GAP / 2, rect.center().y())) is None

    picked, steps, todays = [], [], []
    grid.day_selected.connect(picked.append)
    grid.month_step.connect(steps.append)
    grid.today_requested.connect(lambda: todays.append(True))
    window.select_day(date(2026, 9, 15))
    # Each key moves on from the day the one before it selected.
    for key in (Qt.Key.Key_Right, Qt.Key.Key_Down, Qt.Key.Key_Left, Qt.Key.Key_Up):
        QTest.keyClick(grid, key)
    assert picked == [date(2026, 9, 16), date(2026, 9, 23), date(2026, 9, 22), date(2026, 9, 15)]
    assert grid.selected() == date(2026, 9, 15)
    QTest.keyClick(grid, Qt.Key.Key_PageDown)
    QTest.keyClick(grid, Qt.Key.Key_T)
    assert steps == [1] and todays == [True]


def test_a_crowded_day_shows_every_kind_first():
    items = ([item(f"d{n}", datetime(2026, 9, 1, 8, n)) for n in range(6)]
             + [item("m", datetime(2026, 9, 1, 12), kind=MEETING)]
             + [item("f", datetime(2026, 9, 1, 13), kind=FILE)])
    assert [it.record_id for it in pick_lines(items, 3)] == ["d0", "m", "f"]
    assert [it.record_id for it in pick_lines(items, 4)] == ["d0", "d1", "m", "f"]
    assert pick_lines(items[:2], 3) == items[:2]


def test_agenda_card_shows_where_and_how_long():
    card = AgendaCard(item("x", datetime(2026, 9, 1, 15, 4), kind=FILE, title="call.m4a",
                           preview="Talked about the roof", seconds=125, place=(ON, "jed")),
                      milestone=100)
    labels = {label.objectName(): label.text() for label in card.findChildren(type(card.time_label))}
    assert labels["calendarCardTime"] == "3:04 PM"
    assert labels["calendarKindPill"] == "File"
    assert labels["calendarPlaceChip"] == "On jed"
    assert labels["calendarLengthChip"] == "2m 5s"
    assert labels["calendarCardTitle"] == "call.m4a"
    assert labels["calendarCardPreview"] == "Talked about the roof"
    assert labels["calendarMilestone"] == "✦ Your 100th recording"


def test_reopening_returns_to_today(dialog):
    window = dialog()
    window.show_month((2026, 5))
    window.hide()
    window.show()
    QApplication.processEvents()
    assert window.grid.month == (2026, 9)
    assert window.grid.selected() == TODAY
    assert window._pulse_timer.isActive() or window.grid._pulse is not None

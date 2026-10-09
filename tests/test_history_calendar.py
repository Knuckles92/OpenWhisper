"""History calendar data: local-day placement, month reads, and the index's stats."""
from datetime import date, datetime, timedelta, timezone

import pytest

from meeting.persist.repository import SqlMeetingRepository
from services.database import db
from services.history_calendar import (
    DICTATION, FILE, FROM, HERE, MEETING, ON, CalendarIndex, CalendarSource, Mark,
    classify_source, is_milestone, local_wall_time, milestone_text, month_weeks,
    place_label, query_bounds, shift_month,
)
from services.models import TranscriptionHistory

#: Pacific daylight time: UTC-7, so 05:30 UTC on the 1st is 22:30 the day before.
PDT = timezone(timedelta(hours=-7))


def mark(day, kind=DICTATION, place=HERE, record_id=None, seconds=0.0, hour=9):
    when = datetime(day.year, day.month, day.day, hour)
    return Mark(when, kind, place, record_id or f"{kind}-{day.isoformat()}-{hour}", seconds)


def add_entry(entry_id, timestamp, text="hello there", source_name=None, origin=None,
              audio_duration=None, entry_kind=None, title=None):
    db.put_history_entry(TranscriptionHistory(
        id=entry_id, text=text, timestamp=timestamp, model="parakeet",
        source_name=source_name, origin_device_id="dev-1" if origin else None,
        origin_device_name=origin, audio_duration=audio_duration,
        entry_kind=entry_kind, title=title,
    ))


def add_meeting(repository, meeting_id, started_at, ended_at=None, status="ended", title=""):
    repository.create_meeting(
        id=meeting_id, title=title, status=status, started_at=started_at, ended_at=ended_at,
        host_token="host", guest_token="guest", cloud_enabled=False, spool_dir="",
    )


class FakeRecords:
    """Stands in for services.remote_records.sync.record_sync."""

    def __init__(self, remote=None, copies=None, fail=None, host="jed"):
        self.remote = remote or {}
        self.copies = copies or {}
        self.fail = fail
        self.host = host

    def listing_wanted(self, kind):
        return kind in self.remote or self.fail is not None

    def list_remote(self, kind, query="", limit=100):
        if self.fail is not None:
            raise self.fail
        return [dict(item, stored_on=self.host) for item in self.remote.get(kind, [])]

    def copies_on_host(self, kind, ids):
        return set(self.copies.get(kind, ())) & set(ids)

    def status(self):
        return type("Status", (), {"host_name": self.host})()


# ---- pure helpers ----

def test_local_wall_time_converts_aware_values_and_keeps_naive_ones():
    assert local_wall_time("2026-09-01T05:30:00+00:00", PDT) == datetime(2026, 8, 31, 22, 30)
    assert local_wall_time("2026-09-01T05:30:00Z", PDT) == datetime(2026, 8, 31, 22, 30)
    # Legacy entries were stored as local wall time and aren't shifted.
    assert local_wall_time("2026-09-01T05:30:00", PDT) == datetime(2026, 9, 1, 5, 30)
    assert local_wall_time("", PDT) is None
    assert local_wall_time("yesterday", PDT) is None


@pytest.mark.parametrize("source_name, expected", [
    (None, (DICTATION, "")),
    ("", (DICTATION, "")),
    ("Quick Record", (DICTATION, "")),
    ("Quick Record · Email", (DICTATION, "Email")),
    ("interview.wav", (FILE, "interview.wav")),
    ("3 files: a.mp3, b.mp3, c.mp3", (FILE, "3 files: a.mp3, b.mp3, c.mp3")),
    ("Quick Recordings.m4a", (FILE, "Quick Recordings.m4a")),
])
def test_classify_source(source_name, expected):
    assert classify_source(source_name) == expected


@pytest.mark.parametrize("source_name, entry_kind, expected", [
    ("Quick Record · Email", "dictation", (DICTATION, "Email")),
    ("Quick Record", "command", (DICTATION, "Command")),
    ("Transform · Polish", "transform", (DICTATION, "Transform · Polish")),
    # Synced rows may carry the bare transform name.
    ("Polish", "transform", (DICTATION, "Transform · Polish")),
    (None, "transform", (DICTATION, "Transform")),
    ("Quick Record", "file", (FILE, "Quick Record")),
    ("call.m4a", "file", (FILE, "call.m4a")),
])
def test_classify_source_trusts_the_entry_kind(source_name, entry_kind, expected):
    assert classify_source(source_name, entry_kind) == expected


def test_query_bounds_are_a_day_wider_than_the_month():
    assert query_bounds(2026, 9) == ("2026-08-31", "2026-10-02")
    assert query_bounds(2026, 12) == ("2026-11-30", "2027-01-02")
    assert query_bounds(2028, 2) == ("2028-01-31", "2028-03-02")


def test_month_weeks_start_on_the_chosen_weekday():
    sunday_first = month_weeks(2026, 9, first_weekday=6)
    assert sunday_first[0][0] == date(2026, 8, 30)
    assert sunday_first[-1][-1] == date(2026, 10, 3)
    monday_first = month_weeks(2026, 9, first_weekday=0)
    assert monday_first[0][0] == date(2026, 8, 31)


def test_shift_month_wraps_years():
    assert shift_month((2026, 12), 1) == (2027, 1)
    assert shift_month((2026, 1), -1) == (2025, 12)
    assert shift_month((2026, 9), -21) == (2024, 12)


def test_milestones_and_labels():
    assert [n for n in range(1, 3001) if is_milestone(n)] == [1, 10, 50, 100, 250, 500, 1000,
                                                               2000, 3000]
    assert milestone_text(1) == "Your first recording"
    assert milestone_text(500) == "Your 500th recording"
    assert milestone_text(1000) == "Your 1,000th recording"
    assert place_label(None) == "Everywhere"
    assert place_label(HERE) == "This computer"
    assert place_label((FROM, "laptop")) == "From laptop"
    assert place_label((ON, "jed")) == "Kept on jed"


# ---- the index ----

def test_index_counts_days_and_months_with_filters():
    index = CalendarIndex([
        mark(date(2026, 9, 1), seconds=10),
        mark(date(2026, 9, 1), FILE, hour=10, seconds=30),
        mark(date(2026, 9, 1), MEETING, hour=11, seconds=600),
        mark(date(2026, 9, 3), place=(FROM, "laptop")),
        mark(date(2026, 8, 20)),
    ])
    days = index.days(2026, 9)
    assert set(days) == {date(2026, 9, 1), date(2026, 9, 3)}
    assert days[date(2026, 9, 1)].count == 3
    assert days[date(2026, 9, 1)].seconds == 640
    assert dict(days[date(2026, 9, 1)].by_kind) == {DICTATION: 1, FILE: 1, MEETING: 1}
    assert index.month_counts() == {(2026, 9): 4, (2026, 8): 1}
    assert index.month_counts(kinds=frozenset({MEETING})) == {(2026, 9): 1}
    assert set(index.days(2026, 9, place=(FROM, "laptop"))) == {date(2026, 9, 3)}
    assert index.places() == [HERE, (FROM, "laptop")]


def test_month_stats_pick_the_earliest_of_the_busiest_days():
    index = CalendarIndex(
        [mark(date(2026, 9, 5), hour=h) for h in (8, 9)]
        + [mark(date(2026, 9, 2), hour=h) for h in (8, 9)]
        + [mark(date(2026, 9, 9))]
    )
    stats = index.month_stats(2026, 9)
    assert stats.count == 5
    assert stats.active_days == 3
    assert stats.busiest == (date(2026, 9, 2), 2)
    assert index.month_stats(2026, 10).busiest is None


def test_streak_counts_back_from_today_or_yesterday():
    days = [date(2026, 9, 27) - timedelta(days=n) for n in (1, 2, 3, 5)]
    index = CalendarIndex([mark(day) for day in days])
    # Nothing yet today: the run up to yesterday is still alive.
    assert index.streak(date(2026, 9, 27)) == 3
    assert index.streak(date(2026, 9, 26)) == 3
    assert index.streak(date(2026, 9, 29)) == 0
    assert CalendarIndex([mark(date(2026, 9, 27))]).streak(date(2026, 9, 27)) == 1


def test_milestones_follow_every_recording_in_time_order():
    start = datetime(2026, 1, 1, 9)
    marks = [Mark(start + timedelta(hours=n), DICTATION, HERE, f"r{n}") for n in range(12)]
    index = CalendarIndex(reversed(marks))
    assert index.milestone("r0") == 1
    assert index.milestone("r9") == 10
    assert index.milestone("r1") is None
    assert index.milestone_days(2026, 1) == {date(2026, 1, 1): 10}


def test_nearest_month_prefers_the_closer_then_the_earlier():
    index = CalendarIndex([mark(date(2026, 5, 2)), mark(date(2026, 9, 2))])
    assert index.nearest_month((2026, 7)) == (2026, 5)
    assert index.nearest_month((2026, 8)) == (2026, 9)
    assert index.nearest_month((2027, 3)) == (2026, 9)
    assert index.nearest_month((2026, 7), kinds=frozenset({MEETING})) is None


# ---- reading from the database ----

def test_source_places_utc_entries_on_their_local_day():
    add_entry("utc-late", "2026-09-01T05:30:00+00:00", source_name="Quick Record")
    add_entry("naive", "2026-09-01T05:30:00", source_name="notes.wav", audio_duration=12.5)
    add_entry("paired", "2026-09-15T12:00:00+00:00", origin="laptop")
    add_entry("october", "2026-10-01T06:59:00+00:00")
    source = CalendarSource(records=FakeRecords(), tz=PDT)

    index = CalendarIndex(source.load_index())
    assert index.days(2026, 8) and date(2026, 8, 31) in index.days(2026, 8)
    assert set(index.days(2026, 9)) == {date(2026, 9, 1), date(2026, 9, 15), date(2026, 9, 30)}
    assert (FROM, "laptop") in index.places()

    items = source.load_month(2026, 9)
    assert [item.record_id for item in items] == ["naive", "paired", "october"]
    naive = items[0]
    assert (naive.kind, naive.title, naive.seconds) == (FILE, "notes.wav", 12.5)
    assert items[1].place == (FROM, "laptop")
    assert items[1].location() == ("From laptop", "Kept here for laptop, a paired computer")
    august = source.load_month(2026, 8)
    assert [item.record_id for item in august] == ["utc-late"]
    assert august[0].kind == DICTATION and august[0].label() == "hello there"


def test_source_sorts_rewrites_with_dictations_and_shows_given_titles():
    add_entry("cmd", "2026-09-02T15:00:00+00:00", source_name="Quick Record",
              entry_kind="command")
    add_entry("polish", "2026-09-02T16:00:00+00:00", source_name="Transform · Polish",
              entry_kind="transform")
    add_entry("upload", "2026-09-02T17:00:00+00:00", source_name="call.m4a",
              entry_kind="file", title="Call with Sam")
    source = CalendarSource(records=FakeRecords(), tz=PDT)

    kinds = {mark.record_id: mark.kind for mark in source.load_index()}
    assert kinds == {"cmd": DICTATION, "polish": DICTATION, "upload": FILE}
    items = {item.record_id: item for item in source.load_month(2026, 9)}
    assert items["cmd"].title == "Command"
    assert items["polish"].title == "Transform · Polish"
    assert (items["upload"].kind, items["upload"].title) == (FILE, "Call with Sam")


def test_source_reads_meetings_by_their_start():
    repository = SqlMeetingRepository()
    add_meeting(repository, "m_sep", "2026-09-10T16:00:00Z", "2026-09-10T16:30:00Z", title="Standup")
    add_meeting(repository, "m_edge", "2026-10-01T03:00:00Z", "2026-10-01T03:10:00Z")
    add_meeting(repository, "m_live", "2026-09-12T16:00:00Z", status="active")
    source = CalendarSource(repository=repository, records=FakeRecords(), tz=PDT)

    marks = [m for m in source.load_index() if m.kind == MEETING]
    assert sorted(m.record_id for m in marks) == ["m_edge", "m_sep"]
    assert next(m for m in marks if m.record_id == "m_sep").seconds == 1800

    items = source.load_month(2026, 9)
    assert [(item.record_id, item.title) for item in items] == [
        ("m_sep", "Standup"), ("m_edge", "Untitled meeting"),
    ]
    assert items[0].when == datetime(2026, 9, 10, 9, 0)


def test_meeting_summaries_can_be_bounded_by_start():
    repository = SqlMeetingRepository()
    for index, day in enumerate(("2026-08-31", "2026-09-05", "2026-10-02")):
        add_meeting(repository, f"m{index}", f"{day}T10:00:00Z")
    rows = repository.list_past_meeting_summaries(started_from="2026-09-01",
                                                  started_before="2026-10-01")
    assert [row["id"] for row in rows] == ["m1"]
    assert [row[0] for row in repository.list_past_meeting_times()] == ["m0", "m1", "m2"]


def test_source_merges_host_kept_records_and_copies():
    add_entry("local", "2026-09-03T15:00:00+00:00")
    add_entry("both", "2026-09-04T15:00:00+00:00")
    records = FakeRecords(
        remote={
            DICTATION: [
                {"id": "hosted", "text": "kept on the host", "timestamp": "2026-09-05T15:00:00+00:00",
                 "model": "parakeet", "audio_duration": 3.0, "has_audio": True},
                # Also here: listed once, as this computer's.
                {"id": "both", "text": "dup", "timestamp": "2026-09-04T15:00:00+00:00",
                 "model": "parakeet"},
            ],
            MEETING: [
                {"id": "m_host", "title": "Planning", "status": "ended",
                 "started_at": "2026-09-06T16:00:00Z", "ended_at": "2026-09-06T17:00:00Z"},
            ],
        },
        copies={DICTATION: {"both"}},
    )
    source = CalendarSource(records=records, tz=PDT)
    local = source.load_index()
    assert source.remote_wanted()
    remote = source.load_remote(mark.record_id for mark in local)
    assert sorted((m.record_id, m.place) for m in remote) == [
        ("hosted", (ON, "jed")), ("m_host", (ON, "jed")),
    ]
    assert source.notice == ""

    items = {item.record_id: item for item in source.load_month(2026, 9)}
    assert set(items) == {"local", "both", "hosted", "m_host"}
    assert items["both"].location() == ("Also on jed", "Kept here and on jed")
    assert items["hosted"].location()[0] == "On jed"
    assert items["hosted"].source.stored_on == "jed"
    assert items["m_host"].title == "Planning"
    assert items["local"].location() == ("", "")


def test_source_reports_an_unreachable_host():
    source = CalendarSource(records=FakeRecords(fail=RuntimeError("jed isn't reachable right now.")))
    assert source.load_remote([]) == []
    assert source.notice == "jed isn't reachable right now."

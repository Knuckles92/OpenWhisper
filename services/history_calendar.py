"""What was recorded when: History and Past Meetings arranged by local day.

The History calendar shows every dictation, transcribed file and meeting on
the day it happened, wherever it is kept. Stored times mix aware UTC values
with legacy naive local ones (see ``services.format_utils.format_timestamp``),
so rows are fetched with bounds a day wider than the month and placed here by
local wall time, never by the stored string.

Two reads feed it. ``CalendarSource.load_index`` lists every record's time,
kind and place without its text, which is enough for the month counts, the
heat of each day, streaks and milestones. ``CalendarSource.load_month`` then
reads the full rows of one month for what the day cells and agenda show.
"""
from __future__ import annotations

import calendar
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

DICTATION = "dictation"
FILE = "file"
MEETING = "meeting"
KINDS: Tuple[str, ...] = (DICTATION, FILE, MEETING)
KIND_LABELS = {DICTATION: "Dictation", FILE: "File", MEETING: "Meeting"}
KIND_PLURALS = {DICTATION: "dictations", FILE: "files", MEETING: "meetings"}

#: Where a record is kept, as ``(scope, name)``: recorded and kept here;
#: kept here for a paired computer (``from``); or kept only on the paired
#: host (``on``). A copy kept both here and on the host counts as here.
Place = Tuple[str, str]
HERE: Place = ("here", "")
FROM = "from"
ON = "on"

#: History's label for a Quick Record dictation, optionally followed by
#: `` · <profile>`` (services/runtime/transcription.py).
QUICK_RECORD = "Quick Record"

#: History's ``entry_kind`` for Command Mode and transform rewrites, and the
#: prefix a transform's ``source_name`` carries (services/runtime/command.py).
COMMAND_KIND = "command"
TRANSFORM_KIND = "transform"
TRANSFORM_PREFIX = "Transform · "

MonthKey = Tuple[int, int]


def local_wall_time(value: Any, tz: Optional[tzinfo] = None) -> Optional[datetime]:
    """A stored timestamp as naive local wall time, or None when unreadable.

    Aware values (``+00:00`` or ``Z``) are converted to ``tz``, the local zone
    by default; legacy naive values already are local wall time.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(tz).replace(tzinfo=None)


def classify_source(source_name: Optional[str],
                    entry_kind: Optional[str] = None) -> Tuple[str, str]:
    """``(kind, label)`` for a History entry's ``source_name`` and ``entry_kind``.

    Newer entries say what they are. Command Mode and transform rewrites are
    live work like dictations (Stats counts them together), so they show as
    dictations labelled ``Command`` or ``Transform · <name>``.

    Older entries have only ``source_name``: dictations carry nothing or
    ``Quick Record`` with an optional profile name, which becomes the label.
    Anything else names the transcribed file or files.
    """
    name = (source_name or "").strip()
    # A host's listing may come from an older or newer version.
    kind = entry_kind.strip().lower() if isinstance(entry_kind, str) else ""
    if kind == COMMAND_KIND:
        return DICTATION, "Command"
    if kind == TRANSFORM_KIND:
        transform = name.removeprefix(TRANSFORM_PREFIX).strip()
        return DICTATION, f"{TRANSFORM_PREFIX}{transform}" if transform else "Transform"
    if kind == FILE:
        return FILE, name
    if not name:
        return DICTATION, ""
    if name == QUICK_RECORD or name.startswith(QUICK_RECORD + " ·"):
        return DICTATION, name[len(QUICK_RECORD):].lstrip(" ·").strip()
    return (DICTATION if kind == DICTATION else FILE), name


def record_place(record: Any) -> Place:
    """Where a History entry or meeting (object or dict) is kept."""
    def read(name: str) -> str:
        value = record.get(name) if isinstance(record, dict) else getattr(record, name, None)
        return str(value or "").strip()

    stored_on = read("stored_on")
    if stored_on:
        return (ON, stored_on)
    origin = read("origin_device_name")
    if origin:
        return (FROM, origin)
    return HERE


def place_label(place: Optional[Place]) -> str:
    """How a place reads in the Where filter."""
    if place is None:
        return "Everywhere"
    scope, name = place
    if scope == FROM:
        return f"From {name}"
    if scope == ON:
        return f"Kept on {name}"
    return "This computer"


def is_milestone(ordinal: int) -> bool:
    """Whether the ``ordinal``-th recording ever is worth a star."""
    return ordinal in (1, 10, 50, 100, 250, 500) or (ordinal > 0 and ordinal % 1000 == 0)


def ordinal_suffix(number: int) -> str:
    """``1st``, ``2nd``, ``11th``, ``500th``."""
    if 10 <= number % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number:,}{suffix}"


def milestone_text(ordinal: int) -> str:
    return "Your first recording" if ordinal == 1 else f"Your {ordinal_suffix(ordinal)} recording"


def shift_month(key: MonthKey, months: int) -> MonthKey:
    index = key[0] * 12 + (key[1] - 1) + months
    return index // 12, index % 12 + 1


def month_distance(a: MonthKey, b: MonthKey) -> int:
    return (b[0] * 12 + b[1]) - (a[0] * 12 + a[1])


def month_key(day: date) -> MonthKey:
    return day.year, day.month


def month_weeks(year: int, month: int, first_weekday: int = calendar.SUNDAY) -> List[List[date]]:
    """The month's weeks as full rows of dates, padded with the neighbours' days.

    ``first_weekday`` uses Python's numbering: 0 is Monday, 6 is Sunday.
    """
    return calendar.Calendar(first_weekday).monthdatescalendar(year, month)


def query_bounds(year: int, month: int) -> Tuple[str, str]:
    """``[start, end)`` stored-string bounds that hold every record of the month.

    A stored UTC date is at most a day from its local date, so the bounds are
    a day wider on each side; callers then keep the local month exactly.
    """
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    return (first - timedelta(days=1)).isoformat(), (last + timedelta(days=2)).isoformat()


@dataclass(frozen=True)
class Mark:
    """One record in the index: when, what kind, where, and how long."""

    when: datetime
    kind: str
    place: Place
    record_id: str
    seconds: float = 0.0


@dataclass
class DaySummary:
    count: int = 0
    seconds: float = 0.0
    by_kind: Counter = field(default_factory=Counter)


@dataclass
class MonthStats:
    count: int = 0
    seconds: float = 0.0
    active_days: int = 0
    busiest: Optional[Tuple[date, int]] = None
    by_kind: Counter = field(default_factory=Counter)


@dataclass
class CalendarItem:
    """One record of the visible month, with what the calendar shows of it.

    ``source`` is the History entry or meeting dict it was made from, which
    is what opening it needs.
    """

    kind: str
    record_id: str
    when: datetime
    title: str
    preview: str
    seconds: float
    place: Place
    source: Any
    also_on: str = ""

    @property
    def day(self) -> date:
        return self.when.date()

    def label(self) -> str:
        """The few words a day cell shows for this record."""
        if self.kind == DICTATION:
            return self.preview or self.title or "Dictation"
        return self.title or self.preview or KIND_LABELS[self.kind]

    def location(self) -> Tuple[str, str]:
        """``(chip, tooltip)`` for where it's kept, or empty for this computer alone."""
        scope, name = self.place
        if scope == ON:
            return f"On {name}", f"Kept on {name}; opened from there"
        if self.also_on:
            return f"Also on {self.also_on}", f"Kept here and on {self.also_on}"
        if scope == FROM:
            return f"From {name}", f"Kept here for {name}, a paired computer"
        return "", ""


def _matches(mark: Mark, kinds: Optional[FrozenSet[str]], place: Optional[Place]) -> bool:
    return (kinds is None or mark.kind in kinds) and (place is None or mark.place == place)


class CalendarIndex:
    """Every record's day, kind and place: the calendar's counts, heat and stars.

    ``kinds`` (a set of kind names) and ``place`` narrow every count; None
    means all of them.
    """

    def __init__(self, marks: Iterable[Mark] = ()):
        self.marks: List[Mark] = sorted(marks, key=lambda mark: (mark.when, mark.record_id))
        self._milestones: Dict[str, int] = {}
        for ordinal, mark in enumerate(self.marks, 1):
            if is_milestone(ordinal):
                self._milestones[mark.record_id] = ordinal
        self.active_days: FrozenSet[date] = frozenset(mark.when.date() for mark in self.marks)

    def __len__(self) -> int:
        return len(self.marks)

    def select(self, kinds: Optional[FrozenSet[str]] = None,
               place: Optional[Place] = None) -> List[Mark]:
        if kinds is None and place is None:
            return self.marks
        return [mark for mark in self.marks if _matches(mark, kinds, place)]

    def places(self) -> List[Place]:
        """The places records are kept, this computer first."""
        found = {mark.place for mark in self.marks}
        others = sorted(found - {HERE}, key=lambda place: (place[0] != FROM, place[1].lower()))
        return ([HERE] if HERE in found else []) + others

    def month_counts(self, kinds: Optional[FrozenSet[str]] = None,
                     place: Optional[Place] = None) -> Counter:
        return Counter(month_key(mark.when.date()) for mark in self.select(kinds, place))

    def days(self, year: int, month: int, kinds: Optional[FrozenSet[str]] = None,
             place: Optional[Place] = None) -> Dict[date, DaySummary]:
        result: Dict[date, DaySummary] = {}
        for mark in self.select(kinds, place):
            day = mark.when.date()
            if (day.year, day.month) != (year, month):
                continue
            summary = result.setdefault(day, DaySummary())
            summary.count += 1
            summary.seconds += mark.seconds
            summary.by_kind[mark.kind] += 1
        return result

    def month_stats(self, year: int, month: int, kinds: Optional[FrozenSet[str]] = None,
                    place: Optional[Place] = None) -> MonthStats:
        days = self.days(year, month, kinds, place)
        stats = MonthStats(active_days=len(days))
        for day, summary in days.items():
            stats.count += summary.count
            stats.seconds += summary.seconds
            stats.by_kind.update(summary.by_kind)
            if stats.busiest is None or summary.count > stats.busiest[1] or (
                summary.count == stats.busiest[1] and day < stats.busiest[0]
            ):
                stats.busiest = (day, summary.count)
        return stats

    def streak(self, today: date) -> int:
        """Days in a row with a recording, up to today.

        A streak stays alive through today before anything is recorded, so it
        counts back from yesterday until today has a recording of its own.
        """
        day = today if today in self.active_days else today - timedelta(days=1)
        count = 0
        while day in self.active_days:
            count += 1
            day -= timedelta(days=1)
        return count

    def milestone(self, record_id: str) -> Optional[int]:
        """The record's place among every recording ever, when it's a milestone."""
        return self._milestones.get(record_id)

    def milestone_days(self, year: int, month: int) -> Dict[date, int]:
        """The month's days holding a milestone, with the largest one on each."""
        result: Dict[date, int] = {}
        for mark in self.marks:
            ordinal = self._milestones.get(mark.record_id)
            day = mark.when.date()
            if ordinal and (day.year, day.month) == (year, month):
                result[day] = max(result.get(day, 0), ordinal)
        return result

    def nearest_month(self, key: MonthKey, kinds: Optional[FrozenSet[str]] = None,
                      place: Optional[Place] = None) -> Optional[MonthKey]:
        """The month with records closest to ``key``, the earlier one on a tie."""
        months = self.month_counts(kinds, place)
        if not months:
            return None
        return min(months, key=lambda other: (abs(month_distance(key, other)),
                                              month_distance(key, other) > 0))


class CalendarSource:
    """Reads what the calendar shows from History, Past Meetings and the paired host.

    Blocking: the calendar window calls it off the Qt thread. Host-kept
    records come from the host's newest ``REMOTE_LIMIT`` of each kind, read by
    ``load_remote`` and reused by ``load_month`` until the next one.
    """

    #: The host lists at most this many of a kind (host_store.MAX_LIST).
    REMOTE_LIMIT = 500

    def __init__(self, db=None, repository=None, records=None, tz: Optional[tzinfo] = None):
        self._db = db
        self._repository = repository
        self._records = records
        self.tz = tz
        self._remote: Dict[str, List[dict]] = {DICTATION: [], MEETING: []}
        #: Why the host's records couldn't be listed, or "".
        self.notice = ""

    # ---- dependencies ----

    def _database(self):
        if self._db is None:
            from services.database import db

            self._db = db
        return self._db

    def _meetings(self):
        if self._repository is None:
            from meeting.persist.repository import SqlMeetingRepository

            self._repository = SqlMeetingRepository()
        return self._repository

    def _sync(self):
        if self._records is None:
            from services.remote_records.sync import record_sync

            self._records = record_sync
        return self._records

    # ---- the index ----

    def load_index(self) -> List[Mark]:
        """Marks for every record kept on this computer."""
        marks: List[Mark] = []
        for row_id, stamp, source_name, origin, seconds, entry_kind in (
            self._database().history_calendar_rows()
        ):
            when = local_wall_time(stamp, self.tz)
            if when is None:
                continue
            kind, _label = classify_source(source_name, entry_kind)
            place = (FROM, origin) if origin else HERE
            marks.append(Mark(when, kind, place, row_id, float(seconds or 0.0)))
        for row_id, started, ended, paused, origin in self._meetings().list_past_meeting_times():
            when = local_wall_time(started, self.tz)
            if when is None:
                continue
            place = (FROM, origin) if origin else HERE
            marks.append(Mark(when, MEETING, place, row_id, _meeting_seconds(
                {"started_at": started, "ended_at": ended, "paused_total_s": paused}
            )))
        return marks

    def remote_wanted(self) -> bool:
        """Whether the paired host may keep records of this computer's."""
        try:
            records = self._sync()
            return any(records.listing_wanted(kind) for kind in (DICTATION, MEETING))
        except Exception:
            logger.debug("Record sync unavailable for the History calendar", exc_info=True)
            return False

    def load_remote(self, local_ids: Iterable[str]) -> List[Mark]:
        """Marks for the records only the paired host keeps; may wait on the network."""
        self.notice = ""
        remote: Dict[str, List[dict]] = {DICTATION: [], MEETING: []}
        try:
            records = self._sync()
            for kind in remote:
                if records.listing_wanted(kind):
                    remote[kind] = list(records.list_remote(kind, "", self.REMOTE_LIMIT))
        except Exception as exc:
            logger.debug("Couldn't list the host's records for the calendar: %s", exc)
            self.notice = str(exc) or "The host couldn't be asked."
        known = set(local_ids)
        remote = {kind: [item for item in items if str(item.get("id") or "") not in known]
                  for kind, items in remote.items()}
        self._remote = remote
        marks: List[Mark] = []
        for item in remote[DICTATION]:
            when = local_wall_time(item.get("timestamp"), self.tz)
            if when is not None:
                kind, _label = classify_source(item.get("source_name"), item.get("entry_kind"))
                marks.append(Mark(when, kind, record_place(item), str(item["id"]),
                                  float(item.get("audio_duration") or 0.0)))
        for item in remote[MEETING]:
            when = local_wall_time(item.get("started_at"), self.tz)
            if when is not None:
                marks.append(Mark(when, MEETING, record_place(item), str(item["id"]),
                                  _meeting_seconds(item)))
        return marks

    # ---- one month ----

    def load_month(self, year: int, month: int) -> List[CalendarItem]:
        """Every record of the local month, oldest first."""
        start, end = query_bounds(year, month)

        def in_month(when: Optional[datetime]) -> bool:
            return when is not None and (when.year, when.month) == (year, month)

        items: List[CalendarItem] = []
        entries = []
        for entry in self._database().get_history_entries_between(start, end):
            when = local_wall_time(entry.timestamp, self.tz)
            if in_month(when):
                entries.append((entry, when))
        also_on = self._copies_on_host(DICTATION, [entry.id for entry, _when in entries])
        for entry, when in entries:
            items.append(entry_item(entry, when, also_on.get(entry.id, "")))
        if self._remote[DICTATION]:
            from services.history_manager import remote_history_entry

            for item in self._remote[DICTATION]:
                when = local_wall_time(item.get("timestamp"), self.tz)
                if in_month(when):
                    items.append(entry_item(remote_history_entry(item), when))

        meetings = []
        for meeting in self._meetings().list_past_meeting_summaries(
            limit=501, started_from=start, started_before=end,
        ):
            when = local_wall_time(meeting.get("started_at"), self.tz)
            if in_month(when):
                meetings.append((meeting, when))
        also_on = self._copies_on_host(MEETING, [str(m.get("id") or "") for m, _when in meetings])
        for meeting, when in meetings:
            items.append(meeting_item(meeting, when, also_on.get(str(meeting.get("id") or ""), "")))
        for meeting in self._remote[MEETING]:
            when = local_wall_time(meeting.get("started_at"), self.tz)
            if in_month(when):
                items.append(meeting_item(meeting, when))

        items.sort(key=lambda item: (item.when, item.record_id))
        return items

    def _copies_on_host(self, kind: str, ids: List[str]) -> Dict[str, str]:
        """``{id: host}`` for the records of ``ids`` also kept on the paired host."""
        if not ids:
            return {}
        try:
            records = self._sync()
            copies = records.copies_on_host(kind, ids)
            if not copies:
                return {}
            host = records.status().host_name or "the host"
        except Exception:
            logger.debug("Couldn't tell which records the host also keeps", exc_info=True)
            return {}
        return {record_id: host for record_id in copies}


def _meeting_seconds(meeting: Dict[str, Any]) -> float:
    from meeting.time_utils import meeting_duration_s

    return float(meeting_duration_s(meeting) or 0.0)


def entry_item(entry: Any, when: datetime, also_on: str = "") -> CalendarItem:
    """A History entry (this computer's or a host-kept one) as a calendar item.

    A title the entry was given is shown in place of its source's label.
    """
    kind, label = classify_source(getattr(entry, "source_name", None),
                                  getattr(entry, "entry_kind", None))
    return CalendarItem(
        kind=kind,
        record_id=str(entry.id),
        when=when,
        title=str(getattr(entry, "title", None) or "").strip() or label,
        preview=entry.preview_text if (entry.text or "").strip() else "",
        seconds=float(entry.audio_duration or 0.0),
        place=record_place(entry),
        source=entry,
        also_on=also_on,
    )


def meeting_item(meeting: Dict[str, Any], when: datetime, also_on: str = "") -> CalendarItem:
    """A past meeting summary (this computer's or a host-kept one) as a calendar item."""
    from meeting.content import fallback_meeting_title, meeting_preview_text

    return CalendarItem(
        kind=MEETING,
        record_id=str(meeting.get("id") or ""),
        when=when,
        title=fallback_meeting_title(meeting),
        preview=meeting_preview_text(meeting),
        seconds=_meeting_seconds(meeting),
        place=record_place(meeting),
        source=meeting,
        also_on=also_on,
    )

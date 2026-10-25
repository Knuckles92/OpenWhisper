"""Local-only dictation statistics for the Stats window.

Rows live in the dictation_stats table, written when history is saved, so
they survive moving history to a paired host and Clear history.

Words are the words you said: a transcript before AI cleanup, with a
snippet counted as its trigger rather than the text it expands to (the
history-save fields carry that as ``spoken_text``). A command or
transform rewrites text you selected rather than said, so it adds no words,
but what the AI changed there still counts toward words cleaned up. Pace is
those words per minute of recording, over recordings of a second or more.
Time saved is typing those words at an average typist's pace, less saying
them at yours.

Timestamps are bucketed by local time: new rows carry aware UTC times,
history from before them naive local wall time.
"""

from __future__ import annotations

import difflib
import logging
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Callable, Iterable, Optional

from sqlalchemy import and_, or_

from services.database import db
from services.models import DictationStat, TranscriptionHistory

logger = logging.getLogger(__name__)

#: Entry kinds that count; uploads, batches and re-transcriptions don't.
COUNTED_KINDS = ("dictation", "command", "transform", "action")
#: A row that only says history was already counted (or stats were reset),
#: so it is never counted again.
_MARKER_KIND = "marker"
#: Shorter recordings say little about pace.
MIN_PACE_SECONDS = 1.0
TOP_APPS = 5
#: An average typist's pace, the baseline for time saved.
TYPING_WPM = 40
#: Days the activity map can show: 53 weeks, so a full year always fits.
ACTIVITY_DAYS = 371
#: All time shows a bar per month: at most this many, at least MIN_MONTH_BARS.
MONTH_BARS = 12
MIN_MONTH_BARS = 6
#: Word counts worth a milestone; past the last, every further million.
MILESTONES = (1_000, 5_000, 10_000, 25_000, 50_000, 100_000, 250_000, 500_000, 1_000_000)
#: The spans the window switches between: key and days, None for all time.
PERIODS = (("week", 7), ("month", 30), ("all", None))

# Chinese and Japanese are written without spaces, so each character counts
# as a word; Korean separates words with spaces like Latin scripts.
_CJK = "぀-ヿ㐀-䶿一-鿿豈-﫿"
_TOKEN = re.compile(rf"[{_CJK}]|[^\s{_CJK}]+")
_WORDLIKE = re.compile(r"\w")

# One writer at a time: a dictation saved while history is being counted
# must not be counted twice.
_write_lock = threading.Lock()
#: Called on the history-save worker after each new stats row is saved.
_listeners: list[Callable[[], None]] = []


@dataclass(frozen=True)
class PeriodStats:
    """The counts for one span: the last 7 days, the last 30, or all time."""

    key: str = "week"
    #: Days in the span, or None for all time.
    days: Optional[int] = 7
    words: int = 0
    #: Words in the same number of days just before; None for all time.
    previous_words: Optional[int] = None
    dictations: int = 0
    #: Commands, transforms and actions: text the AI rewrote when asked.
    rewrites: int = 0
    average_wpm: float = 0.0
    words_cleaned_up: int = 0
    #: ``(app_name, words)``, most words first.
    top_apps: tuple[tuple[str, int], ...] = ()
    #: ``(start, words)``, oldest first: a bar per day, or per month for all time.
    bars: tuple[tuple[date, int], ...] = ()
    #: Words by local hour, midnight first.
    hours: tuple[int, ...] = (0,) * 24
    #: Words by weekday, Monday first.
    weekdays: tuple[int, ...] = (0,) * 7

    @property
    def minutes_saved(self) -> float:
        """Minutes typing these words would take, less saying them; 0 without a pace."""
        if self.average_wpm <= TYPING_WPM:
            return 0.0
        return self.words / TYPING_WPM - self.words / self.average_wpm


@dataclass(frozen=True)
class StatsSummary:
    words_all_time: int = 0
    #: Every row counted: dictations, commands, transforms and actions.
    entries: int = 0
    day_streak: int = 0
    longest_streak: int = 0
    dictated_today: bool = False
    first_day: Optional[date] = None
    #: ``(day, words)`` for the last ACTIVITY_DAYS days, oldest first.
    activity: tuple[tuple[date, int], ...] = ()
    #: One per PERIODS key, in that order.
    periods: tuple[PeriodStats, ...] = ()

    def period(self, key: str) -> PeriodStats:
        for period in self.periods:
            if period.key == key:
                return period
        return PeriodStats(key=key, days=dict(PERIODS)[key])


@dataclass(frozen=True)
class _Row:
    moment: datetime
    kind: str
    app_name: Optional[str]
    words: int
    edited: int
    seconds: Optional[float]


def add_listener(callback: Callable[[], None]) -> None:
    """Call ``callback`` (on a worker thread) whenever a dictation is counted."""
    if callback not in _listeners:
        _listeners.append(callback)


def remove_listener(callback: Callable[[], None]) -> None:
    if callback in _listeners:
        _listeners.remove(callback)


def next_milestone(words: int) -> int:
    """The smallest milestone above ``words``."""
    for milestone in MILESTONES:
        if milestone > words:
            return milestone
    return (words // 1_000_000 + 1) * 1_000_000


def words(text: Optional[str]) -> list[str]:
    """The words of ``text``: whitespace-separated, or one per CJK character."""
    return [token for token in _TOKEN.findall(text or "") if _WORDLIKE.search(token)]


def count_words(text: Optional[str]) -> int:
    return len(words(text))


def words_edited(before: Optional[str], after: Optional[str]) -> int:
    """Words replaced, inserted or deleted going from ``before`` to ``after``."""
    old, new = words(before), words(after)
    if old == new:
        return 0
    edited = 0
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "replace":
            edited += max(i2 - i1, j2 - j1)
        elif op == "delete":
            edited += i2 - i1
        elif op == "insert":
            edited += j2 - j1
    return edited


def local_time(timestamp: Optional[str], tz: Optional[tzinfo] = None) -> Optional[datetime]:
    """The local wall time of a stored timestamp, naive, or None if unreadable.

    Aware values are converted to ``tz`` (the system's zone when None);
    naive ones are already local wall time.
    """
    try:
        moment = datetime.fromisoformat(str(timestamp or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(tz).replace(tzinfo=None)
    return moment


def _kind(fields: dict) -> str:
    kind = fields.get("entry_kind")
    if kind:
        return kind
    source = fields.get("source_name") or ""
    return "dictation" if source.startswith("Quick Record") else "file"


def _stat(fields: dict) -> Optional[DictationStat]:
    """The stats row for one entry's fields, or None when it doesn't count."""
    kind = _kind(fields)
    if kind not in COUNTED_KINDS or fields.get("origin_device_id"):
        return None
    text = fields.get("text") or ""
    raw = fields.get("raw_text")
    ai_version = fields.get("cleaned_text") or text
    spoken = audio = None
    if kind == "dictation":
        spoken = count_words(fields.get("spoken_text") or raw or text)
        audio = fields.get("audio_duration")
    return DictationStat(
        entry_id=fields.get("id"),
        timestamp=fields.get("timestamp") or datetime.now(timezone.utc).isoformat(),
        entry_kind=kind,
        app_name=fields.get("app_name") or None,
        app_category=fields.get("app_category") or None,
        cleanup_level=fields.get("cleanup_level") or None,
        spoken_words=spoken or 0,
        final_words=count_words(text),
        words_edited=words_edited(raw, ai_version) if raw else 0,
        audio_seconds=float(audio) if isinstance(audio, (int, float)) else None,
        language=fields.get("language") or None,
    )


_ROW_FIELDS = (
    "id", "timestamp", "text", "raw_text", "cleaned_text", "audio_duration", "entry_kind",
    "source_name", "app_name", "app_category", "cleanup_level", "language", "origin_device_id",
)


def record(entry_fields: dict, row) -> None:
    """Add a stats row for a saved history entry; runs on the history-save worker.

    ``row`` is the saved entry; ``entry_fields`` what it was saved from,
    for anything the row doesn't carry.
    """
    fields = dict(entry_fields or {})
    for name in _ROW_FIELDS:
        value = getattr(row, name, None) if row is not None else None
        if value is not None:
            fields[name] = value
    stat = _stat(fields)
    if stat is None:
        return
    with _write_lock, db.get_session() as session:
        if stat.entry_id and session.query(DictationStat.id).filter(
            DictationStat.entry_id == stat.entry_id
        ).first() is not None:
            return
        session.add(stat)
    for listener in tuple(_listeners):
        listener()


def _marker() -> DictationStat:
    return DictationStat(
        timestamp=datetime.now(timezone.utc).isoformat(), entry_kind=_MARKER_KIND,
        spoken_words=0, final_words=0, words_edited=0,
    )


def _ensure_backfilled() -> None:
    """Count the dictations already in history, once.

    Live dictations saved since this version may already be here, so
    entries with a row are skipped rather than the table being empty.
    """
    with _write_lock:
        with db.get_session() as session:
            if session.query(DictationStat.id).filter(
                DictationStat.entry_kind == _MARKER_KIND
            ).first() is not None:
                return
            counted = {
                entry_id for (entry_id,) in session.query(DictationStat.entry_id).filter(
                    DictationStat.entry_id.isnot(None)
                )
            }
            history = session.query(TranscriptionHistory).filter(
                TranscriptionHistory.origin_device_id.is_(None),
                or_(
                    TranscriptionHistory.entry_kind == "dictation",
                    and_(
                        TranscriptionHistory.entry_kind.is_(None),
                        TranscriptionHistory.source_name.like("Quick Record%"),
                    ),
                ),
            ).all()
            added = 0
            for entry in history:
                if entry.id in counted:
                    continue
                stat = _stat({name: getattr(entry, name, None) for name in _ROW_FIELDS})
                if stat is not None:
                    session.add(stat)
                    added += 1
            session.add(_marker())
    logger.info("Counted %d earlier dictations for Stats", added)


def _rows(tz: Optional[tzinfo]) -> list[_Row]:
    with db.get_session() as session:
        found = session.query(
            DictationStat.timestamp,
            DictationStat.entry_kind,
            DictationStat.app_name,
            DictationStat.spoken_words,
            DictationStat.words_edited,
            DictationStat.audio_seconds,
        ).filter(DictationStat.entry_kind.in_(COUNTED_KINDS)).all()
    rows = []
    for timestamp, kind, app_name, spoken, edited, seconds in found:
        moment = local_time(timestamp, tz)
        if moment is not None:
            rows.append(_Row(moment, kind, app_name or None, spoken or 0, edited or 0, seconds))
    return rows


def _words_by_day(rows: Iterable[_Row]) -> dict[date, int]:
    """Words per local day; a day with only a rewrite is there with no words."""
    by_day: dict[date, int] = {}
    for row in rows:
        day = row.moment.date()
        by_day[day] = by_day.get(day, 0) + row.words
    return by_day


def _streak(days: Iterable[date], today: date) -> int:
    active = set(days)
    day = today if today in active else today - timedelta(days=1)
    streak = 0
    while day in active:
        streak += 1
        day -= timedelta(days=1)
    return streak


def _longest_streak(days: Iterable[date]) -> int:
    longest = run = 0
    previous = None
    for day in sorted(days):
        run = run + 1 if previous is not None and day - previous == timedelta(days=1) else 1
        longest = max(longest, run)
        previous = day
    return longest


def _pace(rows: Iterable[_Row]) -> float:
    words = 0
    seconds = 0.0
    for row in rows:
        if row.seconds is not None and row.seconds >= MIN_PACE_SECONDS and row.words:
            words += row.words
            seconds += row.seconds
    return round(words / (seconds / 60.0), 1) if seconds else 0.0


def _top_apps(rows: Iterable[_Row]) -> tuple[tuple[str, int], ...]:
    by_app: dict[str, int] = {}
    for row in rows:
        if row.app_name and row.words:
            by_app[row.app_name] = by_app.get(row.app_name, 0) + row.words
    return tuple(sorted(by_app.items(), key=lambda item: (-item[1], item[0].casefold()))[:TOP_APPS])


def _days_back(today: date, count: int) -> list[date]:
    """The ``count`` days ending today, oldest first."""
    return [today - timedelta(days=offset) for offset in range(count - 1, -1, -1)]


def _month_bars(by_day: dict[date, int], today: date) -> tuple[tuple[date, int], ...]:
    """Words per month back to the first dictation, within MIN/MONTH_BARS months."""
    by_month: dict[date, int] = {}
    for day, words in by_day.items():
        month = day.replace(day=1)
        by_month[month] = by_month.get(month, 0) + words
    first = min(by_month, default=today.replace(day=1))
    months = [today.replace(day=1)]
    while len(months) < MONTH_BARS and (months[-1] > first or len(months) < MIN_MONTH_BARS):
        months.append((months[-1] - timedelta(days=1)).replace(day=1))
    return tuple((month, by_month.get(month, 0)) for month in reversed(months))


def _period(rows: list[_Row], by_day: dict[date, int], key: str, days: Optional[int],
            today: date) -> PeriodStats:
    if days is None:
        inside, previous, bars = rows, None, _month_bars(by_day, today)
    else:
        start = today - timedelta(days=days - 1)
        inside = [row for row in rows if start <= row.moment.date() <= today]
        previous = sum(by_day.get(start - timedelta(days=back), 0) for back in range(1, days + 1))
        bars = tuple((day, by_day.get(day, 0)) for day in _days_back(today, days))
    hours, weekdays = [0] * 24, [0] * 7
    for row in inside:
        hours[row.moment.hour] += row.words
        weekdays[row.moment.weekday()] += row.words
    dictations = sum(1 for row in inside if row.kind == "dictation")
    return PeriodStats(
        key=key,
        days=days,
        words=sum(row.words for row in inside),
        previous_words=previous,
        dictations=dictations,
        rewrites=len(inside) - dictations,
        average_wpm=_pace(inside),
        words_cleaned_up=sum(row.edited for row in inside),
        top_apps=_top_apps(inside),
        bars=bars,
        hours=tuple(hours),
        weekdays=tuple(weekdays),
    )


def load_summary(today: date | None = None, tz: tzinfo | None = None) -> StatsSummary:
    """Everything the Stats window shows; blocking, so call it off the UI thread.

    Args:
        today: The local day to count back from; today when None.
        tz: The zone that days are local to; the system's when None.
    """
    _ensure_backfilled()
    rows = _rows(tz)
    if today is None:
        today = datetime.now(tz).date() if tz is not None else date.today()
    by_day = _words_by_day(rows)
    return StatsSummary(
        words_all_time=sum(row.words for row in rows),
        entries=len(rows),
        day_streak=_streak(by_day, today),
        longest_streak=_longest_streak(by_day),
        dictated_today=today in by_day,
        first_day=min(by_day, default=None),
        activity=tuple((day, by_day.get(day, 0)) for day in _days_back(today, ACTIVITY_DAYS)),
        periods=tuple(_period(rows, by_day, key, days, today) for key, days in PERIODS),
    )


def reset() -> None:
    """Delete every stats row; history is untouched and isn't counted again."""
    with _write_lock, db.get_session() as session:
        session.query(DictationStat).delete()
        session.add(_marker())
    logger.info("Stats reset")

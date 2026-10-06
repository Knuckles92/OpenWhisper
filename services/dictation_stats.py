"""Local-only dictation statistics for the Stats window.

Rows live in the dictation_stats table, written when history is saved, so
they survive moving history to a paired host and Clear history.

Words are the words you said: a transcript before AI cleanup, with a
snippet counted as its trigger rather than the text it expands to (the
history-save fields carry that as ``spoken_text``). A command or
transform rewrites text you selected rather than said, so it adds no words,
but what the AI changed there still counts toward words cleaned up. Pace is
those words per minute of recording, over recordings of a second or more.

Timestamps are bucketed by local day: new rows carry aware UTC times,
history from before them naive local wall time.
"""

from __future__ import annotations

import difflib
import logging
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Iterable, Optional

from sqlalchemy import and_, or_

from services.database import db
from services.models import DictationStat, TranscriptionHistory

logger = logging.getLogger(__name__)

#: Entry kinds that count; uploads, batches and re-transcriptions don't.
COUNTED_KINDS = ("dictation", "command", "transform")
#: A row that only says history was already counted (or stats were reset),
#: so it is never counted again.
_MARKER_KIND = "marker"
#: Shorter recordings say little about pace.
MIN_PACE_SECONDS = 1.0
WEEK_DAYS = 7
STRIP_DAYS = 14
TOP_APPS = 5

# Chinese and Japanese are written without spaces, so each character counts
# as a word; Korean separates words with spaces like Latin scripts.
_CJK = "぀-ヿ㐀-䶿一-鿿豈-﫿"
_TOKEN = re.compile(rf"[{_CJK}]|[^\s{_CJK}]+")
_WORDLIKE = re.compile(r"\w")

# One writer at a time: a dictation saved while history is being counted
# must not be counted twice.
_write_lock = threading.Lock()


@dataclass(frozen=True)
class StatsSummary:
    words_this_week: int = 0
    words_all_time: int = 0
    average_wpm: float = 0.0
    words_cleaned_up: int = 0
    day_streak: int = 0
    #: ``(app_name, words)``, most words first.
    top_apps: tuple[tuple[str, int], ...] = ()
    #: ``(day, words)`` for the last 14 days, oldest first.
    daily_words: tuple[tuple[date, int], ...] = ()
    #: Dictations, commands and transforms counted.
    dictations: int = 0
    dictated_today: bool = False


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


def local_day(timestamp: Optional[str], tz: Optional[tzinfo] = None) -> Optional[date]:
    """The local calendar day of a stored timestamp, or None if unreadable.

    Aware values are converted to ``tz`` (the system's zone when None);
    naive ones are already local wall time.
    """
    try:
        moment = datetime.fromisoformat(str(timestamp or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(tz)
    return moment.date()


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


def _rows() -> list:
    with db.get_session() as session:
        return session.query(
            DictationStat.timestamp,
            DictationStat.app_name,
            DictationStat.spoken_words,
            DictationStat.words_edited,
            DictationStat.audio_seconds,
        ).filter(DictationStat.entry_kind.in_(COUNTED_KINDS)).all()


def _streak(days: Iterable[date], today: date) -> int:
    active = set(days)
    day = today if today in active else today - timedelta(days=1)
    streak = 0
    while day in active:
        streak += 1
        day -= timedelta(days=1)
    return streak


def load_summary(today: date | None = None, tz: tzinfo | None = None) -> StatsSummary:
    """Everything the Stats window shows; blocking, so call it off the UI thread.

    Args:
        today: The local day to count back from; today when None.
        tz: The zone that days are local to; the system's when None.
    """
    _ensure_backfilled()
    rows = _rows()
    if today is None:
        today = datetime.now(tz).date() if tz is not None else date.today()
    by_day: dict[date, int] = {}
    by_app: dict[str, int] = {}
    total = edited = pace_words = 0
    pace_seconds = 0.0
    for timestamp, app_name, spoken, changed, seconds in rows:
        spoken = spoken or 0
        total += spoken
        edited += changed or 0
        if seconds is not None and seconds >= MIN_PACE_SECONDS and spoken:
            pace_words += spoken
            pace_seconds += seconds
        if app_name and spoken:
            by_app[app_name] = by_app.get(app_name, 0) + spoken
        day = local_day(timestamp, tz)
        if day is not None:
            by_day[day] = by_day.get(day, 0) + spoken
    week = sum(
        by_day.get(today - timedelta(days=offset), 0) for offset in range(WEEK_DAYS)
    )
    strip = tuple(
        (day, by_day.get(day, 0))
        for day in (today - timedelta(days=offset) for offset in range(STRIP_DAYS - 1, -1, -1))
    )
    apps = sorted(by_app.items(), key=lambda item: (-item[1], item[0].casefold()))[:TOP_APPS]
    return StatsSummary(
        words_this_week=week,
        words_all_time=total,
        average_wpm=round(pace_words / (pace_seconds / 60.0), 1) if pace_seconds else 0.0,
        words_cleaned_up=edited,
        day_streak=_streak(by_day, today),
        top_apps=tuple(apps),
        daily_words=strip,
        dictations=len(rows),
        dictated_today=today in by_day,
    )


def reset() -> None:
    """Delete every stats row; history is untouched and isn't counted again."""
    with _write_lock, db.get_session() as session:
        session.query(DictationStat).delete()
        session.add(_marker())
    logger.info("Stats reset")

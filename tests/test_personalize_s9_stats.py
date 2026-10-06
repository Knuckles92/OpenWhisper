"""Dictation stats: what counts, local days, the summary, backfill and reset."""

import importlib
from datetime import date, datetime, timedelta, timezone

import pytest

UTC = timezone.utc
PACIFIC = timezone(timedelta(hours=-7))
TOKYO = timezone(timedelta(hours=9))
TODAY = date(2026, 10, 6)


def _stats():
    return importlib.import_module("services.dictation_stats")


def _db():
    return importlib.import_module("services.database").db


def _rows():
    from services.models import DictationStat

    with _db().get_session() as session:
        return session.query(DictationStat).filter(DictationStat.entry_kind != "marker").all()


def _at(day: date, hour=12, tz=PACIFIC) -> str:
    """An aware UTC timestamp for ``hour`` o'clock local time on ``day``."""
    return datetime(day.year, day.month, day.day, hour, tzinfo=tz).astimezone(UTC).isoformat()


def _record(text, *, day=TODAY, raw=None, seconds=None, kind="dictation", app=None, entry_id=None,
            hour=12):
    _stats().record(
        {"text": text, "raw_text": raw, "audio_duration": seconds, "entry_kind": kind,
         "app_name": app, "id": entry_id, "timestamp": _at(day, hour)},
        None,
    )


@pytest.mark.parametrize("text, expected", [
    ("Hello, world.", 2),
    ("  so — anyway ...  ", 2),
    ("我喜欢你", 4),
    ("今日はgood day", 5),
    ("안녕 하세요", 2),
    ("", 0),
    (None, 0),
])
def test_words_count_spaces_or_cjk_characters(text, expected):
    assert _stats().count_words(text) == expected


@pytest.mark.parametrize("before, after, expected", [
    ("a b c", "a b c", 0),
    ("a b c", "a x c", 1),
    ("a b c", "a c", 1),
    ("a c", "a b c", 1),
    ("um so I think", "I think", 2),
    ("one two", "three four five", 3),
])
def test_words_edited_counts_word_level_changes(before, after, expected):
    assert _stats().words_edited(before, after) == expected


def test_local_days_convert_aware_times_and_keep_naive_ones():
    local_day = _stats().local_day
    assert local_day("2026-10-05T23:30:00+00:00", PACIFIC) == date(2026, 10, 5)
    assert local_day("2026-10-05T23:30:00+00:00", TOKYO) == date(2026, 10, 6)
    assert local_day("2026-10-05T23:30:00Z", TOKYO) == date(2026, 10, 6)
    assert local_day("2026-10-05T23:30:00", TOKYO) == date(2026, 10, 5)
    assert local_day("not a time") is None


def test_a_saved_dictation_is_counted_from_its_entry():
    from services.dictation_pipeline import record_stats
    from services.history_manager import history_manager

    fields = dict(text="Hello, world.", raw_text="um hello world", model="base", audio_duration=3.0,
                  entry_kind="dictation", app_name="Slack", app_category="work",
                  cleanup_level="medium", language="en", source_name="Quick Record")
    entry = history_manager.add_entry(**fields)
    record_stats(fields, entry)
    record_stats(fields, entry)

    [row] = _rows()
    assert (row.entry_id, row.entry_kind, row.app_name, row.app_category) == (
        entry.id, "dictation", "Slack", "work")
    assert (row.spoken_words, row.final_words, row.words_edited) == (3, 2, 3)
    assert row.audio_seconds == 3.0
    assert (row.cleanup_level, row.language, row.timestamp) == ("medium", "en", entry.timestamp)


def test_files_and_other_computers_entries_are_not_counted():
    stats = _stats()
    stats.record({"text": "a talk", "entry_kind": "file", "timestamp": _at(TODAY)}, None)
    stats.record({"text": "upload", "source_name": "talk.mp3", "timestamp": _at(TODAY)}, None)
    stats.record({"text": "theirs", "entry_kind": "dictation", "origin_device_id": "laptop",
                  "timestamp": _at(TODAY)}, None)
    assert _rows() == []


def test_a_rewrite_adds_its_edits_but_no_spoken_words():
    _record("Make it formal, please.", raw="make it formal pls", kind="command", seconds=4.0)
    [row] = _rows()
    assert (row.spoken_words, row.final_words, row.audio_seconds) == (0, 4, None)
    assert row.words_edited == 3


def test_the_summary_counts_days_pace_streak_and_apps():
    stats = _stats()
    _stats().reset()
    _record("one two three four five six", seconds=3.0, app="Slack")       # 120 wpm
    _record("one two three", day=TODAY - timedelta(days=1), seconds=0.5, app="Outlook")
    _record("one two", day=TODAY - timedelta(days=2), seconds=60.0, app="Slack", raw="1 2 3 4")
    _record("old words here", day=TODAY - timedelta(days=7), app="Notion")
    _record("older", day=TODAY - timedelta(days=20), app="Word")
    _record("rewrite", kind="transform", raw="draft")

    summary = stats.load_summary(today=TODAY, tz=PACIFIC)

    assert summary.words_all_time == 6 + 3 + 4 + 3 + 1
    assert summary.words_this_week == 6 + 3 + 4
    assert summary.average_wpm == pytest.approx((6 + 4) / (63.0 / 60), abs=0.1)
    assert summary.words_cleaned_up == 4 + 1
    assert summary.day_streak == 3
    assert summary.dictated_today is True
    assert summary.top_apps == (("Slack", 10), ("Notion", 3), ("Outlook", 3), ("Word", 1))
    assert [day for day, _words in summary.daily_words] == [
        TODAY - timedelta(days=offset) for offset in range(13, -1, -1)
    ]
    assert dict(summary.daily_words)[TODAY] == 6
    assert dict(summary.daily_words)[TODAY - timedelta(days=7)] == 3
    assert summary.dictations == 6


def test_the_streak_survives_until_the_day_ends_and_breaks_on_a_gap():
    stats = _stats()
    stats.reset()
    for offset in (1, 2, 3, 5):
        _record("words", day=TODAY - timedelta(days=offset))
    summary = stats.load_summary(today=TODAY, tz=PACIFIC)
    assert summary.day_streak == 3 and summary.dictated_today is False
    assert stats.load_summary(today=TODAY + timedelta(days=1), tz=PACIFIC).day_streak == 0


def test_days_follow_the_local_zone():
    stats = _stats()
    stats.reset()
    # 11 pm Pacific on the 5th is already the 6th in Tokyo.
    _record("late night words", day=TODAY - timedelta(days=1), hour=23)
    pacific = dict(stats.load_summary(today=TODAY, tz=PACIFIC).daily_words)
    tokyo = dict(stats.load_summary(today=TODAY, tz=TOKYO).daily_words)
    assert pacific[TODAY - timedelta(days=1)] == 3 and pacific[TODAY] == 0
    assert tokyo[TODAY] == 3


def test_top_apps_keep_the_five_busiest():
    stats = _stats()
    stats.reset()
    for count, app in enumerate(["A", "B", "C", "D", "E", "F"], start=1):
        _record(" ".join(["w"] * count), app=app)
    _record("no app here at all")
    assert [name for name, _ in stats.load_summary(today=TODAY, tz=PACIFIC).top_apps] == [
        "F", "E", "D", "C", "B",
    ]


def _history(entry_id, text, timestamp, **fields):
    _db().add_history_entry(entry_id=entry_id, text=text, timestamp=timestamp, model="base", **fields)


def test_earlier_dictations_are_counted_once_on_first_load():
    from services.history_manager import history_manager
    from services.models import TranscriptionHistory

    stats = _stats()
    _history("legacy", "said this before", "2026-10-05T09:00:00", source_name="Quick Record",
             audio_duration=2.0)
    _history("profile", "dear team", "2026-10-04T09:00:00", source_name="Quick Record · Email",
             raw_text="dear team um")
    _history("v17", "Kind dictation.", "2026-10-03T16:00:00+00:00", entry_kind="dictation")
    _history("upload", "a talk", "2026-10-05T10:00:00", source_name="talk.mp3")
    _history("retranscribe", "again", "2026-10-05T11:00:00", entry_kind="file",
             source_name="recording_x.wav")
    _history("theirs", "laptop words", "2026-10-05T12:00:00", source_name="Quick Record")
    with _db().get_session() as session:
        session.get(TranscriptionHistory, "theirs").origin_device_id = "laptop"
    # Saved live after the upgrade, before Stats was ever opened.
    fields = dict(text="fresh words", model="base", entry_kind="dictation")
    live = history_manager.add_entry(**fields)
    stats.record(fields, live)

    summary = stats.load_summary(today=TODAY, tz=PACIFIC)

    assert sorted(row.entry_id for row in _rows()) == sorted(["legacy", "profile", "v17", live.id])
    assert summary.words_all_time == 3 + 3 + 2 + 2
    assert summary.words_cleaned_up == 1
    stats.load_summary(today=TODAY, tz=PACIFIC)
    assert len(_rows()) == 4


def test_reset_clears_stats_keeps_history_and_doesnt_count_it_again():
    stats = _stats()
    _history("legacy", "said this before", "2026-10-05T09:00:00", source_name="Quick Record")
    assert stats.load_summary(today=TODAY, tz=PACIFIC).words_all_time == 3

    stats.reset()

    empty = stats.load_summary(today=TODAY, tz=PACIFIC)
    assert empty.words_all_time == 0 and empty.dictations == 0 and empty.top_apps == ()
    assert _db().get_history_entry_by_id("legacy") is not None
    _record("after the reset")
    assert stats.load_summary(today=TODAY, tz=PACIFIC).words_all_time == 3

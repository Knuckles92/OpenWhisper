"""Stats beyond dictated words: files, walkthroughs, meetings, actions, snippets, dictionary."""

import importlib
import os
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

UTC = timezone.utc
PACIFIC = timezone(timedelta(hours=-7))
TODAY = date(2026, 10, 6)


def _stats():
    return importlib.import_module("services.dictation_stats")


def _db():
    return importlib.import_module("services.database").db


def _at(day: date, hour=12) -> str:
    return datetime(day.year, day.month, day.day, hour, tzinfo=PACIFIC).astimezone(UTC).isoformat()


def _record(text, *, day=TODAY, kind="dictation", hour=12, counts=None, **fields):
    _stats().record({"text": text, "entry_kind": kind, "timestamp": _at(day, hour),
                     "stats_counts": counts or {}, **fields}, None)


def _meeting(meeting_id, *, started, minutes=30, paused=0.0, status="ended", origin=None,
             words=("hello there", "three more words")):
    from services.models import MeetingSegment, MeetingSession

    ended = None if status == "active" else (started + timedelta(minutes=minutes)).isoformat()
    with _db().get_session() as session:
        session.add(MeetingSession(
            id=meeting_id, title="Standup", status=status, started_at=started.isoformat(),
            ended_at=ended, paused_total_s=paused, host_token="h", guest_token="g",
            spool_dir="", origin_device_id=origin,
        ))
        session.flush()
        for index, text in enumerate(words):
            session.add(MeetingSegment(
                id=f"{meeting_id}-{index}", meeting_id=meeting_id, channel="mic",
                start_s=float(index), end_s=index + 1.0, text=text,
                created_at=started.isoformat(),
            ))


def _week():
    return _stats().load_summary(today=TODAY, tz=PACIFIC).period("week")


def test_each_feature_gets_its_own_counts_and_none_of_the_dictated_words():
    stats = _stats()
    stats.reset()
    _record("eight words said aloud for the real count", raw_text="eight words said aloud for the real count",
            counts={"snippets": 2, "snippet_words": 40, "corrections": 3})
    _record("a walkthrough", kind="walkthrough", audio_duration=120.0, counts={"shots": 5})
    _record("to claude", kind="action", counts={"target": "Claude"})
    _record("to claude again", kind="action", day=TODAY - timedelta(days=1), counts={"target": "Claude"})
    _record("to the hook", kind="action", counts={"target": "Webhook"})
    _record("Formal now.", kind="command", raw_text="formal nw")
    _record("a talk of five words", kind="file", audio_duration=600.0)

    summary = stats.load_summary(today=TODAY, tz=PACIFIC)
    week = summary.period("week")

    assert summary.words_all_time == week.words == 8
    assert [feature.key for feature in week.features] == list(stats.FEATURES)
    walkthroughs = week.feature("walkthroughs")
    assert (walkthroughs.count, walkthroughs.shots, walkthroughs.seconds) == (1, 5, 120.0)
    actions = week.feature("actions")
    assert actions.count == 3 and actions.top == (("Claude", 2), ("Webhook", 1))
    assert actions.trend == (0, 0, 0, 0, 0, 1, 2)
    assert (week.feature("commands").count, week.feature("commands").words) == (1, 2)
    assert (week.feature("snippets").count, week.feature("snippets").words) == (2, 40)
    assert week.feature("dictionary").count == 3
    assert (week.feature("files").count, week.feature("files").words) == (1, 5)
    # A file's cleanup isn't words AI changed in something you dictated.
    assert week.words_cleaned_up == 2
    assert week.feature("meetings").count == 0


def test_a_feature_alone_keeps_no_streak_but_fills_the_window():
    stats = _stats()
    stats.reset()
    _record("only a file", kind="file")
    summary = stats.load_summary(today=TODAY, tz=PACIFIC)
    assert summary.entries == 1
    assert (summary.day_streak, summary.dictated_today, summary.first_day) == (0, False, None)


def test_words_break_down_by_app_kind_cleanup_language_and_hour():
    stats = _stats()
    stats.reset()
    _record("one two three four", app_category="work", cleanup_level="light", language="en", hour=9)
    _record("one two", app_category="email", language="it", hour=15)
    _record("one", day=TODAY - timedelta(days=1), hour=9)

    week = _week()

    assert week.categories == (("work", 4), ("email", 2), ("", 1))
    assert week.cleanup_levels == (("light", 4), ("", 3))
    assert week.languages == (("en", 4), ("it", 2), ("", 1))
    assert week.grid[TODAY.weekday()][9] == 4 and week.grid[TODAY.weekday()][15] == 2
    assert week.grid[(TODAY - timedelta(days=1)).weekday()][9] == 1
    assert week.hours[9] == 5 and week.weekdays[TODAY.weekday()] == 6


def test_a_breakdown_no_row_names_is_left_out():
    stats = _stats()
    stats.reset()
    _record("no context at all")
    week = _week()
    assert week.categories == week.cleanup_levels == week.languages == ()


def test_finished_meetings_are_counted_once_and_outlive_their_deletion():
    from meeting.persist.repository import SqlMeetingRepository

    stats = _stats()
    stats.reset()
    start = datetime(2026, 10, 6, 17, tzinfo=UTC)
    with _db().get_session() as session:
        from services.models import DictationStat

        # As on an install that has never counted meetings.
        session.query(DictationStat).filter(DictationStat.target == "features").delete()
    _meeting("done", started=start, minutes=30, paused=300.0)
    _meeting("running", started=start, status="active")
    _meeting("theirs", started=start, origin="laptop")

    meetings = _week().feature("meetings")
    assert (meetings.count, meetings.seconds, meetings.words) == (1, 1500.0, 5)

    SqlMeetingRepository(_db()).delete_meeting("done")
    assert _week().feature("meetings").count == 1


def test_a_meeting_deleted_before_stats_were_opened_is_still_counted():
    from meeting.persist.repository import SqlMeetingRepository

    stats = _stats()
    stats.reset()
    _meeting("quick", started=datetime.now(UTC) + timedelta(minutes=1), minutes=10)
    SqlMeetingRepository(_db()).delete_meeting("quick")
    assert stats.load_summary().period("all").feature("meetings").count == 1


def test_a_reset_forgets_past_meetings_and_counts_later_ones():
    stats = _stats()
    _meeting("before", started=datetime.now(UTC) - timedelta(hours=2))
    assert stats.load_summary().period("all").feature("meetings").count == 1

    stats.reset()
    assert stats.load_summary().entries == 0

    _meeting("after", started=datetime.now(UTC) + timedelta(minutes=1))
    assert stats.load_summary().period("all").feature("meetings").count == 1


def test_history_from_before_is_counted_once_without_retranscribed_recordings():
    stats = _stats()
    add = _db().add_history_entry
    add(entry_id="upload", text="a talk", timestamp="2026-10-05T10:00:00", model="base",
        source_name="talk.mp3", audio_duration=60.0)
    add(entry_id="again", text="said before", timestamp="2026-10-05T11:00:00", model="base",
        entry_kind="file", source_name="recording_20261005.wav")
    add(entry_id="walk", text="step one", timestamp="2026-10-05T12:00:00", model="base",
        entry_kind="walkthrough")
    add(entry_id="failed", text="", timestamp="2026-10-05T13:00:00", model="base",
        entry_kind="failed_recording")

    for _load in range(2):
        every = stats.load_summary(today=TODAY, tz=PACIFIC).period("all")
        assert every.feature("files").count == 1
        assert every.feature("walkthroughs").count == 1


def test_a_prepared_transcript_says_what_snippets_and_the_dictionary_did():
    from services.dictation_pipeline import PreparedText, stats_counts
    from services.snippets import Snippet, SnippetPlan

    sign_off = Snippet(id="1", trigger="sign off", text="Kind regards, Dana Fiori, Acme")
    inline = PreparedText(
        original="tell acme sign off", text="tell Acme sign off", cleanup_input="tell Acme [[S1]]",
        plan=SnippetPlan("tell Acme [[S1]]", placeholders=(("[[S1]]", sign_off),)),
        skip_cleanup=False,
    )
    assert stats_counts(inline) == {"snippets": 1, "snippet_words": 3, "corrections": 1}

    whole = PreparedText(original="sign off", text="sign off", cleanup_input="sign off",
                         plan=SnippetPlan("sign off", whole=sign_off), skip_cleanup=True)
    assert stats_counts(whole) == {"snippets": 1, "snippet_words": 3}

    plain = PreparedText("just words", "just words", "just words", SnippetPlan("just words"), False)
    assert stats_counts(plain) == {}


def test_a_v17_stats_table_gains_the_feature_columns(tmp_path):
    from services.database import SCHEMA_VERSION, DatabaseManager

    path = str(tmp_path / "v17.db")
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO schema_version(version) VALUES (17)")
        conn.execute(
            "CREATE TABLE dictation_stats (id INTEGER PRIMARY KEY AUTOINCREMENT, entry_id TEXT, "
            "timestamp TEXT NOT NULL, entry_kind TEXT NOT NULL, app_name TEXT, app_category TEXT, "
            "cleanup_level TEXT, spoken_words INTEGER NOT NULL, final_words INTEGER NOT NULL, "
            "words_edited INTEGER NOT NULL, audio_seconds REAL, language TEXT)"
        )
        conn.execute(
            "INSERT INTO dictation_stats (timestamp, entry_kind, spoken_words, final_words, "
            "words_edited) VALUES ('2026-10-05T10:00:00', 'dictation', 4, 4, 0)"
        )
    for _opening in range(2):
        manager = DatabaseManager(db_path=path)
        try:
            with manager.engine.connect() as connection:
                row = connection.exec_driver_sql(
                    "SELECT target, snippets, snippet_words, corrections, shots, spoken_words "
                    "FROM dictation_stats"
                ).one()
                version = connection.exec_driver_sql("SELECT version FROM schema_version").scalar_one()
            assert tuple(row) == (None, 0, 0, 0, 0, 4)
            assert version == SCHEMA_VERSION == 18
        finally:
            manager.close()
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("UPDATE schema_version SET version = 17")


# ---- the window ----


@pytest.fixture
def synchronous(monkeypatch):
    module = importlib.import_module("ui_qt.dialogs.stats_dialog")
    monkeypatch.setattr(
        module, "_run",
        lambda work, token: module._relay().loaded.emit(token, work(), ""),
    )
    return module


def _now(days_back=0) -> str:
    day = date.today() - timedelta(days=days_back)
    return datetime(day.year, day.month, day.day, 12).astimezone(UTC).isoformat()


def _seed_window():
    stats = _stats()
    stats.record({"text": "four words said here", "entry_kind": "dictation", "timestamp": _now(),
                  "app_category": "work", "cleanup_level": "medium", "language": "en",
                  "stats_counts": {"snippets": 1, "snippet_words": 12}}, None)
    stats.record({"text": "due parole", "entry_kind": "dictation", "timestamp": _now(),
                  "language": "it"}, None)
    stats.record({"text": "a talk", "entry_kind": "file", "audio_duration": 600.0,
                  "timestamp": _now()}, None)
    stats.record({"text": "ask it", "entry_kind": "action", "timestamp": _now(),
                  "stats_counts": {"target": "Claude"}}, None)
    stats.record({"text": "steps", "entry_kind": "walkthrough", "timestamp": _now(20),
                  "stats_counts": {"shots": 3}}, None)


def test_the_window_lists_a_row_for_each_feature_ever_used(synchronous):
    _seed_window()
    dialog = synchronous.StatsDialog()
    dialog.show()

    assert not dialog.features_section.isHidden()
    assert dialog.feature_rows == [
        ("files", "Audio files", "1 file · 10 min of audio · 2 words"),
        ("walkthroughs", "Walkthroughs", "None in this span"),
        ("actions", "Actions", "1 sent · Claude 1"),
        ("snippets", "Snippets", "1 used · wrote 12 words for you"),
    ]
    dialog.period_bar.buttons[1].click()
    assert dialog.feature_rows[1] == ("walkthroughs", "Walkthroughs", "1 walkthrough · 3 screenshots")
    dialog.close()


def test_the_window_breaks_the_words_down(synchronous):
    _seed_window()
    dialog = synchronous.StatsDialog()
    dialog.show()

    assert dialog.category_breakdown.legend.text() == "Work 67% · Unrecognized apps 33%"
    assert dialog.cleanup_breakdown.legend.text() == "Medium 67% · Off 33%"
    assert dialog.language_breakdown.legend.text() == "English 67% · Italian 33%"
    assert dialog.category_breakdown.bar.accessibleDescription() == "Work: 67%; Unrecognized apps: 33%"
    assert [named for _label, _words, named in dialog.cleanup_breakdown.bar.parts] == [True, False]
    assert not dialog.breakdown_section.isHidden()
    dialog.close()


def test_the_breakdown_is_hidden_when_nothing_names_one(synchronous):
    _stats().record({"text": "plain words", "entry_kind": "dictation", "timestamp": _now(),
                     "language": "en"}, None)
    dialog = synchronous.StatsDialog()
    dialog.show()
    assert dialog.language_breakdown.widget.isHidden()
    assert dialog.breakdown_section.isHidden()
    dialog.close()


def test_a_window_with_only_a_file_is_not_empty(synchronous):
    _stats().record({"text": "a talk", "entry_kind": "file", "timestamp": _now()}, None)
    dialog = synchronous.StatsDialog()
    dialog.show()
    assert dialog.pages.currentIndex() == 2
    assert dialog.hero_value.text() == "0 words"
    assert dialog.hour_label.text() == "Nothing dictated in this span yet"
    assert [row[0] for row in dialog.feature_rows] == ["files"]
    dialog.close()


def test_the_hour_grid_reads_out_its_busiest_slot(_session_qt_application):
    from ui_qt.widgets.stats_charts import HourGrid

    grid = HourGrid()
    assert grid.peak() is None
    words = [[0] * 24 for _weekday in range(7)]
    words[3][10], words[0][9] = 12, 4
    grid.set_grid(words)
    assert grid.peak() == (3, 10)
    assert grid.accessibleDescription() == "Busiest on Thursdays around 10 AM, with 12 words"
    assert [grid.level(count) for count in (0, 4, 12)] == [0, 1, 4]
    grid.set_grid([[0] * 24] * 7)
    assert grid.accessibleDescription() == "No words yet"


def test_a_feature_trend_words_its_columns_by_what_it_counts(_session_qt_application):
    from ui_qt.widgets.stats_charts import Bar, BarChart

    trend = BarChart(22, value_text=lambda count: f"{count} sent")
    trend.set_bars([Bar("Mon", 2), Bar("Today", 1)], "Actions over this span")
    assert trend.accessibleDescription() == "Mon: 2 sent; Today: 1 sent"

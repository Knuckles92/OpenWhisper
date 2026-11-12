"""The Stats charts give screen readers each column's words."""
from datetime import date, datetime, timedelta, timezone

from tests import test_personalize_s9_stats_dialog as stats_tests

TODAY = date.today()
_stats = stats_tests._stats
synchronous = stats_tests.synchronous


def _seed():
    stats = _stats()
    for offset, count in enumerate([7, 4, 0, 3]):
        if not count:
            continue
        day = TODAY - timedelta(days=offset)
        moment = datetime(day.year, day.month, day.day, 12).astimezone(timezone.utc)
        stats.record({
            "text": " ".join(["word"] * count), "raw_text": " ".join(["word"] * count),
            "entry_kind": "dictation", "audio_duration": 3.0, "app_name": "Slack",
            "timestamp": moment.isoformat(),
        }, None)


def test_the_chart_reads_out_every_day(synchronous):
    _seed()
    dialog = synchronous.StatsDialog()
    dialog.show()
    bars = dialog.bars
    assert bars.accessibleName() == "Words per day, last 7 days"
    days = bars.accessibleDescription().split("; ")
    assert len(days) == 7
    assert days[-1] == "Today: 7 words"
    yesterday = TODAY - timedelta(days=1)
    assert days[-2] == f"{yesterday:%a}, {yesterday:%b} {yesterday.day}: 4 words"
    assert days[-3].endswith(": 0 words")
    first = TODAY - timedelta(days=6)
    assert days[0].startswith(f"{first:%a}, {first:%b} {first.day}: ")
    dialog.close()


def test_the_chart_text_follows_a_reload(synchronous):
    from ui_qt.widgets.stats_charts import Bar

    dialog = synchronous.StatsDialog()
    bars = dialog.bars
    bars.set_bars([Bar("Yesterday", 1), Bar("Today", 2)], "Words per day, last 2 days")
    assert bars.accessibleName() == "Words per day, last 2 days"
    assert bars.accessibleDescription().endswith("; Today: 2 words")
    bars.set_bars([], "Words per day")
    assert bars.accessibleDescription() == ""


def test_the_activity_map_summarises_the_year(synchronous):
    _seed()
    dialog = synchronous.StatsDialog()
    dialog.show()
    activity = dialog.activity
    assert activity.accessibleName() == "Activity, a square per day"
    assert activity.accessibleDescription() == (
        f"3 days with words; the busiest, {TODAY:%B} {TODAY.day}, had 7 words"
    )
    assert [activity.level(words) for words in (0, 3, 4, 7)] == [0, 1, 2, 4]
    dialog.close()

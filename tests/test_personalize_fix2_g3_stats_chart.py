"""The Stats 14-day chart gives screen readers each day's words."""
import importlib
from datetime import date, datetime, timedelta, timezone

import pytest

TODAY = date.today()


def _dialog_module():
    return importlib.import_module("ui_qt.dialogs.stats_dialog")


def _stats():
    return importlib.import_module("services.dictation_stats")


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


@pytest.fixture
def synchronous(monkeypatch):
    module = _dialog_module()
    monkeypatch.setattr(
        module, "_run",
        lambda work, delivery, generation: delivery.loaded.emit(generation, work(), ""),
    )
    return module


def test_the_chart_reads_out_every_day(synchronous):
    _seed()
    dialog = synchronous.StatsDialog()
    dialog.show()
    bars = dialog.bars
    assert bars.accessibleName() == "Words per day, last 14 days"
    days = bars.accessibleDescription().split("; ")
    assert len(days) == 14
    assert days[-1] == "Today: 7 words"
    yesterday = TODAY - timedelta(days=1)
    assert days[-2] == f"{yesterday:%a}, {yesterday:%b} {yesterday.day}: 4 words"
    assert days[-3].endswith(": 0 words")
    first = TODAY - timedelta(days=13)
    assert days[0].startswith(f"{first:%a}, {first:%b} {first.day}: ")
    dialog.close()


def test_the_chart_text_follows_a_reload(synchronous):
    dialog = synchronous.StatsDialog()
    bars = dialog.bars
    bars.set_days([(TODAY - timedelta(days=1), 1), (TODAY, 2)])
    assert bars.accessibleName() == "Words per day, last 2 days"
    assert bars.accessibleDescription().endswith("; Today: 2 words")
    bars.set_days([])
    assert bars.accessibleDescription() == ""

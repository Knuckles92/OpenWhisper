"""The Stats window: loading off the UI thread, empty and full states, reset."""

import importlib
import os
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6 import sip
from PyQt6.QtWidgets import QAbstractButton, QApplication, QLabel, QMessageBox

TODAY = date.today()


def _dialog_module():
    return importlib.import_module("ui_qt.dialogs.stats_dialog")


def _stats():
    return importlib.import_module("services.dictation_stats")


def _pump(until, timeout=3.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if until():
            return True
        time.sleep(0.01)
    return until()


def _seed():
    stats = _stats()
    for offset, (count, app) in enumerate([(6, "Slack"), (4, "Outlook"), (3, "Slack")]):
        day = TODAY - timedelta(days=offset)
        moment = datetime(day.year, day.month, day.day, 12).astimezone(timezone.utc)
        stats.record({
            "text": " ".join(["word"] * count), "raw_text": " ".join(["word"] * count) + " um",
            "entry_kind": "dictation", "audio_duration": 3.0, "app_name": app,
            "timestamp": moment.isoformat(),
        }, None)


@pytest.fixture
def synchronous(monkeypatch):
    """Run the window's background work inline, as the worker would."""
    module = _dialog_module()
    monkeypatch.setattr(
        module, "_run",
        lambda work, token: module._relay().loaded.emit(token, work(), ""),
    )
    return module


def test_with_nothing_counted_it_says_so_and_offers_no_reset(synchronous):
    dialog = synchronous.StatsDialog()
    dialog.show()
    assert dialog.pages.currentIndex() == 1
    assert dialog.empty_text.text().startswith("Dictate something")
    assert not dialog.reset_button.isEnabled()
    dialog.close()


def test_it_shows_the_counts_days_and_apps(synchronous):
    _seed()
    dialog = synchronous.StatsDialog()
    dialog.show()

    assert dialog.pages.currentIndex() == 2
    assert dialog.hero_caption.text() == "Last 7 days"
    assert dialog.hero_value.text() == "16 words"
    assert dialog.trend_chip.isHidden()
    assert dialog.saved_value.text() == "Under a minute"
    assert dialog.milestone_label.text() == "Next milestone: 1,000 words"
    assert dialog.milestone_left.text() == "984 to go"
    assert dialog.pace_card.value_label.text() == "107 wpm"
    assert dialog.pace_card.detail_label.text() == "2.7× faster than typing"
    assert dialog.dictations_card.value_label.text() == "3"
    assert dialog.dictations_card.detail_label.text() == "5 words each"
    assert dialog.cleanup_card.value_label.text() == "3 words"
    assert dialog.streak_card.value_label.text() == "3 days"
    assert dialog.streak_card.detail_label.text() == "Keep it going"
    assert len(dialog.bars.bars) == 7 and dialog.bars.bars[-1].words == 7
    assert dialog.app_rows == [("Slack", 11), ("Outlook", 5)]
    assert dialog.best_label.text().endswith("7 words")
    assert dialog.hour_label.text() == "Most active around 12 PM"
    assert dialog.no_apps_label.isHidden()
    assert len(dialog.activity.days) == _stats().ACTIVITY_DAYS
    assert dialog.active_days_label.text() == "3 active days this past year"
    assert dialog.reset_button.isEnabled()
    dialog.close()


def test_the_period_switch_recounts_every_section(synchronous):
    _seed()
    dialog = synchronous.StatsDialog()
    dialog.show()

    dialog.period_bar.buttons[1].click()
    assert dialog.period_key == "month"
    assert dialog.hero_caption.text() == "Last 30 days"
    assert len(dialog.bars.bars) == 30

    dialog.period_bar.buttons[2].click()
    assert dialog.hero_caption.text() == "All time"
    assert dialog.trend_chip.text().startswith("Since ")
    assert dialog.chart_title.text() == "Words per month"
    assert dialog.bars.bars[-1].axis == f"{TODAY:%b}"
    assert dialog.app_rows == [("Slack", 11), ("Outlook", 5)]
    dialog.close()


def test_a_new_dictation_refreshes_the_open_window(synchronous):
    _seed()
    dialog = synchronous.StatsDialog()
    dialog.show()
    _stats().record({"text": "one more for the road", "entry_kind": "dictation",
                     "timestamp": datetime.now(timezone.utc).isoformat()}, None)
    assert _pump(lambda: dialog.hero_value.text() == "21 words")
    dialog.close()


def test_a_slower_older_count_never_replaces_a_newer_one(monkeypatch):
    module = _dialog_module()
    runs = []
    monkeypatch.setattr(module, "_run", lambda work, token: runs.append(token))
    dialog = module.StatsDialog()
    dialog.reload()
    dialog.reload()
    old, new = runs
    full = _stats().StatsSummary(words_all_time=5, entries=1)
    empty = _stats().StatsSummary()

    module._relay().loaded.emit(new, full, "")
    module._relay().loaded.emit(old, empty, "")

    assert dialog.summary is full and dialog.pages.currentIndex() == 2


def test_counting_runs_off_the_ui_thread(monkeypatch):
    import threading

    module = _dialog_module()
    threads = []
    real = _stats().load_summary

    def load(*args, **kwargs):
        threads.append(threading.current_thread())
        return real(*args, **kwargs)

    monkeypatch.setattr(_stats(), "load_summary", load)
    dialog = module.StatsDialog()
    dialog.show()
    assert _pump(lambda: dialog.pages.currentIndex() == 1)
    assert threads and threads[0] is not threading.main_thread()
    dialog.close()


def test_a_failed_count_says_so(synchronous, monkeypatch):
    monkeypatch.setattr(synchronous, "_run",
                        lambda work, token: synchronous._relay().loaded.emit(token, None, "locked"))
    dialog = synchronous.StatsDialog()
    dialog.show()
    assert dialog.pages.currentIndex() == 0
    assert dialog.loading_label.text() == "Stats couldn't be counted right now."
    dialog.close()


def test_reset_asks_first_then_starts_over(synchronous):
    _seed()
    dialog = synchronous.StatsDialog()
    dialog.show()

    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
        dialog.reset_button.click()
    assert dialog.pages.currentIndex() == 2

    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
        dialog.reset_button.click()
    assert dialog.pages.currentIndex() == 1
    assert _stats().load_summary().entries == 0
    dialog.close()


def test_show_stats_keeps_one_window_and_leaves_settings_alone(synchronous, monkeypatch):
    from services.settings import settings_manager

    monkeypatch.setattr(synchronous, "_window", None)
    before = settings_manager.load_all_settings()
    ui = SimpleNamespace(main_window=None)
    synchronous.show_stats(ui)
    first = synchronous._window
    assert first.isVisible() and not first.isModal()
    synchronous.show_stats(ui)
    assert synchronous._window is first
    first.close()
    synchronous.show_stats(ui)
    assert synchronous._window is first and first.isVisible()
    assert settings_manager.load_all_settings() == before
    first.close()
    sip.delete(first)
    synchronous.show_stats(ui)
    assert synchronous._window is not first
    synchronous._window.close()


@pytest.mark.usefixtures("isolated_app_theme")
@pytest.mark.parametrize("ui_mode,width", [("classic", 720), ("omarchy", 460), ("classic", 460)])
def test_it_fits_narrow_windows_at_large_fonts(synchronous, monkeypatch, ui_mode, width):
    from ui_qt.utils.font_scale import apply_ui_font_scale
    from ui_qt.utils.theme_manager import ThemeManager

    _seed()
    monkeypatch.setenv("OPENWHISPER_UI", ui_mode)
    app = QApplication.instance()
    dialog = None
    try:
        apply_ui_font_scale(130, app=app, theme_manager=ThemeManager())
        dialog = synchronous.StatsDialog()
        dialog.show()
        dialog.resize(width, 640)
        for _ in range(8):
            app.processEvents()
        page = dialog.pages.currentWidget()
        assert dialog.width() == width
        for control in dialog.findChildren(QAbstractButton) + page.findChildren(QLabel):
            if not control.isVisible():
                continue
            right = control.mapTo(dialog, control.rect().bottomRight()).x()
            assert control.mapTo(dialog, control.rect().topLeft()).x() >= 0
            assert right < dialog.width(), control.objectName()
        for label in page.findChildren(QLabel):
            if label.isVisible() and label.wordWrap():
                assert label.height() >= label.heightForWidth(label.width())
    finally:
        if dialog is not None:
            dialog.close()

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
        lambda work, delivery, generation: delivery.loaded.emit(generation, work(), ""),
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
    assert dialog.week_card.value_label.text() == "16 words"
    assert dialog.week_card.detail_label.text() == "16 all time"
    assert dialog.pace_card.value_label.text() == "107 wpm"
    assert dialog.cleanup_card.value_label.text() == "3 words"
    assert dialog.streak_card.value_label.text() == "3 days"
    assert dialog.streak_card.detail_label.text() == "Keep it going"
    assert len(dialog.bars.days) == 14 and dialog.bars.days[-1] == (TODAY, 7)
    assert dialog.app_rows == [("Slack", 11), ("Outlook", 5)]
    assert dialog.best_day_label.text().endswith("7 words")
    assert dialog.no_apps_label.isHidden()
    assert dialog.reset_button.isEnabled()
    dialog.close()


def test_a_slower_older_count_never_replaces_a_newer_one(monkeypatch):
    module = _dialog_module()
    runs = []
    monkeypatch.setattr(module, "_run", lambda work, delivery, generation: runs.append(
        (delivery, generation)))
    dialog = module.StatsDialog()
    dialog.reload()
    dialog.reload()
    (old, old_generation), (new, new_generation) = runs
    full = _stats().StatsSummary(words_this_week=5, words_all_time=5, dictations=1)
    empty = _stats().StatsSummary()

    new.loaded.emit(new_generation, full, "")
    old.loaded.emit(old_generation, empty, "")

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
                        lambda work, delivery, generation: delivery.loaded.emit(generation, None, "locked"))
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
    assert _stats().load_summary().dictations == 0
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


@pytest.mark.parametrize("ui_mode,width", [("classic", 720), ("omarchy", 460), ("classic", 460)])
def test_it_fits_narrow_windows_at_large_fonts(synchronous, monkeypatch, ui_mode, width):
    from ui_qt.utils.font_scale import apply_ui_font_scale, current_ui_font_scale_percent
    from ui_qt.utils.palette import current_palette, set_current_palette
    from ui_qt.utils.theme_manager import ThemeManager

    _seed()
    monkeypatch.setenv("OPENWHISPER_UI", ui_mode)
    app = QApplication.instance()
    previous_style, previous_font = app.styleSheet(), app.font()
    previous_scale, previous_palette = current_ui_font_scale_percent(), current_palette()
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
        apply_ui_font_scale(previous_scale, app=app)
        set_current_palette(previous_palette)
        app.setFont(previous_font)
        app.setStyleSheet(previous_style)

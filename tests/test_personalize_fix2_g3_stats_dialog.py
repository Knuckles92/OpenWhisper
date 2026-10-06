"""Enter in the Stats window closes it; only a focused Reset asks to reset."""
import importlib
from datetime import date, datetime, timedelta, timezone

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QMessageBox, QPushButton

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


@pytest.fixture
def asked(monkeypatch):
    questions = []

    def question(_parent, title, *_args, **_kwargs):
        questions.append(title)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", question)
    return questions


def _defaults(dialog):
    return [button.text() for button in dialog.findChildren(QPushButton) if button.isDefault()]


@pytest.mark.parametrize("key", [Qt.Key.Key_Return, Qt.Key.Key_Enter])
def test_enter_closes_the_window_instead_of_offering_a_reset(synchronous, asked, key):
    _seed()
    dialog = synchronous.StatsDialog()
    for _opening in range(2):
        dialog.show()
        assert dialog.reset_button.isEnabled()
        keypad = Qt.KeyboardModifier.KeypadModifier if key == Qt.Key.Key_Enter else Qt.KeyboardModifier.NoModifier
        QTest.keyClick(dialog.focusWidget() or dialog, key, keypad)
        assert asked == []
        assert not dialog.isVisible()
        assert _defaults(dialog) == ["Close"]


def test_enter_on_the_focused_reset_button_still_asks_first(synchronous, asked):
    _seed()
    dialog = synchronous.StatsDialog()
    dialog.show()
    dialog.reset_button.setFocus()
    QTest.keyClick(dialog.reset_button, Qt.Key.Key_Return)
    assert asked == ["Reset stats"]
    assert dialog.isVisible()
    assert dialog.summary.dictations
    dialog.close()

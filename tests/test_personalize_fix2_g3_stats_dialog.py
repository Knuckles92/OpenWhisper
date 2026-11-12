"""Enter in the Stats window closes it; only a focused Reset asks to reset."""

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QMessageBox, QPushButton

from tests import test_personalize_fix2_g3_stats_chart as chart_tests

_seed = chart_tests._seed
synchronous = chart_tests.synchronous


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
    assert dialog.summary.entries
    dialog.close()

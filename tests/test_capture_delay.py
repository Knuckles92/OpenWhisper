"""Delay preferences, the live picker, and its capture countdown affordance."""

from unittest.mock import Mock

import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest

from services.settings import SettingsKey, resolve_walkthrough_capture_delay
from tests.fakes.settings import InMemorySettings
from ui_qt.snap.toolbar import SnapToolbar
from ui_qt.utils.palette import current_palette
from ui_qt.widgets import capture_delay


@pytest.mark.parametrize("raw, expected", [(None, 0), (True, 0), ("3", 0), (-1, 0),
                                           (30, 0), (0, 0), (3, 3), (10, 10)])
def test_invalid_delay_settings_fall_back_to_instant(raw, expected):
    assert resolve_walkthrough_capture_delay({SettingsKey.WALKTHROUGH_CAPTURE_DELAY: raw}) == expected


def test_live_picker_saves_delay_without_taking_focus_and_cancels_pending_capture(monkeypatch):
    settings = InMemorySettings()
    monkeypatch.setattr(capture_delay, "settings_manager", settings)
    monkeypatch.setattr(capture_delay.affinity, "keep_out_of_screenshots", Mock())
    toolbar = SnapToolbar(None, current_palette(), live=True)
    toolbar.show()
    chip = toolbar.capture_delay
    chosen, cancelled = [], []
    chip.changed.connect(chosen.append)
    chip.cancelled.connect(lambda: cancelled.append(True))
    try:
        assert chip.focusPolicy() == Qt.FocusPolicy.NoFocus
        QTest.mouseClick(chip, Qt.MouseButton.LeftButton)
        picker = chip._menu
        assert picker.isVisible()
        assert picker.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
        assert not picker.grab().isNull()  # Exercise all the painted preset states.
        capture_delay.affinity.keep_out_of_screenshots.assert_called_with(picker, True)
        QTest.mouseClick(picker, Qt.MouseButton.LeftButton, pos=picker._option(4).center().toPoint())
        assert chosen == [5]
        assert settings.values[SettingsKey.WALKTHROUGH_CAPTURE_DELAY] == chip.seconds == 5
        assert chip._menu is None
        chip.set_countdown(2.4, 5)
        assert not chip.grab().isNull()
        QTest.mouseClick(chip, Qt.MouseButton.LeftButton, pos=QPoint(chip.width() - 14, 17))
        assert cancelled == [True]
        assert chosen == [5]
    finally:
        toolbar.close()


def test_tab_delay_can_be_changed_with_arrow_keys(monkeypatch):
    settings = InMemorySettings()
    monkeypatch.setattr(capture_delay, "settings_manager", settings)
    chip = capture_delay.CaptureDelay()
    chip.set_seconds(0)
    QTest.keyClick(chip, Qt.Key.Key_Right)
    assert chip.seconds == 1
    QTest.keyClick(chip, Qt.Key.Key_Left)
    assert chip.seconds == 0
    assert settings.values[SettingsKey.WALKTHROUGH_CAPTURE_DELAY] == 0
    chip.close()


def test_toolbar_wraps_delay_and_controls_on_small_screens():
    toolbar = SnapToolbar(None, current_palette(), live=True)
    toolbar.fit_to_width(780)
    assert toolbar.width() <= 780
    assert toolbar.button_rect("delay").top() > toolbar.button_rect("tool").top()
    assert toolbar.capture_delay.geometry() == toolbar.button_rect("delay")
    toolbar.fit_to_width(1400)
    assert toolbar.button_rect("delay").top() == toolbar.button_rect("tool").top()
    toolbar.close()

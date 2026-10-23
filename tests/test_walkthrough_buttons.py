"""The walkthrough's buttons: Start/Finish in the main window, Snap and Done on the overlay."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QWidget

from ui_qt.overlays import waveform_overlay
from ui_qt.overlays.waveform_overlay import WaveformOverlay
from ui_qt.ui_controller import UIController
from ui_qt.widgets.quick_record_tab import QuickRecordTab


@pytest.fixture
def tab():
    widget = QuickRecordTab()
    yield widget
    widget.close()


@pytest.fixture
def overlay(monkeypatch):
    monkeypatch.setattr(waveform_overlay, "_platform_takes_overlay_clicks", lambda: True)
    widget = WaveformOverlay()
    yield widget
    widget.hide()
    widget.close()


def _paint(widget):
    with patch.object(waveform_overlay.logger, "error") as error:
        widget.grab()
    error.assert_not_called()


def _recording_walkthrough(overlay):
    overlay.set_recording_destination("Walkthrough · 1 shot")
    overlay.set_walkthrough_controls(True)
    overlay.show_at_cursor(overlay.STATE_RECORDING)
    _paint(overlay)


def _center(overlay, key) -> QPoint:
    return overlay._control_rects[key].center().toPoint()


# --- the main window's button ---------------------------------------------


def test_the_button_asks_for_a_walkthrough(tab):
    spy = QSignalSpy(tab.walkthrough_requested)

    tab.walkthrough_button.click()

    assert len(spy) == 1
    assert tab.walkthrough_button.text() == "Start Walkthrough"


def test_during_a_walkthrough_the_button_finishes_it(tab):
    tab.is_recording = True
    tab.set_walkthrough_active(True)
    tab._update_recording_state()

    assert tab.walkthrough_button.text() == "Finish Walkthrough"
    assert tab.walkthrough_button._active

    tab.is_recording = False
    tab.set_walkthrough_active(False)
    tab._update_recording_state()
    assert tab.walkthrough_button.text() == "Start Walkthrough"
    assert tab.walkthrough_button._active


def test_a_dictation_in_progress_holds_the_button(tab):
    spy = QSignalSpy(tab.walkthrough_requested)
    tab.is_recording = True
    tab._update_recording_state()

    QTest.mouseClick(tab.walkthrough_button, Qt.MouseButton.LeftButton)

    assert not tab.walkthrough_button._active
    assert len(spy) == 0


def test_the_button_shows_the_walkthrough_shortcut(tab):
    tab.walkthrough_button.set_hotkey = Mock()

    tab.update_hotkeys("Num *", "Num -", "", "Ctrl+Alt+W")

    tab.walkthrough_button.set_hotkey.assert_called_once_with("Ctrl+Alt+W")


def test_the_button_leaves_its_explanation_to_the_badge(tab):
    # Its own hover shows the shortcut pill, as the buttons above it do.
    assert tab.walkthrough_button.toolTip() == ""
    assert "screenshots" in tab.walkthrough_badge.toolTip()
    assert tab.walkthrough_badge.parent() is tab.walkthrough_button


def test_the_badge_sits_on_the_buttons_right_side_clear_of_its_label(tab):
    # A hidden widget gets its resize event only once shown.
    tab.show()
    tab.walkthrough_button.resize(360, 48)

    badge = tab.walkthrough_badge.geometry()
    assert 360 - 20 < badge.right() < 360
    assert abs(badge.center().y() - 24) <= 1
    label = tab.walkthrough_button.fontMetrics().horizontalAdvance("Start Walkthrough")
    assert badge.left() > (360 + label) / 2
    assert not tab.walkthrough_badge.is_compact()


def test_in_a_narrow_button_new_folds_to_its_sparkle(tab):
    tab.show()
    tab.walkthrough_button.resize(220, 48)

    badge = tab.walkthrough_badge
    assert badge.is_compact()
    assert badge.width() == badge.height()
    label = tab.walkthrough_button.fontMetrics().horizontalAdvance("Start Walkthrough")
    assert badge.geometry().left() > (220 + label) / 2


def test_clicking_the_badge_never_starts_a_walkthrough(tab):
    tab.show()
    spy = QSignalSpy(tab.walkthrough_requested)

    QTest.mouseClick(tab.walkthrough_badge, Qt.MouseButton.LeftButton)

    assert len(spy) == 0


def test_the_badge_says_new_until_the_first_walkthrough(tab):
    from services.settings import SettingsKey, settings_manager

    assert tab.walkthrough_badge.text() == "New"

    tab.set_walkthrough_active(True)

    assert tab.walkthrough_badge.text() == "?"
    assert settings_manager.load_all_settings()[SettingsKey.WALKTHROUGH_INTRO_SEEN] is True
    again = QuickRecordTab()
    try:
        assert again.walkthrough_badge.text() == "?"
    finally:
        again.close()


# --- Snap and Done on the overlay -------------------------------------------


def test_snap_and_done_show_only_while_a_walkthrough_records(overlay):
    overlay.show_at_cursor(overlay.STATE_RECORDING)
    _paint(overlay)
    assert overlay._control_rects == {}

    _recording_walkthrough(overlay)
    assert set(overlay._control_rects) == {"snap", "finish"}
    snap, done = overlay._control_rects["snap"], overlay._control_rects["finish"]
    assert snap.right() < done.left()
    assert done.right() <= overlay.width()
    assert snap.left() > overlay._badge_rect.right()

    overlay.set_state(overlay.STATE_PROCESSING)
    _paint(overlay)
    assert overlay._control_rects == {}


def test_clicking_snap_and_done(overlay):
    snap = QSignalSpy(overlay.walkthrough_snap_requested)
    done = QSignalSpy(overlay.walkthrough_finish_requested)
    _recording_walkthrough(overlay)

    QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 60))
    assert (len(snap), len(done)) == (0, 0)
    QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=_center(overlay, "snap"))
    assert (len(snap), len(done)) == (1, 0)
    QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=_center(overlay, "finish"))
    assert (len(snap), len(done)) == (1, 1)


def test_hovering_a_control_shows_a_pointing_hand(overlay):
    _recording_walkthrough(overlay)

    QTest.mouseMove(overlay, _center(overlay, "snap"))
    assert overlay._hover == "snap"
    assert overlay.cursor().shape() == Qt.CursorShape.PointingHandCursor

    overlay.set_walkthrough_controls(False)
    assert overlay._hover == ""
    assert overlay._control_rects == {}


def test_beside_a_language_chip_the_controls_drop_their_words(overlay):
    overlay._language, overlay._language_choices = "en", ("en", "es")
    _recording_walkthrough(overlay)
    overlay._language, overlay._language_choices = "en", ("en", "es")
    _paint(overlay)

    snap, done = overlay._control_rects["snap"], overlay._control_rects["finish"]
    assert done.right() < overlay._chip_rect.left()
    assert snap.left() > overlay._badge_rect.right()
    assert snap.width() == done.width() < 30


def test_no_controls_where_overlay_clicks_could_take_focus(overlay, monkeypatch):
    monkeypatch.setattr(waveform_overlay, "_platform_takes_overlay_clicks", lambda: False)
    spy = QSignalSpy(overlay.walkthrough_snap_requested)

    _recording_walkthrough(overlay)
    QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=QPoint(200, 18))

    assert overlay._control_rects == {}
    assert len(spy) == 0


def test_no_controls_on_an_embedded_overlay(monkeypatch):
    monkeypatch.setattr(waveform_overlay, "_platform_takes_overlay_clicks", lambda: True)
    host = QWidget()
    host.resize(600, 400)
    host.show()
    embedded = WaveformOverlay(host)
    try:
        _recording_walkthrough(embedded)
        assert embedded._control_rects == {}
    finally:
        host.close()


def test_a_capture_leaves_the_recording_overlay_as_it_was(overlay):
    from ui_qt.snap import capture

    _recording_walkthrough(overlay)

    hidden = capture.hide_windows([overlay])
    assert hidden == [overlay] and not overlay.isVisible()
    capture.restore_windows(hidden)

    assert overlay.isVisible()
    assert overlay.current_state == overlay.STATE_RECORDING
    assert overlay.recording_destination == "Walkthrough · 1 shot"
    _paint(overlay)
    assert set(overlay._control_rects) == {"snap", "finish"}


# --- the UI controller passes it all on -------------------------------------


@pytest.mark.parametrize("window, keep_out", [(True, 1), (False, 0)])
def test_snaps_keep_the_overlay_out_only_when_it_is_its_own_window(monkeypatch, window, keep_out):
    import ui_qt.snap as snap

    seen = {}
    monkeypatch.setattr(snap, "start_snap", lambda **kwargs: seen.update(kwargs) or "session")
    ui = SimpleNamespace(overlay=Mock(), _walkthrough_panel=None)
    ui.overlay.isWindow.return_value = window

    assert UIController.start_snap(ui, hint="talk") == "session"

    assert len(seen["keep_out"]) == keep_out
    assert seen["hint"] == "talk"


def test_the_controller_shows_the_walkthrough_everywhere():
    ui = SimpleNamespace(
        tray_manager=Mock(),
        main_window=SimpleNamespace(quick_record_tab=Mock()),
        overlay=Mock(),
        _show_walkthrough_panel=Mock(),
    )

    UIController.set_walkthrough_active(ui, True)

    ui.tray_manager.set_walkthrough_active.assert_called_once_with(True)
    ui.main_window.quick_record_tab.set_walkthrough_active.assert_called_once_with(True)
    ui.overlay.set_walkthrough_controls.assert_called_once_with(True)
    ui._show_walkthrough_panel.assert_called_once_with(True)


def test_snaps_keep_the_walkthrough_column_out_while_it_shows(monkeypatch):
    import ui_qt.snap as snap

    seen = {}
    monkeypatch.setattr(snap, "start_snap", lambda **kwargs: seen.update(kwargs) or "session")
    panel = Mock()
    panel.isVisible.return_value = True
    ui = SimpleNamespace(overlay=Mock(), _walkthrough_panel=panel)
    ui.overlay.isWindow.return_value = True

    UIController.start_snap(ui, hint="")

    assert seen["keep_out"] == (ui.overlay, panel)


def test_buttons_reach_the_walkthrough_as_the_user():
    calls = []
    ui = SimpleNamespace(on_walkthrough=lambda source: calls.append(("walkthrough", source)),
                         on_screenshot=lambda source: calls.append(("screenshot", source)))

    UIController._on_window_walkthrough(ui)
    UIController._on_overlay_snap(ui)
    UIController._on_overlay_finish(ui)

    assert calls == [("walkthrough", "ui"), ("screenshot", "ui"), ("walkthrough", "ui")]

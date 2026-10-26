"""The walkthrough's buttons: its tab in the main window, Snap and Done on the overlay."""

import json
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QApplication, QWidget

from services.settings import (
    MeetingAgentCore,
    SettingsKey,
    WalkthroughCopyFormat,
    settings_manager,
)
from services.walkthrough import library
from ui_qt.overlays import waveform_overlay
from ui_qt.overlays.waveform_overlay import WaveformOverlay
from ui_qt.ui_controller import UIController
from ui_qt.widgets.walkthrough_tab import COPY_CHOICES, WalkthroughTab


@pytest.fixture
def tab(tmp_path):
    widget = WalkthroughTab(root=tmp_path / "captures", today=lambda: date(2026, 10, 25))
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


# --- the Walkthrough tab -------------------------------------------------


def _draft(root, name):
    folder = root / name
    folder.mkdir(parents=True)
    started = datetime.strptime(name, "%Y-%m-%d_%H-%M-%S").isoformat()
    (folder / library.DRAFT_NAME).write_text(json.dumps({"started_at": started}))
    return folder


def test_the_button_asks_for_a_walkthrough(tab):
    spy = QSignalSpy(tab.walkthrough_requested)

    tab.walkthrough_button.click()

    assert len(spy) == 1
    assert tab.walkthrough_button.text() == "Start Walkthrough"
    assert not tab.cancel_button.isVisibleTo(tab)


def test_during_a_walkthrough_the_tab_finishes_or_cancels_it(tab):
    cancel = QSignalSpy(tab.cancel_requested)
    tab.set_recording_state(True, True)

    assert tab.walkthrough_button.text() == "Finish Walkthrough"
    assert tab.walkthrough_button._active
    assert tab.cancel_button.isVisibleTo(tab)
    tab.cancel_button.click()
    assert len(cancel) == 1

    tab.set_recording_state(False, False)
    assert tab.walkthrough_button.text() == "Start Walkthrough"
    assert tab.walkthrough_button._active
    assert not tab.cancel_button.isVisibleTo(tab)


def test_a_dictation_in_progress_holds_the_button(tab):
    spy = QSignalSpy(tab.walkthrough_requested)
    tab.set_recording_state(True, False)

    QTest.mouseClick(tab.walkthrough_button, Qt.MouseButton.LeftButton)

    assert not tab.walkthrough_button._active
    assert len(spy) == 0


def test_the_tab_shows_both_shortcuts(tab):
    tab.walkthrough_button.set_hotkey = Mock()

    tab.set_hotkeys("Ctrl+Alt+W", "")

    tab.walkthrough_button.set_hotkey.assert_called_once_with("Ctrl+Alt+W")
    assert tab.start_key.text() == "Ctrl+Alt+W"
    assert tab.snap_key.text() == "Not set"
    assert tab.snap_key.property("unset") is True


def test_the_button_leaves_its_explanation_to_the_badge(tab):
    # Its own hover shows the shortcut pill, as the Quick Record buttons do.
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


def test_the_badge_says_new_until_the_first_walkthrough(tab, tmp_path):
    assert tab.walkthrough_badge.text() == "New"

    tab.set_recording_state(True, True)

    assert tab.walkthrough_badge.text() == "?"
    assert settings_manager.load_all_settings()[SettingsKey.WALKTHROUGH_INTRO_SEEN] is True
    again = WalkthroughTab(root=tmp_path / "captures")
    try:
        assert again.walkthrough_badge.text() == "?"
    finally:
        again.close()


def test_picking_an_agent_turns_the_write_up_on_with_it(tab):
    assert not tab.write_up_switch.isChecked()
    assert not any(choice.isChecked() for choice in tab.agent_choices.values())
    assert not tab.notes_switch.isEnabled()

    tab.agent_choices[MeetingAgentCore.CODEX].click()

    settings = settings_manager.load_all_settings()
    assert settings[SettingsKey.WALKTHROUGH_WRITE_UP] is True
    assert settings[SettingsKey.WALKTHROUGH_WRITE_UP_AGENT] == MeetingAgentCore.CODEX
    assert [a for a, c in tab.agent_choices.items() if c.isChecked()] == [MeetingAgentCore.CODEX]
    assert tab.notes_switch.isEnabled()
    assert "Codex" in tab.write_up_text.text()


def test_switching_the_write_up_off_keeps_the_agent_for_next_time(tab):
    tab.agent_choices[MeetingAgentCore.CODEX].click()

    tab.write_up_switch.click()

    settings = settings_manager.load_all_settings()
    assert settings[SettingsKey.WALKTHROUGH_WRITE_UP] is False
    assert settings[SettingsKey.WALKTHROUGH_WRITE_UP_AGENT] == MeetingAgentCore.CODEX
    assert not any(choice.isChecked() for choice in tab.agent_choices.values())
    assert not tab.notes_switch.isEnabled()


def test_the_tab_opens_on_the_saved_choices(tmp_path):
    settings_manager.update_settings({
        SettingsKey.WALKTHROUGH_WRITE_UP: True,
        SettingsKey.WALKTHROUGH_WRITE_UP_AGENT: MeetingAgentCore.OPENCODE_CLI,
        SettingsKey.WALKTHROUGH_WRITE_UP_NOTES: True,
        SettingsKey.WALKTHROUGH_COPY_FORMAT: WalkthroughCopyFormat.PDF,
    })
    tab = WalkthroughTab(root=tmp_path)
    try:
        assert tab.agent_choices[MeetingAgentCore.OPENCODE_CLI].isChecked()
        assert tab.notes_switch.isChecked()
        assert COPY_CHOICES[tab.copy_bar.currentIndex()][0] == WalkthroughCopyFormat.PDF
    finally:
        tab.close()


def test_the_copy_choice_is_saved(tab):
    index = next(i for i, (value, _label) in enumerate(COPY_CHOICES)
                 if value == WalkthroughCopyFormat.AGENT)

    tab.copy_bar.buttons[index].click()

    assert (settings_manager.load_all_settings()[SettingsKey.WALKTHROUGH_COPY_FORMAT]
            == WalkthroughCopyFormat.AGENT)


def test_each_agent_says_whether_the_last_scan_found_it(tab):
    tab.agent_choices[MeetingAgentCore.CLAUDE_CODE].show_found(None)
    assert tab.agent_choices[MeetingAgentCore.CLAUDE_CODE].status_label.text() == "Not installed"


def test_recent_walkthroughs_open_in_the_library(tmp_path):
    root = tmp_path / "captures"
    older = _draft(root, "2026-10-24_09-00-00")
    newer = _draft(root, "2026-10-25_10-30-00")
    tab = WalkthroughTab(root=root, today=lambda: date(2026, 10, 25))
    try:
        assert tab.recent_list.count() == 2
        assert not tab.empty_label.isVisibleTo(tab)
        first = tab.recent_list.item(0)
        assert first.data(Qt.ItemDataRole.UserRole) == str(newer)
        assert "Today" in first.data(Qt.ItemDataRole.AccessibleTextRole)

        spy = QSignalSpy(tab.library_requested)
        tab.recent_list.itemClicked.emit(tab.recent_list.item(1))
        tab.open_all.click()
        assert [args[0] for args in spy] == [older, None]
    finally:
        tab.close()


def test_without_walkthroughs_the_tab_says_so(tab):
    assert tab.recent_list.count() == 0
    assert tab.empty_label.isVisibleTo(tab)
    assert not tab.open_all.isVisibleTo(tab)


def test_finishing_a_walkthrough_lists_it(tab):
    _draft(tab.root, "2026-10-25_10-30-00")
    tab.set_recording_state(True, True)
    assert tab.recent_list.count() == 0

    tab.set_recording_state(False, False)

    assert tab.recent_list.count() == 1


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


def test_hovering_a_control_shows_its_shortcut(overlay, monkeypatch):
    # This checks the positioned pill; compositor-managed desktops use a
    # native Qt tooltip instead, even under an isolated X11 test server.
    monkeypatch.setattr("ui_qt.widgets.buttons.compositor_managed", lambda: False)
    overlay.set_control_hotkeys("Ctrl+Alt+S", "")
    _recording_walkthrough(overlay)

    def hover(key):
        # Test the hover handler without depending on a global pointer warp,
        # which offscreen Qt and bare X servers need not deliver.
        event = QMouseEvent(QEvent.Type.MouseMove, QPointF(_center(overlay, key)),
                            Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(overlay, event)

    hover("snap")
    hint = overlay._hotkey_hint
    assert hint is not None and hint.isVisible()
    assert hint._text_label.text() == "Ctrl+Alt+S"
    assert hint.geometry().bottom() < overlay.mapToGlobal(QPoint(0, 0)).y() + overlay._control_rects["snap"].top()

    # Done's Walkthrough shortcut is unset: no pill.
    hover("finish")
    assert not hint.isVisible()

    hover("snap")
    overlay.hide()
    assert not hint.isVisible()


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
        main_window=Mock(),
        overlay=Mock(),
        _show_walkthrough_panel=Mock(),
    )

    UIController.set_walkthrough_active(ui, True)

    ui.tray_manager.set_walkthrough_active.assert_called_once_with(True)
    ui.main_window.set_walkthrough_active.assert_called_once_with(True)
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

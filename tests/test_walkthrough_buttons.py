"""The walkthrough's buttons: its tab in the main window, Snap and Done on the overlay."""

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
from services.walkthrough import write_up
from services.walkthrough.styles import load_styles
from ui_qt.overlays import waveform_overlay
from ui_qt.overlays.waveform_overlay import WaveformOverlay
from ui_qt.ui_controller import UIController
from ui_qt.widgets.walkthrough_tab import COPY_CHOICES, OWN_PROMPT, WalkthroughTab

#: The styles a new install picks from: the default, then the starters.
STYLES = load_styles({})


@pytest.fixture
def tab():
    widget = WalkthroughTab()
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


def test_the_badge_leads_the_shortcut_row_not_the_button(tab):
    # Off the button, so Start Walkthrough matches the other tabs' Start buttons.
    tab.show()
    QApplication.processEvents()

    badge = tab.walkthrough_badge
    assert not tab.walkthrough_button.isAncestorOf(badge)
    assert badge.geometry().right() < tab.start_key.geometry().left()
    assert abs(badge.geometry().center().y() - tab.start_key.geometry().center().y()) <= 1
    assert not badge.is_compact()


def test_start_walkthrough_matches_the_other_start_buttons(tab):
    from ui_qt.widgets.buttons import START_BUTTON_WIDTH, SuccessButton

    tab.resize(930, 600)
    tab.show()
    QApplication.processEvents()

    assert isinstance(tab.walkthrough_button, SuccessButton)
    assert tab.walkthrough_button.width() == START_BUTTON_WIDTH
    # Centred in its column, beside the summary.
    column = tab.walkthrough_button.parentWidget().parentWidget()
    box = tab.walkthrough_button.parentWidget().geometry()
    assert abs(box.left() - (column.width() - box.right() - 1)) <= 1

    # In a narrower window it gives up width rather than pushing the summary down.
    tab.resize(640, 600)
    QApplication.processEvents()
    assert not tab.split.stacked
    assert tab.walkthrough_button.width() <= START_BUTTON_WIDTH


def _button_with_badge(width):
    from PyQt6.QtWidgets import QPushButton
    from ui_qt.widgets.feature_badge import FeatureBadge

    button = QPushButton("Start Walkthrough")
    badge = FeatureBadge(button, "What it does.")
    button.show()
    button.resize(width, 48)
    return button, badge


def test_on_a_button_the_badge_sits_on_its_right_side_clear_of_its_label():
    button, badge = _button_with_badge(360)
    try:
        box = badge.geometry()
        assert 360 - 20 < box.right() < 360
        assert abs(box.center().y() - 24) <= 1
        label = button.fontMetrics().horizontalAdvance("Start Walkthrough")
        assert box.left() > (360 + label) / 2
        assert not badge.is_compact()
    finally:
        button.close()


def test_in_a_narrow_button_new_folds_to_its_sparkle():
    button, badge = _button_with_badge(220)
    try:
        assert badge.is_compact()
        assert badge.width() == badge.height()
        label = button.fontMetrics().horizontalAdvance("Start Walkthrough")
        assert badge.geometry().left() > (220 + label) / 2
    finally:
        button.close()


def test_clicking_the_badge_never_starts_a_walkthrough(tab):
    tab.show()
    spy = QSignalSpy(tab.walkthrough_requested)

    QTest.mouseClick(tab.walkthrough_badge, Qt.MouseButton.LeftButton)

    assert len(spy) == 0


def test_the_badge_says_new_until_the_first_walkthrough(tab):
    assert tab.walkthrough_badge.text() == "New"

    tab.set_recording_state(True, True)

    assert tab.walkthrough_badge.text() == "?"
    assert settings_manager.load_all_settings()[SettingsKey.WALKTHROUGH_INTRO_SEEN] is True
    again = WalkthroughTab()
    try:
        assert again.walkthrough_badge.text() == "?"
    finally:
        again.close()


def test_picking_an_agent_turns_the_write_up_on_with_it(tab):
    assert not tab.write_up_switch.isChecked()
    assert not any(choice.isChecked() for choice in tab.agent_choices.values())
    assert not tab.prompt_area.isVisibleTo(tab)

    tab.agent_choices[MeetingAgentCore.CODEX].click()

    settings = settings_manager.load_all_settings()
    assert settings[SettingsKey.WALKTHROUGH_WRITE_UP] is True
    assert settings[SettingsKey.WALKTHROUGH_WRITE_UP_AGENT] == MeetingAgentCore.CODEX
    assert [a for a, c in tab.agent_choices.items() if c.isChecked()] == [MeetingAgentCore.CODEX]
    assert tab.prompt_area.isVisibleTo(tab)
    assert "Codex" in tab.write_up_text.text()


def test_switching_the_write_up_off_keeps_the_agent_for_next_time(tab):
    tab.agent_choices[MeetingAgentCore.CODEX].click()

    tab.write_up_switch.click()

    settings = settings_manager.load_all_settings()
    assert settings[SettingsKey.WALKTHROUGH_WRITE_UP] is False
    assert settings[SettingsKey.WALKTHROUGH_WRITE_UP_AGENT] == MeetingAgentCore.CODEX
    assert not any(choice.isChecked() for choice in tab.agent_choices.values())
    assert not tab.prompt_area.isVisibleTo(tab)


def test_the_tab_opens_on_the_saved_choices():
    settings_manager.update_settings({
        SettingsKey.WALKTHROUGH_WRITE_UP: True,
        SettingsKey.WALKTHROUGH_WRITE_UP_AGENT: MeetingAgentCore.OPENCODE_CLI,
        SettingsKey.WALKTHROUGH_WRITE_UP_NOTES: True,
        SettingsKey.WALKTHROUGH_COPY_FORMAT: WalkthroughCopyFormat.BOTH,
        SettingsKey.WALKTHROUGH_WRITE_UP_INSTRUCTIONS: STYLES[2].instructions,
        SettingsKey.WALKTHROUGH_MINIMIZE_TO_TRAY: True,
    })
    tab = WalkthroughTab()
    try:
        assert tab.minimize_check.isChecked()
        assert tab.agent_choices[MeetingAgentCore.OPENCODE_CLI].isChecked()
        assert tab.notes_switch.isChecked()
        assert COPY_CHOICES[tab.copy_bar.currentIndex()][0] == WalkthroughCopyFormat.BOTH
        assert tab.prompt_edit.toPlainText() == STYLES[2].instructions
        assert tab.preset_combo.currentText() == STYLES[2].name
    finally:
        tab.close()


def test_the_copy_choice_is_saved(tab):
    index = next(i for i, (value, _label) in enumerate(COPY_CHOICES)
                 if value == WalkthroughCopyFormat.AGENT)

    tab.copy_bar.buttons[index].click()

    assert (settings_manager.load_all_settings()[SettingsKey.WALKTHROUGH_COPY_FORMAT]
            == WalkthroughCopyFormat.AGENT)


def test_minimizing_to_tray_on_start_is_off_until_ticked_and_then_saved(tab):
    assert not tab.minimize_check.isChecked()

    tab.minimize_check.click()

    assert settings_manager.load_all_settings()[SettingsKey.WALKTHROUGH_MINIMIZE_TO_TRAY] is True


def test_the_summary_beside_start_says_what_happens_when_it_finishes(tab):
    assert tab.copy_summary.text() == "Copies <b>PDF</b>"
    assert tab.write_up_summary.text() == "No AI write-up"
    assert tab.write_up_summary.property("muted") is True
    assert not tab.minimize_summary.isVisibleTo(tab)

    index = next(i for i, (value, _label) in enumerate(COPY_CHOICES)
                 if value == WalkthroughCopyFormat.WORD)
    tab.copy_bar.buttons[index].click()
    tab.agent_choices[MeetingAgentCore.CODEX].click()
    tab.minimize_check.click()

    assert tab.copy_summary.text() == "Copies <b>Word</b>"
    assert tab.write_up_summary.text() == "AI write-up by <b>Codex</b>"
    assert tab.write_up_summary.property("muted") is False
    assert tab.minimize_summary.isVisibleTo(tab)


def test_the_summary_sits_beside_start_and_moves_under_it_when_narrow(tab):
    tab.set_hotkeys("Ctrl+Alt+W", "Ctrl+Alt+S")
    tab.show()
    tab.resize(930, 700)
    QApplication.processEvents()
    split = tab.split
    assert not split.stacked
    assert not tab.keys.stacked
    assert split._summary.geometry().left() > tab.walkthrough_button.mapTo(
        split, tab.walkthrough_button.rect().topRight()).x()

    # Turning the write-up on lengthens the summary, which wraps instead of
    # pushing itself under Start.
    tab.agent_choices[MeetingAgentCore.CLAUDE_CODE].click()
    QApplication.processEvents()
    assert not split.stacked

    # Long shortcuts take a line each before the summary gives up its column.
    tab.set_hotkeys("Ctrl+Alt+Shift+W", "Ctrl+Alt+Shift+S")
    tab.resize(560, 700)
    QApplication.processEvents()
    assert tab.keys.stacked
    assert not split.stacked


def _column(width, height):
    from PyQt6.QtWidgets import QVBoxLayout

    column = QWidget()
    inside = QWidget()
    inside.setFixedSize(width, height)
    QVBoxLayout(column).addWidget(inside)
    return column


def test_too_narrow_for_both_columns_the_summary_goes_under_start():
    # The tab's own choices hold it wider than this; a large font can't.
    from PyQt6.QtWidgets import QVBoxLayout
    from ui_qt.widgets.walkthrough_tab import StartSplit

    start, summary = _column(200, 60), _column(180, 90)
    split = StartSplit(start, summary)
    host = QWidget()
    QVBoxLayout(host).addWidget(split)
    try:
        host.resize(600, 300)
        host.show()
        QApplication.processEvents()
        assert not split.stacked

        host.resize(380, 300)
        QApplication.processEvents()
        assert split.stacked
        # Stacked, each keeps its own height: nothing in the summary is cut off.
        assert summary.geometry().top() >= start.geometry().bottom()
        assert summary.height() >= summary.minimumSizeHint().height()

        host.resize(600, 300)
        QApplication.processEvents()
        assert not split.stacked
    finally:
        host.close()


def test_each_agent_says_whether_the_last_scan_found_it(tab):
    tab.agent_choices[MeetingAgentCore.CLAUDE_CODE].show_found(None)
    assert tab.agent_choices[MeetingAgentCore.CLAUDE_CODE].status_label.text() == "Not installed"


def _saved_prompt():
    return settings_manager.load_all_settings()[SettingsKey.WALKTHROUGH_WRITE_UP_INSTRUCTIONS]


def test_the_prompt_shows_the_default_words_and_saves_them_as_default(tab):
    assert tab.prompt_edit.toPlainText() == write_up.DEFAULT_INSTRUCTIONS
    assert tab.preset_combo.currentText() == "Walkthrough"

    tab._save_prompt()

    assert _saved_prompt() == ""


def test_a_style_fills_the_prompt_and_is_saved_at_once(tab):
    name, words = STYLES[3].name, STYLES[3].instructions

    tab.preset_combo.setCurrentIndex(3)
    tab.preset_combo.activated.emit(3)

    assert tab.prompt_edit.toPlainText() == words
    assert _saved_prompt() == words
    assert not tab._prompt_timer.isActive()
    assert tab.preset_combo.currentText() == name


def test_typing_your_own_prompt_saves_it_and_a_style_can_be_tried_and_undone(tab):
    tab.prompt_edit.setPlainText("Make it a checklist.")

    assert tab.preset_combo.currentText() == OWN_PROMPT
    assert tab._prompt_timer.isActive()  # saved once typing pauses
    tab._prompt_timer.timeout.emit()
    assert _saved_prompt() == "Make it a checklist."

    tab.preset_combo.activated.emit(1)
    assert _saved_prompt() == STYLES[1].instructions
    own = tab.preset_combo.findText(OWN_PROMPT)
    tab.preset_combo.activated.emit(own)
    assert tab.prompt_edit.toPlainText() == "Make it a checklist."
    assert _saved_prompt() == "Make it a checklist."


def test_a_prompt_changed_in_settings_shows_when_the_window_is_back(tab):
    settings_manager.save_setting(SettingsKey.WALKTHROUGH_WRITE_UP_INSTRUCTIONS, "From Settings.")

    tab.load_settings()

    assert tab.prompt_edit.toPlainText() == "From Settings."
    assert tab.preset_combo.currentText() == OWN_PROMPT


def test_typing_not_yet_saved_wins_over_reloading(tab):
    settings_manager.save_setting(SettingsKey.WALKTHROUGH_WRITE_UP_INSTRUCTIONS, "Older.")
    tab.prompt_edit.setPlainText("Newer, still being typed.")

    tab.load_settings()

    assert tab.prompt_edit.toPlainText() == "Newer, still being typed."
    assert _saved_prompt() == "Newer, still being typed."


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


@pytest.mark.parametrize(
    "ticked, visible, active, minimized",
    [
        (True, True, True, True),
        (False, True, True, False),
        # Already in the tray: nothing to minimize.
        (True, False, True, False),
        # Finishing leaves the window where the user put it.
        (True, True, False, False),
    ],
)
def test_a_walkthrough_starting_minimizes_the_window_to_the_tray_when_ticked(
    ticked, visible, active, minimized
):
    settings_manager.update_settings({SettingsKey.WALKTHROUGH_MINIMIZE_TO_TRAY: ticked})
    ui = SimpleNamespace(
        tray_manager=Mock(),
        main_window=Mock(**{"isVisible.return_value": visible}),
        overlay=Mock(),
        _show_walkthrough_panel=Mock(),
    )

    UIController.set_walkthrough_active(ui, active)

    assert ui.main_window.minimize_to_tray.called is minimized


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


# --- the column stands in for the overlay ------------------------------------


def test_while_the_column_shows_the_walkthrough_the_overlay_keeps_to_notices(overlay):
    showing = [True]
    overlay.set_stand_in(lambda: showing[0])

    overlay.show_at_cursor(overlay.STATE_RECORDING)
    assert not overlay.isVisible()
    overlay.show_notice("Couldn't save the screenshot")
    assert overlay.isVisible() and overlay.current_state == overlay.STATE_NOTICE
    # The notice keeps its time; the processing it would turn into stays away.
    overlay.set_state(overlay.STATE_PROCESSING)
    assert overlay.current_state == overlay.STATE_NOTICE

    overlay.hide()
    showing[0] = False
    overlay.show_at_cursor(overlay.STATE_RECORDING)
    assert overlay.isVisible() and overlay.current_state == overlay.STATE_RECORDING


def test_a_column_that_opens_over_the_recording_overlay_hides_it(overlay):
    _recording_walkthrough(overlay)
    assert overlay.isVisible()

    overlay.set_stand_in(lambda: True)

    assert not overlay.isVisible()


def test_the_controller_lets_the_column_stand_in_for_the_overlay(monkeypatch):
    import ui_qt.snap
    from ui_qt.widgets import walkthrough_panel

    panel = Mock()
    monkeypatch.setattr(walkthrough_panel, "make_panel", lambda: panel)
    monkeypatch.setattr(ui_qt.snap, "keep_out_of_screenshots", Mock())
    ui = SimpleNamespace(_walkthrough_panel=None, overlay=Mock())

    UIController._show_walkthrough_panel(ui, True)

    panel.begin.assert_called_once_with()
    ui.overlay.set_stand_in.assert_called_once_with(panel.shows_recording)


def test_without_a_column_wayland_keeps_the_overlay(monkeypatch):
    from ui_qt.widgets import walkthrough_panel

    monkeypatch.setattr(walkthrough_panel, "make_panel", lambda: None)
    ui = SimpleNamespace(_walkthrough_panel=None, overlay=Mock())

    UIController._show_walkthrough_panel(ui, True)

    ui.overlay.set_stand_in.assert_not_called()

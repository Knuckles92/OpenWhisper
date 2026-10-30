"""The column that writes a walkthrough as it records (ui_qt/widgets/walkthrough_panel.py)."""

from types import SimpleNamespace

import pytest
from PyQt6.QtGui import QColor, QImage

from ui_qt.widgets import walkthrough_panel
from ui_qt.widgets.walkthrough_panel import (
    HOLD_S,
    TICK_S,
    Paragraph,
    ShotItem,
    WalkthroughColumn,
)

LOUD = 0.05  # about -26 dBFS: speech on a desk microphone
ROOM = 0.0005  # -66 dBFS: under the speech gate


def _image(width=1600, height=900):
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor("#336699"))
    return image


def _speak(column, level, seconds, now=0.0):
    for _ in range(round(seconds / TICK_S)):
        column.hear(level)
        column.advance(TICK_S, now)


# --- the column's contents --------------------------------------------------


def test_speech_writes_ticks_and_the_room_writes_none():
    column = WalkthroughColumn()

    _speak(column, ROOM, 1.5)
    assert column.items[-1].ticks == []

    _speak(column, LOUD, 1.5)
    ticks = column.items[-1].ticks
    assert len(ticks) == pytest.approx(1.5 / TICK_S, abs=1)
    assert all(0.0 < height <= 1.0 for height in ticks)
    assert column.speech_s == pytest.approx(1.5, abs=TICK_S)


def test_louder_speech_writes_taller_ticks():
    column = WalkthroughColumn()
    _speak(column, 0.01, 0.3)
    _speak(column, 0.1, 0.3)
    quiet, loud = column.items[-1].ticks[0], column.items[-1].ticks[-1]
    assert loud > quiet


def test_a_screenshot_closes_the_paragraph_and_opens_the_next():
    column = WalkthroughColumn()
    _speak(column, LOUD, 0.5)
    column.add_shot(1, _image(), now=1.0)

    first, shot, after = column.items
    assert isinstance(first, Paragraph) and first.ticks
    assert isinstance(shot, ShotItem) and shot.number == 1
    assert isinstance(after, Paragraph) and after.ticks == []
    assert column.shots == 1


def test_screenshots_with_nothing_said_between_them_leave_no_empty_paragraph():
    column = WalkthroughColumn()
    column.add_shot(1, _image(), now=0.0)
    column.add_shot(2, _image(), now=0.1)

    kinds = [type(item).__name__ for item in column.items]
    assert kinds == ["ShotItem", "ShotItem", "Paragraph"]


# --- the panel through a walkthrough ----------------------------------------


@pytest.fixture
def clock(monkeypatch):
    now = SimpleNamespace(t=100.0)
    monkeypatch.setattr(walkthrough_panel.time, "monotonic", lambda: now.t)
    return now


@pytest.fixture
def panel(clock, monkeypatch):
    monkeypatch.setattr(walkthrough_panel, "compositor_managed", lambda: False)
    widget = walkthrough_panel.make_panel()
    yield widget
    widget._frame.stop()
    widget.hide()
    widget.close()


def _run(panel, clock, seconds, level=None):
    end = clock.t + seconds
    while clock.t < end:
        clock.t += 0.016
        if level is not None:
            panel.hear(level)
        panel._advance()


def test_a_walkthrough_fills_the_column_then_it_fades_once_ready(panel, clock):
    panel.begin()
    assert panel.isVisible() and panel.state == panel.RECORDING

    _run(panel, clock, 1.0, level=LOUD)
    panel.add_shot(1, _image())
    _run(panel, clock, 0.6, level=LOUD)
    panel.grab()  # paints every part without error
    assert panel.column.shots == 1

    panel.finish()
    assert panel.state == panel.WRITING
    panel.hear(LOUD)  # after Done the voice writes no more
    ticks = sum(len(item.ticks) for item in panel.column.items if isinstance(item, Paragraph))
    _run(panel, clock, 1.0)
    assert sum(len(item.ticks) for item in panel.column.items
               if isinstance(item, Paragraph)) == ticks
    panel.grab()

    panel.complete(SimpleNamespace(shots=1, duration_s=75.0))
    assert panel._summary == "1:15 · 1 screenshot"
    _run(panel, clock, HOLD_S - 0.5)
    assert panel.isVisible()
    _run(panel, clock, 1.5)
    assert not panel.isVisible() and panel.state == panel.HIDDEN


def test_a_canceled_walkthrough_fades_at_once(panel, clock):
    panel.begin()
    _run(panel, clock, 0.5, level=LOUD)
    panel.finish()
    panel.dismiss()
    _run(panel, clock, 0.6)
    assert not panel.isVisible()


def test_screenshots_are_kept_as_thumbnails(panel):
    panel.begin()
    panel.add_shot(1, _image(3840, 2160))
    (shot,) = [item for item in panel.column.items if isinstance(item, ShotItem)]
    assert shot.image.width() == walkthrough_panel.THUMB_PX


def test_a_new_walkthrough_starts_an_empty_column(panel, clock):
    panel.begin()
    _run(panel, clock, 0.5, level=LOUD)
    panel.add_shot(1, _image())
    panel.finish()

    panel.begin()
    assert panel.column.shots == 0 and panel.column.items[-1].ticks == []


def test_the_column_never_takes_focus_but_takes_the_pointer_to_be_dragged(panel):
    from PyQt6.QtCore import Qt

    flags = panel.windowFlags()
    assert flags & Qt.WindowType.WindowDoesNotAcceptFocus
    assert not flags & Qt.WindowType.WindowTransparentForInput


def test_no_column_where_the_compositor_places_windows(monkeypatch):
    monkeypatch.setattr(walkthrough_panel, "compositor_managed", lambda: True)
    assert walkthrough_panel.make_panel() is None


def test_a_tall_column_scrolls_to_keep_the_newest_in_view(panel, clock):
    panel.begin()
    for number in range(1, 7):
        _run(panel, clock, 0.4, level=LOUD)
        panel.add_shot(number, _image(900, 1600))
    for _ in range(60):
        panel.grab()
        _run(panel, clock, 0.03)
    assert panel._scroll > 0.0


# --- moving and removing screenshots ------------------------------------------


def _board():
    """Words (4 ticks), shot 1, words (8), shot 2, then words being spoken (2)."""
    column = WalkthroughColumn()
    _speak(column, LOUD, 4 * TICK_S)
    column.add_shot(1, _image(), now=0.0, kept_at_s=2.0)
    _speak(column, LOUD, 8 * TICK_S)
    column.add_shot(2, _image(), now=0.0, kept_at_s=4.0)
    _speak(column, LOUD, 2 * TICK_S)
    return column


def _shape(column):
    return [f"shot {item.number}" if isinstance(item, ShotItem) else len(item.ticks)
            for item in column.items]


def test_the_board_starts_in_the_order_kept():
    column = _board()
    assert _shape(column) == [4, "shot 1", 8, "shot 2", 2]
    assert (column.place(1), column.place(2), column.place(9)) == (1, 2, 0)


def test_a_moved_screenshot_takes_its_words_and_the_words_being_spoken_stay_last():
    column = _board()

    assert column.move_shot(2, -1)
    assert _shape(column) == [8, "shot 2", 4, "shot 1", 2]
    assert column.place(2) == 1
    assert not column.move_shot(1, 1)
    assert not column.move_shot(2, -1)


def test_a_removed_screenshots_words_run_into_the_next_section():
    column = _board()

    assert column.remove_shot(1)
    assert _shape(column) == [12, "shot 2", 2]
    assert column.remove_shot(2)
    # Into the section being spoken, which the voice keeps writing.
    assert _shape(column) == [14]
    _speak(column, LOUD, TICK_S)
    assert _shape(column) == [15]
    assert column.shots == 0


# --- the column's buttons ----------------------------------------------------


def _signals(panel):
    heard = []
    panel.snap_requested.connect(lambda: heard.append("snap"))
    panel.undo_requested.connect(lambda: heard.append("undo"))
    panel.cancel_requested.connect(lambda: heard.append("cancel"))
    panel.finish_requested.connect(lambda: heard.append("done"))
    panel.move_requested.connect(lambda number, step: heard.append(("move", number, step)))
    panel.remove_requested.connect(lambda number: heard.append(("remove", number)))
    return heard


def _click(panel, point):
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest

    QTest.mouseClick(panel, Qt.MouseButton.LeftButton, pos=point.toPoint())


def _footer_button(panel, key):
    return panel._footer()[key][0].center()


def test_the_footer_snaps_undoes_cancels_and_finishes(panel, clock):
    heard = _signals(panel)
    panel.begin()
    panel.grab()

    _click(panel, _footer_button(panel, "undo"))  # no screenshot to undo yet
    panel.add_shot(1, _image(), 3.0)
    for key in ("snap", "undo", "cancel", "done"):
        _click(panel, _footer_button(panel, key))

    assert heard == ["snap", "undo", "cancel", "done"]


def test_while_it_is_written_only_cancel_is_left(panel, clock):
    heard = _signals(panel)
    panel.begin()
    panel.finish()
    _run(panel, clock, 1.0)

    _click(panel, _footer_button(panel, "snap"))
    # Cancel has grown over the row Undo and Done were in.
    assert panel._footer()["cancel"][0].width() == pytest.approx(panel._inner().width())
    _click(panel, _footer_button(panel, "done"))

    assert heard == ["cancel"]


def test_hovering_a_screenshot_offers_to_move_or_remove_it(panel, clock):
    heard = _signals(panel)
    panel.begin()
    panel.add_shot(1, _image(), 2.0)
    panel.add_shot(2, _image(), 5.0)
    _run(panel, clock, 1.0)
    panel.grab()

    panel._set_hover(panel._shot_rects[2].center())
    tools = {key.split(":")[0]: rect for key, (rect, enabled) in panel._shot_tools(2).items()
             if enabled}
    assert set(tools) == {"up", "remove"}  # the last screenshot can't go lower
    _click(panel, tools["up"].center())
    _click(panel, tools["remove"].center())

    assert heard == [("move", 2, -1), ("remove", 2)]


def test_the_board_follows_what_the_walkthrough_did(panel, clock):
    panel.begin()
    panel.add_shot(1, _image(), 2.0)
    panel.add_shot(2, _image(), 5.0)

    panel.move_shot(2, -1)
    assert [item.number for item in panel.column.items if isinstance(item, ShotItem)] == [2, 1]
    panel.remove_shot(1)
    assert panel.column.shots == 1
    panel.grab()


def test_dragging_anywhere_but_a_button_moves_the_column(panel, clock):
    from PyQt6.QtCore import QPointF

    panel.begin()
    panel.grab()
    header = QPointF(panel._inner().left() + 10, panel._inner().top() + 10)

    assert panel._hit(header) == ""
    assert panel._hit(_footer_button(panel, "done")) == "done"


def test_every_state_paints(panel, clock):
    panel.begin()
    for number in (1, 2, 3):
        _run(panel, clock, 0.4, level=LOUD)
        panel.add_shot(number, _image(), number * 2.0)
    _run(panel, clock, 1.0)
    panel.grab()
    panel._set_hover(next(iter(panel._shot_rects.values())).center())
    assert panel._hover_shot
    panel.grab()
    panel.finish()
    _run(panel, clock, 0.2)
    panel.grab()
    panel.complete(SimpleNamespace(shots=3, duration_s=9.0))
    panel.grab()


# --- the Snap menu -------------------------------------------------------------


def test_the_chevron_opens_the_menu_beside_the_column(panel, clock):
    from services.settings import WalkthroughSnapMode

    chosen = []
    panel.snap_mode_chosen.connect(chosen.append)
    panel.begin()
    panel.set_snap_mode(WalkthroughSnapMode.SCREEN, "Ctrl+Alt+S")
    panel.grab()

    _click(panel, _footer_button(panel, "snap-menu"))
    menu = panel._menu
    assert menu is not None and menu.isVisible()
    assert menu.current == "screen" and menu.shortcut == "Ctrl+Alt+S"
    # Beside the column, not over it.
    assert menu.geometry().right() < panel.mapToGlobal(panel._card().topLeft().toPoint()).x() + 20
    menu.grab()

    index = menu.modes.index(WalkthroughSnapMode.DELAYED)
    _click(menu, menu._item(index).center())
    assert chosen == ["delayed"]


def test_the_menu_offers_a_window_only_where_one_can_be_found(panel, monkeypatch):
    from ui_qt.snap import pointed

    monkeypatch.setattr(pointed, "supported", lambda: False)
    assert "window" not in panel.snap_modes()
    monkeypatch.setattr(pointed, "supported", lambda: True)
    assert panel.snap_modes() == ("region", "window", "screen", "delayed")


def test_the_countdown_shows_on_snap(panel, clock):
    panel.begin()
    panel.set_countdown(2)
    panel.grab()
    assert panel._countdown == 2
    panel.finish()
    assert panel._countdown == 0


def test_the_menu_never_takes_the_focus(panel, clock):
    from PyQt6.QtCore import Qt

    panel.begin()
    panel.grab()
    _click(panel, _footer_button(panel, "snap-menu"))
    flags = panel._menu.windowFlags()

    assert flags & Qt.WindowType.WindowDoesNotAcceptFocus
    # A popup takes the focus on Windows, whatever it is told.
    assert panel._menu.windowType() == Qt.WindowType.Tool


def test_the_chevron_again_or_another_click_in_the_column_closes_the_menu(panel, clock):
    from PyQt6.QtCore import QPointF

    panel.begin()
    panel.grab()
    _click(panel, _footer_button(panel, "snap-menu"))
    _click(panel, _footer_button(panel, "snap-menu"))
    _pump_events()
    assert panel._menu is None

    _click(panel, _footer_button(panel, "snap-menu"))
    _click(panel, QPointF(panel._inner().left() + 10, panel._inner().top() + 10))
    _pump_events()
    assert panel._menu is None


def test_the_menu_closes_once_the_pointer_has_wandered_off(panel, clock, monkeypatch):
    from PyQt6.QtCore import QRect

    from ui_qt.widgets import walkthrough_snap_menu

    panel.begin()
    panel.grab()
    _click(panel, _footer_button(panel, "snap-menu"))
    menu = panel._menu
    menu._near = QRect()
    monkeypatch.setattr(walkthrough_snap_menu.QCursor, "pos",
                        staticmethod(lambda: menu.geometry().topLeft() - QRect(0, 0, 50, 50).bottomRight()))
    now = [50.0]
    monkeypatch.setattr(walkthrough_snap_menu.time, "monotonic", lambda: now[0])

    menu._check_pointer()
    now[0] += walkthrough_snap_menu.AWAY_S / 2
    menu._check_pointer()
    assert menu.isVisible()
    now[0] += walkthrough_snap_menu.AWAY_S
    menu._check_pointer()
    _pump_events()
    assert panel._menu is None


def _pump_events():
    from PyQt6.QtCore import QCoreApplication, QEvent
    from PyQt6.QtWidgets import QApplication

    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()


# --- Hold for note -------------------------------------------------------------


def _kinds(column):
    return [("note" if item.note else "words", len(item.ticks)) if isinstance(item, Paragraph)
            else f"shot {item.number}" for item in column.items]


def test_words_said_while_held_are_a_note_of_their_own():
    column = WalkthroughColumn()
    _speak(column, LOUD, 2 * TICK_S)
    column.hold_note(True, 1.0)
    _speak(column, LOUD, 3 * TICK_S)
    column.hold_note(False, 2.0)
    _speak(column, LOUD, TICK_S)

    assert _kinds(column) == [("words", 2), ("note", 3), ("words", 1)]
    assert (column.items[1].started_s, column.items[1].ended_s) == (1.0, 2.0)


def test_a_note_held_in_silence_leaves_nothing():
    column = WalkthroughColumn()
    column.hold_note(True, 1.0)
    column.hold_note(False, 1.5)

    assert _kinds(column) == [("words", 0)]


def test_a_screenshot_while_held_closes_the_section_and_the_note_goes_on():
    column = WalkthroughColumn()
    column.hold_note(True, 1.0)
    _speak(column, LOUD, 2 * TICK_S)
    column.add_shot(1, _image(), now=0.0, kept_at_s=3.0)
    _speak(column, LOUD, TICK_S)

    assert _kinds(column) == [("note", 2), "shot 1", ("note", 1)]
    assert column.items[0].ended_s == 3.0


def test_notes_never_run_into_the_words_beside_them():
    column = WalkthroughColumn()
    _speak(column, LOUD, 2 * TICK_S)
    column.add_shot(1, _image(), now=0.0, kept_at_s=1.0)
    column.hold_note(True, 1.0)
    _speak(column, LOUD, TICK_S)
    column.hold_note(False, 2.0)

    column.remove_shot(1)

    assert _kinds(column) == [("words", 2), ("note", 1), ("words", 0)]


def test_holding_the_button_writes_a_note_until_it_is_let_go(panel, clock):
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest

    held = []
    panel.note_held.connect(held.append)
    panel.begin()
    panel.grab()
    button = _footer_button(panel, "note").toPoint()

    QTest.mousePress(panel, Qt.MouseButton.LeftButton, pos=button)
    _run(panel, clock, 0.5, level=LOUD)
    panel.grab()
    # Let go off the button: the note still ends.
    QTest.mouseRelease(panel, Qt.MouseButton.LeftButton, pos=button + button / 4)
    _run(panel, clock, 0.3, level=LOUD)

    assert held == [True, False]
    note, after = panel.column.items[-2:]
    assert note.note and note.ticks and not after.note and after.ticks
    panel.grab()


def test_without_an_ai_write_up_there_is_no_one_to_read_a_note(panel, clock):
    held = []
    panel.note_held.connect(held.append)
    panel.begin()
    panel.set_notes_on(False)
    panel.grab()

    _click(panel, _footer_button(panel, "note"))

    assert held == []


def test_done_while_held_ends_the_note(panel, clock):
    held = []
    panel.note_held.connect(held.append)
    panel.begin()
    panel._hold(True)

    panel.finish()

    assert held == [True, False] and not panel.column.holding


# --- Pause -------------------------------------------------------------------------


def test_pause_asks_and_the_paused_column_stops_its_clock_and_its_ticks(panel, clock):
    asked = []
    panel.pause_requested.connect(asked.append)
    panel.begin()
    panel.grab()

    _click(panel, _footer_button(panel, "pause"))
    assert asked == [True]
    panel._clock = SimpleNamespace(elapsed=lambda: 10_000, start=lambda: None)
    panel.set_paused(True)
    ticks = len(panel.column.items[-1].ticks)
    panel._clock = SimpleNamespace(elapsed=lambda: 25_000, start=lambda: None)
    _run(panel, clock, 0.5, level=LOUD)

    assert panel._recorded() == pytest.approx(10.0)
    assert len(panel.column.items[-1].ticks) == ticks
    panel.grab()

    _click(panel, _footer_button(panel, "pause"))
    assert asked == [True, False]
    panel.set_paused(False)
    panel._clock = SimpleNamespace(elapsed=lambda: 27_000, start=lambda: None)
    assert panel._recorded() == pytest.approx(12.0)


def test_pausing_ends_a_note_and_no_note_starts_while_paused(panel, clock):
    held = []
    panel.note_held.connect(held.append)
    panel.begin()
    panel._hold(True)

    panel.set_paused(True)
    panel._hold(True)

    assert held == [True, False]
    assert not panel._footer()["note"][1]

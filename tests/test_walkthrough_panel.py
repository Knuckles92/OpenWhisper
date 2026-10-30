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

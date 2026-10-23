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
    WalkthroughPanel,
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


def test_the_column_never_takes_focus_or_clicks(panel):
    from PyQt6.QtCore import Qt

    flags = panel.windowFlags()
    assert flags & Qt.WindowType.WindowTransparentForInput
    assert flags & Qt.WindowType.WindowDoesNotAcceptFocus


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

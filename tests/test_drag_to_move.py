"""Dragging the recording overlay and the walkthrough column aside (ui_qt/utils/drag_move.py)."""

from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication

from ui_qt.overlays import waveform_overlay
from ui_qt.overlays.waveform_overlay import WaveformOverlay
from ui_qt.widgets import walkthrough_panel


def _send(widget, kind, local: QPoint):
    left = Qt.MouseButton.LeftButton
    held = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else left
    event = QMouseEvent(kind, QPointF(local), QPointF(widget.mapToGlobal(local)),
                        left, held, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget, event)


def _drag(widget, start: QPoint, by: QPoint) -> None:
    """Press at ``start``, carry the pointer ``by``, release; positions follow the window."""
    _send(widget, QEvent.Type.MouseButtonPress, start)
    # The grab point stays under the pointer, so the event's local position is
    # where it was pressed, at a global spot ``by`` further on.
    global_end = widget.mapToGlobal(start) + by
    move = QMouseEvent(QEvent.Type.MouseMove, QPointF(start), QPointF(global_end),
                       Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget, move)
    _send(widget, QEvent.Type.MouseButtonRelease, start)


def _room(widget) -> QPoint:
    """A drag vector that stays on screen from the widget's spot: toward the middle."""
    area = widget.screen().availableGeometry()
    return QPoint(-40 if widget.x() > area.center().x() else 40,
                  -30 if widget.y() > area.center().y() else 30)


# --- the recording overlay --------------------------------------------------


@pytest.fixture
def overlay(monkeypatch):
    monkeypatch.setattr(waveform_overlay, "_platform_takes_overlay_clicks", lambda: True)
    widget = WaveformOverlay()
    yield widget
    widget._finish_hide()
    widget.close()


def test_the_overlay_opens_by_the_pointer_and_can_be_dragged_aside(overlay):
    overlay.show_at_cursor(overlay.STATE_RECORDING)
    opened = overlay.pos()
    by = _room(overlay)

    _drag(overlay, QPoint(20, overlay.height() - 6), by)

    assert overlay.pos() == opened + by


def test_a_dragged_overlay_stays_put_through_the_recording(overlay):
    overlay.show_at_cursor(overlay.STATE_STREAMING)
    _drag(overlay, QPoint(20, overlay.height() - 6), _room(overlay))
    placed = overlay.pos()

    # Re-showing while visible (a streaming restart, Copied) and the preview
    # growing keep the user's spot.
    overlay.show_at_cursor(overlay.STATE_STREAMING)
    overlay.update_streaming_text("a few words of preview text")
    overlay.set_state(overlay.STATE_PROCESSING)

    assert overlay.pos() == placed


def test_the_next_recording_opens_by_the_pointer_again(overlay):
    overlay.show_at_cursor(overlay.STATE_RECORDING)
    opened = overlay.pos()
    _drag(overlay, QPoint(20, overlay.height() - 6), _room(overlay))
    overlay._finish_hide()

    overlay.show_at_cursor(overlay.STATE_RECORDING)

    assert overlay.pos() == opened


def test_a_drag_cannot_carry_the_overlay_off_screen(overlay):
    overlay.show_at_cursor(overlay.STATE_RECORDING)
    area = overlay.screen().availableGeometry()

    _drag(overlay, QPoint(20, overlay.height() - 6), QPoint(-10_000, -10_000))

    assert overlay.pos() == area.topLeft() + QPoint(8, 8)


def test_pressing_snap_still_snaps_and_does_not_drag(overlay):
    from PyQt6.QtTest import QSignalSpy

    overlay.set_recording_destination("Walkthrough · 1 shot")
    overlay.set_walkthrough_controls(True)
    overlay.show_at_cursor(overlay.STATE_RECORDING)
    overlay.grab()  # lays out the controls
    snap = overlay._control_rects["snap"].center().toPoint()
    spy = QSignalSpy(overlay.walkthrough_snap_requested)
    opened = overlay.pos()

    _drag(overlay, snap, _room(overlay))

    assert len(spy) == 1
    assert overlay.pos() == opened


def test_the_settings_preview_and_embedded_overlay_do_not_drag():
    from PyQt6.QtWidgets import QWidget

    host = QWidget()
    try:
        assert WaveformOverlay(host)._drag is None
        assert WaveformOverlay(host, preview=True)._drag is None
    finally:
        host.close()


# --- in the corner through a walkthrough -------------------------------------


def _corner(overlay) -> QPoint:
    area = overlay.screen().availableGeometry()
    return area.topLeft() + QPoint(waveform_overlay._PARK_MARGIN, waveform_overlay._PARK_MARGIN)


def test_a_walkthrough_sends_the_overlay_to_the_corner_until_hidden(overlay):
    overlay.show_at_cursor(overlay.STATE_STREAMING)
    overlay.set_walkthrough_controls(True)
    assert overlay.pos() == _corner(overlay)

    overlay.update_streaming_text("a few words of preview text")
    overlay.show_at_cursor(overlay.STATE_STREAMING)
    # Done: the controls go, the finish plays where the walkthrough was.
    overlay.set_walkthrough_controls(False)
    overlay.set_state(overlay.STATE_PROCESSING)
    assert overlay.pos() == _corner(overlay)

    overlay._finish_hide()
    overlay.show_at_cursor(overlay.STATE_RECORDING)
    assert not overlay._cornered


def test_a_walkthrough_overlay_dragged_aside_stays_there(overlay):
    overlay.show_at_cursor(overlay.STATE_STREAMING)
    overlay.set_walkthrough_controls(True)
    _drag(overlay, QPoint(20, overlay.height() - 6), QPoint(60, 50))
    placed = overlay.pos()
    assert placed != _corner(overlay)

    overlay.update_streaming_text("a few words of preview text")
    overlay.show_at_cursor(overlay.STATE_STREAMING)
    overlay.park(overlay.screen().geometry())
    overlay.unpark()

    assert overlay.pos() == placed


# --- parked in the corner while a snap is open -------------------------------


def test_a_snap_parks_the_overlay_in_its_corner_and_puts_it_back(overlay):
    overlay.show_at_cursor(overlay.STATE_STREAMING)
    _drag(overlay, QPoint(20, overlay.height() - 6), _room(overlay))
    placed = overlay.pos()
    area = overlay.screen().geometry()

    overlay.park(area)
    corner = area.topLeft() + QPoint(waveform_overlay._PARK_MARGIN, waveform_overlay._PARK_MARGIN)
    assert overlay.pos() == corner
    # The preview growing, or a re-show, leaves it in the corner.
    overlay.update_streaming_text("a few words of preview text")
    overlay.show_at_cursor(overlay.STATE_STREAMING)
    assert overlay.pos() == corner

    overlay.unpark()
    assert overlay.pos() == placed


def test_an_overlay_that_was_not_dragged_returns_by_the_pointer(overlay):
    overlay.show_at_cursor(overlay.STATE_RECORDING)
    opened = overlay.pos()

    overlay.park(overlay.screen().geometry())
    overlay.unpark()

    assert overlay.pos() == opened
    assert not overlay._placed()


def test_a_hidden_overlay_is_not_parked_and_hiding_forgets_the_park(overlay):
    area = overlay.screen().geometry()
    overlay.park(area)
    assert not overlay.isVisible()
    assert not overlay._placed()

    overlay.show_at_cursor(overlay.STATE_RECORDING)
    overlay.park(area)
    overlay._finish_hide()
    overlay.unpark()

    assert not overlay._placed()


# --- the walkthrough column -------------------------------------------------


@pytest.fixture
def panel(monkeypatch):
    now = SimpleNamespace(t=100.0)
    monkeypatch.setattr(walkthrough_panel.time, "monotonic", lambda: now.t)
    monkeypatch.setattr(walkthrough_panel, "compositor_managed", lambda: False)
    widget = walkthrough_panel.make_panel()
    yield widget
    widget._frame.stop()
    widget.hide()
    widget.close()


def test_the_column_can_be_dragged_and_the_next_walkthrough_opens_at_the_edge(panel):
    panel.begin()
    edge = panel.pos()

    _drag(panel, QPoint(80, 80), QPoint(-60, 0))
    assert panel.pos() == edge + QPoint(-60, 0)

    panel.begin()
    assert panel.pos() == edge

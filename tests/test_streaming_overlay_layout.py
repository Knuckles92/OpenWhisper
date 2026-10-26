"""Where the recording overlay puts its look once live preview words arrive."""
import os
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QRectF
from PyQt6.QtWidgets import QApplication

from services.settings import (
    SettingsKey,
    StreamingOverlayLayout,
    resolve_streaming_overlay_layout,
)
from ui_qt.overlays import recording_looks, waveform_overlay
from ui_qt.overlays.waveform_overlay import WaveformOverlay

HEADER, FOOTER = StreamingOverlayLayout.HEADER, StreamingOverlayLayout.FOOTER
WORDS = "So I was thinking about the launch plan and June works if we freeze the API by May fifteenth."


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def overlay():
    widget = WaveformOverlay()
    widget._refresh_language = lambda: None
    yield widget
    widget.hide()
    widget.close()


def _listen(overlay, layout, look=recording_looks.BARS, *, latched=False):
    overlay._streaming_layout = layout
    overlay.set_recording_look(look)
    overlay.set_state(overlay.STATE_STREAMING)
    overlay.timer.stop()
    if latched:
        overlay.set_hands_free(True)
        overlay.set_language("en", ["en", "es"])
    overlay.update_streaming_text(WORDS)
    with patch.object(waveform_overlay.logger, "error") as error:
        # Twice: the header leaves room for where the badge and chip were last frame.
        overlay.grab()
        overlay.grab()
    error.assert_not_called()


def test_header_is_the_default_and_unknown_values_fall_back_to_it():
    assert resolve_streaming_overlay_layout({}) == HEADER
    assert resolve_streaming_overlay_layout({SettingsKey.STREAMING_OVERLAY_LAYOUT: "sideways"}) == HEADER
    assert resolve_streaming_overlay_layout({SettingsKey.STREAMING_OVERLAY_LAYOUT: FOOTER}) == FOOTER


def test_the_header_keeps_the_look_clear_of_the_badge_chip_and_words(overlay):
    _listen(overlay, HEADER, latched=True)
    room = overlay._header_room
    assert room.width() >= waveform_overlay._HEADER_LOOK_MIN_WIDTH
    assert room.left() > overlay._badge_rect.right()
    assert room.right() < overlay._chip_rect.left()
    above, _ = overlay._preview_text_margins()
    assert room.bottom() < above, "the words start under the header"


def test_the_header_is_shorter_than_the_footer_for_the_same_words(overlay):
    _listen(overlay, HEADER)
    header_height = overlay.height()
    _listen(overlay, FOOTER)
    assert overlay.height() > header_height
    assert overlay.height() - header_height < overlay._base_height


def test_the_footer_puts_the_look_and_its_label_under_the_words(overlay):
    _listen(overlay, FOOTER, latched=True)
    above, below = overlay._preview_text_margins()
    assert above > overlay._badge_rect.bottom(), "the words start under the badge row"
    assert below > overlay._base_height, "the compact band fits under the words"
    assert overlay._status_rect().bottom() > overlay.height() - 10


def test_a_caption_takes_the_headers_look_room(overlay):
    _listen(overlay, HEADER)
    overlay.show_caption("Switched to the USB microphone")
    overlay.grab()
    status = overlay._status_rect().adjusted(12, 0, -12, 0)
    assert status == overlay._header_room.toRect()


@pytest.fixture
def walkthrough(overlay):
    """The overlay with a walkthrough's badge, Snap and Done."""
    with patch.object(waveform_overlay, "_platform_takes_overlay_clicks", return_value=True):
        overlay._takes_clicks_quietly = True
        overlay.set_recording_destination("Walkthrough")
        overlay.set_walkthrough_controls(True)
        yield overlay


# Spies record plain values: a QPainter kept past its paint (as a Mock's
# call_args would) crashes the worker when it is garbage-collected later.

def test_snap_and_done_give_the_headers_clock_and_look_a_line_of_their_own(walkthrough, monkeypatch):
    _listen(walkthrough, HEADER, latched=True)
    snap = walkthrough._control_rects["snap"]
    labels = []
    monkeypatch.setattr(waveform_overlay.live_recording, "draw_live_label",
                        lambda painter, rect, *args, clock, **kwargs: labels.append((QRectF(rect), clock)))
    walkthrough.grab()
    rect, clock = labels[-1]
    assert clock, "the clock keeps its place"
    assert rect.top() > snap.bottom()
    room = walkthrough._header_room
    assert room.top() > snap.bottom()
    assert room.width() >= waveform_overlay._HEADER_LOOK_MIN_WIDTH
    above, _ = walkthrough._preview_text_margins()
    assert room.bottom() < above, "the words start under the header"


def test_the_compact_look_sits_under_snap_and_done(walkthrough, monkeypatch):
    walkthrough.set_recording_look(recording_looks.BARS)
    walkthrough.set_state(walkthrough.STATE_RECORDING)
    walkthrough.timer.stop()
    tops = []
    draw_band = walkthrough._draw_listening_band

    def spy(painter, status, tone, top=0.0):
        tops.append(top)
        draw_band(painter, status, tone, top)

    monkeypatch.setattr(walkthrough, "_draw_listening_band", spy)
    walkthrough.grab()
    top = tops[-1]
    assert top >= walkthrough._control_rects["snap"].bottom()
    assert walkthrough.height() == walkthrough._base_height + top
    assert walkthrough._status_rect().bottom() < walkthrough.height()


def test_the_work_stages_stay_under_the_walkthrough_badge(walkthrough, monkeypatch):
    walkthrough.set_recording_look(recording_looks.BARS)
    walkthrough.set_state(walkthrough.STATE_RECORDING)
    walkthrough.set_state(walkthrough.STATE_TRANSCRIBING)
    walkthrough.timer.stop()
    walkthrough.set_walkthrough_controls(False)
    shifts = []
    monkeypatch.setattr(walkthrough._visual, "draw_work",
                        lambda painter, *_: shifts.append(painter.transform().dy()))
    walkthrough.grab()
    assert shifts == [waveform_overlay._TOP_ROW]
    assert walkthrough.height() == walkthrough._base_height + waveform_overlay._TOP_ROW


def test_the_overlay_drops_back_to_compact_once_the_badge_row_empties(walkthrough):
    walkthrough.set_state(walkthrough.STATE_RECORDING)
    walkthrough.timer.stop()
    assert walkthrough.height() > walkthrough._base_height
    walkthrough.set_walkthrough_controls(False)
    walkthrough.set_state(walkthrough.STATE_COPIED)
    walkthrough.timer.stop()
    assert walkthrough.height() == walkthrough._base_height


def test_changing_the_layout_reflows_the_preview(overlay):
    _listen(overlay, HEADER)
    header_height = overlay.height()
    with patch.object(waveform_overlay, "resolve_streaming_overlay_layout", return_value=FOOTER):
        overlay.refresh_streaming_layout()
    assert overlay._streaming_layout == FOOTER
    assert overlay.height() > header_height


@pytest.mark.parametrize("look", recording_looks.LOOKS)
def test_every_look_draws_small_in_the_header(overlay, look):
    _listen(overlay, HEADER, look)
    if overlay._visual is not None:
        assert overlay._visual.band_width is None, "the small copy leaves the look's width as it was"

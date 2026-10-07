"""The overlay's one-shot moments share one choreography; see ui_qt/overlays/moments.py."""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import patch

import pytest
from PyQt6.QtCore import QPointF
from PyQt6.QtWidgets import QApplication

from ui_qt.overlays import moments, waveform_overlay
from ui_qt.overlays.waveform_overlay import WaveformOverlay
from ui_qt.utils.palette import (
    DARK_PALETTE,
    LIGHT_PALETTE,
    current_palette,
    set_current_palette,
)


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


@pytest.fixture(params=[DARK_PALETTE, LIGHT_PALETTE], ids=["dark", "light"])
def theme(request):
    previous = current_palette()
    set_current_palette(request.param)
    yield
    set_current_palette(previous)


def _paint_through(overlay, until):
    """Tick and paint the current state from 0 to ``until`` seconds; no errors."""
    overlay.timer.stop()
    with patch.object(waveform_overlay.logger, "error") as error:
        while overlay.animation_time < until:
            overlay.last_frame_time = time.monotonic() - 1 / 30
            overlay._update_animation()
            overlay.timer.stop()
            overlay.grab()
    error.assert_not_called()


@pytest.mark.parametrize(
    ("state", "name"),
    [("stt_enable", "enabled"), ("stt_disable", "disabled"), ("copied", "copied")],
)
def test_each_toggle_and_copy_pops_its_moment(overlay, theme, state, name):
    overlay.show_at_cursor(state)
    assert overlay._moment() is moments.MOMENTS[name]
    moment = moments.MOMENTS[name]
    if moment.pop_at:
        overlay.timer.stop()
        overlay.animation_time = moment.pop_at / 2
        overlay._update_animation()
        assert overlay.stt_particles == [], "the confetti waits for the disc"
    _paint_through(overlay, moment.pop_at + 0.05)
    assert overlay.stt_particles
    _paint_through(overlay, overlay._transient_ms() / 1000)


def test_a_cancel_implodes_the_recording_then_slams_and_bursts(overlay, theme):
    overlay.show_at_cursor(overlay.STATE_RECORDING)
    overlay.update_audio_levels([0.5] * 20)
    _paint_through(overlay, 0.5)
    overlay.set_state(overlay.STATE_CANCELING)
    assert overlay._collapse_from, "what was on screen is kept to implode"
    _paint_through(overlay, moments.CANCELED.pop_at / 2)
    assert overlay.stt_particles == []
    _paint_through(overlay, moments.CANCELED.pop_at + 0.05)
    assert len(overlay.stt_particles) > 40
    _paint_through(overlay, overlay._cancel_seconds() + 0.1)
    assert overlay.current_state == overlay.STATE_IDLE


def test_a_cancel_from_hidden_still_plays(overlay):
    overlay.show_at_cursor(overlay.STATE_CANCELING)
    assert overlay._collapse_from == []
    _paint_through(overlay, overlay._cancel_seconds() + 0.1)
    assert overlay.current_state == overlay.STATE_IDLE


def test_switching_off_lets_its_confetti_fall(overlay):
    overlay.show_at_cursor(overlay.STATE_STT_DISABLE)
    _paint_through(overlay, 0.1)
    heights = [p.vy for p in overlay.stt_particles]
    _paint_through(overlay, 0.4)
    assert sum(p.vy for p in overlay.stt_particles) / len(overlay.stt_particles) > sum(heights) / len(heights)


def test_a_moment_fades_out_before_it_hides():
    assert moments.exit_opacity(0.2, 1.5) == 1.0
    assert 0.0 < moments.exit_opacity(1.4, 1.5) < 1.0
    assert moments.exit_opacity(1.5, 1.5) == 0.0


def test_a_problem_notice_shakes_but_never_celebrates(overlay, theme):
    overlay.show_notice("Select the text to change first")
    _paint_through(overlay, overlay._transient_ms() / 1000)
    assert overlay._moment() is None and overlay.stt_particles == []


def test_the_language_notice_and_the_split_still_paint(overlay, theme):
    overlay.set_language("es", ["en", "es"])
    overlay.show_language_notice()
    _paint_through(overlay, 1.5)
    overlay.hide()
    overlay.set_large_file_info(48.2)
    overlay.show_at_cursor(overlay.STATE_LARGE_FILE_SPLITTING)
    _paint_through(overlay, 3.0)


def test_glyphs_draw_from_nothing_to_whole():
    center = QPointF(50, 40)
    for glyph in moments.GLYPHS:
        assert moments.glyph_path(glyph, center, 0.0).length() == 0.0
        half = moments.glyph_path(glyph, center, 0.5).length()
        whole = moments.glyph_path(glyph, center, 1.0).length()
        assert 0.0 < half < whole

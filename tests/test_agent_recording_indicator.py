from unittest.mock import patch

import pytest
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import QWidget

from services.actions.model import AGENT_NAMES, Action, ActionKind, AgentMode
from services.dictation_pipeline import JobMode
from services.runtime import transcription
from services.settings import SettingsKey
from tests import test_automation_foundation_pipeline as foundation
from tests.test_personalize_foundation_runtime import FakeRecorder
from ui_qt.overlays import recording_looks, waveform_overlay
from ui_qt.overlays.waveform_overlay import WaveformOverlay
from ui_qt.utils.palette import DARK_PALETTE, LIGHT_PALETTE, current_palette, set_current_palette

h = foundation.h


@pytest.fixture(params=[False, True], ids=["floating", "embedded"])
def overlay(request):
    host = QWidget() if request.param else None
    if host is not None:
        host.resize(600, 400)
        host.show()
    widget = WaveformOverlay(host)
    widget._refresh_language = lambda: None
    yield widget
    widget._finish_hide()
    widget.close()
    if host is not None:
        host.close()


@pytest.fixture(params=[DARK_PALETTE, LIGHT_PALETTE], ids=["dark", "light"])
def theme(request):
    previous = current_palette()
    set_current_palette(request.param)
    yield
    set_current_palette(previous)


@pytest.fixture
def painted_text(monkeypatch):
    texts = []

    class TextPainter(QPainter):
        def drawText(self, *args):
            texts.append(args[-1])
            return super().drawText(*args)

    monkeypatch.setattr(waveform_overlay, "QPainter", TextPainter)
    return texts


def paint(widget, texts):
    texts.clear()
    with patch.object(waveform_overlay.logger, "error") as errors:
        widget.grab()
    errors.assert_not_called()


@pytest.mark.parametrize("look", recording_looks.LOOKS)
def test_agent_destination_survives_preview_captions_and_work(overlay, theme, painted_text, look):
    overlay.set_recording_look(look)
    overlay.set_label_parts(False, False, False)
    overlay.set_recording_destination("Agent · Codex")
    overlay.show_at_cursor(overlay.STATE_RECORDING)
    overlay.set_language("es", ["en", "es"])
    overlay.set_hands_free(True)
    overlay.show_caption("Switched to Laptop mic")
    for state in (overlay.STATE_RECORDING, overlay.STATE_STREAMING, overlay.STATE_PROCESSING,
                  overlay.STATE_TRANSCRIBING, overlay.STATE_CLEANING):
        overlay.set_state(state)
        if state == overlay.STATE_STREAMING:
            overlay.update_streaming_text("Add a visible indicator for the agent recording " * 3)
        paint(overlay, painted_text)
        assert "Agent · Codex" in painted_text


@pytest.mark.parametrize("end", ["canceling", "idle", "command_listening", "copied", "notice"])
def test_destination_clears_when_capture_is_abandoned_or_replaced(overlay, painted_text, end):
    overlay.set_recording_destination("Agent · Codex")
    overlay.show_at_cursor(overlay.STATE_RECORDING)
    overlay.set_state(end)
    paint(overlay, painted_text)
    assert "Agent · Codex" not in painted_text
    assert overlay.recording_destination == ""


def test_finished_capture_keeps_the_destination_through_fade_then_clears_it(overlay, painted_text):
    overlay.set_recording_destination("Agent · Codex")
    overlay.show_at_cursor(overlay.STATE_RECORDING)
    overlay.set_state(overlay.STATE_TRANSCRIBING)
    overlay.hide()
    paint(overlay, painted_text)
    assert "Agent · Codex" in painted_text
    overlay._fading_since -= waveform_overlay._FADE_OUT_S
    overlay._update_animation()
    assert not overlay.isVisible()
    overlay.show_at_cursor(overlay.STATE_RECORDING)
    paint(overlay, painted_text)
    assert "Agent · Codex" not in painted_text


@pytest.fixture
def recording(h, overlay, monkeypatch):
    monkeypatch.setattr(transcription.audio_player, "stop_playback", lambda: None)
    h.controller.recorder = FakeRecorder()
    h.controller._streaming_enabled = False
    h.ui.clear_transcription_stats = lambda: None
    h.controller.recording_state_changed = foundation.Signal()
    h.controller.recording_destination_changed.handler = overlay.set_recording_destination
    h.controller.overlay_state_update.handler = lambda state: overlay.show_at_cursor(state.value)
    return h


@pytest.mark.parametrize("agent", AGENT_NAMES)
@pytest.mark.parametrize("mode", AgentMode.ALL)
def test_recording_shows_its_selected_agent_before_streaming_starts(recording, overlay, agent, mode):
    h = recording
    action = Action("agent", "My custom action name", ActionKind.AGENT, {"agent": agent, "mode": mode},
                    shaping="raw")
    h.settings.update_settings({SettingsKey.ACTIONS: [action.to_dict()]})
    expected = f"Agent · {AGENT_NAMES[agent]}"
    observed = []
    h.controller.streaming_runtime.start_streaming_session.side_effect = lambda: observed.append(
        overlay.recording_destination
    )
    assert h.runtime.start_recording(mode=JobMode.ACTION, action_id=action.id)
    assert observed == [expected]
    assert overlay.recording_destination == expected
    h.settings.update_settings({SettingsKey.ACTIONS: []})
    overlay.set_state(overlay.STATE_TRANSCRIBING)
    assert overlay.recording_destination == expected


def test_plain_dictation_clears_the_previous_destination_even_during_a_fade(recording, overlay):
    overlay.set_recording_destination("Agent · Codex")
    overlay.show_at_cursor(overlay.STATE_TRANSCRIBING)
    overlay.hide()
    assert recording.runtime.start_recording()
    assert overlay.recording_destination == ""
    assert overlay.current_state == overlay.STATE_RECORDING


def test_refused_start_does_not_publish_an_agent_destination(recording):
    recording.controller.recorder.start_ok = False
    action = Action("agent", "Agent hotkey", ActionKind.AGENT, {"agent": "codex"}, shaping="raw")
    recording.settings.update_settings({SettingsKey.ACTIONS: [action.to_dict()]})
    assert not recording.runtime.start_recording(mode=JobMode.ACTION, action_id=action.id)
    assert recording.controller.recording_destination_changed.calls == []

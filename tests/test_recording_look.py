"""The recording overlay's live look, its legacy option and the Settings preview."""
import os
import tempfile
import time
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from services.settings import (
    RecordingOverlayLook,
    SettingsKey,
    SettingsManager,
    SettingsView,
    resolve_recording_overlay_clock,
    resolve_recording_overlay_dot,
    resolve_recording_overlay_look,
    resolve_recording_overlay_text,
)
from ui_qt.overlays import live_recording, recording_looks, waveform_overlay
from ui_qt.overlays.waveform_overlay import WaveformOverlay
from ui_qt.utils.loudness import level_to_height
from ui_qt.utils.palette import (
    DARK_PALETTE,
    LIGHT_PALETTE,
    current_palette,
    set_current_palette,
)
from ui_qt.widgets import recording_look_preview
from ui_qt.widgets.recording_look_preview import RecordingLookPreview, sample_level

SPEECH = 0.06  # a word on a desk microphone, as raw RMS
SILENCE = 0.002


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


def _run(overlay, seconds, level):
    overlay.timer.stop()
    with patch.object(waveform_overlay.logger, "error") as error:
        end = overlay.animation_time + seconds
        while overlay.animation_time < end:
            overlay.update_audio_levels([level] * 20)
            overlay.last_frame_time = time.monotonic() - 1 / 30
            overlay._update_animation()
            overlay.timer.stop()
            overlay.grab()
    error.assert_not_called()


# --- the setting -----------------------------------------------------------------


def test_the_live_look_is_the_default_and_unknown_values_fall_back_to_it():
    assert resolve_recording_overlay_look({}) == RecordingOverlayLook.RIBBON
    assert resolve_recording_overlay_look({SettingsKey.RECORDING_OVERLAY_LOOK: "sparkly"}) == "ribbon"
    assert resolve_recording_overlay_look({SettingsKey.RECORDING_OVERLAY_LOOK: "legacy"}) == "legacy"
    # Saved before the looks split.
    assert resolve_recording_overlay_look({SettingsKey.RECORDING_OVERLAY_LOOK: "live"}) == "ribbon"
    assert recording_looks.normalize_look(None) == recording_looks.RIBBON
    assert recording_looks.normalize_look("live") == recording_looks.RIBBON
    # Orb and Bubble lift were replaced; a saved choice lands on the successor.
    assert resolve_recording_overlay_look({SettingsKey.RECORDING_OVERLAY_LOOK: "orb"}) == "pulse"
    assert resolve_recording_overlay_look({SettingsKey.RECORDING_OVERLAY_LOOK: "bubbles"}) == "baitball"
    assert recording_looks.normalize_look("orb") == recording_looks.PULSE


def test_settings_and_the_overlay_agree_on_the_looks():
    assert RecordingOverlayLook.ALL == recording_looks.LOOKS
    assert set(RecordingOverlayLook.LABELS) == set(RecordingOverlayLook.DETAILS) == set(recording_looks.LOOKS)


def test_the_label_parts_default_on():
    assert resolve_recording_overlay_dot({}) is True
    assert resolve_recording_overlay_text({}) is True
    assert resolve_recording_overlay_clock({}) is True


# --- the ribbon ------------------------------------------------------------------


def test_the_ribbon_rises_with_speech_and_lies_flat_in_silence():
    ribbon = live_recording.VoiceRibbon()
    for _ in range(60):
        ribbon.hear(SILENCE)
        ribbon.advance(1 / 30)
    flat = max(ribbon.height_at(x, 300) for x in range(14, 287, 4))
    for _ in range(30):
        ribbon.hear(SPEECH)
        ribbon.advance(1 / 30)
    newest = ribbon.height_at(285, 300)
    assert flat < 0.15, "room noise barely lifts it"
    assert newest > 0.5 > 3 * flat, "speech fills the right, where the newest audio is"
    assert ribbon.crest(285, 300, 50) < 50 - live_recording.RISE * 0.5
    ribbon.reset()
    assert ribbon.height_at(285, 300) == 0.0


# --- the overlay -----------------------------------------------------------------


def test_the_live_look_hears_speech_the_legacy_one_barely_does(overlay):
    overlay.set_recording_look(recording_looks.RIBBON)
    overlay.update_audio_levels([SPEECH] * 20)
    assert overlay.style.audio_levels[0] == pytest.approx(level_to_height(SPEECH))
    assert overlay.style.emitter is not None
    overlay.set_recording_look(recording_looks.LEGACY)
    overlay.update_audio_levels([SPEECH] * 20)
    assert overlay.style.audio_levels[0] == SPEECH
    assert overlay.style.emitter is None
    assert level_to_height(SPEECH) > 10 * SPEECH


def test_live_particles_leave_from_the_ribbon(overlay):
    overlay.set_recording_look(recording_looks.RIBBON)
    overlay.show_at_cursor(overlay.STATE_RECORDING)
    _run(overlay, 1.0, SPEECH)
    baseline = overlay._base_height - live_recording.BASELINE_FROM_BOTTOM
    assert overlay.style.emitter(overlay.width() - 20) < baseline - 5


@pytest.mark.parametrize("look", recording_looks.LOOKS)
@pytest.mark.parametrize("state", ["recording", "streaming", "command_listening"])
def test_every_listening_state_paints_in_both_looks(overlay, theme, look, state):
    overlay.set_recording_look(look)
    overlay.show_at_cursor(state)
    _run(overlay, 0.6, SPEECH)
    if state != "recording":
        overlay.update_streaming_text("hello there, this is a live preview")
        _run(overlay, 0.2, SPEECH)
    overlay.show_caption("Switched to the USB microphone")
    _run(overlay, 0.2, SILENCE)


def test_a_new_recording_restarts_the_clock_and_clears_the_ribbon(overlay):
    overlay.set_recording_look(recording_looks.RIBBON)
    overlay.show_at_cursor(overlay.STATE_RECORDING)
    _run(overlay, 0.5, SPEECH)
    overlay._listen_started -= 30
    overlay.set_state(overlay.STATE_TRANSCRIBING)
    overlay.set_state(overlay.STATE_RECORDING)
    assert time.monotonic() - overlay._listen_started < 1.0
    assert overlay._ribbon.height_at(285, 300) == 0.0


def test_the_live_label_reads_a_running_clock():
    assert live_recording.format_clock(0.4) == "0:00"
    assert live_recording.format_clock(65.2) == "1:05"


def test_refresh_reads_the_saved_look_and_label(overlay):
    saved = {SettingsKey.RECORDING_OVERLAY_LOOK: "fish", SettingsKey.RECORDING_OVERLAY_CLOCK: False}
    with patch.object(waveform_overlay.settings_manager, "load_all_settings", return_value=saved):
        overlay.refresh_recording_look()
    assert overlay.recording_look == recording_looks.FISH
    assert overlay.label_parts == (True, True, False)


@pytest.mark.parametrize("parts", [(False, True, True), (True, False, True), (True, True, False),
                                   (False, False, True), (False, False, False)])
def test_any_mix_of_label_parts_paints(overlay, parts):
    overlay.set_recording_look(recording_looks.RIBBON)
    overlay.set_label_parts(*parts)
    overlay.show_at_cursor(overlay.STATE_RECORDING)
    _run(overlay, 0.5, SPEECH)




# --- the preview -----------------------------------------------------------------


class FakeRecorder:
    def __init__(self, starts=True):
        self.starts = starts
        self.callback = None
        self.canceled = self.cleaned = False
        self.last_start_error = "device busy"

    def set_audio_level_callback(self, callback):
        self.callback = callback

    def start_recording(self):
        return self.starts

    def cancel_recording(self):
        self.canceled = True

    def cleanup(self):
        self.cleaned = True


@pytest.fixture
def preview():
    recorders = []

    def factory():
        recorders.append(FakeRecorder())
        return recorders[-1]

    widget = RecordingLookPreview(recorder_factory=factory, microphone_name=lambda: "USB mic")
    widget.overlay._refresh_language = lambda: None
    widget.recorders = recorders
    yield widget
    widget.hide()
    widget.close()


def test_the_preview_animates_only_while_shown_and_opens_no_windows(preview):
    assert not preview._timer.isActive() and not preview.overlay.isVisible()
    preview.resize(560, 200)
    preview.show()
    assert preview._timer.isActive()
    assert preview.overlay.isVisible() and preview.overlay.current_state == "recording"
    assert [w for w in QApplication.topLevelWidgets() if w.isVisible() and w is not preview] == []
    preview.hide()
    assert not preview._timer.isActive() and preview.overlay.current_state == "idle"


def test_the_preview_shows_the_chosen_look(preview):
    preview.show()
    for look in recording_looks.LOOKS:
        preview.set_look(look)
        assert preview.overlay.recording_look == look
    preview.set_label_parts(False, True, False)
    assert preview.overlay.label_parts == (False, True, False)


def test_the_sample_voice_speaks_then_pauses():
    assert sample_level(1.0) > 0.02 or sample_level(1.1) > 0.02
    assert sample_level(5.5) < 0.01


def test_the_microphone_opens_only_while_on_and_keeps_nothing(preview):
    preview.show()
    preview.mic_button.setChecked(True)
    recorder = preview.recorders[-1]
    assert "USB mic" in preview.source_label.text() and "Nothing is recorded" in preview.source_label.text()
    recorder.callback(SPEECH)
    QApplication.processEvents()
    assert preview.overlay.style.audio_levels[0] == pytest.approx(level_to_height(SPEECH))
    preview.mic_button.setChecked(False)
    assert recorder.canceled and recorder.cleaned and preview._recorder is None


def test_leaving_the_page_closes_the_microphone(preview):
    preview.show()
    preview.mic_button.setChecked(True)
    recorder = preview.recorders[-1]
    preview.hide()
    assert recorder.canceled and recorder.cleaned
    assert not preview.mic_button.isChecked()
    assert not preview.overlay.isVisible()


def test_a_microphone_that_will_not_open_says_why(preview, monkeypatch):
    preview._recorder_factory = lambda: FakeRecorder(starts=False)
    preview.show()
    preview.mic_button.setChecked(True)
    assert not preview.mic_button.isChecked()
    assert "device busy" in preview.source_label.text()
    assert preview._recorder is None


def test_no_microphone_means_no_toggle():
    widget = RecordingLookPreview()
    assert widget.mic_button.isHidden()
    widget.close()


# --- Settings --------------------------------------------------------------------


@pytest.fixture
def make_dialog():
    from ui_qt.dialogs import settings_dialog as settings_dialog_module
    from ui_qt.dialogs import settings_downloads as downloads_module
    from ui_qt.dialogs import settings_models as models_module

    stacks, dialogs = [], []
    temp = tempfile.TemporaryDirectory()

    def build(values=None):
        store = SettingsManager(os.path.join(temp.name, f"settings{len(stacks)}.json"))
        store.save_all_settings({SettingsKey.SETTINGS_VIEW: SettingsView.ADVANCED,
                                 SettingsKey.SELECTED_MODEL: "parakeet", **(values or {})})
        stack = ExitStack()
        for module in (settings_dialog_module, models_module, downloads_module):
            stack.enter_context(patch.object(module, "settings_manager", store))
        stack.enter_context(patch.object(settings_dialog_module.history_manager, "set_retention"))
        for module in (models_module, downloads_module):
            stack.enter_context(patch.object(module, "scan_cached_models", return_value={}))
        stack.enter_context(patch("services.local_asr.cache.inventory", return_value={}))
        stacks.append(stack)
        dialog = settings_dialog_module.SettingsDialog(get_loaded_model=lambda: None, background_cache_scan=False)
        dialog.on_settings_changed = MagicMock()
        dialogs.append(dialog)
        dialog.resize(1100, 820)
        dialog.show()
        return dialog, store

    yield build
    for dialog in dialogs:
        dialog.hide()
        dialog.close()
    for stack in reversed(stacks):
        stack.close()
    temp.cleanup()


def _open_appearance(dialog):
    from ui_qt.dialogs.settings_destinations import APPEARANCE

    dialog.select_destination(APPEARANCE)
    for _ in range(3):
        QApplication.processEvents()


def test_settings_loads_saves_and_pushes_the_look(make_dialog):
    dialog, store = make_dialog({SettingsKey.RECORDING_OVERLAY_LOOK: "legacy"})
    dialog.on_recording_look_changed = MagicMock()
    _open_appearance(dialog)
    assert dialog.recording_look_bar.currentIndex() == RecordingOverlayLook.ALL.index("legacy")
    assert dialog.recording_look_preview.overlay.recording_look == "legacy"
    dialog.on_recording_look_changed.assert_not_called()

    assert not dialog.recording_parts.isEnabled(), "Classic has no live label"

    glow = RecordingOverlayLook.ALL.index("glow")
    QTest.mouseClick(dialog.recording_look_bar.buttons[glow], recording_look_preview.Qt.MouseButton.LeftButton)
    assert store.get(SettingsKey.RECORDING_OVERLAY_LOOK) == "glow"
    assert dialog.recording_look_preview.overlay.recording_look == "glow"
    assert dialog.recording_parts.isEnabled()
    dialog.on_recording_look_changed.assert_called_once_with()

    dialog.recording_dot_switch.click()
    assert store.get(SettingsKey.RECORDING_OVERLAY_DOT) is False
    assert dialog.recording_look_preview.overlay.label_parts == (False, True, True)
    assert dialog.on_recording_look_changed.call_count == 2


def test_settings_loads_the_label_switches(make_dialog):
    dialog, _ = make_dialog({SettingsKey.RECORDING_OVERLAY_TEXT: False, SettingsKey.RECORDING_OVERLAY_CLOCK: False})
    dialog.on_recording_look_changed = MagicMock()
    _open_appearance(dialog)
    assert dialog.recording_dot_switch.isChecked()
    assert not dialog.recording_text_switch.isChecked()
    assert not dialog.recording_clock_switch.isChecked()
    assert dialog.recording_look_preview.overlay.label_parts == (True, False, False)
    dialog.on_recording_look_changed.assert_not_called()


def test_settings_search_finds_the_overlay_look(make_dialog):
    from ui_qt.dialogs import settings_metadata

    titles = [title for attr, title, _ in settings_metadata.PAGE_SEARCH_FIELDS["appearance"]]
    assert "Recording overlay" in titles
    dialog, _ = make_dialog()
    _open_appearance(dialog)
    assert dialog.recording_look_tile.isVisible()


def test_pulse_ripples_when_a_word_starts(overlay):
    overlay.set_recording_look(recording_looks.PULSE)
    overlay.show_at_cursor(overlay.STATE_RECORDING)
    _run(overlay, 0.4, SILENCE)
    assert overlay._visual._ripples == []
    _run(overlay, 0.2, SPEECH)
    assert overlay._visual._ripples


@pytest.mark.parametrize("look", [recording_looks.BAIT_BALL, recording_looks.FISH, recording_looks.GLOW])
def test_the_school_heaves_the_swell_with_the_voice(overlay, look):
    overlay.set_recording_look(look)
    overlay.show_at_cursor(overlay.STATE_RECORDING)
    _run(overlay, 1.5, SILENCE)
    calm = max(overlay._visual.u[0])
    _run(overlay, 1.5, SPEECH)
    assert max(overlay._visual.u[0]) > calm + 3, "the school surges up and pushes the bottom swell"
    # Every layer stays above the one beneath it.
    visual = overlay._visual
    for p in range(visual.POINTS):
        for layer in range(1, visual.LAYERS):
            assert visual.y_of(layer, p) <= visual.y_of(layer - 1, p) - visual.GAP_MIN + 0.5


def test_the_bait_ball_rises_while_talking_and_sinks_in_pauses(overlay):
    """Real speech is words with gaps; the ball must follow phrases, not average them away."""
    overlay.set_recording_look(recording_looks.BAIT_BALL)
    overlay.show_at_cursor(overlay.STATE_RECORDING)
    fish = overlay._visual.fish

    def height():
        return overlay._visual.BASE - sum(f["y"] for f in fish) / len(fish)

    _run(overlay, 1.0, SILENCE)
    talking, resting = [], []
    for _ in range(2):
        for word in range(6):  # a phrase: words with short gaps
            _run(overlay, 0.18, SPEECH)
            _run(overlay, 0.07, SILENCE)
            if word >= 2:
                talking.append(height())
        for beat in range(9):  # a pause
            _run(overlay, 0.1, SILENCE)
            if beat >= 5:
                resting.append(height())
    swing = sum(talking) / len(talking) - sum(resting) / len(resting)
    assert swing > 12, f"talking should lift the ball well above where it rests (swing {swing:.1f} px)"


def test_fizz_fills_further_for_a_louder_voice(overlay):
    fills = []
    for level in (0.015, 0.06):  # quiet speech, then ordinary speech
        overlay.set_recording_look(recording_looks.FIZZ)
        overlay.show_at_cursor(overlay.STATE_RECORDING)
        _run(overlay, 1.5, level)
        visual = overlay._visual
        fills.append(sum(visual.h) / len(visual.h) / visual.MAX_H)
        overlay.hide()
    quiet, ordinary = fills
    assert quiet < 0.45 and ordinary > 0.6, "the level shows how loud, not just that someone spoke"


def test_fizz_fills_with_the_voice_and_drains_in_silence(overlay):
    overlay.set_recording_look(recording_looks.FIZZ)
    overlay.show_at_cursor(overlay.STATE_RECORDING)
    _run(overlay, 1.5, SPEECH)
    full = sum(overlay._visual.h)
    _run(overlay, 1.5, SILENCE)
    assert sum(overlay._visual.h) < full / 2


def _finish(overlay, look):
    overlay.set_recording_look(look)
    overlay.show_at_cursor(overlay.STATE_RECORDING)
    _run(overlay, 0.8, SPEECH)
    overlay.set_state(overlay.STATE_PROCESSING)


@pytest.mark.parametrize("look", recording_looks.LOOKS)
def test_every_look_finishes_through_processing_and_transcribing(overlay, theme, look):
    _finish(overlay, look)
    _run(overlay, 0.7, 0.0)
    overlay.set_state(overlay.STATE_TRANSCRIBING)
    _run(overlay, 0.5, 0.0)
    overlay.hide()
    _run(overlay, 0.1, 0.0)


def test_transcribing_speeds_the_finish_up_without_a_jump(overlay):
    _finish(overlay, recording_looks.RIBBON)
    _run(overlay, 0.5, 0.0)
    assert overlay._work_rate == pytest.approx(1.0)
    before = overlay._work_clock
    overlay.set_state(overlay.STATE_TRANSCRIBING)
    assert overlay._work_clock == before, "the clock carries on from where it was"
    _run(overlay, 0.1, 0.0)
    assert 1.0 < overlay._work_rate < 1.7, "the rate eases up rather than jumping"
    _run(overlay, 1.0, 0.0)
    assert overlay._work_rate == pytest.approx(1.7, abs=0.05)


def test_the_stage_label_crossfades(overlay):
    _finish(overlay, recording_looks.BARS)
    assert overlay._label_from == ""
    overlay.set_state(overlay.STATE_TRANSCRIBING)
    assert overlay._label_from == "Processing..."
    assert time.monotonic() - overlay._label_changed < 0.1


def test_a_finished_result_fades_out_then_hides(overlay):
    _finish(overlay, recording_looks.AURORA)
    overlay.hide()
    assert overlay.isVisible(), "it fades first"
    _run(overlay, 0.1, 0.0)
    assert overlay.isVisible()
    overlay._fading_since -= waveform_overlay._FADE_OUT_S  # the fade runs on the wall clock
    _run(overlay, 0.1, 0.0)
    assert not overlay.isVisible() and overlay.current_state == overlay.STATE_IDLE


def test_a_new_recording_cancels_the_fade(overlay):
    _finish(overlay, recording_looks.DOTS)
    overlay.hide()
    overlay.show_at_cursor(overlay.STATE_RECORDING)
    _run(overlay, waveform_overlay._FADE_OUT_S + 0.1, SPEECH)
    assert overlay.isVisible() and overlay.current_state == overlay.STATE_RECORDING


def test_states_that_are_not_work_hide_at_once(overlay):
    overlay.show_at_cursor(overlay.STATE_RECORDING)
    overlay.hide()
    assert not overlay.isVisible()


def test_classic_finishes_with_the_shared_vortex(overlay):
    _finish(overlay, recording_looks.LEGACY)
    assert overlay._visual is None
    _run(overlay, 0.5, 0.0)
    assert overlay.style.particles, "the vortex is spinning"


class _FakeClock:
    def __init__(self):
        self.ms = 0

    def elapsed(self):
        return self.ms

    def restart(self):
        self.ms = 0


def test_the_preview_loops_through_the_looks_finish(preview):
    preview.show()
    preview.set_look("bars")
    preview._clock = _FakeClock()
    seen = []
    for _name, start in recording_look_preview.STAGES:
        preview._clock.ms = int((start + 0.05) * 1000)
        preview._tick()
        seen.append(preview.overlay.current_state)
    assert seen[:3] == ["recording", "processing", "transcribing"]
    assert preview.overlay._fading_since is not None, "the finish fades out"
    preview._clock.ms = int(recording_look_preview.LOOP_S * 1000) + 10
    preview._tick()
    assert preview.overlay.current_state == "recording" and preview.overlay._fading_since is None

"""Tests for beta system-audio acoustic echo cancellation (AEC)."""

from __future__ import annotations

import threading
import time
import wave
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from config import config
from services import echo_cancellation as aec
from services.runtime.transcription import TranscriptionRuntime
from services.settings import SettingsKey


def _write_mono_wav(path: str, samples: np.ndarray, sample_rate: int) -> None:
    arr = np.ascontiguousarray(samples, dtype=np.int16).reshape(-1)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(arr.tobytes())


def _read_mono_wav(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        rate = wf.getframerate()
        data = wf.readframes(wf.getnframes())
    return np.frombuffer(data, dtype=np.int16), rate


def _synthetic_voice(n: int, sr: int, base_f0: float, rate_hz: float, *, start_s: float = 0.0) -> np.ndarray:
    t = np.arange(n, dtype=np.float64) / float(sr)
    f0 = base_f0 + 25.0 * np.sin(2.0 * np.pi * 1.1 * t)
    phase = 2.0 * np.pi * np.cumsum(f0) / float(sr)
    harmonics = sum((0.4 / k) * np.sin(k * phase) for k in range(1, 8))
    envelope = ((np.sin(2.0 * np.pi * rate_hz * t) > -0.15) & (t >= start_s)).astype(np.float64)
    return (0.28 * harmonics * envelope).astype(np.float32)


def _db(x: np.ndarray) -> float:
    return aec.rms_dbfs(x)


def test_setting_defaults_off_and_rejects_non_bools():
    assert SettingsKey.AUDIO_ECHO_CANCELLATION.default is False
    assert SettingsKey.AUDIO_ECHO_CANCELLATION.read({}) is False
    assert SettingsKey.AUDIO_ECHO_CANCELLATION.read({SettingsKey.AUDIO_ECHO_CANCELLATION: True}) is True
    assert SettingsKey.AUDIO_ECHO_CANCELLATION.read({SettingsKey.AUDIO_ECHO_CANCELLATION: "yes"}) is False


def test_gcc_phat_detects_positive_negative_and_rejects_uncorrelated_headphones():
    rng = np.random.default_rng(7)
    sr = 16000
    n = sr * 4
    far = (0.3 * rng.standard_normal(n)).astype(np.float32)
    speech = _synthetic_voice(n, sr, 140.0, 2.8, start_s=0.3)

    # Headphones: uncorrelated far-end should have low confidence_z
    _lag_hp, z_hp = aec.estimate_echo_lag(speech, far, sr)
    assert z_hp < aec.MIN_ECHO_CORRELATION_Z

    # Positive lag (+65 ms) buried under near-end speech
    d_pos = int(0.065 * sr)
    ir = np.zeros(d_pos + 100, dtype=np.float32)
    ir[d_pos] = 0.25
    ir[d_pos + 32] = -0.1
    echo_pos = np.convolve(far, ir)[:n].astype(np.float32)
    lag_pos, z_pos = aec.estimate_echo_lag(speech + echo_pos, far, sr)
    assert abs(lag_pos - d_pos) <= 2
    assert z_pos > aec.MIN_ECHO_CORRELATION_Z

    # Negative lag (-40 ms): loopback timestamps lagged behind mic timestamps
    d_neg = int(0.040 * sr)
    echo_neg = np.zeros(n, dtype=np.float32)
    echo_neg[: n - d_neg] = 0.4 * far[d_neg:]
    lag_neg, z_neg = aec.estimate_echo_lag(speech + echo_neg, far, sr)
    assert abs(lag_neg - (-d_neg)) <= 2
    assert z_neg > aec.MIN_ECHO_CORRELATION_Z


def test_align_reference_places_echo_at_causal_headroom():
    far = np.arange(1, 101, dtype=np.float32)
    # Positive lag=30 with headroom=10 -> shift right by 20
    aligned_pos = aec.align_reference(far, 100, lag_samples=30, headroom_samples=10)
    assert np.all(aligned_pos[:20] == 0.0)
    assert aligned_pos[20] == 1.0
    assert aligned_pos[30] == 11.0  # matches far[t - 30 + 10]

    # Negative lag=-20 with headroom=10 -> shift left by 30
    aligned_neg = aec.align_reference(far, 100, lag_samples=-20, headroom_samples=10)
    assert aligned_neg[0] == 31.0
    assert np.all(aligned_neg[70:] == 0.0)


@pytest.mark.skipif(not aec.is_available(), reason="pywebrtc-audio not installed")
def test_cancel_echo_strips_speaker_bleed_and_preserves_headphones():
    rng = np.random.default_rng(19)
    sr = 16000
    n = sr * 4
    t = np.arange(n) / float(sr)
    far_env = 0.5 + 0.5 * np.sin(2.0 * np.pi * 1.4 * t)
    far = (far_env * (0.25 * rng.standard_normal(n) + 0.15 * np.sin(2.0 * np.pi * 330.0 * t))).astype(np.float32)
    speech = (1.6 * _synthetic_voice(n, sr, 135.0, 2.5, start_s=1.0)).astype(np.float32)
    far_i16 = np.clip(far * 32767.0, -32768.0, 32767.0).astype(np.int16)
    speech_i16 = np.clip(speech * 32767.0, -32768.0, 32767.0).astype(np.int16)

    # 1. Silent system audio -> skipped
    res_silent = aec.cancel_echo(speech_i16, np.zeros(n, dtype=np.int16))
    assert not res_silent.applied
    assert res_silent.reason == "no_system_audio"

    # 2. Headphones (loud system audio, no mic bleed) -> skipped, speech untouched
    res_hp = aec.cancel_echo(speech_i16, far_i16)
    assert not res_hp.applied
    assert res_hp.reason == "no_echo_detected"

    # 3. Speaker bleed at 75 ms delay -> strong cancellation on echo-only region
    delay = int(0.075 * sr)
    ir = np.zeros(delay + 120, dtype=np.float32)
    ir[delay] = 0.45
    ir[delay + 40] = -0.15
    echo = np.convolve(far, ir)[:n].astype(np.float32)
    near_i16 = np.clip((echo + speech) * 32767.0, -32768.0, 32767.0).astype(np.int16)

    res = aec.cancel_echo(near_i16, far_i16)
    assert res.applied
    assert res.audio is not None
    assert abs(res.delay_ms - 75.0) < 2.0
    # First 0.9 s is echo-only (before speech starts at 1.0 s): verify >= 18 dB reduction.
    echo_before_db = _db(near_i16[: int(0.9 * sr)])
    echo_after_db = _db(res.audio[: int(0.9 * sr)])
    assert echo_before_db - echo_after_db >= 18.0
    # Speech region stays strong and audible (within 6 dB of clean speech).
    speech_after_db = _db(res.audio[int(1.2 * sr) : int(3.5 * sr)])
    assert speech_after_db > _db(speech_i16[int(1.2 * sr) : int(3.5 * sr)]) - 6.0


def test_build_aligned_reference_handles_resample_and_pauses():
    # Two 1-second 48 kHz loopback blocks separated by a 1-second pause span
    sr_in = 48000
    block_a = np.full(sr_in, 5000, dtype=np.int16)
    block_paused = np.full(sr_in, 20000, dtype=np.int16)
    block_b = np.full(sr_in, -5000, dtype=np.int16)
    blocks = (
        aec.ReferenceBlock(t_mono=10.0, sample_rate=sr_in, frames=block_a),
        aec.ReferenceBlock(t_mono=11.0, sample_rate=sr_in, frames=block_paused),
        aec.ReferenceBlock(t_mono=12.0, sample_rate=sr_in, frames=block_b),
    )

    ref_16k = aec.build_aligned_reference_16k(
        blocks,
        target_samples=32000,
        mic_start_mono=10.0,
        pause_spans=((11.0, 12.0),),
    )
    assert ref_16k.shape == (32000,)
    # First second should reflect block_a (+5000), second should reflect block_b (-5000),
    # with the paused block dropped cleanly.
    assert np.mean(ref_16k[1000:15000]) > 3500
    assert np.mean(ref_16k[17000:31000]) < -3500


@pytest.mark.skipif(not aec.is_available(), reason="pywebrtc-audio not installed")
def test_cancel_echo_in_wav_rewrites_atomically_and_preserves_padding(tmp_path):
    rng = np.random.default_rng(31)
    sr_mic = 44100
    sr_lb = 48000
    duration_s = 3.0
    t_lb = np.arange(int(sr_lb * duration_s)) / float(sr_lb)
    far_lb = (0.3 * rng.standard_normal(t_lb.size) + 0.1 * np.sin(2.0 * np.pi * 440.0 * t_lb)).astype(np.float32)
    far_lb_i16 = np.clip(far_lb * 32767.0, -32768.0, 32767.0).astype(np.int16)

    # Create mic echo at 44.1 kHz delayed by 50 ms + 500 ms trailing end padding
    far_44k = aec.resample_int16_mono(far_lb_i16, sr_lb, sr_mic).astype(np.float32) / 32768.0
    delay_44k = int(0.050 * sr_mic)
    echo_44k = np.zeros_like(far_44k)
    echo_44k[delay_44k:] = 0.5 * far_44k[:-delay_44k]
    pad_44k = np.zeros(int(0.5 * sr_mic), dtype=np.float32)
    mic_wav_i16 = np.clip(np.concatenate([echo_44k, pad_44k]) * 32767.0, -32768.0, 32767.0).astype(np.int16)

    wav_path = str(tmp_path / "recorded_audio.wav")
    _write_mono_wav(wav_path, mic_wav_i16, sr_mic)

    reference = aec.CapturedReference(
        blocks=(aec.ReferenceBlock(t_mono=100.0, sample_rate=sr_lb, frames=far_lb_i16),),
        backend="wasapi",
    )
    result = aec.cancel_echo_in_wav(
        wav_path,
        reference,
        mic_start_mono=100.0,
        end_padding_ms=500,
    )
    assert result.applied
    cleaned, out_rate = _read_mono_wav(wav_path)
    assert out_rate == aec.AEC_SAMPLE_RATE
    # 3.0s body + 0.5s padding = ~3.5s at 16 kHz
    assert abs(cleaned.size - int(3.5 * aec.AEC_SAMPLE_RATE)) <= 32
    assert _db(cleaned[: int(3.0 * aec.AEC_SAMPLE_RATE)]) < result.input_rms_db - 18.0
    # Trailing 500 ms silence preserved
    assert np.all(cleaned[-int(0.45 * aec.AEC_SAMPLE_RATE) :] == 0)


def test_loopback_reference_capture_caps_buffer_and_stops_late_source():
    cap = aec.LoopbackReferenceCapture(max_bytes=16)
    block = SimpleNamespace(t_mono=1.0, sample_rate=16000, frames=np.ones(6, dtype=np.int16))
    cap._on_block(block)  # 12 bytes
    cap._on_block(block)  # +12 = 24 > 16 -> overflow
    ref = cap.stop()
    assert ref is not None
    assert ref.overflowed is True
    assert ref.blocks == ()

    # Late source opened after stop() must be torn down immediately
    gate = threading.Event()
    stopped = threading.Event()

    class LateSource:
        def stop(self):
            stopped.set()

    cap2 = aec.LoopbackReferenceCapture()
    with patch.object(cap2, "_start_best_source", side_effect=lambda: (gate.wait(2.0), (LateSource(), "wasapi"))[1]):
        cap2.start()
        time.sleep(0.02)
        cap2.stop(discard=True, timeout=0.05)
        gate.set()
        assert stopped.wait(2.0)


def _make_runtime(tmp_path):
    wav_path = str(tmp_path / "recorded_audio.wav")
    _write_mono_wav(wav_path, np.full(4410, 1000, dtype=np.int16), 44100)

    recorder = MagicMock()
    recorder.is_recording = False
    recorder.capture_canceled = False
    recorder.last_capture_error = None
    recorder.first_block_mono = 50.0
    recorder.pause_spans = []
    recorder.start_recording.return_value = True
    recorder.stop_recording.return_value = True
    recorder.wait_for_stop_completion.return_value = True
    recorder.has_recording_data.return_value = True
    recorder.save_recording.return_value = True
    recorder.get_recording_duration.return_value = 1.0

    controller = MagicMock()
    controller._shutting_down = False
    controller._backup_in_progress = False
    controller._streaming_enabled = False
    controller.recorder = recorder
    controller.current_backend = MagicMock(is_remote=False, is_transcribing=False, prepares_for_transcription=False)
    controller.is_meeting_active.return_value = False
    controller.transcription_readiness_message.return_value = None
    controller.streaming_runtime.stop_streaming_session.return_value = ""
    controller.streaming_runtime.finished_stream.return_value = None
    runtime = TranscriptionRuntime(controller)
    return runtime, controller, wav_path


def test_transcription_runtime_off_by_default_skips_loopback(tmp_path, monkeypatch):
    runtime, controller, _wav = _make_runtime(tmp_path)
    monkeypatch.setattr("services.runtime.transcription.audio_player.stop_playback", lambda: None)
    monkeypatch.setattr("services.runtime.transcription.settings_manager.load_all_settings", lambda: {})

    with patch("services.echo_cancellation.LoopbackReferenceCapture") as cap_cls, \
         patch.object(runtime._incremental, "start") as inc_start:
        assert runtime.start_recording() is True
        cap_cls.assert_not_called()
        inc_start.assert_called_once()


def test_transcription_runtime_beta_aec_runs_before_transcription_and_fails_open(tmp_path, monkeypatch):
    runtime, controller, wav_path = _make_runtime(tmp_path)
    monkeypatch.setattr(config, "RECORDED_AUDIO_FILE", wav_path)
    monkeypatch.setattr("services.runtime.transcription.audio_player.stop_playback", lambda: None)
    monkeypatch.setattr(
        "services.runtime.transcription.settings_manager.load_all_settings",
        lambda: {SettingsKey.AUDIO_ECHO_CANCELLATION: True},
    )

    fake_ref = aec.CapturedReference(
        blocks=(aec.ReferenceBlock(t_mono=50.0, sample_rate=48000, frames=np.ones(480, dtype=np.int16)),),
        backend="wasapi",
    )
    fake_cap = MagicMock()
    fake_cap.stop.return_value = fake_ref

    with patch("services.echo_cancellation.is_available", return_value=True), \
         patch("services.echo_cancellation.LoopbackReferenceCapture", return_value=fake_cap), \
         patch.object(runtime._incremental, "start") as inc_start, \
         patch.object(runtime._incremental, "keep_stream_final") as keep_stream:
        assert runtime.start_recording() is True
        fake_cap.start.assert_called_once()
        # Incremental dictation is skipped while AEC is active so raw audio isn't decoded early.
        inc_start.assert_not_called()

        # Simulate AEC raising an unexpected error: must fail open and still transcribe.
        assert runtime._claim_job(runtime._job) is True
        with patch("services.echo_cancellation.cancel_echo_in_wav", side_effect=RuntimeError("boom")) as cancel_fn, \
             patch.object(runtime, "_run_transcription_job") as run_tx:
            runtime.finish_recording_job()
            cancel_fn.assert_called_once()
            keep_stream.assert_not_called()
            run_tx.assert_called_once_with(wav_path)


def test_transcription_runtime_cancel_discards_loopback_capture(tmp_path, monkeypatch):
    runtime, controller, _wav = _make_runtime(tmp_path)
    monkeypatch.setattr("services.runtime.transcription.audio_player.stop_playback", lambda: None)
    monkeypatch.setattr(
        "services.runtime.transcription.settings_manager.load_all_settings",
        lambda: {SettingsKey.AUDIO_ECHO_CANCELLATION: True},
    )
    fake_cap = MagicMock()

    with patch("services.echo_cancellation.is_available", return_value=True), \
         patch("services.echo_cancellation.LoopbackReferenceCapture", return_value=fake_cap):
        assert runtime.start_recording() is True
        controller.recorder.is_recording = True
        assert runtime.cancel() is True
        fake_cap.stop.assert_called_with(discard=True)


@pytest.mark.qt
def test_settings_recording_page_exposes_beta_echo_cancellation_toggle(monkeypatch):
    from tests.fakes.personalize import settings_dialogs
    from ui_qt.dialogs import settings_dialog as settings_dialog_module
    from ui_qt.dialogs import settings_metadata
    from ui_qt.dialogs.settings_destinations import RECORDING

    monkeypatch.setattr(settings_dialog_module.AudioRecorder, "get_input_devices", staticmethod(lambda: []))

    with settings_dialogs(get_loaded_model=lambda: None) as new_dialog:
        dialog, store = new_dialog()
        dialog.select_destination(RECORDING)

        assert not dialog.audio_echo_cancellation_check.isChecked()
        dialog.audio_echo_cancellation_check.setChecked(True)
        assert store.get(SettingsKey.AUDIO_ECHO_CANCELLATION) is True

        dialog.audio_echo_cancellation_check.setChecked(False)
        assert store.get(SettingsKey.AUDIO_ECHO_CANCELLATION) is False

        # Search metadata matches the built tile copy
        unbuilt = {name: (title, desc) for name, title, desc in settings_metadata.PAGE_SEARCH_FIELDS[RECORDING]}
        assert unbuilt["audio_echo_cancellation_tile"] == (
            dialog.audio_echo_cancellation_tile.title_label.text(),
            dialog.audio_echo_cancellation_tile.description_label.text(),
        )
        assert settings_metadata.CONTROL_DESTINATIONS["audio_echo_cancellation_tile"] == RECORDING
        assert settings_metadata.CONTROL_DESTINATIONS["audio_echo_cancellation_check"] == RECORDING

        # Live recording toggles are grouped together under "During recording"
        from PyQt6.QtWidgets import QLabel

        recording_page = dialog._pages[RECORDING]
        titles = [
            label.text()
            for label in recording_page.findChildren(QLabel)
            if label.objectName() == "settingsTileGroupTitle"
        ]
        assert titles == ["Microphone", "During recording", "Saved recordings"]


@pytest.mark.qt
def test_settings_home_exposes_echo_cancellation_alongside_dictation_toggles(monkeypatch):
    from tests.fakes.personalize import settings_dialogs
    from ui_qt.dialogs import settings_dialog as settings_dialog_module
    from ui_qt.dialogs.settings_destinations import OVERVIEW, RECORDING

    monkeypatch.setattr(settings_dialog_module.AudioRecorder, "get_input_devices", staticmethod(lambda: []))

    with settings_dialogs(get_loaded_model=lambda: None) as new_dialog:
        dialog, store = new_dialog()
        # Home renders the toggle without eagerly building Recording
        assert dialog._built_pages == {OVERVIEW}
        dictation_card = dialog.home.cards["dictation"]
        echo_switch = dialog.home.controls[SettingsKey.AUDIO_ECHO_CANCELLATION]
        assert dictation_card.isAncestorOf(echo_switch)
        assert dictation_card.isAncestorOf(dialog.home.controls[SettingsKey.TRANSCRIPT_CLEANUP_ENABLED])
        assert dictation_card.isAncestorOf(dialog.home.controls[SettingsKey.STREAMING_ENABLED])
        assert not echo_switch.isChecked()

        # Toggling on Home persists, notifies the main window callback, and syncs Recording
        dialog.on_echo_cancel_changed = MagicMock()
        echo_switch.click()
        assert store.get(SettingsKey.AUDIO_ECHO_CANCELLATION) is True
        dialog.on_echo_cancel_changed.assert_called_once_with()
        dialog.select_destination(RECORDING)
        assert dialog.audio_echo_cancellation_check.isChecked()

        # Toggling on Recording syncs back to Home
        dialog.audio_echo_cancellation_check.setChecked(False)
        dialog.show_home()
        assert not echo_switch.isChecked()


@pytest.mark.qt
def test_main_ui_echo_cancel_checkbox_placement_locking_and_persistence():
    from PyQt6.QtWidgets import QApplication

    from ui_qt.widgets.quick_record_tab import QuickRecordTab
    from ui_qt.widgets.upload_file_tab import UploadFileTab

    stored: dict = {}
    manager = MagicMock()
    manager.load_all_settings.side_effect = lambda: dict(stored)

    def save_setting(key, value):
        stored[key] = value
        return True

    manager.save_setting.side_effect = save_setting

    with patch("ui_qt.widgets.transcription_tab_base.settings_manager", manager):
        tab = QuickRecordTab()
        tab.resize(680, 500)
        tab.show()
        QApplication.processEvents()
        try:
            assert tab.echo_cancel_check.isVisible()
            assert tab.echo_cancel_check.text() == "Echo cancel (Beta)"
            assert tab.engine_card.isAncestorOf(tab.echo_cancel_check)
            preview_x = tab.live_preview_check.mapTo(tab, tab.live_preview_check.rect().topLeft()).x()
            echo_x = tab.echo_cancel_check.mapTo(tab, tab.echo_cancel_check.rect().topLeft()).x()
            assert preview_x < echo_x

            # Defaults off, toggles persist and emit
            assert not tab.echo_cancel_check.isChecked()
            emitted = []
            tab.echo_cancel_changed.connect(lambda: emitted.append(True))
            tab.echo_cancel_check.setChecked(True)
            assert stored[SettingsKey.AUDIO_ECHO_CANCELLATION] is True
            assert emitted == [True]

            # Failed save reverts without emitting
            manager.save_setting.side_effect = OSError("disk full")
            tab.echo_cancel_check.setChecked(False)
            assert tab.echo_cancel_check.isChecked() is True
            assert emitted == [True]

            # Locks while recording and unlocks after
            tab.is_recording = True
            tab._update_recording_state()
            assert not tab.echo_cancel_check.isEnabled()
            tab.is_recording = False
            tab._update_recording_state()
            assert tab.echo_cancel_check.isEnabled()
        finally:
            tab.close()

        upload = UploadFileTab()
        upload.resize(680, 500)
        upload.show()
        QApplication.processEvents()
        try:
            assert not upload.echo_cancel_check.isVisible()
        finally:
            upload.close()


@pytest.mark.qt
def test_main_window_and_ui_controller_sync_echo_cancel_checkbox():
    from ui_qt.main_window import MainWindow
    from ui_qt.ui_controller import UIController

    stored = {SettingsKey.AUDIO_ECHO_CANCELLATION: False}
    manager = MagicMock()
    manager.load_all_settings.side_effect = lambda: dict(stored)

    def save_setting(key, value):
        stored[key] = value
        return True

    manager.save_setting.side_effect = save_setting

    with patch("ui_qt.widgets.transcription_tab_base.settings_manager", manager), \
            patch("ui_qt.main_window.settings_manager", manager), \
            patch("ui_qt.dialogs.meeting_intro_dialog.maybe_show_meeting_mode_intro", return_value=False):
        window = MainWindow()
        try:
            emitted = []
            window.echo_cancel_changed.connect(lambda: emitted.append(True))
            window.quick_record_tab.echo_cancel_check.setChecked(True)
            assert emitted == [True]
            assert window.upload_file_tab.echo_cancel_check.isChecked()
        finally:
            window.close()

    # Controller hooks keep Settings dialog and main window in sync
    controller = UIController.__new__(UIController)
    controller.main_window = MagicMock()
    controller._settings_dialog = MagicMock()
    controller._on_echo_cancel_changed()
    controller._settings_dialog.refresh.assert_called_once_with()
    controller._on_settings_echo_cancel_changed()
    controller.main_window.refresh_echo_cancel_controls.assert_called_once_with()

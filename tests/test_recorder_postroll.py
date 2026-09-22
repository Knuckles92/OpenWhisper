"""Adaptive post-roll: capture after a stop press ends once the tail is quiet.

Synthetic blocks drive ``AudioRecorder._audio_callback`` directly, shaped the
way sounddevice delivers them, so no audio device is needed. The input stream
is a mock; the recorder's own thread still waits on the stop and quiet
events exactly as it does in the app.
"""
import logging
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from config import config
from services import recorder as recorder_module
from services.recorder import AudioRecorder, PostRollGate, block_level_db

BLOCK = config.CHUNK_SIZE
RATE = config.SAMPLE_RATE
BLOCK_MS = BLOCK * 1000.0 / RATE
QUIET_BLOCKS = int(np.ceil(config.POST_ROLL_QUIET_MS / BLOCK_MS))


def _block(level_db: float, seed: int) -> np.ndarray:
    """One (frames, 1) int16 block of Gaussian noise at ``level_db`` dBFS RMS."""
    rng = np.random.default_rng(seed)
    rms = 10 ** (level_db / 20.0) * 32768.0
    samples = rng.standard_normal(BLOCK) * rms
    return np.clip(np.round(samples), -32768, 32767).astype(np.int16).reshape(-1, 1)


def _dictation(seconds: float, *, floor_db=-60.0, speech_db=-25.0, gap_db=None):
    """Lead-in quiet, then speech with a short quiet gap every ~0.5 s."""
    gap_db = floor_db if gap_db is None else gap_db
    n = int(round(seconds * 1000 / BLOCK_MS))
    levels = []
    for i in range(n):
        if i < 10 or i % 22 in (0, 1, 2):
            levels.append(gap_db if i >= 10 else floor_db)
        else:
            levels.append(speech_db)
    return levels


@pytest.fixture
def recorder(tmp_path):
    with patch.object(recorder_module.sd, "InputStream", MagicMock()) as stream_cls:
        rec = AudioRecorder(output_file=str(tmp_path / "postroll.wav"))
        rec._stream_cls = stream_cls
        yield rec
        rec.cleanup()


def _feed(rec, levels, seed=0):
    for i, level in enumerate(levels):
        rec._audio_callback(_block(level, seed + i), BLOCK, None, None)


def _feed_until_end(rec, levels, seed=10_000):
    """Feed post-stop blocks; return how many went in before the gate fired."""
    for i, level in enumerate(levels):
        rec._audio_callback(_block(level, seed + i), BLOCK, None, None)
        if rec._post_roll_end_event.is_set():
            return i + 1
    return None


def test_speech_then_silence_ends_after_the_quiet_window(recorder, caplog):
    assert recorder.start_recording()
    _feed(recorder, _dictation(4.0))
    started = time.monotonic()
    assert recorder.stop_recording()

    # The last word runs 150 ms past the press, then the room goes quiet.
    ended = _feed_until_end(recorder, [-25.0] * 6 + [-60.0] * 60)

    assert ended == 6 + QUIET_BLOCKS
    assert recorder._post_roll_end_reason == "quiet"
    with caplog.at_level(logging.INFO, logger="services.recorder"):
        assert recorder.wait_for_stop_completion()
    elapsed = time.monotonic() - started
    assert elapsed < config.POST_ROLL_MS / 1000.0 / 2
    assert not recorder.is_recording
    # abort(), not stop(): the buffered remainder is post-roll anyway.
    stream = recorder._stream_cls.return_value
    stream.abort.assert_called_once()
    stream.stop.assert_not_called()
    stream.close.assert_called_once()
    lines = [r.getMessage() for r in caplog.records if "Post-roll ended" in r.getMessage()]
    assert len(lines) == 1
    assert lines[0].startswith("Post-roll ended by quiet after ")
    assert "quiet below -48 dBFS" in lines[0]
    # Only what was captured before the gate fired is saved.
    expected_frames = (len(_dictation(4.0)) + ended) * BLOCK
    assert recorder._recorded_sample_frames == expected_frames


def test_pause_shorter_than_the_window_does_not_end_capture(recorder):
    assert recorder.start_recording()
    _feed(recorder, _dictation(3.0))
    assert recorder.stop_recording()

    # "uh ... what": a pause one block short of the window, then more speech.
    tail = [-60.0] * (QUIET_BLOCKS - 1) + [-28.0] * 8 + [-60.0] * 40
    ended = _feed_until_end(recorder, tail)

    assert ended == len(tail) - 40 + QUIET_BLOCKS


def test_continued_speech_runs_to_the_cap(recorder, caplog):
    with patch.object(config, "POST_ROLL_MS", 150):
        assert recorder.start_recording()
        _feed(recorder, _dictation(3.0))
        started = time.monotonic()
        assert recorder.stop_recording()
        assert _feed_until_end(recorder, [-25.0] * 80) is None
        with caplog.at_level(logging.INFO, logger="services.recorder"):
            assert recorder.wait_for_stop_completion(timeout=2.0)
    assert time.monotonic() - started >= 0.14
    assert any(
        r.getMessage().startswith("Post-roll ended by cap after ") for r in caplog.records
    )


def test_noise_only_recording_runs_to_the_cap(recorder):
    assert recorder.start_recording()
    _feed(recorder, [-55.0] * 150)  # nobody spoke: floor and "speech" coincide
    assert recorder.stop_recording()

    assert _feed_until_end(recorder, [-70.0] * 60) is None
    assert recorder._post_roll_gate.threshold_db is None
    assert "above the noise floor" in recorder._post_roll_gate.fallback


def test_loud_room_falls_back_to_the_cap(recorder):
    assert recorder.start_recording()
    # Speech only 10 dB over the room: a 12 dB margin cannot separate them.
    _feed(recorder, _dictation(4.0, floor_db=-35.0, speech_db=-25.0))
    assert recorder.stop_recording()

    assert _feed_until_end(recorder, [-60.0] * 60) is None
    assert "speech only 10 dB above the noise floor" == recorder._post_roll_gate.fallback


def test_disabled_switch_keeps_the_fixed_post_roll(recorder):
    with patch.object(config, "POST_ROLL_ADAPTIVE", False):
        assert recorder.start_recording()
        _feed(recorder, _dictation(4.0))
        assert recorder.stop_recording()
        assert _feed_until_end(recorder, [-60.0] * 60) is None

    gate = recorder._post_roll_gate
    assert gate.fallback == "adaptive post-roll disabled"
    cap = recorder._post_roll_deadline - recorder._stop_requested_at
    assert cap == pytest.approx(config.POST_ROLL_MS / 1000.0)
    assert config.POST_ROLL_MS == 1200


def test_very_short_recording_runs_to_the_cap(recorder):
    assert recorder.start_recording()
    short = int(config.POST_ROLL_FLOOR_MIN_MS / BLOCK_MS) - 1
    _feed(recorder, [-60.0] * 4 + [-25.0] * (short - 4))
    assert recorder.stop_recording()

    assert _feed_until_end(recorder, [-60.0] * 60) is None
    assert "too short for a noise floor" in recorder._post_roll_gate.fallback


def test_short_recording_with_a_lead_in_uses_the_gate(recorder):
    assert recorder.start_recording()
    blocks = int(np.ceil(config.POST_ROLL_FLOOR_MIN_MS / BLOCK_MS)) + 2
    _feed(recorder, [-60.0] * 6 + [-25.0] * (blocks - 6))
    assert recorder.stop_recording()

    assert _feed_until_end(recorder, [-60.0] * 60) == QUIET_BLOCKS


def test_stop_before_any_audio_runs_to_the_cap(recorder):
    assert recorder.start_recording()
    assert recorder.stop_recording()
    assert _feed_until_end(recorder, [-90.0] * 60) is None


def test_near_digital_silence_floor_still_ends_early(recorder):
    # Noise suppression gates pauses to about -95 dBFS. Faint rustle at
    # -70 dBFS is 25 dB over that floor but 45 dB under the speech.
    assert recorder.start_recording()
    _feed(recorder, _dictation(5.0, floor_db=-95.0, speech_db=-25.0))
    assert recorder.stop_recording()

    gate = recorder._post_roll_gate
    assert gate.floor_db <= -94.0
    assert gate.threshold_db == pytest.approx(gate.speech_db - config.POST_ROLL_SPEECH_HEADROOM_DB)
    assert _feed_until_end(recorder, [-70.0, -95.0] * 30) == QUIET_BLOCKS


def test_near_digital_silence_floor_keeps_soft_speech(recorder):
    assert recorder.start_recording()
    _feed(recorder, _dictation(5.0, floor_db=-95.0, speech_db=-25.0))
    assert recorder.stop_recording()

    # A trailing word 15 dB softer than the dictation is still speech.
    tail = [-95.0] * 4 + [-40.0] * 10 + [-95.0] * 40
    assert _feed_until_end(recorder, tail) == 14 + QUIET_BLOCKS


def test_repeat_stop_keeps_the_first_cap(recorder):
    assert recorder.start_recording()
    _feed(recorder, _dictation(2.0))
    assert recorder.stop_recording()
    deadline = recorder._post_roll_deadline
    time.sleep(0.02)
    assert recorder.stop_recording()  # e.g. cancel pressed during post-roll
    assert recorder._post_roll_deadline == deadline


def test_cleanup_does_not_wait_out_the_post_roll(recorder, caplog):
    assert recorder.start_recording()
    _feed(recorder, _dictation(2.0))
    thread = recorder.recording_thread
    started = time.monotonic()
    with caplog.at_level(logging.INFO, logger="services.recorder"):
        recorder.cleanup()
    assert time.monotonic() - started < 0.5
    assert not thread.is_alive()
    assert not recorder.is_recording
    assert any(
        r.getMessage().startswith("Post-roll ended by cleanup after ") for r in caplog.records
    )


def test_next_recording_gets_a_fresh_gate(recorder):
    assert recorder.start_recording()
    _feed(recorder, _dictation(2.0))
    assert recorder.stop_recording()
    _feed_until_end(recorder, [-60.0] * 60)
    assert recorder.wait_for_stop_completion()

    assert recorder.start_recording()
    gate = recorder._post_roll_gate
    assert not gate.armed and gate.frames_before_stop == 0
    assert not recorder._post_roll_end_event.is_set()
    assert not recorder._stop_event.is_set()


def test_block_level_db():
    t = np.arange(BLOCK) / RATE
    full_scale_sine = (np.sin(2 * np.pi * 1000 * t) * 32767).astype(np.int16)
    assert block_level_db(full_scale_sine) == pytest.approx(-3.0, abs=0.1)
    assert block_level_db(np.zeros(BLOCK, np.int16)) == -120.0
    assert block_level_db(np.full((BLOCK, 1), 0.1, np.float32)) == pytest.approx(-20.0, abs=0.01)
    assert block_level_db(np.zeros(0, np.int16)) == -120.0


def test_gate_percentiles_come_from_the_whole_recording():
    gate = PostRollGate(RATE)
    for level in [-60.0] * 30 + [-20.0] * 70:
        gate.observe(level, BLOCK)
    gate.arm()
    assert gate.floor_db == -60.0
    assert gate.speech_db == -20.0
    assert gate.threshold_db == -48.0
    # Blocks after the stop no longer move the floor.
    gate.observe(-90.0, BLOCK)
    assert gate.frames_after_stop == BLOCK
    assert gate.floor_db == -60.0

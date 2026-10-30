"""Cutting a walkthrough's recording into one clip per section."""

import wave

import numpy as np
import pytest

from services.walkthrough.sections import (
    MIN_CLIP_S,
    REACH_S,
    clip_stretches,
    cut_frames,
    split_recording,
)

RATE = 16_000


def _tone(seconds: float) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    return (8000 * np.sin(2 * np.pi * 220 * t)).astype(np.int16)


def _quiet(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * RATE), dtype=np.int16)


def _write(path, samples: np.ndarray, *, channels: int = 1, width: int = 2) -> None:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(RATE)
        out.writeframes(samples.tobytes())


def test_a_cut_moves_into_the_pause_near_its_mark():
    audio = np.concatenate([_tone(2.0), _quiet(0.3), _tone(2.0)])

    (cut,) = cut_frames(audio, RATE, [1.8])

    assert 2.0 * RATE <= cut <= 2.3 * RATE


def test_without_a_pause_the_cut_stays_at_the_mark():
    audio = _tone(4.0)

    (cut,) = cut_frames(audio, RATE, [1.5])

    assert cut == 1.5 * RATE


def test_a_softer_stretch_that_is_not_a_pause_does_not_move_the_cut():
    audio = np.concatenate([_tone(1.4), (_tone(0.2) * 0.8).astype(np.int16), _tone(1.4)])

    (cut,) = cut_frames(audio, RATE, [1.2])

    assert cut == 1.2 * RATE


def test_a_pause_out_of_reach_is_ignored():
    audio = np.concatenate([_tone(3.0), _quiet(0.3), _tone(1.0)])

    (cut,) = cut_frames(audio, RATE, [3.0 - REACH_S - 0.5])

    assert cut < 3.0 * RATE - 0.4 * RATE


def test_cuts_never_pass_the_next_mark_or_go_back():
    # The only pause is after both marks: neither cut may reorder them.
    audio = np.concatenate([_tone(1.4), _quiet(0.3), _tone(2.0)])

    first, second = cut_frames(audio, RATE, [1.0, 1.1])

    assert first <= 1.1 * RATE
    assert first <= second


def test_marks_past_the_end_cut_at_the_end():
    audio = _tone(1.0)

    assert cut_frames(audio, RATE, [5.0, 6.0]) == [RATE, RATE]


def test_split_writes_one_clip_per_section(tmp_path):
    audio = np.concatenate([_tone(1.5), _quiet(0.2), _tone(1.5), _quiet(0.2), _tone(1.5)])
    source = tmp_path / "recording.wav"
    _write(source, audio)

    clips = split_recording(source, [1.6, 3.3], tmp_path)

    assert len(clips) == 3
    assert all(clip.path is not None and clip.path.exists() for clip in clips)
    assert sum(clip.duration_s for clip in clips) == pytest.approx(len(audio) / RATE)
    with wave.open(str(clips[1].path), "rb") as middle:
        assert (middle.getframerate(), middle.getnchannels(), middle.getsampwidth()) == (RATE, 1, 2)
        assert middle.getnframes() / RATE == pytest.approx(clips[1].duration_s)


def test_a_section_too_short_to_hold_words_is_not_written(tmp_path):
    audio = _tone(2.0)
    source = tmp_path / "recording.wav"
    _write(source, audio)

    clips = split_recording(source, [2.0 - MIN_CLIP_S / 2], tmp_path)

    assert clips[0].path is not None
    assert clips[1].path is None and clips[1].duration_s < MIN_CLIP_S


def test_a_screenshot_kept_before_any_words_leaves_an_empty_first_section(tmp_path):
    source = tmp_path / "recording.wav"
    _write(source, _tone(2.0))

    clips = split_recording(source, [0.0], tmp_path)

    assert clips[0].path is None
    assert clips[1].duration_s == pytest.approx(2.0)


def test_stereo_recordings_are_cut_on_whole_frames(tmp_path):
    mono = np.concatenate([_tone(1.0), _quiet(0.3), _tone(1.0)])
    source = tmp_path / "stereo.wav"
    _write(source, np.column_stack([mono, mono]).ravel(), channels=2)

    clips = split_recording(source, [0.9], tmp_path)

    assert 1.0 <= clips[0].duration_s <= 1.3
    with wave.open(str(clips[0].path), "rb") as first:
        assert first.getnchannels() == 2


def test_only_16_bit_audio_is_cut(tmp_path):
    source = tmp_path / "eight-bit.wav"
    _write(source, np.full(RATE, 128, dtype=np.uint8), width=1)

    with pytest.raises(ValueError):
        split_recording(source, [0.5], tmp_path)


# --- stretches held for a note -------------------------------------------------


def _frames(path):
    with wave.open(str(path), "rb") as clip:
        return clip.getnframes()


def _samples(path):
    with wave.open(str(path), "rb") as clip:
        return np.frombuffer(clip.readframes(clip.getnframes()), dtype="<i2")


def test_a_held_stretch_is_left_out_of_its_section_and_cut_on_its_own(tmp_path):
    # Speech, a note (louder), more speech; a screenshot kept at 4.0 s.
    note = (_tone(1.0) * 2).astype(np.int16)
    audio = np.concatenate([_tone(1.0), note, _tone(2.0), _tone(2.0)])
    source = tmp_path / "recording.wav"
    _write(source, audio)

    first, rest = split_recording(source, [4.0], tmp_path, leave_out_s=[(1.0, 2.0)])
    (held,) = clip_stretches(source, [(1.0, 2.0)], tmp_path)

    assert first.duration_s == pytest.approx(3.0, abs=0.01)
    assert np.abs(_samples(first.path)).max() <= 8000
    assert rest.duration_s == pytest.approx(2.0, abs=0.01)
    assert held.path.name == "note-1.wav" and _frames(held.path) == RATE
    assert np.abs(_samples(held.path)).max() > 8000


def test_a_stretch_held_across_a_screenshot_leaves_both_sections(tmp_path):
    source = tmp_path / "recording.wav"
    _write(source, _tone(6.0))

    first, second = split_recording(source, [3.0], tmp_path, leave_out_s=[(2.0, 4.0)])

    assert first.duration_s == pytest.approx(2.0, abs=0.01)
    assert second.duration_s == pytest.approx(2.0, abs=0.01)


def test_a_stretch_held_to_the_end_runs_to_the_last_frame(tmp_path):
    source = tmp_path / "recording.wav"
    _write(source, _tone(3.0))

    (held,) = clip_stretches(source, [(2.0, float("inf"))], tmp_path)
    (spoken,) = split_recording(source, [], tmp_path, leave_out_s=[(2.0, float("inf"))])

    assert _frames(held.path) == RATE
    assert spoken.duration_s == pytest.approx(2.0, abs=0.01)


def test_a_moment_held_too_short_to_hold_words_is_no_clip(tmp_path):
    source = tmp_path / "recording.wav"
    _write(source, _tone(2.0))

    (held,) = clip_stretches(source, [(1.0, 1.0 + MIN_CLIP_S / 2)], tmp_path)

    assert held.path is None

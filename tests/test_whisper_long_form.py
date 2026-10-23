"""Long Local Whisper takes must not skip windows or silently drop speech."""
import wave
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from config import config
from services.local_asr.audio import (
    LONG_TRANSCRIPT_S,
    MIN_CHARS_PER_SPEECH_S,
    audible_duration_s,
    transcript_looks_truncated,
    wav_duration_s,
)
from transcriber.local_backend import LocalWhisperBackend

RATE = 16_000


def _tone(seconds: float) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    return (8000 * np.sin(2 * np.pi * 220 * t)).astype(np.int16)


def _write_wav(path, samples: np.ndarray) -> None:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(samples.tobytes())


def _info():
    return SimpleNamespace(language="en", language_probability=1.0)


def _backend(side_effect):
    backend = LocalWhisperBackend(model_name="base", load=False)
    backend.model = Mock()
    backend.model.transcribe.side_effect = side_effect
    backend.should_cancel = False
    backend.is_transcribing = False
    return backend


def test_a_fragment_of_long_speech_looks_truncated():
    assert transcript_looks_truncated("The", 75.0)
    assert transcript_looks_truncated("", 50.0)
    assert not transcript_looks_truncated("word " * 200, 75.0)
    assert not transcript_looks_truncated("The", 10.0)


def test_wav_duration_and_audible_time(tmp_path):
    tone = tmp_path / "tone.wav"
    quiet = tmp_path / "quiet.wav"
    _write_wav(tone, _tone(2.0))
    _write_wav(quiet, np.zeros(2 * RATE, dtype=np.int16))

    assert wav_duration_s(tone) == pytest.approx(2.0)
    assert audible_duration_s(tone) == pytest.approx(2.0, abs=0.15)
    assert audible_duration_s(quiet) == pytest.approx(0.0)
    assert wav_duration_s(tmp_path / "missing.wav") is None


def test_long_truncated_pass_is_decoded_again_without_vad(tmp_path):
    path = tmp_path / "long.wav"
    _write_wav(path, _tone(LONG_TRANSCRIPT_S + 5.0))
    recovered = "word " * int((LONG_TRANSCRIPT_S + 5) * MIN_CHARS_PER_SPEECH_S)
    calls = []

    def transcribe(audio_path, **options):
        calls.append(options)
        text = "The" if options.get("vad_filter") else recovered
        return iter([SimpleNamespace(text=text)]), _info()

    backend = _backend(transcribe)

    assert backend.transcribe(str(path)) == recovered.strip()
    assert [call["vad_filter"] for call in calls] == [True, False]
    assert all(call["condition_on_previous_text"] is False for call in calls)


def test_a_complete_long_transcript_is_not_decoded_twice(tmp_path):
    path = tmp_path / "long.wav"
    _write_wav(path, _tone(LONG_TRANSCRIPT_S + 5.0))
    text = "word " * int((LONG_TRANSCRIPT_S + 5) * MIN_CHARS_PER_SPEECH_S)

    def transcribe(audio_path, **options):
        return iter([SimpleNamespace(text=text)]), _info()

    backend = _backend(transcribe)

    assert backend.transcribe(str(path)) == text.strip()
    assert backend.model.transcribe.call_count == 1


def test_a_short_clip_is_not_retried_when_empty(tmp_path):
    path = tmp_path / "short.wav"
    _write_wav(path, _tone(5.0))

    def transcribe(audio_path, **options):
        return iter([SimpleNamespace(text="The")]), _info()

    backend = _backend(transcribe)

    assert backend.transcribe(str(path)) == "The"
    assert backend.model.transcribe.call_count == 1


def test_long_silence_is_not_retried(tmp_path):
    path = tmp_path / "quiet.wav"
    _write_wav(path, np.zeros(int((LONG_TRANSCRIPT_S + 5) * RATE), dtype=np.int16))

    def transcribe(audio_path, **options):
        return iter([SimpleNamespace(text="The")]), _info()

    backend = _backend(transcribe)

    assert backend.transcribe(str(path)) == "The"
    assert backend.model.transcribe.call_count == 1


def test_retry_keeps_the_longer_of_the_two_passes(tmp_path):
    path = tmp_path / "long.wav"
    _write_wav(path, _tone(LONG_TRANSCRIPT_S + 5.0))

    def transcribe(audio_path, **options):
        text = "kept fragment " * 5 if options.get("vad_filter") else "no"
        return iter([SimpleNamespace(text=text)]), _info()

    backend = _backend(transcribe)

    assert backend.transcribe(str(path)).startswith("kept fragment")
    assert backend.model.transcribe.call_count == 2
    assert config.FASTER_WHISPER_VAD_ENABLED is True

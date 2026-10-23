"""Short walkthrough clips: language pin, short-utterance options, failure marks."""

import json
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from services.recognition_context import RecognitionContext
from services.walkthrough.decode import (
    UNTRANSCRIBED,
    SectionDecoder,
    pin_recognition,
    wav_stats,
)
from transcriber.local_backend import (
    SHORT_VAD_S,
    WHISPER_WINDOW_S,
    LocalWhisperBackend,
    whisper_section_options,
    whisper_section_retry_options,
)

RATE = 16_000


def _write(path: Path, samples: np.ndarray) -> Path:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(samples.tobytes())
    return path


def _tone(seconds: float, amplitude: int = 8000) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    return (amplitude * np.sin(2 * np.pi * 220 * t)).astype(np.int16)


class RecordingBackend:
    """Records transcribe kwargs and returns the next scripted text."""

    supports_recognition = True

    def __init__(self, texts=("",), language=("en", 0.95)):
        self.texts = list(texts)
        self.language = language
        self.calls = []

    def transcribe(self, path, recognition=None, *, decode_options=None):
        self.calls.append(SimpleNamespace(
            path=Path(path).name, recognition=recognition, decode_options=decode_options,
        ))
        return self.texts[min(len(self.calls) - 1, len(self.texts) - 1)]

    def detect_language(self, audio_path):
        return self.language


def test_short_clips_are_decoded_as_one_utterance_without_the_no_speech_skip():
    options = whisper_section_options(4.0)
    assert options["without_timestamps"] is True
    assert options["no_speech_threshold"] is None
    assert "vad_filter" not in options
    assert json.loads(json.dumps(options)) == options


def test_very_short_clips_also_skip_vad():
    options = whisper_section_options(SHORT_VAD_S / 2)
    assert options["vad_filter"] is False
    assert options["vad_parameters"] is None


def test_long_sections_keep_the_usual_whisper_decode():
    assert whisper_section_options(WHISPER_WINDOW_S) == {}
    assert whisper_section_options(75.0) == {}


def test_retry_options_disable_vad_and_the_no_speech_skip():
    options = whisper_section_retry_options()
    assert options == {
        "without_timestamps": True,
        "no_speech_threshold": None,
        "vad_filter": False,
        "vad_parameters": None,
    }


def test_language_is_pinned_from_the_full_recording_when_auto():
    backend = RecordingBackend(language=("en", 0.91))
    recognition = pin_recognition(backend, "full.wav", RecognitionContext())
    assert recognition.language == "en"


def test_a_chosen_language_is_left_alone():
    backend = RecordingBackend(language=("ms", 0.99))
    recognition = pin_recognition(
        backend, "full.wav", RecognitionContext(language="en", phrases=("Ksenia",)),
    )
    assert recognition.language == "en" and recognition.phrases == ("Ksenia",)


def test_a_low_confidence_guess_is_not_pinned():
    backend = RecordingBackend(language=("ms", 0.26))
    recognition = pin_recognition(backend, "full.wav", RecognitionContext())
    assert recognition is None or recognition.language == ""


def test_a_section_with_speech_that_comes_back_empty_is_marked(tmp_path):
    clip = _write(tmp_path / "section-1.wav", _tone(3.0))
    backend = RecordingBackend(texts=["", ""])
    decoder = SectionDecoder(backend, RecognitionContext(), Mock(), str(tmp_path / "full.wav"))

    assert decoder(str(clip)) == UNTRANSCRIBED
    assert [call.decode_options["without_timestamps"] for call in backend.calls] == [True, True]
    assert backend.calls[1].decode_options["vad_filter"] is False


def test_silence_is_left_blank(tmp_path):
    clip = _write(tmp_path / "section-1.wav", np.zeros(2 * RATE, dtype=np.int16))
    backend = RecordingBackend(texts=[""])
    decoder = SectionDecoder(backend, None, Mock(), str(tmp_path / "full.wav"))

    assert decoder(str(clip)) == ""
    assert len(backend.calls) == 1


def test_a_stock_hallucination_on_a_short_clip_is_marked(tmp_path):
    clip = _write(tmp_path / "section-1.wav", _tone(2.5))
    backend = RecordingBackend(texts=["Thank you guys", "Thank you guys"])
    decoder = SectionDecoder(backend, RecognitionContext(), Mock(), str(tmp_path / "full.wav"))

    assert decoder(str(clip)) == UNTRANSCRIBED


def test_a_retry_that_recovers_the_words_is_kept(tmp_path):
    clip = _write(tmp_path / "section-1.wav", _tone(3.0))
    backend = RecordingBackend(texts=["", "Step two. Click the green start button."])
    decoder = SectionDecoder(backend, RecognitionContext(), Mock(), str(tmp_path / "full.wav"))

    assert decoder(str(clip)) == "Step two. Click the green start button."


def test_pinned_language_reaches_each_section(tmp_path):
    clip = _write(tmp_path / "section-1.wav", _tone(3.0))
    backend = RecordingBackend(texts=["Open the settings window."], language=("en", 0.97))
    decoder = SectionDecoder(backend, RecognitionContext(), Mock(), str(tmp_path / "full.wav"))

    assert decoder(str(clip)) == "Open the settings window."
    assert backend.calls[0].recognition.language == "en"


def test_wav_stats_read_duration_and_peak(tmp_path):
    clip = _write(tmp_path / "tone.wav", _tone(1.5, amplitude=4000))
    duration, peak = wav_stats(str(clip))
    assert duration == pytest.approx(1.5)
    assert 3500 <= peak <= 4000


def test_decode_options_overlay_the_whisper_defaults():
    backend = LocalWhisperBackend(model_name="base", load=False)
    backend.model = Mock()
    backend.model.transcribe.return_value = (
        iter([SimpleNamespace(text="hello")]),
        SimpleNamespace(language="en", language_probability=1.0),
    )

    assert backend.transcribe(
        "a.wav", decode_options=whisper_section_options(4.0),
    ) == "hello"
    kwargs = backend.model.transcribe.call_args.kwargs
    assert kwargs["without_timestamps"] is True
    assert kwargs["no_speech_threshold"] is None
    assert kwargs["vad_filter"] is True


def test_detect_language_reads_the_model(tmp_path):
    backend = LocalWhisperBackend(model_name="base", load=False)
    backend.model = Mock()
    backend.model.detect_language.return_value = ("de", 0.88)
    assert backend.detect_language(str(tmp_path / "full.wav")) == ("de", 0.88)


def test_engines_without_decode_options_still_mark_lost_speech(tmp_path):
    clip = _write(tmp_path / "section-1.wav", _tone(3.0))
    backend = SimpleNamespace(
        transcribe=Mock(return_value=""),
        supports_recognition=False,
    )
    incremental = Mock()
    incremental.transcribe.side_effect = lambda engine, path, recognition=None: engine.transcribe(path)
    decoder = SectionDecoder(backend, None, incremental, str(tmp_path / "full.wav"))

    assert decoder(str(clip)) == UNTRANSCRIBED
    assert backend.transcribe.call_count == 2

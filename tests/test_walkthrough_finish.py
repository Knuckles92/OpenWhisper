"""Walkthrough section decode: flag a long take that came back as a fragment."""
import wave
from pathlib import Path

import numpy as np

from services.walkthrough.finish import transcribe_sections
from services.walkthrough.session import Shot

RATE = 16_000


def _tone(seconds: float) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    return (8000 * np.sin(2 * np.pi * 220 * t)).astype(np.int16)


def _write(path, samples: np.ndarray) -> None:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(samples.tobytes())


def test_a_long_section_that_comes_back_short_is_flagged(tmp_path):
    source = tmp_path / "recording.wav"
    _write(source, np.concatenate([_tone(50.0), _tone(2.0)]))
    shots = [Shot(1, tmp_path / "shot-1.png", 50.0)]
    words = {"section-1.wav": "The", "section-2.wav": "Done."}

    decoded = transcribe_sections(
        str(source), shots, lambda path: words[Path(path).name],
    )

    assert decoded.texts[0] == "The"
    assert decoded.short == (1,)


def test_a_long_section_with_its_words_is_not_flagged(tmp_path):
    source = tmp_path / "recording.wav"
    _write(source, _tone(50.0))
    shots = [Shot(1, tmp_path / "shot-1.png", 50.0)]
    text = "item " * 200

    decoded = transcribe_sections(str(source), shots, lambda path: text)

    assert decoded.texts[0] == text
    assert decoded.short == ()


def test_a_short_empty_section_is_not_flagged(tmp_path):
    source = tmp_path / "recording.wav"
    _write(source, _tone(4.0))
    shots = [Shot(1, tmp_path / "shot-1.png", 2.0)]

    decoded = transcribe_sections(str(source), shots, lambda path: "")

    assert decoded.texts == ["", ""]
    assert decoded.short == ()

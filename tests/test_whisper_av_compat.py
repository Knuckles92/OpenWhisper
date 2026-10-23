"""Source installs must keep PyAV below 19 until faster-whisper catches up.

faster-whisper 1.2.1 still calls ``av.open(..., metadata_errors="ignore")``.
PyAV 19 removed that argument, and faster-whisper only declares ``av>=11``,
so a README ``pip install -r requirements.txt`` was resolving to 19.x and
every Local Whisper transcription failed. The packaged release pins
``av==18.1.0``.
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]


def _declared(filename: str, name: str) -> Requirement:
    for raw in (ROOT / filename).read_text(encoding="utf-8").splitlines():
        line = raw.partition(" #")[0].strip()
        if not line or line.startswith("#"):
            continue
        req = Requirement(line)
        if req.name == name:
            return req
    raise AssertionError(f"{name} is not declared in {filename}")


def test_readme_and_release_installs_agree_on_pyav_below_19():
    source = _declared("requirements.txt", "av")
    release = _declared("requirements-release-constraints.txt", "av")

    assert Version("18.1.0") in source.specifier
    assert Version("11.0.0") in source.specifier
    assert Version("19.0.0") not in source.specifier
    assert Version("19.0.2") not in source.specifier
    assert Version("18.1.0") in release.specifier
    assert Version("19.0.0") not in release.specifier


def test_faster_whisper_decode_audio_accepts_a_wav(tmp_path):
    from faster_whisper.audio import decode_audio

    path = tmp_path / "clip.wav"
    tone = (np.sin(2 * np.pi * 440.0 * np.linspace(0, 0.25, 4000, endpoint=False)) * 8000).astype(
        np.int16
    )
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(tone.tobytes())

    audio = decode_audio(str(path), sampling_rate=16000)
    assert audio.dtype == np.float32
    assert audio.size > 0

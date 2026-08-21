"""Shared non-fixture helpers for tests (import, don't rely on conftest)."""
from __future__ import annotations

import wave

import numpy as np


def write_wav(path, value=1000, duration_s=0.5, sample_rate=16000):
    """Write a mono 16-bit PCM wav filled with a constant sample value."""
    frames = np.full(int(duration_s * sample_rate), value, dtype=np.int16)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(frames.tobytes())

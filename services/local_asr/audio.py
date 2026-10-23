"""Bounded decoding windows for file transcription; timestamps stay in source time."""
from __future__ import annotations

import wave
from pathlib import Path
from typing import Optional, Union

import numpy as np

SAMPLE_RATE = 16000
MAX_SAMPLES = 30 * SAMPLE_RATE
MIN_SAMPLES = 24 * SAMPLE_RATE

#: Peak below this (float32, full scale 1.0) is digital silence; matches
#: ``LocalSpeechBackend._audible``.
AUDIBLE_PEAK = 0.00025
#: Whisper's internal window is 30 s. Long-form skip/truncation shows up
#: around 75 s of speech, so this sits between one window and that floor.
LONG_TRANSCRIPT_S = 45.0
#: Typical English is about 12 characters per second of speech. A third of
#: that still flags a section that lost most of its words, without treating
#: slow or paused speech as a failure.
MIN_CHARS_PER_SPEECH_S = 4.0

_WavPath = Union[str, Path]


def split_point(audio: np.ndarray) -> int:
    if len(audio) < MAX_SAMPLES:
        return len(audio)
    region = audio[MIN_SAMPLES:MAX_SAMPLES]
    energy = np.mean(region.reshape(-1, 1600) ** 2, axis=1)
    return MIN_SAMPLES + (int(np.argmin(energy)) + 1) * 1600


def resampler():
    """The 16 kHz mono converter behind every window.

    Incremental dictation runs recorder PCM through its own instance and
    relies on matching ``windows()`` bit for bit, so both are built here.
    """
    import av

    return av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)


class WindowSplitter:
    """``windows()``'s chunking for a 16 kHz stream that arrives in pieces.

    A split looks only at the first MAX_SAMPLES pending, so the windows are a
    function of the sample stream alone and do not depend on how it was cut
    into frames. Pieces are joined once per window: concatenating on every
    resampler frame spent 133 ms copying buffers for an 88.8 s dictation
    (September 2026), more than PyAV took to decode and resample it.
    """

    def __init__(self):
        self._pieces: list[np.ndarray] = []
        self._pending = 0
        #: Samples already returned in earlier windows.
        self.offset = 0

    @property
    def total(self) -> int:
        """Samples pushed so far."""
        return self.offset + self._pending

    def push(self, frames) -> list[tuple[float, np.ndarray]]:
        """Add resampler output frames; return the windows they complete."""
        ready = []
        for frame in frames:
            samples = frame.to_ndarray().reshape(-1).astype(np.float32) / 32768.
            self._pieces.append(samples)
            self._pending += len(samples)
            if self._pending < MAX_SAMPLES:
                continue
            pending = np.concatenate(self._pieces)
            while len(pending) >= MAX_SAMPLES:
                end = split_point(pending)
                ready.append((self.offset / SAMPLE_RATE, pending[:end]))
                self.offset += end
                pending = pending[end:]
            self._pieces, self._pending = [pending], len(pending)
        return ready

    def finish(self) -> list[tuple[float, np.ndarray]]:
        """Return the final partial window, if any audio is left."""
        if not self._pending:
            return []
        pending = np.concatenate(self._pieces)
        window = (self.offset / SAMPLE_RATE, pending)
        self.offset += len(pending)
        self._pieces, self._pending = [], 0
        return [window]


def windows(audio_path: str):
    import av

    splitter = WindowSplitter()
    convert = resampler()
    with av.open(audio_path) as container:
        for frame in container.decode(audio=0):
            frame.pts = None
            yield from splitter.push(convert.resample(frame))
        yield from splitter.push(convert.resample(None))
        yield from splitter.finish()


def wav_duration_s(path: _WavPath) -> Optional[float]:
    """Length of a 16-bit PCM WAV in seconds, or None if it cannot be read."""
    try:
        with wave.open(str(path), "rb") as wav:
            rate = wav.getframerate()
            if rate <= 0:
                return None
            return wav.getnframes() / float(rate)
    except (OSError, wave.Error, EOFError):
        return None


def audible_duration_s(path: _WavPath, *, frame_s: float = 0.1) -> Optional[float]:
    """Seconds of ``path`` whose 100 ms peaks rise above digital silence."""
    try:
        with wave.open(str(path), "rb") as wav:
            rate = wav.getframerate()
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            frames = wav.getnframes()
            pcm = wav.readframes(frames)
    except (OSError, wave.Error, EOFError):
        return None
    if rate <= 0 or width != 2 or channels < 1:
        return None
    samples = np.frombuffer(pcm, dtype="<i2")
    usable = (len(samples) // channels) * channels
    if usable <= 0:
        return 0.0
    samples = samples[:usable]
    if channels > 1:
        samples = samples.reshape(-1, channels).max(axis=1)
    audio = samples.astype(np.float32) / 32768.0
    hop = max(1, int(round(frame_s * rate)))
    n = (len(audio) // hop) * hop
    if n <= 0:
        return float(np.max(np.abs(audio)) > AUDIBLE_PEAK) * (len(audio) / rate)
    peaks = np.max(np.abs(audio[:n].reshape(-1, hop)), axis=1)
    return float(np.count_nonzero(peaks > AUDIBLE_PEAK)) * (hop / rate)


def transcript_looks_truncated(text: str, speech_s: float) -> bool:
    """Whether ``text`` is far too short for ``speech_s`` of audible audio.

    Used to retry a long Local Whisper pass without VAD, and to tell the user
    when a walkthrough section still came back as a fragment. Short clips are
    left alone: empty or garbled few-second sections are a different failure.
    """
    if speech_s < LONG_TRANSCRIPT_S:
        return False
    return len((text or "").strip()) < speech_s * MIN_CHARS_PER_SPEECH_S

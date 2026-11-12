"""Bounded decoding windows for file transcription; timestamps stay in source time."""
from __future__ import annotations

import numpy as np

from services.wav_metadata import is_mono_pcm16

SAMPLE_RATE = 16000
MAX_SAMPLES = 30 * SAMPLE_RATE
MIN_SAMPLES = 24 * SAMPLE_RATE
# Before it decodes 16-bit PCM from a WAV, FFmpeg searches the first 32
# packets for a compressed stream carried inside it (DTS or AC-3, as on a
# surround CD), which takes two channels. A one-channel recording has
# nothing to find, and the search cost every dictation of five seconds or
# more 17 ms at its stop (November 2026). This ends it after one packet;
# the frames decoded are the same.
_MONO_PCM_OPTIONS = {"probesize": "4096"}


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


def _open(audio_path: str):
    import av

    if is_mono_pcm16(audio_path):
        return av.open(audio_path, options=_MONO_PCM_OPTIONS)
    return av.open(audio_path)


def windows(audio_path: str):
    splitter = WindowSplitter()
    convert = resampler()
    with _open(audio_path) as container:
        for frame in container.decode(audio=0):
            frame.pts = None
            yield from splitter.push(convert.resample(frame))
        yield from splitter.push(convert.resample(None))
        yield from splitter.finish()

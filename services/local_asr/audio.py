"""Bounded decoding windows for file transcription; timestamps stay in source time."""
from __future__ import annotations

import numpy as np

SAMPLE_RATE = 16000
MAX_SAMPLES = 30 * SAMPLE_RATE
MIN_SAMPLES = 24 * SAMPLE_RATE


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

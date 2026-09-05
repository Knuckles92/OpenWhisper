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


def windows(audio_path: str):
    import av

    pending = np.empty(0, dtype=np.float32)
    offset = 0
    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    with av.open(audio_path) as container:
        def frames():
            for frame in container.decode(audio=0):
                frame.pts = None
                yield from resampler.resample(frame)
            yield from resampler.resample(None)

        for frame in frames():
            pending = np.concatenate((pending, frame.to_ndarray().reshape(-1).astype(np.float32) / 32768.))
            while len(pending) >= MAX_SAMPLES:
                end = split_point(pending)
                yield offset / SAMPLE_RATE, pending[:end]
                offset += end
                pending = pending[end:]
        if pending.size:
            yield offset / SAMPLE_RATE, pending


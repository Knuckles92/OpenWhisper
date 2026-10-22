"""Cut a walkthrough's recording into one clip per section.

Each screenshot closes a section at the moment it was kept. The recorder's
frame count, which those moments are read from, matches the saved WAV
sample for sample, so a moment is an exact place in the file. The cut then
moves to the quietest tenth of a second nearby, so a word the user was
finishing as they kept the screenshot stays whole in one section.
"""

from __future__ import annotations

import wave
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

#: How far a cut may move from the moment its screenshot was kept.
REACH_S = 0.75
#: The stretch of quiet a cut looks for; windows step by half of it.
FRAME_S = 0.1
#: A cut moves only to a window at most this loud next to the mark's own,
#: so steady speech with no real pause is cut where the screenshot was kept.
QUIET_RATIO = 0.5
#: Shorter clips hold no words worth sending to an engine.
MIN_CLIP_S = 0.25
#: The recorder writes 16-bit PCM.
_SAMPLE_WIDTH = 2


@dataclass(frozen=True)
class Clip:
    #: The section's audio as a WAV, or None when it is too short to decode.
    path: Optional[Path]
    duration_s: float


def cut_frames(samples: np.ndarray, rate: int, marks_s: Sequence[float]) -> list[int]:
    """The frame each section ends at, one per mark, never decreasing.

    Args:
        samples: The recording, shaped (frames,) or (frames, channels).
        rate: Frames per second.
        marks_s: When each screenshot was kept, in seconds, in order.
    """
    total = len(samples)
    reach = int(REACH_S * rate)
    cuts: list[int] = []
    floor = 0
    for index, mark in enumerate(marks_s):
        target = min(max(int(round(mark * rate)), floor), total)
        ceiling = total
        if index + 1 < len(marks_s):
            ceiling = min(total, max(int(round(marks_s[index + 1] * rate)), target))
        low, high = max(floor, target - reach), min(ceiling, target + reach)
        cut = _quietest(samples, rate, low, high, target)
        cuts.append(cut)
        floor = cut
    return cuts


def _quietest(samples: np.ndarray, rate: int, low: int, high: int, target: int) -> int:
    """Where to cut in ``[low, high)``: the middle of its quietest FRAME_S window
    (nearest ``target`` on ties), or ``target`` when no window is clearly quieter."""
    width = max(1, int(FRAME_S * rate))
    at_target = min(max(target, low), high)
    if high - low < width:
        return at_target
    window = samples[low:high].astype(np.float64)
    power = window * window
    if power.ndim > 1:
        power = power.sum(axis=1)
    sums = np.concatenate(([0.0], np.cumsum(power)))
    offsets = np.arange(0, high - low - width + 1, max(1, width // 2))
    energy = sums[offsets + width] - sums[offsets]
    middles = low + offsets + width // 2
    distance = np.abs(middles - target)
    quietest = np.lexsort((distance, energy))[0]
    if energy[quietest] > QUIET_RATIO * energy[np.argmin(distance)]:
        return at_target
    return int(middles[quietest])


def split_recording(wav_path: Path, marks_s: Sequence[float], out_dir: Path) -> list[Clip]:
    """One clip per section: before the first mark, between marks, and after the last.

    Raises:
        ValueError: The recording isn't 16-bit PCM.
        OSError, wave.Error: It can't be read, or a clip can't be written.
    """
    with wave.open(str(wav_path), "rb") as source:
        params = source.getparams()
        pcm = source.readframes(params.nframes)
    if params.sampwidth != _SAMPLE_WIDTH:
        raise ValueError(f"expected 16-bit audio, got {8 * params.sampwidth}-bit")
    channels = params.nchannels
    frame_bytes = _SAMPLE_WIDTH * channels
    frames = len(pcm) // frame_bytes
    samples = np.frombuffer(pcm, dtype="<i2", count=frames * channels)
    if channels > 1:
        samples = samples.reshape(frames, channels)
    edges = [0, *cut_frames(samples, params.framerate, marks_s), frames]
    clips = []
    for index, (start, end) in enumerate(pairwise(edges), start=1):
        duration = (end - start) / params.framerate
        if duration < MIN_CLIP_S:
            clips.append(Clip(None, duration))
            continue
        path = out_dir / f"section-{index}.wav"
        with wave.open(str(path), "wb") as clip:
            clip.setparams(params)
            clip.writeframes(pcm[start * frame_bytes:end * frame_bytes])
        clips.append(Clip(path, duration))
    return clips

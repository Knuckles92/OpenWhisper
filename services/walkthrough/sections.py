"""Cut a walkthrough's recording into one clip per section.

Each screenshot closes a section at the moment it was kept. The recorder's
frame count, which those moments are read from, matches the saved WAV
sample for sample, so a moment is an exact place in the file. The cut then
moves to the quietest tenth of a second nearby, so a word the user was
finishing as they kept the screenshot stays whole in one section.

The stretches the user held Hold for note are taken out of the sections
and cut as clips of their own, at exactly the moments held.
"""

from __future__ import annotations

import wave
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Optional, Sequence, Tuple

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


Stretch = Tuple[float, float]


def split_recording(wav_path: Path, marks_s: Sequence[float], out_dir: Path, *,
                    leave_out_s: Sequence[Stretch] = ()) -> list[Clip]:
    """One clip per section: before the first mark, between marks, and after the last.

    Args:
        leave_out_s: Stretches, (start, end) in seconds in time order, that
            no section holds.

    Raises:
        ValueError: The recording isn't 16-bit PCM.
        OSError, wave.Error: It can't be read, or a clip can't be written.
    """
    recording = _Recording(wav_path)
    gaps = recording.frames_of(leave_out_s)
    edges = [0, *cut_frames(recording.samples, recording.rate, marks_s), recording.frames]
    return [recording.clip(_without(start, end, gaps), out_dir / f"section-{index}.wav")
            for index, (start, end) in enumerate(pairwise(edges), start=1)]


def clip_stretches(wav_path: Path, stretches_s: Sequence[Stretch], out_dir: Path) -> list[Clip]:
    """One clip per stretch, (start, end) in seconds, cut exactly there.

    Raises:
        As ``split_recording``.
    """
    recording = _Recording(wav_path)
    return [recording.clip([span], out_dir / f"note-{index}.wav")
            for index, span in enumerate(recording.frames_of(stretches_s), start=1)]


class _Recording:
    """A 16-bit PCM WAV, read whole."""

    def __init__(self, wav_path: Path) -> None:
        with wave.open(str(wav_path), "rb") as source:
            self.params = source.getparams()
            self.pcm = source.readframes(self.params.nframes)
        if self.params.sampwidth != _SAMPLE_WIDTH:
            raise ValueError(f"expected 16-bit audio, got {8 * self.params.sampwidth}-bit")
        channels = self.params.nchannels
        self.rate = self.params.framerate
        self.frame_bytes = _SAMPLE_WIDTH * channels
        self.frames = len(self.pcm) // self.frame_bytes
        samples = np.frombuffer(self.pcm, dtype="<i2", count=self.frames * channels)
        self.samples = samples.reshape(self.frames, channels) if channels > 1 else samples

    def frames_of(self, stretches_s: Sequence[Stretch]) -> list[tuple[int, int]]:
        """Stretches in seconds as frame ranges inside the recording."""
        def frame(seconds: float) -> int:
            return self.frames if seconds == float("inf") else \
                min(max(int(round(seconds * self.rate)), 0), self.frames)

        return [(frame(start), frame(end)) for start, end in stretches_s]

    def clip(self, spans: Sequence[tuple[int, int]], path: Path) -> Clip:
        """``spans`` of frames joined into a WAV at ``path``; no file when too short."""
        duration = sum(end - start for start, end in spans) / self.rate
        if duration < MIN_CLIP_S:
            return Clip(None, duration)
        with wave.open(str(path), "wb") as clip:
            clip.setparams(self.params)
            for start, end in spans:
                clip.writeframes(self.pcm[start * self.frame_bytes:end * self.frame_bytes])
        return Clip(path, duration)


def _without(start: int, end: int, gaps: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """``[start, end)`` less every gap (in order, not overlapping), as spans in order."""
    spans, at = [], start
    for low, high in gaps:
        if high <= at or low >= end:
            continue
        if low > at:
            spans.append((at, low))
        at = max(at, high)
    if at < end:
        spans.append((at, end))
    return spans

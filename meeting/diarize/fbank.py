"""Pure-numpy Kaldi-style 80-dim log-mel filterbank features.

Feature extraction front end for the speaker-embedding model (WeSpeaker
family), matching Kaldi's ``compute-fbank-feats`` defaults closely enough for
embedding parity:

- 16 kHz input, 25 ms frames (400 samples), 10 ms hop (160 samples),
  snip-edges framing (only frames fully inside the signal).
- Per-frame DC removal, pre-emphasis 0.97 (first sample paired with itself,
  as Kaldi does).
- Povey window. The task brief allowed a Hann approximation, but the true
  Povey window is exactly ``hann ** 0.85`` — one extra numpy op — so we use
  the real thing and avoid any window-shape mismatch with Kaldi-trained
  models.
- 512-point FFT, power spectrum, 80 triangular mel bins spaced on Kaldi's
  mel scale (``1127 * ln(1 + f/700)``) between 20 Hz and Nyquist (8000 Hz).
- Natural log with a floor (no dither anywhere: extraction is deterministic
  and unit-testable).

Input audio is float32 in [-1, 1]; it is scaled to the int16 range
internally to match the Kaldi/torchaudio convention. The scale only matters
for the log floor — cepstral mean normalization (``apply_cmn``) removes any
constant gain offset.
"""
from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
FRAME_LENGTH = 400  # 25 ms at 16 kHz
FRAME_SHIFT = 160   # 10 ms at 16 kHz
N_FFT = 512         # next power of two >= FRAME_LENGTH
NUM_MEL_BINS = 80
LOW_FREQ = 20.0
#: Upper mel edge. Kaldi's ``compute-fbank-feats`` and
#: ``torchaudio.compliance.kaldi.fbank`` default to ``high_freq=0``, which
#: means "Nyquist" (8000 Hz at 16 kHz) — and that is what WeSpeaker's
#: preprocessing uses to train the speaker-embedding model. Do NOT "restore"
#: 7600 Hz (a mel/HTK-toolkit convention): narrowing the band here silently
#: shifts every mel bin, mismatches the model's training features, and
#: degrades every embedding. The Nyquist FFT bin still gets zero weight
#: because it sits exactly on the last triangle's right edge.
HIGH_FREQ = SAMPLE_RATE / 2.0
PREEMPHASIS = 0.97
LOG_FLOOR = 1e-10
INT16_SCALE = 32768.0

_mel_filters: np.ndarray | None = None
_window: np.ndarray | None = None


def _mel_scale(hz: np.ndarray | float) -> np.ndarray | float:
    """Kaldi mel scale: ``1127 * ln(1 + hz / 700)``."""
    return 1127.0 * np.log(1.0 + np.asarray(hz, dtype=np.float64) / 700.0)


def _build_mel_filters() -> np.ndarray:
    """Triangular mel filterbank matrix of shape [NUM_MEL_BINS, N_FFT//2 + 1].

    Triangles are constructed in the mel domain (Kaldi convention): filter
    ``i`` rises from edge ``i`` to center ``i+1`` and falls to edge ``i+2`` of
    ``NUM_MEL_BINS + 2`` equally spaced mel points between LOW_FREQ and
    HIGH_FREQ.
    """
    fft_freqs = np.linspace(0.0, SAMPLE_RATE / 2.0, N_FFT // 2 + 1)
    mel_freqs = _mel_scale(fft_freqs)
    mel_points = np.linspace(
        _mel_scale(LOW_FREQ), _mel_scale(HIGH_FREQ), NUM_MEL_BINS + 2
    )
    filters = np.zeros((NUM_MEL_BINS, fft_freqs.size), dtype=np.float64)
    for i in range(NUM_MEL_BINS):
        left, center, right = mel_points[i], mel_points[i + 1], mel_points[i + 2]
        up = (mel_freqs - left) / (center - left)
        down = (right - mel_freqs) / (right - center)
        filters[i] = np.maximum(0.0, np.minimum(up, down))
    return filters.astype(np.float32)


def _build_window() -> np.ndarray:
    """Povey window: ``(0.5 - 0.5*cos(2*pi*n/(N-1))) ** 0.85`` (Hann^0.85)."""
    n = np.arange(FRAME_LENGTH, dtype=np.float64)
    hann = 0.5 - 0.5 * np.cos(2.0 * np.pi * n / (FRAME_LENGTH - 1))
    return (hann ** 0.85).astype(np.float32)


def compute_fbank(audio_f32_16k: np.ndarray) -> np.ndarray:
    """Compute 80-dim log-mel filterbank features from 16 kHz mono audio.

    Args:
        audio_f32_16k: 1-D float32 mono audio at 16 kHz, nominally in
            [-1, 1] (int16 input is accepted and normalized).

    Returns:
        Float32 array of shape [T, 80]; T == 0 when the audio is shorter
        than one 25 ms frame.
    """
    global _mel_filters, _window

    audio = np.asarray(audio_f32_16k)
    if audio.ndim > 1:
        audio = audio.reshape(audio.shape[0], -1).mean(axis=1)
    if audio.dtype == np.int16:
        audio = audio.astype(np.float32) / INT16_SCALE
    audio = np.ascontiguousarray(audio, dtype=np.float32).reshape(-1)

    if audio.size < FRAME_LENGTH:
        return np.zeros((0, NUM_MEL_BINS), dtype=np.float32)

    if _mel_filters is None:
        _mel_filters = _build_mel_filters()
    if _window is None:
        _window = _build_window()

    # Kaldi/torchaudio operate on int16-range samples; the gain only affects
    # values near the log floor (CMN cancels any constant offset).
    scaled = audio.astype(np.float64) * INT16_SCALE

    # Snip-edges framing: frames fully contained in the signal.
    frames = np.lib.stride_tricks.sliding_window_view(scaled, FRAME_LENGTH)
    frames = frames[::FRAME_SHIFT].copy()  # [T, FRAME_LENGTH]

    # Per-frame DC removal (Kaldi remove_dc_offset=True).
    frames -= frames.mean(axis=1, keepdims=True)

    # Pre-emphasis with the first sample paired with itself (Kaldi behavior).
    prev = np.concatenate([frames[:, :1], frames[:, :-1]], axis=1)
    frames = frames - PREEMPHASIS * prev

    frames *= _window.astype(np.float64)

    spectrum = np.fft.rfft(frames, n=N_FFT, axis=1)
    power = spectrum.real ** 2 + spectrum.imag ** 2  # [T, N_FFT//2 + 1]

    mel_energies = power @ _mel_filters.T.astype(np.float64)  # [T, 80]
    logmel = np.log(np.maximum(mel_energies, LOG_FLOOR))
    return logmel.astype(np.float32)


def apply_cmn(feats: np.ndarray) -> np.ndarray:
    """Apply cepstral mean normalization over the time axis.

    Args:
        feats: Feature matrix of shape [T, D].

    Returns:
        Float32 features with the per-dimension mean (over time) subtracted;
        an empty input is returned unchanged.
    """
    feats = np.asarray(feats, dtype=np.float32)
    if feats.shape[0] == 0:
        return feats
    return feats - feats.mean(axis=0, keepdims=True)

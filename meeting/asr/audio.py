"""Audio conversion helpers for the meeting ASR engine.

Spooled chunks are 16 kHz mono int16 WAV files, but these helpers accept any
sample rate so the engine can also transcribe audio that bypassed the spool
(e.g. diarization snippets or recovered files). Resampling reuses
``fft_resample`` from the streaming transcriber instead of pulling scipy into
the distribution.
"""
from __future__ import annotations

import logging
import wave
from typing import Tuple

import numpy as np

from services.streaming_transcriber import fft_resample

logger = logging.getLogger(__name__)

#: Sample rate faster-whisper expects for raw numpy input.
WHISPER_SAMPLE_RATE = 16000


def prepare_for_whisper(frames_int16: np.ndarray, src_rate: int) -> np.ndarray:
    """Convert int16 frames to the float32 mono 16 kHz format Whisper expects.

    Args:
        frames_int16: int16 samples; 1-D mono or 2-D ``(frames, channels)``.
        src_rate: Sample rate of ``frames_int16`` in Hz.

    Returns:
        float32 mono audio at 16 kHz, clipped to ``[-1.0, 1.0]``. Empty input
        returns an empty float32 array.
    """
    audio = np.asarray(frames_int16).astype(np.float32) / 32768.0
    if audio.ndim == 2:
        audio = audio.mean(axis=1)
    if audio.size == 0:
        return audio.astype(np.float32)
    if src_rate != WHISPER_SAMPLE_RATE:
        num_samples = max(1, int(round(audio.size * WHISPER_SAMPLE_RATE / src_rate)))
        audio = fft_resample(audio, num_samples)
    return np.clip(audio, -1.0, 1.0).astype(np.float32)


def load_wav_int16(audio_path: str) -> Tuple[np.ndarray, int]:
    """Load a 16-bit PCM WAV file as int16 frames plus its sample rate.

    Multi-channel files are downmixed to mono by averaging channels (spooled
    chunks are already mono; this is defensive).

    Args:
        audio_path: Path to the WAV file.

    Returns:
        Tuple of (mono int16 frames, sample_rate).

    Raises:
        ValueError: If the file is not 16-bit PCM.
        wave.Error, OSError: If the file is missing or not a valid WAV.
    """
    with wave.open(audio_path, "rb") as wav_file:
        sample_rate = wav_file.getframerate()
        n_channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        raw = wav_file.readframes(wav_file.getnframes())

    if sample_width != 2:
        raise ValueError(
            f"Expected 16-bit PCM WAV, got {sample_width * 8}-bit: {audio_path}"
        )

    frames = np.frombuffer(raw, dtype=np.int16)
    if n_channels > 1:
        usable = (frames.size // n_channels) * n_channels
        frames = frames[:usable].reshape(-1, n_channels)
        frames = frames.astype(np.int32).mean(axis=1).astype(np.int16)
    return frames, sample_rate

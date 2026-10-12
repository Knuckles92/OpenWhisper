"""Common PCM and signal measurements; no microphone or network access."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

RATE = 48_000
FRAME = 480


def read_audio(path: Path) -> np.ndarray:
    from faster_whisper.audio import decode_audio

    audio = decode_audio(str(path), sampling_rate=RATE)
    if not audio.size or not np.isfinite(audio).all():
        raise ValueError(f"Empty or non-finite audio: {path}")
    return audio


def write_audio(path: Path, audio: np.ndarray) -> None:
    if not audio.size or not np.isfinite(audio).all():
        raise ValueError("Cannot save empty or non-finite audio")
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.rint(np.clip(audio, -1, 32767 / 32768) * 32768).astype("<i2")
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(RATE)
        target.writeframes(pcm.tobytes())


def rms(audio: np.ndarray) -> float:
    return (
        float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))
        if audio.size
        else 0.0
    )


def db(value: float) -> float:
    return float(20 * np.log10(max(value, 1e-12)))


def speech_mask(length: int, intervals: list) -> np.ndarray:
    mask = np.zeros(length, dtype=bool)
    for start, end in intervals:
        if not 0 <= start < end <= length / RATE + 1 / RATE:
            raise ValueError("Speech intervals must lie inside the recording")
        mask[round(start * RATE) : round(end * RATE)] = True
    return mask


def si_sdr(reference: np.ndarray, estimate: np.ndarray) -> float | None:
    """Scale-invariant SDR, undefined for a silent reference."""
    ref = reference.astype(np.float64) - np.mean(reference)
    est = estimate.astype(np.float64) - np.mean(estimate)
    energy = float(ref @ ref)
    if energy < 1e-12:
        return None
    target = ref * float(est @ ref) / energy
    residual = est - target
    return float(
        10
        * np.log10(
            max(float(target @ target), 1e-12) / max(float(residual @ residual), 1e-12)
        )
    )


def signal_metrics(raw, output, reference, intervals):
    if output.shape != raw.shape or not np.isfinite(output).all():
        raise ValueError("DSP changed duration or produced non-finite samples")
    mask = speech_mask(len(raw), intervals)
    before = si_sdr(reference, raw) if reference is not None else None
    after = si_sdr(reference, output) if reference is not None else None
    blocks = [output[start : start + FRAME] for start in range(0, len(output), FRAME)]
    return {
        "input_rms_dbfs": db(rms(raw)),
        "output_rms_dbfs": db(rms(output)),
        "output_peak_dbfs": db(float(np.max(np.abs(output)))),
        "clipped_sample_fraction": float(np.mean(np.abs(output) >= 32767 / 32768)),
        "speech_level_delta_db": db(rms(output[mask])) - db(rms(raw[mask]))
        if mask.any()
        else None,
        "pause_level_delta_db": db(rms(output[~mask])) - db(rms(raw[~mask]))
        if (~mask).any()
        else None,
        "energy_above_minus40_dbfs_fraction": float(
            np.mean([db(rms(block)) > -40 for block in blocks])
        ),
        "input_si_sdr_db": before,
        "output_si_sdr_db": after,
        "si_sdr_delta_db": after - before
        if before is not None and after is not None
        else None,
    }

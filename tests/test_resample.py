"""Tests for the FFT resampler that replaced scipy.signal.resample.

These assert signal-level invariants rather than comparing against scipy,
since scipy is deliberately no longer a dependency (it cost ~110 MB in the
packaged build for this one function).
"""
import numpy as np
import pytest

from config import config
from services.streaming_transcriber import fft_resample


def _sine(freq_hz: float, sample_rate: int, duration_s: float) -> np.ndarray:
    t = np.arange(int(sample_rate * duration_s)) / sample_rate
    return np.sin(2 * np.pi * freq_hz * t).astype(np.float32)


def _dominant_freq(samples: np.ndarray, sample_rate: int) -> float:
    spectrum = np.abs(np.fft.rfft(samples))
    return float(np.fft.rfftfreq(len(samples), 1 / sample_rate)[np.argmax(spectrum)])


def test_output_length_and_dtype():
    out = fft_resample(_sine(440, 44100, 1.0), 16000)
    assert len(out) == 16000
    assert out.dtype == np.float32


@pytest.mark.parametrize("freq_hz", [220.0, 1000.0, 3000.0])
def test_downsample_preserves_tone(freq_hz):
    """The recorder's 44.1 kHz -> Whisper's 16 kHz path must keep pitch."""
    src_rate = config.SAMPLE_RATE
    dst_rate = config.WHISPER_TARGET_SAMPLE_RATE
    signal = _sine(freq_hz, src_rate, 1.0)

    out = fft_resample(signal, dst_rate)

    assert _dominant_freq(out, dst_rate) == pytest.approx(freq_hz, abs=2.0)


def test_downsample_preserves_amplitude():
    out = fft_resample(_sine(1000, 44100, 1.0), 16000)
    # A unit sine has RMS 1/sqrt(2); resampling must not rescale it.
    assert float(np.sqrt(np.mean(out**2))) == pytest.approx(0.7071, abs=0.01)


def test_downsample_attenuates_above_nyquist():
    """Content above the new Nyquist must be removed, not aliased down."""
    dst_rate = config.WHISPER_TARGET_SAMPLE_RATE
    # 12 kHz is above the 8 kHz Nyquist of the 16 kHz output. Naive decimation
    # would fold it back to an audible 4 kHz tone; FFT resampling drops it.
    out = fft_resample(_sine(12000, 44100, 1.0), dst_rate)

    assert float(np.max(np.abs(out))) < 0.05


def test_identity_length_roundtrips():
    signal = _sine(440, 16000, 0.5)
    out = fft_resample(signal, len(signal))
    assert np.allclose(out, signal, atol=1e-5)


def test_upsample_preserves_tone():
    duration_s = 0.5
    dst_rate = 22050
    out = fft_resample(_sine(440, 16000, duration_s), int(dst_rate * duration_s))
    assert _dominant_freq(out, dst_rate) == pytest.approx(440.0, abs=3.0)


@pytest.mark.parametrize("num_samples", [1, 2, 3, 999, 1000, 1001])
def test_short_and_odd_lengths_do_not_raise(num_samples):
    out = fft_resample(_sine(440, 44100, 0.05), num_samples)
    assert len(out) == num_samples
    assert np.all(np.isfinite(out))

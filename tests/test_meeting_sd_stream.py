"""Capture timestamps must never turn valid microphone buffers into gaps."""
from types import SimpleNamespace
from unittest.mock import Mock
import wave

import numpy as np
import pytest

from meeting.capture import sd_stream
from meeting.capture.spool import SpoolWriter, resample_to_16k
from meeting.clock import MeetingClock


def _source(callback):
    source = sd_stream.SdCaptureSource("mic", 1, 44100, 1)
    source._active = True
    source._on_block = callback
    # MME's stream clock is live even when callback timestamps are unavailable.
    source._stream = SimpleNamespace(time=300000.0)
    return source


@pytest.mark.parametrize("adc,current", [
    (0.0, 0.0),
    (1024 / 44100, 0.0),  # MME's second adapted buffer: offset from a zero clock
    (2.0, 1.0),
    (-1.0, 1.0),
    (float("nan"), 1.0),
    (1.0, float("nan")),
    (1.0, float("inf")),
])
def test_unavailable_callback_times_keep_monotonic_timestamp(monkeypatch, adc, current):
    monkeypatch.setattr(sd_stream, "time", SimpleNamespace(monotonic=lambda: 1000.0))
    blocks = []
    source = _source(blocks.append)
    source._callback(np.full((1024, 1), 1000, np.int16), 1024,
                     SimpleNamespace(inputBufferAdcTime=adc, currentTime=current), None)
    assert len(blocks) == 1
    assert blocks[0].t_mono == 1000.0


@pytest.mark.parametrize("adc,current", [(75.0, 75.04), (0.0, 0.04)])
def test_valid_callback_clock_preserves_adc_latency(monkeypatch, adc, current):
    monkeypatch.setattr(sd_stream, "time", SimpleNamespace(monotonic=lambda: 1000.0))
    blocks = []
    source = _source(blocks.append)
    source._callback(np.full((1024, 1), 1000, np.int16), 1024,
                     SimpleNamespace(inputBufferAdcTime=adc, currentTime=current), None)
    assert blocks[0].t_mono == pytest.approx(999.96)


def test_mme_batched_callbacks_preserve_audio_through_spool(tmp_path, monkeypatch):
    """Reproduce the real Blue Snowball MME clock pattern at 44.1 kHz.

    PortAudio delivers two buffers together, with currentTime=0 for both
    and ADC times 0 and 1024/44100. Mixing these with stream.time used to
    discard every second buffer and replace ~half the recording with silence.
    """
    now = [1000.0]
    monkeypatch.setattr(sd_stream, "time", SimpleNamespace(monotonic=lambda: now[0]))
    clock = MeetingClock()
    clock.resume_from_recovery(0.0)
    clock._t0 = now[0]
    repo = SimpleNamespace(register_chunk=Mock(return_value=1))
    writer = SpoolWriter("m_timing", "mic", str(tmp_path), clock, repo,
                         on_chunk=lambda chunk: None, queue_size=256)
    source = _source(writer.feed)
    count = 100 * sd_stream.BLOCKSIZE
    # No source silence: any generated silent interval is a capture defect.
    samples = (8000 + 2000 * np.sin(2 * np.pi * 440 * np.arange(count) / 44100)).astype(np.int16)
    try:
        for i, start in enumerate(range(0, count, sd_stream.BLOCKSIZE)):
            now[0] = 1000.2 + (i // 2) * 2 * sd_stream.BLOCKSIZE / 44100
            data = samples[start:start + sd_stream.BLOCKSIZE].copy().reshape(-1, 1)
            source._callback(data, len(data), SimpleNamespace(
                inputBufferAdcTime=(i % 2) * sd_stream.BLOCKSIZE / 44100,
                currentTime=0.0,
            ), None)
            data.fill(0)  # PortAudio may reuse the input buffer immediately.
    finally:
        writer.flush()
    with wave.open(str(tmp_path / "mic_session.wav"), "rb") as recording:
        actual = np.frombuffer(recording.readframes(recording.getnframes()), np.int16)
    expected = resample_to_16k(samples, 44100)
    np.testing.assert_array_equal(actual, expected)
    assert writer._gap_logs == 0

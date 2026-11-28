"""Tests for Meeting Mode acoustic echo cancellation (AEC) wiring."""
from __future__ import annotations

import wave
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from meeting.asr.offline import transcribe_meeting_sessions
from meeting.capture.echo_cancel import MeetingEchoCanceller, cancel_session_echo
from meeting.capture.spool import (
    SpoolWriter,
    session_meta_path,
    session_wav_path,
    write_session_meta,
)
from meeting.capture_session import MeetingCapture
from meeting.clock import MeetingClock
from meeting.engine import MeetingEngineOptions
from meeting.interfaces import (
    CHANNEL_LOOPBACK,
    CHANNEL_MIC,
    CaptureBlock,
    TranscriptSegment,
)
from services import echo_cancellation as aec


def _write_mono_wav(path: str, samples: np.ndarray, sample_rate: int = 16000) -> None:
    arr = np.ascontiguousarray(samples, dtype=np.int16).reshape(-1)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(arr.tobytes())


def _read_mono_wav(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    return np.frombuffer(raw, dtype=np.int16), rate


def _synthetic_voice(
    n: int, sr: int, base_f0: float, rate_hz: float, *, start_s: float = 0.0
) -> np.ndarray:
    t = np.arange(n, dtype=np.float64) / float(sr)
    f0 = base_f0 + 25.0 * np.sin(2.0 * np.pi * 1.1 * t)
    phase = 2.0 * np.pi * np.cumsum(f0) / float(sr)
    harmonics = sum((0.4 / k) * np.sin(k * phase) for k in range(1, 8))
    envelope = (
        (np.sin(2.0 * np.pi * rate_hz * t) > -0.15) & (t >= start_s)
    ).astype(np.float64)
    return (0.28 * harmonics * envelope).astype(np.float32)


def _make_far_and_echo(
    duration_s: float,
    *,
    sr_mic: int = 16000,
    sr_lb: int = 16000,
    delay_ms: float = 65.0,
    seed: int = 17,
    speech_start_s: float | None = 1.5,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return ``(far_lb_i16, near_mic_i16, speech_mic_i16)``."""
    rng = np.random.default_rng(seed)
    n_lb = int(round(duration_s * sr_lb))
    t_lb = np.arange(n_lb) / float(sr_lb)
    far_env = 0.5 + 0.5 * np.sin(2.0 * np.pi * 1.3 * t_lb)
    far_lb = (
        far_env
        * (0.25 * rng.standard_normal(n_lb) + 0.12 * np.sin(2.0 * np.pi * 310.0 * t_lb))
    ).astype(np.float32)
    far_lb_i16 = np.clip(far_lb * 32767.0, -32768.0, 32767.0).astype(np.int16)

    far_mic = (
        aec.resample_int16_mono(far_lb_i16, sr_lb, sr_mic).astype(np.float32) / 32768.0
    )
    n_mic = far_mic.size
    delay = int(round((delay_ms / 1000.0) * sr_mic))
    ir = np.zeros(delay + 100, dtype=np.float32)
    ir[delay] = 0.45
    ir[delay + 36] = -0.14
    echo = np.convolve(far_mic, ir)[:n_mic].astype(np.float32)

    if speech_start_s is not None:
        speech = (
            1.6 * _synthetic_voice(n_mic, sr_mic, 140.0, 2.4, start_s=speech_start_s)
        ).astype(np.float32)
    else:
        speech = np.zeros(n_mic, dtype=np.float32)

    near_i16 = np.clip((echo + speech) * 32767.0, -32768.0, 32767.0).astype(np.int16)
    speech_i16 = np.clip(speech * 32767.0, -32768.0, 32767.0).astype(np.int16)
    return far_lb_i16, near_i16, speech_i16


class _FakeRepo:
    def __init__(self) -> None:
        self._seq = 0
        self.rows: list[dict] = []

    def next_chunk_seq(self, meeting_id: str, channel: str) -> int:
        return 0

    def register_chunk(self, **fields) -> int:
        self._seq += 1
        fields["id"] = self._seq
        self.rows.append(fields)
        return self._seq


@pytest.mark.skipif(not aec.is_available(), reason="pywebrtc-audio not installed")
def test_meeting_echo_canceller_strips_speaker_bleed_and_preserves_headphones():
    far_48k, near_16k, speech_16k = _make_far_and_echo(
        4.0, sr_mic=16000, sr_lb=48000, delay_ms=65.0, speech_start_s=1.5
    )

    canceller = MeetingEchoCanceller()
    # Feed 48 kHz loopback in 100 ms blocks starting at meeting time 10.0 s.
    step = 4800
    for i in range(0, far_48k.size, step):
        t_m = 10.0 + i / 48000.0
        canceller.feed_loopback(far_48k[i : i + step], t_m, 48000)

    # 1. Speaker mode: echo-only prefix drops >= 18 dB, near-end speech stays strong.
    cleaned = canceller.cancel_chunk(near_16k, start_s=10.0)
    assert canceller.chunks_applied == 1
    assert canceller.chunks_skipped == 0
    echo_before = aec.rms_dbfs(near_16k[: int(1.3 * 16000)])
    echo_after = aec.rms_dbfs(cleaned[: int(1.3 * 16000)])
    assert echo_before - echo_after >= 18.0
    speech_orig = aec.rms_dbfs(speech_16k[int(1.7 * 16000) : int(3.7 * 16000)])
    speech_after = aec.rms_dbfs(cleaned[int(1.7 * 16000) : int(3.7 * 16000)])
    assert speech_after > speech_orig - 6.0

    # 2. Headphone mode (clean speech, no echo): skipped untouched.
    hp_out = canceller.cancel_chunk(speech_16k, start_s=10.0)
    assert canceller.chunks_skipped == 1
    assert np.array_equal(hp_out, speech_16k)


def test_meeting_echo_canceller_bounds_memory_and_fails_open():
    canceller = MeetingEchoCanceller(window_s=2.0, max_bytes=200)
    block = np.ones(40, dtype=np.int16)  # 80 bytes
    canceller.feed_loopback(block, 0.0, 16000)
    canceller.feed_loopback(block, 1.0, 16000)
    canceller.feed_loopback(block, 2.0, 16000)
    assert canceller._bytes <= 200

    # Fail-open when underlying cancel_echo raises.
    mic = np.full(4000, 1000, dtype=np.int16)
    with patch("services.echo_cancellation.cancel_echo", side_effect=RuntimeError("boom")):
        out = canceller.cancel_chunk(mic, start_s=2.0)
    assert np.array_equal(out, mic)
    assert canceller.chunks_skipped == 1

    canceller.clear()
    assert canceller._bytes == 0


def _started_clock(t0: float = 100.0) -> MeetingClock:
    clock = MeetingClock()
    clock.start()
    clock._t0 = float(t0)
    return clock


@pytest.mark.skipif(not aec.is_available(), reason="pywebrtc-audio not installed")
def test_spool_writer_with_meeting_echo_canceller_cleans_chunk_wav(tmp_path):
    far_48k, near_16k, _ = _make_far_and_echo(
        3.5, sr_mic=16000, sr_lb=48000, delay_ms=55.0, speech_start_s=None
    )

    clock = _started_clock(100.0)
    repo = _FakeRepo()
    emitted = []
    canceller = MeetingEchoCanceller()

    # Feed 48 kHz loopback blocks onto the meeting clock first.
    step_lb = 4800
    for i in range(0, far_48k.size, step_lb):
        canceller.feed_loopback(far_48k[i : i + step_lb], i / 48000.0, 48000)

    writer = SpoolWriter(
        "m_aec",
        CHANNEL_MIC,
        str(tmp_path),
        clock,
        repo,
        on_chunk=emitted.append,
        target_sec=10.0,
        max_sec=20.0,
        chunk_transform=canceller.cancel_chunk,
    )
    step_mic = 1600
    for i in range(0, near_16k.size, step_mic):
        writer.feed(
            CaptureBlock(
                channel=CHANNEL_MIC,
                frames=near_16k[i : i + step_mic],
                sample_rate=16000,
                t_mono=100.0 + i / 16000.0,
            )
        )
    writer.flush()

    assert len(emitted) == 1
    chunk_pcm, chunk_rate = _read_mono_wav(emitted[0].file_path)
    assert chunk_rate == 16000
    assert aec.rms_dbfs(near_16k) - aec.rms_dbfs(chunk_pcm) >= 18.0
    assert canceller.chunks_applied == 1

    # Raw session WAV stays untouched on disk for offline re-decode.
    sess_pcm, sess_rate = _read_mono_wav(session_wav_path(str(tmp_path), CHANNEL_MIC))
    assert sess_rate == 16000
    assert abs(aec.rms_dbfs(sess_pcm) - aec.rms_dbfs(near_16k)) < 1.0


def test_spool_writer_chunk_transform_fails_open(tmp_path):
    clock = _started_clock(50.0)
    repo = _FakeRepo()
    emitted = []
    raw = np.full(16000, 1200, dtype=np.int16)

    def _boom(samples, start_s):
        raise RuntimeError("transform error")

    writer = SpoolWriter(
        "m_failopen",
        CHANNEL_MIC,
        str(tmp_path),
        clock,
        repo,
        on_chunk=emitted.append,
        target_sec=10.0,
        max_sec=20.0,
        chunk_transform=_boom,
    )
    writer.feed(CaptureBlock(CHANNEL_MIC, raw, 16000, 50.0))
    writer.flush()

    assert len(emitted) == 1
    saved, _ = _read_mono_wav(emitted[0].file_path)
    assert np.array_equal(saved, raw)


def test_meeting_capture_wires_echo_canceller_and_clears_on_flush(tmp_path):
    class _FakeSource:
        def __init__(self, channel, device_id=0, samplerate=16000, channels=1):
            self.channel = channel
            self.device_id = device_id
            self.cb = None
            self.active = False

        def start(self, cb):
            self.cb = cb
            self.active = True

        def stop(self):
            self.active = False

        def is_active(self):
            return self.active

    clock = _started_clock(10.0)
    repo = _FakeRepo()
    options = MeetingEngineOptions(echo_cancellation=True, demo_mode=False)
    capture = MeetingCapture(
        options,
        clock,
        repo,
        recording=lambda: True,
        asr=lambda: None,
        store=lambda: None,
        on_chunk=lambda c: None,
        emit_status=lambda note=None: None,
        emit=lambda kind, payload: None,
    )

    with (
        patch("services.echo_cancellation.is_available", return_value=True),
        patch(
            "meeting.capture.devices.find_mic_device",
            return_value={"index": 1, "samplerate": 16000, "channels": 1, "name": "Mic", "hostapi": "Mock"},
        ),
        patch(
            "meeting.capture.devices.find_loopback_device",
            return_value={"index": 2, "samplerate": 16000, "channels": 1},
        ),
        patch("meeting.capture.sd_stream.SdCaptureSource", _FakeSource),
    ):
        capture.start("m_cap", str(tmp_path))
        try:
            canceller = capture._echo_canceller
            assert canceller is not None
            mic_spool = capture.spools[CHANNEL_MIC]
            assert mic_spool._chunk_transform == canceller.cancel_chunk

            lb_source = capture.source(CHANNEL_LOOPBACK)
            assert lb_source is not None and lb_source.cb is not None
            lb_source.cb(
                CaptureBlock(CHANNEL_LOOPBACK, np.full(1600, 500, dtype=np.int16), 16000, 10.0)
            )
            assert canceller._bytes == 1600 * 2
        finally:
            capture.stop()
            capture.flush()

    assert capture._echo_canceller is None
    assert canceller._bytes == 0


def test_meeting_capture_skips_echo_canceller_when_disabled_or_demo():
    clock = _started_clock(0.0)
    repo = _FakeRepo()

    for opts in (
        MeetingEngineOptions(echo_cancellation=False),
        MeetingEngineOptions(demo_mode=True),
        MeetingEngineOptions(system_audio_policy="disabled"),
    ):
        cap = MeetingCapture(
            opts,
            clock,
            repo,
            recording=lambda: True,
            asr=lambda: None,
            store=lambda: None,
            on_chunk=lambda c: None,
            emit_status=lambda note=None: None,
            emit=lambda kind, payload: None,
        )
        cap._system_audio_disabled = opts.system_audio_policy == "disabled"
        with patch("services.echo_cancellation.is_available", return_value=True):
            assert cap._init_echo_canceller() is None


@pytest.mark.skipif(not aec.is_available(), reason="pywebrtc-audio not installed")
def test_cancel_session_echo_handles_origin_offset_and_windows():
    far_16k, near_16k, speech_16k = _make_far_and_echo(
        4.0, sr_mic=16000, sr_lb=16000, delay_ms=60.0, speech_start_s=None
    )
    # Shift loopback origin earlier by 0.5 s by prepending 0.5 s of silence to far_16k.
    pad = np.zeros(8000, dtype=np.int16)
    lb_shifted = np.concatenate((pad, far_16k))

    cleaned, applied = cancel_session_echo(
        near_16k,
        mic_origin_s=1.0,
        loopback_frames=lb_shifted,
        loopback_origin_s=0.5,
        sample_rate=16000,
        window_s=2.0,
    )
    assert applied is True
    assert aec.rms_dbfs(near_16k) - aec.rms_dbfs(cleaned) >= 18.0

    # Headphones (uncorrelated mic): skipped, returns original audio unchanged.
    uncorr = _synthetic_voice(near_16k.size, 16000, 150.0, 2.0)
    uncorr_i16 = np.clip(uncorr * 32767.0, -32768.0, 32767.0).astype(np.int16)
    hp_out, hp_applied = cancel_session_echo(
        uncorr_i16,
        mic_origin_s=1.0,
        loopback_frames=lb_shifted,
        loopback_origin_s=0.5,
        sample_rate=16000,
    )
    assert hp_applied is False
    assert np.array_equal(hp_out, uncorr_i16)


@pytest.mark.skipif(not aec.is_available(), reason="pywebrtc-audio not installed")
def test_transcribe_meeting_sessions_applies_session_aec_to_mic(tmp_path):
    far_16k, near_16k, _ = _make_far_and_echo(
        3.5, sr_mic=16000, sr_lb=16000, delay_ms=50.0, speech_start_s=None
    )
    spool_dir = str(tmp_path)
    _write_mono_wav(session_wav_path(spool_dir, CHANNEL_MIC), near_16k, 16000)
    _write_mono_wav(session_wav_path(spool_dir, CHANNEL_LOOPBACK), far_16k, 16000)
    write_session_meta(session_meta_path(spool_dir, CHANNEL_MIC), 16000, near_16k.size, 0.0)
    write_session_meta(session_meta_path(spool_dir, CHANNEL_LOOPBACK), 16000, far_16k.size, 0.0)

    seen_with_aec: dict[str, np.ndarray] = {}

    def _capture_decoder(model, frames, rate, *, meeting_id, channel, origin_s, **kwargs):
        seen_with_aec[channel] = frames.copy()
        return [
            TranscriptSegment(
                segment_id=f"seg_{channel}",
                meeting_id=meeting_id,
                chunk_id=None,
                channel=channel,
                start_s=origin_s,
                end_s=origin_s + 1.0,
                text="hello world" if channel == CHANNEL_LOOPBACK else "my note",
            )
        ]

    transcribe_meeting_sessions(
        MagicMock(),
        spool_dir,
        "m_offline",
        transcribe_fn=_capture_decoder,
        echo_cancellation=True,
    )
    assert CHANNEL_MIC in seen_with_aec and CHANNEL_LOOPBACK in seen_with_aec
    # Mic audio fed to Whisper has echo strongly attenuated; loopback is untouched.
    assert aec.rms_dbfs(near_16k) - aec.rms_dbfs(seen_with_aec[CHANNEL_MIC]) >= 18.0
    assert np.array_equal(seen_with_aec[CHANNEL_LOOPBACK], far_16k)

    # With echo_cancellation=False, mic audio is passed raw.
    seen_without_aec: dict[str, np.ndarray] = {}

    def _capture_raw(model, frames, rate, *, meeting_id, channel, origin_s, **kwargs):
        seen_without_aec[channel] = frames.copy()
        return []

    transcribe_meeting_sessions(
        MagicMock(),
        spool_dir,
        "m_offline",
        transcribe_fn=_capture_raw,
        echo_cancellation=False,
    )
    assert np.array_equal(seen_without_aec[CHANNEL_MIC], near_16k)

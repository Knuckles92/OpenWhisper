"""Tests for the PyAV-free client audio path across:
- services/local_asr/audio.py (WAV decode + stateful polyphase resampler + remote compressed decode)
- services/audio_processor.py (preview + bounded silence-aware splitting)
- services/incremental_dictation.py (bit-identical live stream vs saved WAV)
- services/echo_cancellation.py (int16 resampling + aligned reference assembly)
"""
from __future__ import annotations

import builtins
import random
import struct
import wave
from types import SimpleNamespace

import numpy as np
import pytest

from config import config
from services import echo_cancellation as aec
from services.audio_processor import AudioProcessor, UnreadableAudioError
from services.incremental_dictation import DictationSession
from services.local_asr import audio as local_audio
from services.recorder import AudioRecorder
from services.recording_journal import RecordingJournal
from services.remote_asr.meeting_host import HostMeetingServices
from tests.test_remote_meeting_services import _WireConnection
from transcriber.optional_backend import LocalSpeechBackend


@pytest.fixture
def without_pyav(monkeypatch):
    """Make ``import av`` raise ModuleNotFoundError throughout the test."""
    real_import = builtins.__import__

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "av" or name.startswith("av."):
            raise ModuleNotFoundError("No module named 'av'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded)


def _write_pcm_wav(
    path,
    samples_f32: np.ndarray,
    *,
    rate: int = 16000,
    channels: int = 1,
    sampwidth: int = 2,
) -> str:
    arr = np.asarray(samples_f32, dtype=np.float64).reshape(-1)
    if channels > 1:
        arr = np.repeat(arr[:, None], channels, axis=1).reshape(-1)
    if sampwidth == 1:
        raw = np.clip(np.rint(arr * 128.0) + 128.0, 0, 255).astype(np.uint8).tobytes()
    elif sampwidth == 2:
        raw = np.clip(np.rint(arr * 32768.0), -32768, 32767).astype("<i2").tobytes()
    elif sampwidth == 3:
        vals = np.clip(np.rint(arr * 8388608.0), -8388608, 8388607).astype(np.int32)
        uvals = np.where(vals < 0, vals + 0x1000000, vals).astype(np.uint32)
        b0 = (uvals & 0xFF).astype(np.uint8)
        b1 = ((uvals >> 8) & 0xFF).astype(np.uint8)
        b2 = ((uvals >> 16) & 0xFF).astype(np.uint8)
        raw = np.stack([b0, b1, b2], axis=1).tobytes()
    elif sampwidth == 4:
        raw = np.clip(np.rint(arr * 2147483648.0), -2147483648, 2147483647).astype("<i4").tobytes()
    else:
        raise ValueError(sampwidth)

    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(rate)
        wf.writeframes(raw)
    return str(path)


def _write_float32_wav(path, samples_f32: np.ndarray, *, rate: int = 44100, channels: int = 1) -> str:
    arr = np.asarray(samples_f32, dtype="<f4").reshape(-1)
    if channels > 1:
        arr = np.repeat(arr[:, None], channels, axis=1).reshape(-1)
    data = arr.tobytes()
    byte_rate = rate * channels * 4
    block_align = channels * 4
    fmt_chunk = struct.pack("<4sIHHIIHH", b"fmt ", 16, 3, channels, rate, byte_rate, block_align, 32)
    data_chunk = struct.pack("<4sI", b"data", len(data)) + data
    riff = struct.pack("<4sI4s", b"RIFF", 4 + len(fmt_chunk) + len(data_chunk), b"WAVE")
    with open(path, "wb") as handle:
        handle.write(riff + fmt_chunk + data_chunk)
    return str(path)


def _speech_like(seconds: float, rate: int = 44100, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    parts, size, total = [], 0, int(seconds * rate)
    while size < total:
        n = int(rng.uniform(0.2, 1.4) * rate)
        if rng.random() < 0.3:
            part = rng.standard_normal(n) * 30
        else:
            t = np.arange(n) / rate
            part = (
                np.sin(2 * np.pi * rng.uniform(150, 900) * t) * rng.uniform(1500, 9000)
                + rng.standard_normal(n) * 500
            )
        parts.append(part)
        size += n
    return np.clip(np.concatenate(parts)[:total], -32768, 32767).astype(np.int16)


# -------------------------------------------- services/local_asr/audio.py


@pytest.mark.parametrize("sampwidth", [1, 2, 3, 4])
@pytest.mark.parametrize("channels", [1, 2])
@pytest.mark.parametrize("rate", [16000, 44100, 48000])
def test_wav_windows_without_pyav_all_pcm_formats(
    without_pyav, tmp_path, sampwidth, channels, rate
):
    assert local_audio.has_pyav() is False
    t = np.arange(int(rate * 1.2)) / float(rate)
    tone = (0.4 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
    wav_path = _write_pcm_wav(
        tmp_path / f"w_{sampwidth}_{channels}_{rate}.wav",
        tone,
        rate=rate,
        channels=channels,
        sampwidth=sampwidth,
    )
    wins = list(local_audio.windows(wav_path))
    assert len(wins) == 1
    offset_s, out = wins[0]
    assert offset_s == 0.0
    assert out.dtype == np.float32
    expected_len = int(round(len(tone) * 16000 / rate))
    assert abs(len(out) - expected_len) <= 1
    # Verify waveform amplitude is preserved (not crushed or clipped).
    assert 0.2 < float(np.max(np.abs(out))) < 0.55


def test_ieee_float32_wav_windows_and_probe_without_pyav(without_pyav, tmp_path):
    rate = 48000
    t = np.arange(rate) / float(rate)
    tone = (0.35 * np.sin(2 * np.pi * 300.0 * t)).astype(np.float32)
    path = _write_float32_wav(tmp_path / "float32.wav", tone, rate=rate, channels=2)

    dur, r, ch = local_audio.probe_wav_header(path)
    assert dur == pytest.approx(1.0, abs=0.01)
    assert r == 48000 and ch == 2

    wins = list(local_audio.windows(path))
    assert len(wins) == 1
    assert len(wins[0][1]) == 16000
    assert 0.25 < float(np.max(np.abs(wins[0][1]))) < 0.45


def test_numpy_resampler_preserves_dc_and_is_chunk_size_invariant(without_pyav):
    # DC preservation
    dc = np.full(48000, 5000, dtype=np.int16)
    dc_16k = local_audio.resample_int16_mono_numpy(dc, 48000, 16000)
    assert len(dc_16k) == 16000
    assert np.allclose(dc_16k, 5000, atol=1)

    # 16 kHz -> 16 kHz is bit-exact
    raw16 = np.array([-32768, -1234, 0, 1234, 32767], dtype=np.int16)
    np.testing.assert_array_equal(
        local_audio.resample_int16_mono_numpy(raw16, 16000, 16000), raw16
    )

    # Chunk-size invariance at 44.1 kHz -> 16 kHz
    pcm = _speech_like(12.0, rate=44100, seed=9)
    whole = local_audio.resample_int16_mono_numpy(pcm, 44100, 16000)

    conv = local_audio.NumpyAudioResampler(rate=16000)
    rng = random.Random(42)
    pieces = []
    pos = 0
    while pos < len(pcm):
        step = rng.randint(1, 3777)
        for frame in conv.resample(local_audio.PcmFrame(pcm[pos : pos + step], 44100)):
            pieces.append(frame.to_ndarray().reshape(-1))
        pos += step
    for frame in conv.resample(None):
        pieces.append(frame.to_ndarray().reshape(-1))
    streamed = np.concatenate(pieces)
    np.testing.assert_array_equal(streamed, whole)


def test_compressed_file_delegates_to_paired_host_without_pyav(
    without_pyav, tmp_path, monkeypatch
):
    fake_mp3 = tmp_path / "voice.mp3"
    fake_mp3.write_bytes(b"ID3compressed-audio-bytes")

    # Unpaired: clear actionable error
    monkeypatch.setattr("services.remote_asr.meeting_client.has_paired_host", lambda: False)
    with pytest.raises(RuntimeError, match="paired OpenWhisper host"):
        list(local_audio.windows(str(fake_mp3)))

    # Paired: delegates via file_decode_* RPC
    expected = np.linspace(-0.25, 0.25, 1600, dtype=np.float32)
    services = HostMeetingServices(
        file_decode_root=str(tmp_path / "staging"),
        windows_fn=lambda _p: [(0.0, expected)],
        probe_fn=lambda _p: (12.5, 44100, 2),
    )
    monkeypatch.setattr("services.remote_asr.meeting_client.has_paired_host", lambda: True)
    monkeypatch.setattr(
        "services.remote_asr.meeting_client._default_connect",
        lambda: _WireConnection(services),
    )
    wins = list(local_audio.windows(str(fake_mp3)))
    assert len(wins) == 1
    assert wins[0][0] == 0.0
    assert np.allclose(wins[0][1], expected, atol=1 / 32768)

    # AudioProcessor preview + split on compressed file without PyAV also routes to host!
    proc = AudioProcessor()
    preview = proc.preview_file(str(fake_mp3), engine_splits=False)
    assert preview.duration_seconds == pytest.approx(12.5)
    assert preview.sample_rate == 44100
    assert preview.channels == 2
    chunks = proc.split_audio_file(str(fake_mp3))
    try:
        assert len(chunks) == 1
    finally:
        proc.cleanup_temp_files()
    services.shutdown()


# -------------------------------------------- services/audio_processor.py


def test_audio_processor_preview_and_split_wav_without_pyav(
    without_pyav, tmp_path, monkeypatch
):
    proc = AudioProcessor()
    rate = 8000
    samples = np.tile(np.array([-32768, -10000, 0, 10000, 32767], dtype=np.int16), 4000)
    wav_path = str(tmp_path / "pcm.wav")
    with wave.open(wav_path, "wb") as wf:
        wf.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        wf.writeframes(samples.tobytes())

    # Preview reads WAV header only without decoding
    proc._load_audio_metadata = lambda *_: pytest.fail("should not decode for preview")
    preview = proc.preview_file(wav_path, engine_splits=False)
    assert preview.sample_rate == rate
    assert preview.channels == 1
    assert preview.duration_seconds == pytest.approx(len(samples) / rate, abs=0.01)

    # Splitting preserves exact PCM samples
    monkeypatch.setattr(config, "MAX_FILE_SIZE_MB", 0.01)
    monkeypatch.setattr(config, "OVERLAP_DURATION_SEC", 0)
    chunks = proc.split_audio_file(wav_path)
    decoded = []
    try:
        assert len(chunks) > 1
        for chunk in chunks:
            with wave.open(chunk, "rb") as wf:
                decoded.append(np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2"))
        np.testing.assert_array_equal(np.concatenate(decoded), samples)
    finally:
        proc.cleanup_temp_files()

    # Corrupt file raises UnreadableAudioError cleanly
    bad = tmp_path / "broken.wav"
    bad.write_bytes(b"not audio at all")
    monkeypatch.setattr("services.remote_asr.meeting_client.has_paired_host", lambda: False)
    with pytest.raises(UnreadableAudioError):
        proc.preview_file(str(bad), engine_splits=False)


# -------------------------------------- services/incremental_dictation.py


def test_incremental_dictation_matches_saved_wav_bit_for_bit_without_pyav(
    without_pyav, tmp_path
):
    backend = LocalSpeechBackend("parakeet")
    backend.is_available = lambda: True
    decoded_digests = []

    def fake_recognize(audio, language=None):
        decoded_digests.append(len(audio))
        return {"text": f"w{len(decoded_digests)}", "segments": []}

    backend._recognize = fake_recognize

    out_wav = str(tmp_path / "recorded.wav")
    recorder = AudioRecorder(output_file=out_wav)
    recorder._audio_spool = RecordingJournal(
        recorder.output_file, recorder.rate, recorder.channels, 2, recorder._fail_capture
    )
    controller = SimpleNamespace(
        current_backend=backend, recorder=recorder, is_meeting_active=lambda: False
    )
    try:
        session = DictationSession(controller, backend, recorder)
        pcm = _speech_like(66.0, rate=recorder.rate, seed=7)
        recorder.is_recording = True
        rng = random.Random(13)
        pos = 0
        blocks = 0
        while pos < len(pcm):
            size = rng.randint(256, 4096)
            chunk = pcm[pos : pos + size]
            pos += size
            recorder._audio_callback(chunk.reshape(-1, 1), len(chunk), None, None)
            blocks += 1
            if blocks % 30 == 0:
                recorder._audio_spool._queue.join()
                session.poll()
        recorder.is_recording = False
        recorder._audio_spool._queue.join()
        session.poll()
        assert recorder.save_recording()
        assert session.early_windows >= 1

        text = session.finish(out_wav)
        # Reused early decodes cleanly because NumpyAudioResampler matched windows(out_wav) bit-for-bit!
        assert text is not None
        assert session._invalid is None
        assert len(list(local_audio.windows(out_wav))) == len(decoded_digests)
    finally:
        recorder.cleanup()


# ----------------------------------------- services/echo_cancellation.py


def test_echo_cancellation_resampling_without_pyav(without_pyav):
    sr_in = 48000
    block_a = np.full(sr_in, 5000, dtype=np.int16)
    block_paused = np.full(sr_in, 20000, dtype=np.int16)
    block_b = np.full(sr_in, -5000, dtype=np.int16)
    blocks = (
        aec.ReferenceBlock(t_mono=10.0, sample_rate=sr_in, frames=block_a),
        aec.ReferenceBlock(t_mono=11.0, sample_rate=sr_in, frames=block_paused),
        aec.ReferenceBlock(t_mono=12.0, sample_rate=sr_in, frames=block_b),
    )
    ref_16k = aec.build_aligned_reference_16k(
        blocks,
        target_samples=32000,
        mic_start_mono=10.0,
        pause_spans=((11.0, 12.0),),
    )
    assert ref_16k.shape == (32000,)
    assert np.mean(ref_16k[1000:15000]) > 4500
    assert np.mean(ref_16k[17000:31000]) < -4500

    # Direct resample_int16_mono works for 44.1 kHz -> 16 kHz too
    t = np.arange(44100) / 44100.0
    sine = (np.sin(2 * np.pi * 440.0 * t) * 12000).astype(np.int16)
    out = aec.resample_int16_mono(sine, 44100, 16000)
    assert out.dtype == np.int16
    assert abs(len(out) - 16000) <= 1
    assert 10000 < int(np.max(np.abs(out))) <= 12500

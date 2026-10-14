"""Qwen skips non-speech without changing the other engines' input paths."""
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from services.local_asr import qwen_speech
from transcriber.optional_backend import LocalSpeechBackend


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setattr(LocalSpeechBackend, "_settings", staticmethod(lambda: {}))
    backend = LocalSpeechBackend("qwen_asr")
    backend._process = SimpleNamespace(
        request=Mock(return_value=dict(text="invented", segments=[])),
        process=SimpleNamespace(poll=lambda: None),
    )
    backend.model = object()
    return backend


@pytest.mark.parametrize("rms_db,peak_db", [(-67, -43), (-67, -53), (-63, -33)])
def test_real_detector_rejects_quiet_noise_with_peaks_that_pass_old_gate(backend, rms_db, peak_db):
    audio = np.random.default_rng(49).normal(0, 10 ** (rms_db / 20), 25 * 16000).astype(np.float32)
    audio[16000] = 10 ** (peak_db / 20)
    assert np.max(np.abs(audio)) > .00025
    assert backend._transcribe_audio(audio) == dict(text="", segments=[])
    backend._process.request.assert_not_called()


@pytest.mark.parametrize("path", ["recognize", "decode_window", "preview_audio", "transcribe_windows", "meeting"])
def test_every_qwen_decode_path_skips_non_speech(backend, monkeypatch, path):
    from transcriber.optional_backend import SpeechDecoder

    monkeypatch.setattr(qwen_speech, "has_speech", lambda audio: False)
    audio = np.full(16000, .001, np.float32)
    if path == "transcribe_windows":
        assert backend.transcribe_windows([(0, audio)]) == [""]
    elif path == "meeting":
        segments, _ = SpeechDecoder(backend).transcribe(audio)
        assert list(segments) == []
    else:
        result = getattr(backend, path)(audio)
        assert result == ("" if path == "decode_window" else dict(text="", segments=[]))
    backend._process.request.assert_not_called()


def test_speech_window_is_sent_intact_with_language_and_no_timestamp_changes(backend, monkeypatch):
    # One quiet word in a long window must not be rejected by its average RMS.
    audio = np.zeros(25 * 16000, np.float32)
    audio[12 * 16000:12 * 16000 + 2000] = .002
    monkeypatch.setattr(qwen_speech, "has_speech", lambda samples: True)
    expected = dict(text="yes", segments=[dict(text="yes", start=0., end=25.)])

    def request(op, audio_path, **options):
        assert op == "transcribe" and options["language"] == "fr"
        np.testing.assert_array_equal(np.fromfile(audio_path, dtype=np.float32), audio)
        return expected

    backend._process.request.side_effect = request
    assert backend.recognize(audio, "fr") is expected


@pytest.mark.parametrize("family", ["parakeet", "parakeet_mlx", "nemotron", "moonshine"])
def test_other_engines_never_run_the_qwen_detector(backend, monkeypatch, family):
    backend.backend_id = family
    detector = Mock(side_effect=AssertionError("Qwen filter reached another engine"))
    monkeypatch.setattr(qwen_speech, "has_speech", detector)
    audio = np.full(16000, .001, np.float32)
    assert backend._transcribe_audio(audio)["text"] == "invented"
    backend._process.request.assert_called_once()
    detector.assert_not_called()


def test_detector_failure_preserves_transcription(backend, monkeypatch, caplog):
    monkeypatch.setattr(qwen_speech, "has_speech", Mock(side_effect=RuntimeError("broken ONNX model")))
    assert backend.recognize(np.ones(16000, np.float32))["text"] == "invented"
    backend._process.request.assert_called_once()
    assert "Qwen speech detection failed" in caplog.text


def test_silence_cannot_hide_unloaded_or_canceled_backend(backend, monkeypatch):
    detector = Mock(return_value=False)
    monkeypatch.setattr(qwen_speech, "has_speech", detector)
    backend._process = None
    with pytest.raises(RuntimeError, match="not loaded"):
        backend.recognize(np.zeros(16000, np.float32))
    backend.should_cancel = True
    with pytest.raises(RuntimeError, match="canceled"):
        backend.recognize(np.zeros(16000, np.float32))
    detector.assert_not_called()


@pytest.mark.parametrize("fails", [False, True])
def test_cancel_during_detection_still_cancels(backend, monkeypatch, fails):
    def detect(audio):
        backend.should_cancel = True
        if fails:
            raise RuntimeError("detector interrupted")
        return False

    monkeypatch.setattr(qwen_speech, "has_speech", detect)
    with pytest.raises(RuntimeError, match="canceled"):
        backend.recognize(np.ones(16000, np.float32))
    backend._process.request.assert_not_called()


def test_upload_file_skips_quiet_noise_after_resampling(backend, tmp_path):
    import wave

    audio = np.random.default_rng(49).normal(0, .00045, 3 * 16000)
    audio[16000] = .007
    path = tmp_path / "call.wav"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes((audio * 32768).astype(np.int16).tobytes())
    assert backend.transcribe(str(path)) == ""
    backend._process.request.assert_not_called()


def test_detector_keeps_short_speech_and_does_not_modify_audio(monkeypatch):
    from faster_whisper import vad

    audio = np.zeros(48000, np.float32)
    audio[16000:16800] = .003
    original = audio.copy()

    def detect(samples, vad_options):
        np.testing.assert_array_equal(samples, original)
        assert vad_options.min_speech_duration_ms == 0
        assert vad_options.min_silence_duration_ms == 500
        return [dict(start=16000, end=16800)]

    monkeypatch.setattr(vad, "get_speech_timestamps", detect)
    assert qwen_speech.has_speech(audio)
    np.testing.assert_array_equal(audio, original)
    assert not qwen_speech.has_speech(np.array([], np.float32))

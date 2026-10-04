"""Opt-in real inference on Apple Silicon with the pinned downloads installed.

OPENWHISPER_TEST_MLX=1 .venv/bin/python -m pytest tests/test_parakeet_mlx_hardware.py -q

Run outside a sandbox that denies Metal or Apple's speech synthesis service.
Fixtures are synthesized locally; no private recording or download is needed.
"""

import os
import platform
import subprocess
import sys
import threading
import time

import numpy as np
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("OPENWHISPER_TEST_MLX") != "1"
    or sys.platform != "darwin"
    or platform.machine().lower() != "arm64"
    or sys.version_info[:2] != (3, 12),
    reason="Opt-in Apple Silicon/Python 3.12 test; requires installed MLX runtime and weights",
)


@pytest.fixture(scope="module")
def speech_fixtures(tmp_path_factory):
    from faster_whisper.audio import decode_audio

    root = tmp_path_factory.mktemp("mlx-speech")
    clips = {}
    for language, voice, text in (
        ("en", "Samantha", "Today we are testing local speech recognition on an Apple Silicon Mac."),
        ("es", "Mónica", "Hoy estamos probando el reconocimiento de voz en un ordenador de Apple."),
    ):
        path = root / f"{language}.aiff"
        subprocess.run(["say", "-v", voice, "-o", str(path), text], check=True, timeout=30)
        audio = decode_audio(str(path), sampling_rate=16000)
        assert len(audio) > 16000 and np.max(np.abs(audio)) > .01
        clips[language] = (str(path), audio)
    return clips


@pytest.fixture(params=["auto", "cpu"])
def backend(request, monkeypatch):
    from services import components
    from services.local_asr import cache
    from transcriber.optional_backend import LocalSpeechBackend

    assert components.is_installed("asr-parakeet-mlx"), "Install MLX runtime in Downloads first"
    assert cache.is_cached("parakeet-v3-mlx"), "Install MLX model in Downloads first"
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    engine = LocalSpeechBackend("parakeet_mlx", device=request.param)
    engine.reload_model()
    assert engine.is_available()
    assert engine.device == ("metal" if request.param == "auto" else "cpu")
    worker = engine._process.process
    yield engine
    engine.cleanup()
    assert worker.poll() is not None


def test_real_speech_upload_preview_and_timestamps(backend, speech_fixtures):
    for language, phrase in (("en", "local speech recognition"), ("es", "reconocimiento de voz")):
        path, audio = speech_fixtures[language]
        result = backend.recognize(audio)
        assert phrase in result["text"].lower()
        assert result["segments"]
        assert all(0 <= s["start"] <= s["end"] <= len(audio)/16000 for s in result["segments"])
        assert phrase in backend.transcribe(path).lower()
        assert phrase in backend.preview_audio(audio)["text"].lower()


def test_real_short_audio_and_silence(backend):
    for size in (0, 1, 80, 16000):
        result = backend._recognize(np.zeros(size, dtype=np.float32))
        assert all(0 <= s["start"] <= s["end"] <= size/16000 for s in result["segments"])
    # Product paths reject digital silence before decoding to prevent hallucinations.
    assert backend.preview_audio(np.zeros(16000, dtype=np.float32))["text"] == ""


def test_real_cancellation_and_reload(backend, speech_fixtures):
    audio = np.tile(speech_fixtures["en"][1], 30)
    worker = backend._process.process
    errors = []
    started = threading.Event()

    def decode():
        started.set()
        try:
            backend.transcribe_windows([(0, audio)])
        except RuntimeError as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=decode)
    thread.start()
    assert started.wait(2)
    time.sleep(.05)
    backend.cancel_transcription()
    thread.join(timeout=3)
    assert not thread.is_alive() and worker.poll() is not None
    assert errors
    backend.reload_model()
    assert "local speech recognition" in backend.transcribe(speech_fixtures["en"][0]).lower()

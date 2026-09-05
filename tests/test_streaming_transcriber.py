"""The preview worker decodes each window with whichever engine is loaded."""
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from config import config
from services.streaming_transcriber import StreamingTranscriber
from transcriber.optional_backend import LocalSpeechBackend, SpeechDecoder


class _Decoder:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio, **options):
        self.calls.append((len(audio), options))
        segments = [SimpleNamespace(text=" hello"), SimpleNamespace(text="there ")]
        return iter(segments), SimpleNamespace(language="en")


def _preview(backend):
    preview = StreamingTranscriber(backend, chunk_duration_sec=3.0, overlap_sec=0.75)
    preview.sample_rate = config.SAMPLE_RATE
    preview.updates = []
    preview.callback = lambda text, final: preview.updates.append((text, final))
    return preview


def _tone(seconds):
    t = np.arange(int(config.SAMPLE_RATE * seconds)) / config.SAMPLE_RATE
    return (np.sin(2 * np.pi * 220 * t) * 8000).astype(np.int16)


def test_window_decode_uses_the_backend_decoder_and_appends_text():
    decoder = _Decoder()
    preview = _preview(SimpleNamespace(model=decoder))

    preview._process_incremental_chunk([_tone(3.0)])
    preview._process_incremental_chunk([_tone(3.0)])

    assert preview.preview_text == "hello there hello there"
    assert preview.updates[-1] == ("hello there hello there", True)
    assert preview._chunk_count == 2
    first, second = decoder.calls
    assert first == (config.WHISPER_TARGET_SAMPLE_RATE * 3, {"beam_size": 1, "vad_filter": False})
    # The second window carries the 0.75 s overlap tail in front of new audio.
    assert second[0] == int(config.WHISPER_TARGET_SAMPLE_RATE * 3.75)


def test_window_is_skipped_while_the_engine_is_unloaded():
    preview = _preview(SimpleNamespace(model=None))

    preview._process_incremental_chunk([_tone(0.5)])

    assert preview.preview_text == ""
    assert preview._chunk_count == 0
    assert preview.updates == []


def test_optional_engine_worker_decodes_preview_windows(monkeypatch):
    monkeypatch.setattr(LocalSpeechBackend, "_settings", staticmethod(lambda: {}))
    backend = LocalSpeechBackend("parakeet", device="cpu")
    process = Mock()
    process.process.poll.return_value = None
    process.request.return_value = {
        "text": "mister quilter",
        "segments": [{"text": "mister quilter", "start": 0.0, "end": 1.2}],
    }
    backend._process, backend.model = process, SpeechDecoder(backend)
    preview = _preview(backend)

    preview._process_incremental_chunk([_tone(3.0)])

    assert preview.preview_text == "mister quilter"
    assert preview.updates == [("mister quilter", True)]
    (op,), request = process.request.call_args
    assert op == "transcribe"
    assert request["language"] == "auto"
    assert not backend.is_transcribing


def test_quiet_window_never_reaches_the_optional_engine_worker(monkeypatch):
    monkeypatch.setattr(LocalSpeechBackend, "_settings", staticmethod(lambda: {}))
    backend = LocalSpeechBackend("parakeet", device="cpu")
    process = Mock()
    backend._process, backend.model = process, SpeechDecoder(backend)
    preview = _preview(backend)

    preview._process_incremental_chunk([np.zeros(config.SAMPLE_RATE * 3, dtype=np.int16)])

    process.request.assert_not_called()
    assert preview.preview_text == ""
    assert preview._chunk_count == 1

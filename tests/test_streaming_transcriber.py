"""The preview worker decodes each window with whichever engine is loaded."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from config import config
from services.streaming_transcriber import (
    NativePreviewLedger,
    NativeStreamingTranscriber,
    StreamingTranscriber,
)
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


class _Stream:
    """Fake engine stream: interim text replaces, finish commits what was heard."""

    def __init__(self, fail_after=None):
        self.calls = []
        self.canceled = []
        self.fail_after = fail_after
        self.ready = threading.Event()

    def stream_audio(self, session, audio, language=None, *, finish=False):
        self.calls.append((session, len(audio), language, finish))
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise RuntimeError("Speech engine is not loaded")
        pushes = sum(1 for _s, n, _l, _f in self.calls if n)
        heard = " ".join(["word"] * pushes)
        if finish:
            events = [dict(text=heard + ".", final=True)] if heard else []
        else:
            events = [dict(text=heard, final=False)]
        self.ready.set()
        return events

    def cancel_stream(self, session):
        self.canceled.append(session)


def _native(backend, interval=0.75):
    preview = NativeStreamingTranscriber(backend, update_interval_sec=interval)
    preview.updates = []
    preview.start_streaming(config.SAMPLE_RATE, lambda text, final: preview.updates.append((text, final)))
    return preview


def test_native_stream_pushes_each_interval_and_finishes_on_stop():
    stream = _Stream()
    preview = _native(stream)

    # Two 0.4 s recorder blocks cross the 0.75 s threshold together; the third waits.
    for _ in range(3):
        preview.feed_audio(_tone(0.4))
    assert stream.ready.wait(3)
    text = preview.stop_streaming()

    assert text == "word word."
    sessions = {session for session, *_ in stream.calls}
    assert sessions == {NativeStreamingTranscriber.SESSION}
    pushes = [(n, lang, f) for _s, n, lang, f in stream.calls]
    # First push: 0.8 s resampled to 16 kHz, automatic language, no overlap.
    assert pushes[0] == (int(config.WHISPER_TARGET_SAMPLE_RATE * 0.8), "auto", False)
    # Stop flushes the leftover 0.4 s with finish=True so the last utterance commits.
    assert pushes[-1][0] == int(config.WHISPER_TARGET_SAMPLE_RATE * 0.4)
    assert pushes[-1][2] is True
    assert preview.updates[0] == ("word", True)
    assert preview.updates[-1] == ("word word.", True)
    assert stream.canceled == []
    assert not preview._session_open


def test_native_stream_keeps_silence_flowing_to_the_engine():
    stream = _Stream()
    preview = _native(stream)

    preview.feed_audio(np.zeros(config.SAMPLE_RATE, dtype=np.int16))
    assert stream.ready.wait(3)
    preview.stop_streaming()

    # Unlike the window preview, quiet audio reaches the stream: its endpointing needs it.
    assert stream.calls[0][1] == config.WHISPER_TARGET_SAMPLE_RATE


def test_native_stream_without_audio_never_opens_a_session():
    stream = _Stream()
    preview = _native(stream)

    assert preview.stop_streaming() == ""
    assert stream.calls == []
    assert stream.canceled == []


def test_native_stream_failure_cancels_the_session_and_stops_pushing():
    stream = _Stream(fail_after=1)
    preview = _native(stream)

    preview.feed_audio(_tone(0.8))
    assert stream.ready.wait(3)
    stream.ready.clear()
    preview.feed_audio(_tone(0.8))  # raises inside the worker
    preview.feed_audio(_tone(0.8))  # skipped: the stream is marked failed
    text = preview.stop_streaming()

    assert text == "word"
    assert len(stream.calls) == 2
    assert stream.canceled == [NativeStreamingTranscriber.SESSION]
    assert not preview._session_open


def test_native_restart_drops_a_session_the_engine_still_holds():
    stream = _Stream()
    preview = _native(stream)
    preview.feed_audio(_tone(0.8))
    assert stream.ready.wait(3)
    preview.stop_streaming()
    preview._session_open = True  # as if the finish push never reached the worker

    preview.start_streaming(config.SAMPLE_RATE, lambda *_: None)
    preview.stop_streaming()

    assert stream.canceled == [NativeStreamingTranscriber.SESSION]
    assert preview.preview_text == ""


def test_native_ledger_replaces_interims_and_commits_finals_once():
    ledger = NativePreviewLedger()
    assert ledger.apply([dict(text="hello", final=False)]) == "hello"
    assert ledger.apply([dict(text="hello world", final=False)]) == "hello world"
    assert ledger.apply([dict(text="hello world.", final=True)]) == "hello world."
    assert ledger.apply([dict(text="", final=True)]) == "hello world."
    assert ledger.apply([dict(text="next", final=False)]) == "hello world. next"
    assert ledger.apply(None) == "hello world. next"


def test_native_ledger_orders_moonshine_lines_by_start_without_duplicates():
    ledger = NativePreviewLedger()
    ledger.apply([dict(id="b", text="three", start=1.0, final=False)])
    events = [dict(id="a", text="one two", start=0.0, final=True),
              dict(id="b", text="three", start=1.0, final=False)]
    assert ledger.apply(events) == "one two three"
    assert ledger.apply(events) == "one two three"

"""The preview worker decodes each window with whichever engine is loaded."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import numpy as np

from config import config
from services.streaming_transcriber import (
    NativePreviewLedger,
    NativeStreamingTranscriber,
    StreamingTranscriber,
    append_preview_text,
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


@pytest.mark.parametrize("before,chunk,maximum,expected", [
    ("Meet the Alpha team.", "alpha TEAM, tomorrow morning.", 6, "Meet the Alpha team. tomorrow morning."),
    ("one two three", "two three four", 6, "one two three four"),
    ("very", "very good", 6, "very very good"),
    ("yes yes", "yes yes", 6, "yes yes yes yes"),
    ("one two", "one two three", 0, "one two one two three"),
    ("we don't know", "don’t know yet", 6, "we don't know yet"),
    ("one two three", "one two three four", 2, "one two three one two three four"),
    ("one two", "", 6, "one two"),
])
def test_overlap_join_is_bounded_and_preserves_ambiguous_repetitions(before, chunk, maximum, expected):
    assert append_preview_text(before, chunk, max_overlap_words=maximum) == expected


def test_window_overlap_removes_redecoded_phrase_but_zero_overlap_preserves_it():
    for overlap, expected in ((.75, "one two three four"), (0, "one two three two three four")):
        texts = iter(["one two three", "two three four"])
        model = SimpleNamespace(transcribe=lambda *a, **k: (
            iter([SimpleNamespace(text=next(texts))]), None))
        preview = _preview(SimpleNamespace(model=model))
        preview.overlap_sec = overlap
        preview._process_incremental_chunk([_tone(3)])
        preview._process_incremental_chunk([_tone(3)])
        assert preview.preview_text == expected


def test_window_stop_flushes_recording_shorter_than_one_window():
    preview = StreamingTranscriber(SimpleNamespace(model=_Decoder()))
    updates = []
    preview.start_streaming(config.SAMPLE_RATE, lambda *args: updates.append(args))
    preview.feed_audio(_tone(.1))
    assert preview.stop_streaming() == "hello there"
    assert updates == [("hello there", True)]
    assert preview._chunk_count == 1


def test_window_stop_consumes_inflight_generator_and_partial_tail():
    entered, release = threading.Event(), threading.Event()
    calls = []
    def transcribe(audio, **options):
        calls.append(len(audio))
        def segments():
            if len(calls) == 1:
                entered.set()
                assert release.wait(3)
                yield SimpleNamespace(text="first window")
            else:
                yield SimpleNamespace(text="last word")
        return segments(), None
    preview = StreamingTranscriber(SimpleNamespace(model=SimpleNamespace(transcribe=transcribe)), overlap_sec=0)
    preview.start_streaming(config.SAMPLE_RATE, lambda *_: None)
    preview.feed_audio(_tone(3))
    assert entered.wait(3)
    preview.feed_audio(_tone(.1))
    # Request stop while the lazy model result is in flight.
    preview._stop_requested = True
    release.set()
    assert preview.stop_streaming() == "first window last word"
    assert len(calls) == 2


def test_preview_buffer_absorbs_a_decode_stall_without_dropping_recorder_blocks():
    entered, release = threading.Event(), threading.Event()
    def transcribe(audio, **options):
        entered.set()
        assert release.wait(3)
        return iter([SimpleNamespace(text="speech")]), None
    preview = StreamingTranscriber(SimpleNamespace(model=SimpleNamespace(transcribe=transcribe)))
    preview.start_streaming(config.SAMPLE_RATE, lambda *_: None)
    preview.feed_audio(_tone(3))
    assert entered.wait(3)
    for _ in range(40):
        preview.feed_audio(np.zeros(config.CHUNK_SIZE, np.int16))
    assert preview.audio_queue.qsize() == 40
    assert preview.audio_queue.maxsize * config.CHUNK_SIZE >= 3 * config.SAMPLE_RATE
    release.set()
    preview.stop_streaming()


def test_timed_out_worker_cannot_publish_late_or_join_the_next_recording(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def transcribe(audio, **options):
        entered.set()
        assert release.wait(3)
        return iter([SimpleNamespace(text="late old text")]), None
    preview = StreamingTranscriber(SimpleNamespace(model=SimpleNamespace(transcribe=transcribe)))
    updates = []
    preview.start_streaming(config.SAMPLE_RATE, lambda *args: updates.append(args))
    preview.feed_audio(_tone(3))
    assert entered.wait(3)
    worker = preview.worker_thread
    join = worker.join
    monkeypatch.setattr(worker, "join", lambda timeout=None: None)
    assert preview.stop_streaming() == ""
    preview.start_streaming(config.SAMPLE_RATE, lambda *args: updates.append(args))
    assert preview.worker_thread is worker
    assert not preview.is_streaming
    release.set()
    join(3)
    assert not worker.is_alive()
    assert updates == [] and preview.preview_text == ""
    preview.backend = SimpleNamespace(model=_Decoder())
    preview.start_streaming(config.SAMPLE_RATE, lambda *args: updates.append(args))
    preview.feed_audio(_tone(.1))
    assert preview.stop_streaming() == "hello there"


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("action", ["cancel", "timeout"])
def test_abandoned_preview_suppresses_late_output_and_releases_native_session(monkeypatch, native, action):
    entered, release = threading.Event(), threading.Event()
    stream = _Stream()
    def decode(audio, **options):
        entered.set()
        assert release.wait(3)
        return iter([SimpleNamespace(text="old recording")]), None
    def push(session, audio, language=None, *, finish=False):
        entered.set()
        assert release.wait(3)
        return stream.stream_audio(session, audio, language, finish=finish)
    backend = SimpleNamespace(stream_audio=push, cancel_stream=stream.cancel_stream,
                              model=SimpleNamespace(transcribe=decode))
    preview = NativeStreamingTranscriber(backend) if native else StreamingTranscriber(backend)
    updates = []
    preview.start_streaming(config.SAMPLE_RATE, lambda *args: updates.append(args))
    worker = preview.worker_thread
    join = worker.join
    try:
        preview.feed_audio(_tone(3))
        assert entered.wait(3)
        if action == "timeout":
            monkeypatch.setattr(worker, "join", lambda timeout=None: None)
            assert preview.stop_streaming() == ""
        else:
            preview.cancel_streaming()
        # A second recording cannot share the old decoder while it is blocked.
        preview.start_streaming(config.SAMPLE_RATE, lambda *args: updates.append(args))
        assert preview.worker_thread is worker
        assert not preview.is_streaming
    finally:
        release.set()
        join(3)
    assert not worker.is_alive()
    assert updates == [] and preview.preview_text == ""
    assert stream.canceled == ([preview.SESSION] if native else [])
    stream.calls.clear()  # The fake counts pushes; a canceled engine starts a fresh session.
    preview.start_streaming(config.SAMPLE_RATE, lambda *args: updates.append(args))
    preview.feed_audio(_tone(.1))
    assert preview.stop_streaming() == ("word." if native else "old recording")


def test_runtime_stop_mutes_updates_but_preserves_postroll_until_worker_drains():
    from services.runtime.streaming import StreamingRuntime
    events = []
    controller = SimpleNamespace(
        recorder=SimpleNamespace(set_streaming_callback=lambda callback: events.append(("callback", callback))),
        streaming_transcriber=SimpleNamespace(stop_streaming=lambda: events.append(("stop", None)) or "last word"),
        partial_transcription=Mock(), streaming_text_update=Mock(), _streaming_enabled=True)
    runtime = StreamingRuntime(controller)
    runtime.begin_stop_streaming_session()
    runtime.on_partial_transcription("tail", True)
    assert events == []  # post-roll still feeds the decoder
    controller.partial_transcription.emit.assert_not_called()
    controller.streaming_text_update.emit.assert_not_called()
    assert runtime.stop_streaming_session() == "last word"
    assert events == [("callback", None), ("stop", None)]


def test_runtime_cancel_detaches_recorder_and_uses_nonblocking_preview_cancel():
    from services.runtime.streaming import StreamingRuntime
    events = []
    controller = SimpleNamespace(
        recorder=SimpleNamespace(set_streaming_callback=lambda callback: events.append(("callback", callback))),
        streaming_transcriber=SimpleNamespace(
            stop_streaming=Mock(side_effect=AssertionError("cancel must not drain")),
            cancel_streaming=lambda: events.append(("cancel", None))),
        streaming_overlay_hide=Mock(), _streaming_enabled=True)
    runtime = StreamingRuntime(controller)
    runtime.cancel_streaming_session()
    assert events == [("callback", None), ("cancel", None)]
    controller.streaming_overlay_hide.emit.assert_called_once_with()

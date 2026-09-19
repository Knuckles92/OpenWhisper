"""Preview load shedding and recording isolation, without loading ASR models."""
import queue
import threading
from types import SimpleNamespace

import numpy as np

from meeting.asr.preview import WindowSpeechPreview
from meeting.interfaces import CaptureBlock
from transcriber.optional_backend import LocalSpeechBackend


def harness(busy=lambda: False):
    # Drive the consumer deterministically without a worker timing dependency.
    worker = WindowSpeechPreview.__new__(WindowSpeechPreview)
    calls, events = [], []
    worker.backend = SimpleNamespace(preview_audio=lambda samples, *args, **kwargs:
                                     calls.append(samples.copy()) or {"text": "Assistant, note that."})
    worker.callback, worker.busy, worker.language = events.append, busy, "auto"
    worker._sessions, worker._next_decode = {}, 0
    worker._stop = threading.Event()
    worker._queue = queue.Queue(maxsize=2)
    return worker, calls, events


def block(start, channel="mic"):
    return CaptureBlock(channel, np.full(16000, 1000, dtype=np.int16), 16000, 100+start)


def test_one_second_cadence_and_bounded_audio_window():
    worker, calls, events = harness()
    for i in range(20):
        worker._accept(block(i), i)
        worker._next_decode = 0
        worker._decode()
    assert len(calls) == 20
    assert max(len(samples) for samples in calls) == 8*16000
    assert events[-1]["start_s"] == 12 and events[-1]["end_s"] == 20


def test_busy_durable_asr_skips_inference_and_retains_latest_window():
    busy = [True]
    worker, calls, events = harness(lambda: busy[0])
    for i in range(20):
        worker._accept(block(i), i)
        worker._decode()
    assert not calls
    busy[0] = False
    worker._decode()
    assert len(calls) == 1 and events[0]["end_s"] == 20


def test_audio_gap_and_pause_reset_window():
    worker, _, events = harness()
    worker._accept(block(0), 0)
    worker._accept(block(4), 4)
    worker._decode()
    assert events[-1]["start_s"] == 4
    worker._next_decode = 0
    worker._accept(block(20), 5)  # Meeting clock is contiguous; wall clock has a pause.
    worker._decode()
    assert events[-1]["start_s"] == 5


def test_preview_overflow_does_not_modify_capture_buffers_or_block():
    worker, _, _ = harness()
    original = block(0)
    for _ in range(10):
        worker.feed(original, 0)
    assert worker._queue.qsize() == 2
    copied, _ = worker._queue.get_nowait()
    copied.frames[:] = 0
    assert np.all(original.frames == 1000)


def test_backend_preview_never_waits_for_model_and_rechecks_durable_priority():
    backend = LocalSpeechBackend.__new__(LocalSpeechBackend)
    backend._decode_lock = threading.Lock()
    calls = []
    backend._transcribe_audio = lambda *args: calls.append(args) or {"text": "hello"}
    audio = np.ones(16000, np.float32)
    backend._decode_lock.acquire()
    assert backend.preview_audio(audio) is None
    backend._decode_lock.release()
    checks = iter([False, True])
    assert backend.preview_audio(audio, busy=lambda: next(checks)) is None
    assert not calls
    assert backend.preview_audio(audio)["text"] == "hello"
    assert not backend._decode_lock.locked()


def test_engine_records_before_preview_even_if_preview_fails():
    from meeting.engine import MeetingEngine
    calls = []
    def failed_preview(*args):
        calls.append("preview")
        raise RuntimeError("preview unavailable")
    engine = MeetingEngine.__new__(MeetingEngine)
    engine._active = True
    engine.clock = SimpleNamespace(is_paused=False, meeting_time=lambda value: value)
    engine._asr = SimpleNamespace(feed_preview=failed_preview)
    spool = SimpleNamespace(feed=lambda value: calls.append("record"))
    route = engine._make_block_router("mic", spool)
    route(block(0))
    route(block(1))
    assert calls == ["record", "preview", "record", "preview"]


def test_engine_preview_feeds_listener_without_committing_transcript():
    from meeting.engine import MeetingEngine
    engine = MeetingEngine.__new__(MeetingEngine)
    broadcasts, observed = [], []
    engine._active = True
    engine.clock = SimpleNamespace(is_paused=False)
    engine._preview_frontiers = {"mic": 2}
    engine._broadcast = broadcasts.append
    engine._voice_command_listener = lambda: SimpleNamespace(observe_preview=observed.append)
    payload = dict(channel="mic", start_s=2, end_s=3, text="Assistant, note that.", final=False)
    engine._on_speech_preview(payload)
    assert observed == [payload] and broadcasts[0]["type"] == "speech_preview"
    engine.clock.is_paused = True
    engine._on_speech_preview(dict(payload, end_s=4))
    assert len(observed) == 1

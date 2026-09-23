"""The window preview's stop leaves its tail undecoded until the fallback needs it.

Stopping used to drain the unfinished window through the shared engine before
the final decode could start. Now stop returns at once, the tail is kept, and
``finalize_preview`` decodes it only for an empty final transcript.
"""
import queue
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from config import config
from services.streaming_transcriber import StreamingTranscriber

TARGET = config.WHISPER_TARGET_SAMPLE_RATE


def _blocks(seconds, seed=0):
    """Recorder-sized int16 noise blocks, distinct per seed."""
    rng = np.random.default_rng(seed)
    count = int(round(seconds * config.SAMPLE_RATE / config.CHUNK_SIZE))
    return [(rng.standard_normal(config.CHUNK_SIZE) * 3000).astype(np.int16)
            for _ in range(count)]


class _Decoder:
    """Text that depends on the window's content, so any change in window
    boundaries or overlap tails changes the preview. Can stall one call."""

    def __init__(self, stall_on=None):
        self.calls = []
        self.stall_on = stall_on
        self.entered = threading.Event()
        self.release = threading.Event()

    def transcribe(self, audio, **options):
        self.calls.append(len(audio))
        if len(self.calls) == self.stall_on:
            self.entered.set()
            assert self.release.wait(5)
        checksum = int(abs(float(np.sum(audio[:4000]))) * 1e4) % 100003
        text = f"n{len(audio)} c{checksum}"
        return iter([SimpleNamespace(text=text)]), None


def _preview(decoder, *, poll=None):
    preview = StreamingTranscriber(SimpleNamespace(model=decoder),
                                   chunk_duration_sec=3.0, overlap_sec=0.75)
    if poll is not None:
        preview._POLL_SEC = poll
    preview.updates = []
    preview.start_streaming(config.SAMPLE_RATE, lambda *args: preview.updates.append(args))
    return preview


def _feed(preview, blocks):
    for block in blocks:
        preview.feed_audio(block)


def _wait_until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.002)


def _old_drain(blocks):
    """What the old stop returned: every accepted block through the worker's
    windowing (a window per 3 s, then the remainder), no blocks dropped."""
    reference = StreamingTranscriber(SimpleNamespace(model=_Decoder()),
                                     chunk_duration_sec=3.0, overlap_sec=0.75)
    reference.sample_rate = config.SAMPLE_RATE
    window, duration = [], 0.0
    for block in blocks:
        window.append(block)
        duration += len(block) / config.SAMPLE_RATE
        if duration >= reference.chunk_duration_sec:
            reference._process_incremental_chunk(window)
            window, duration = [], 0.0
    if window:
        reference._process_incremental_chunk(window)
    return reference.preview_text.strip()


def test_fast_stop_returns_without_decoding_and_wakes_the_worker():
    decoder = _Decoder()
    # A poll this long proves the stop marker, not the timeout, wakes the worker.
    preview = _preview(decoder, poll=30.0)
    blocks = _blocks(1.0)
    _feed(preview, blocks)
    _wait_until(preview.audio_queue.empty)
    worker = preview.worker_thread

    assert preview.stop_streaming() == ""
    worker.join(5)

    assert not worker.is_alive()
    assert decoder.calls == []
    assert preview.finalize_preview() == _old_drain(blocks)
    assert decoder.calls == [int(len(blocks) * config.CHUNK_SIZE * TARGET / config.SAMPLE_RATE)]
    assert preview.updates == []


def _windows_published(blocks, count):
    """The live updates: one per 130-block (3 s) window decoded before stop."""
    return [(_old_drain(blocks[:130 * n]), True) for n in range(1, count + 1)]


def test_finalize_matches_the_old_drain_when_stop_finds_the_worker_idle():
    # 431 blocks: three windows decoded live and a 41-block partial window.
    blocks = _blocks(10.0, seed=7)
    decoder = _Decoder()
    preview = _preview(decoder)
    # Paced like the recorder, so the 130-block queue never overflows.
    for start in range(0, len(blocks), 100):
        _feed(preview, blocks[start:start + 100])
        _wait_until(preview.audio_queue.empty)
    _wait_until(lambda: len(decoder.calls) == 3)

    preview.stop_streaming()

    assert preview.finalize_preview() == _old_drain(blocks)
    assert preview.updates == _windows_published(blocks, 3)


def test_finalize_matches_the_old_drain_across_a_kept_window_and_queued_blocks():
    """Stop lands with a partial window accumulated and blocks never taken.

    The worker loop runs on this thread so which blocks it took is exact.
    """
    from services.streaming_transcriber import _WAKE

    blocks = _blocks(500 * config.CHUNK_SIZE / config.SAMPLE_RATE, seed=8)
    decoder = _Decoder()
    preview = StreamingTranscriber(SimpleNamespace(model=decoder),
                                   chunk_duration_sec=3.0, overlap_sec=0.75)
    preview.sample_rate = config.SAMPLE_RATE
    preview.audio_queue = queue.Queue()
    preview.is_streaming = True
    preview.updates = []
    preview.callback = lambda *args: preview.updates.append(args)
    # Two windows and a 40-block partial are taken; 200 more are left queued.
    _feed(preview, blocks[:300])
    preview.audio_queue.put_nowait(_WAKE)
    preview._worker_loop()
    _feed(preview, blocks[300:])

    assert preview.stop_streaming() == _old_drain(blocks[:260])
    assert len(preview._retained) == 40 and preview.audio_queue.qsize() == 201
    # Finalize cuts [260:390] from the kept and queued blocks, then [390:500].
    assert preview.finalize_preview() == _old_drain(blocks)
    assert decoder.calls[2:] == [int((130 * config.CHUNK_SIZE + int(0.75 * config.SAMPLE_RATE))
                                     * TARGET / config.SAMPLE_RATE),
                                 int((110 * config.CHUNK_SIZE + int(0.75 * config.SAMPLE_RATE))
                                     * TARGET / config.SAMPLE_RATE)]
    assert preview.updates == _windows_published(blocks, 2)


def test_finalize_matches_the_old_drain_after_a_window_in_flight_at_stop():
    blocks = _blocks(360 * config.CHUNK_SIZE / config.SAMPLE_RATE, seed=6)
    decoder = _Decoder(stall_on=2)
    preview = _preview(decoder)
    # Let the first window finish before feeding the second. Feeding 260
    # blocks at once can overflow the 130-block queue before the worker runs.
    _feed(preview, blocks[:130])
    _wait_until(lambda: len(decoder.calls) == 1)
    # Window 2 stalls in the engine; 100 blocks queue behind it (the queue
    # holds 130, about 3 s).
    _feed(preview, blocks[130:260])
    assert decoder.entered.wait(5)
    _feed(preview, blocks[260:])

    assert preview.stop_streaming() == _old_drain(blocks[:130])
    decoder.release.set()

    assert preview.finalize_preview() == _old_drain(blocks)
    # Window 2 finished after stop, so only window 1 reached the UI.
    assert preview.updates == _windows_published(blocks, 1)


def test_in_flight_window_lands_for_the_fallback_but_never_reaches_the_ui():
    decoder = _Decoder(stall_on=1)
    preview = _preview(decoder)
    blocks = _blocks(3.5, seed=3)
    _feed(preview, blocks[:130])
    assert decoder.entered.wait(5)
    # Queue the tail after the worker starts decoding so none of it is dropped.
    _feed(preview, blocks[130:])

    started = time.perf_counter()
    assert preview.stop_streaming() == ""
    # Stop did not wait for the stalled engine.
    assert time.perf_counter() - started < 1.0
    worker = preview.worker_thread
    decoder.release.set()
    worker.join(5)

    assert preview.preview_text == _old_drain(blocks[:130])
    assert preview.updates == []
    assert preview.finalize_preview() == _old_drain(blocks)
    assert preview.updates == []


def test_next_recording_starts_right_after_a_fast_stop_and_drops_the_old_tail():
    decoder = _Decoder()
    preview = _preview(decoder, poll=30.0)
    _feed(preview, _blocks(1.5, seed=1))
    old_worker = preview.worker_thread
    preview.stop_streaming()
    old_worker.join(5)
    assert not old_worker.is_alive()

    # No finalize: a non-empty transcript never asks for the old tail.
    preview.start_streaming(config.SAMPLE_RATE, lambda *args: preview.updates.append(args))

    assert preview.is_streaming
    assert preview.worker_thread is not old_worker
    assert preview._retained == [] and preview.audio_queue.empty()
    fresh = _blocks(0.5, seed=2)
    _feed(preview, fresh)
    preview.stop_streaming()
    assert preview.finalize_preview() == _old_drain(fresh)
    # Only the new recording's audio was ever decoded.
    assert decoder.calls == [int(len(fresh) * config.CHUNK_SIZE * TARGET / config.SAMPLE_RATE)]


def test_cancel_discards_the_kept_tail_and_late_output_without_waiting():
    decoder = _Decoder(stall_on=1)
    preview = _preview(decoder)
    _feed(preview, _blocks(4.0, seed=4))
    assert decoder.entered.wait(5)
    worker = preview.worker_thread
    preview.stop_streaming()

    started = time.perf_counter()
    preview.cancel_streaming()
    assert preview.finalize_preview() == ""
    # Neither waited on the stalled engine.
    assert time.perf_counter() - started < 1.0
    assert worker.is_alive()

    decoder.release.set()
    worker.join(5)
    assert preview.preview_text == "" and preview.updates == []
    assert preview._retained == []
    assert len(decoder.calls) == 1


def test_cancel_wakes_an_idle_worker_so_the_next_preview_can_start():
    preview = _preview(_Decoder(), poll=30.0)
    worker = preview.worker_thread
    preview.cancel_streaming()
    worker.join(5)
    assert not worker.is_alive()


def test_finalize_deadline_ignores_late_output_and_holds_off_a_replacement():
    decoder = _Decoder(stall_on=1)
    preview = _preview(decoder)
    _feed(preview, _blocks(4.0, seed=5))
    assert decoder.entered.wait(5)
    worker = preview.worker_thread
    preview.stop_streaming()

    assert preview.finalize_preview(timeout=0.05) == ""

    # The stalled worker still owns the preview's state.
    preview.start_streaming(config.SAMPLE_RATE, lambda *args: preview.updates.append(args))
    assert preview.worker_thread is worker and not preview.is_streaming
    decoder.release.set()
    worker.join(5)
    assert preview.preview_text == "" and preview.updates == []
    assert preview.finalize_preview() == ""
    assert len(decoder.calls) == 1

    preview.start_streaming(config.SAMPLE_RATE, lambda *args: preview.updates.append(args))
    assert preview.is_streaming
    preview.cancel_streaming()


def test_cleanup_drops_the_kept_tail():
    decoder = _Decoder()
    preview = _preview(decoder)
    _feed(preview, _blocks(1.0))
    preview.stop_streaming()
    preview.cleanup()
    assert preview.finalize_preview() == ""
    assert decoder.calls == []


def test_finalize_decodes_once_and_leaves_a_live_preview_alone():
    decoder = _Decoder()
    preview = _preview(decoder)
    # Still recording: nothing was stopped, so there is nothing to finish.
    assert preview.finalize_preview() == ""
    blocks = _blocks(1.0)
    _feed(preview, blocks)
    preview.stop_streaming()
    text = preview.finalize_preview()
    assert text == _old_drain(blocks)
    assert preview.finalize_preview() == text
    assert len(decoder.calls) == 1


# StreamingRuntime -----------------------------------------------------------

def _runtime(transcriber):
    from services.runtime.streaming import StreamingRuntime

    controller = SimpleNamespace(
        streaming_transcriber=transcriber, _streaming_enabled=False,
        recorder=SimpleNamespace(set_streaming_callback=lambda _callback: None),
        partial_transcription=Mock(), streaming_text_update=Mock())
    return StreamingRuntime(controller)


def test_runtime_finalize_uses_the_window_preview_and_skips_the_rest():
    blocks = _blocks(1.0)
    preview = StreamingTranscriber(SimpleNamespace(model=_Decoder()))
    runtime = _runtime(preview)
    runtime.start_streaming_session()
    _feed(preview, blocks)
    assert runtime.stop_streaming_session() == ""
    assert runtime.finalize_streaming_text() == _old_drain(blocks)

    # A native stream already flushed at stop and has nothing to finish.
    assert _runtime(SimpleNamespace(stop_streaming=lambda: "done.")).finalize_streaming_text() is None
    assert _runtime(None).finalize_streaming_text() is None
    broken = SimpleNamespace(finalize_preview=Mock(side_effect=RuntimeError("engine gone")))
    assert _runtime(broken).finalize_streaming_text() is None


# TranscriptionRuntime -------------------------------------------------------

def _dictation(tmp_path, monkeypatch, final_text, preview):
    """A recording job run end to end on this thread, with a fake engine."""
    from services.runtime import transcription
    from services.runtime.streaming import StreamingRuntime

    wav = tmp_path / "recorded.wav"
    wav.write_bytes(b"RIFF" + bytes(200))
    monkeypatch.setattr(transcription.config, "RECORDED_AUDIO_FILE", str(wav))
    controller = Mock()
    controller.streaming_transcriber = preview
    controller._streaming_enabled = False
    controller._pending_streaming_text = ""
    controller._pending_file_size = None
    controller._transcription_start_time = None
    controller.current_backend = SimpleNamespace(
        is_available=lambda: True, requires_file_splitting=False,
        transcribe=lambda _path: final_text)
    controller.recorder.wait_for_stop_completion.return_value = True
    controller.recorder.has_recording_data.return_value = True
    controller.recorder.save_recording.return_value = True
    controller.recorder.get_recording_duration.return_value = 4.0
    controller.streaming_runtime = StreamingRuntime(controller)
    runtime = transcription.TranscriptionRuntime(controller)
    monkeypatch.setattr(runtime, "_maybe_cleanup_transcript", lambda raw: (raw, None, None))
    controller.streaming_runtime.start_streaming_session()
    return controller, runtime


def test_empty_dictation_shows_the_lazily_completed_preview(tmp_path, monkeypatch):
    from services.runtime.transcription import EMPTY_PREVIEW_FALLBACK_MESSAGE

    decoder = _Decoder()
    preview = StreamingTranscriber(SimpleNamespace(model=decoder))
    controller, runtime = _dictation(tmp_path, monkeypatch, "  ", preview)
    blocks = _blocks(2.0, seed=9)
    _feed(preview, blocks)
    assert runtime._claim_job()

    runtime.finish_recording_job()

    assert controller._pending_streaming_text == _old_drain(blocks)
    (transcript, raw_text, info), _ = controller.transcription_completed.emit.call_args
    runtime.on_transcription_complete(transcript, raw_text, info)
    controller.ui_controller.set_transcript.assert_called_once_with(_old_drain(blocks), raw=None)
    controller.ui_controller.set_status.assert_called_with(EMPTY_PREVIEW_FALLBACK_MESSAGE)
    assert controller._pending_streaming_text == ""


def test_non_empty_dictation_never_decodes_the_kept_tail(tmp_path, monkeypatch):
    decoder = _Decoder()
    preview = StreamingTranscriber(SimpleNamespace(model=decoder))
    controller, runtime = _dictation(tmp_path, monkeypatch, "Hello there.", preview)
    _feed(preview, _blocks(2.0))
    assert runtime._claim_job()

    runtime.finish_recording_job()

    assert decoder.calls == []
    assert controller._pending_streaming_text == ""
    (transcript, _raw, _info), _ = controller.transcription_completed.emit.call_args
    assert transcript == "Hello there."


def test_an_upload_never_finishes_an_earlier_dictation_preview(tmp_path, monkeypatch):
    decoder = _Decoder()
    preview = StreamingTranscriber(SimpleNamespace(model=decoder))
    controller, runtime = _dictation(tmp_path, monkeypatch, "  ", preview)
    _feed(preview, _blocks(2.0))
    controller.streaming_runtime.stop_streaming_session()
    # The dictation errored before its fallback, leaving the tail kept.
    upload = tmp_path / "upload.wav"
    upload.write_bytes(b"RIFF" + bytes(200))
    controller._pending_audio_path = None

    runtime.transcribe_audio_file(str(upload))

    assert decoder.calls == []
    assert controller._pending_streaming_text == ""


def test_a_canceled_job_skips_the_fallback_decode(tmp_path, monkeypatch):
    decoder = _Decoder()
    preview = StreamingTranscriber(SimpleNamespace(model=decoder))
    controller, runtime = _dictation(tmp_path, monkeypatch, "", preview)
    _feed(preview, _blocks(2.0))
    assert runtime._claim_job()
    runtime._cancel_requested.set()

    runtime.finish_recording_job()

    assert decoder.calls == []

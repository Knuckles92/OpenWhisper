"""Incremental dictation: early window decodes must never change the transcript.

The engine is a real LocalSpeechBackend with its worker replaced by a fake
recognizer that gives each distinct window its own label, so equal text means
the same audio reached the engine window for window. Capture goes through a
real AudioRecorder spool and save_recording(), and the 16 kHz stream through
the real PyAV resampler.
"""
import concurrent.futures
import hashlib
import random
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pytest

from config import config
from services import incremental_dictation as incremental
from services.incremental_dictation import DictationSession, IncrementalDictation
from services.local_asr.audio import (
    MAX_SAMPLES,
    SAMPLE_RATE,
    WindowSplitter,
    resampler,
    split_point,
    windows,
)
from services.recorder import AudioRecorder
from services.settings import settings_manager
from transcriber.optional_backend import LocalSpeechBackend

RATE = 44100
BLOCK = 1024
BLOCKS_PER_POLL = 43  # about one second of 1024-frame callbacks


def speech_like(seconds: float, seed: int = 0) -> np.ndarray:
    """Bursts of tones and noise between quiet gaps, so split points vary."""
    rng = np.random.default_rng(seed)
    parts, size, total = [], 0, int(seconds * RATE)
    while size < total:
        n = int(rng.uniform(0.2, 1.6) * RATE)
        if rng.random() < 0.3:
            part = rng.standard_normal(n) * 30
        else:
            t = np.arange(n) / RATE
            part = (np.sin(2 * np.pi * rng.uniform(120, 900) * t) * rng.uniform(1500, 9000)
                    + rng.standard_normal(n) * 800)
        parts.append(part)
        size += n
    return np.clip(np.concatenate(parts)[:total], -32768, 32767).astype(np.int16)


class Engine:
    """Fake worker: one label per distinct window, some of them empty."""

    LABELS = ("alpha", "", "gamma", "delta", "", "zeta", "eta", "theta")

    def __init__(self, backend):
        self.backend = backend
        self.labels = {}
        self.calls = []  # (samples, engine marked busy)
        self.fail = None
        self.gate = None

    def __call__(self, audio, language=None):
        if self.gate is not None:
            self.gate.wait(5)
        if self.fail is not None:
            raise self.fail
        digest = hashlib.sha1(np.ascontiguousarray(audio).tobytes()).hexdigest()
        label = self.labels.setdefault(digest, self.LABELS[len(self.labels) % len(self.LABELS)])
        self.calls.append((len(audio), self.backend.is_transcribing))
        return dict(text=label, segments=[])


@pytest.fixture
def backend():
    engine_backend = LocalSpeechBackend("parakeet")
    engine_backend.is_available = lambda: True
    engine_backend._recognize = Engine(engine_backend)
    return engine_backend


@pytest.fixture
def recorder():
    capture_ = AudioRecorder(output_file=config.RECORDED_AUDIO_FILE)
    yield capture_
    capture_.cleanup()


@pytest.fixture
def controller(backend, recorder):
    return SimpleNamespace(current_backend=backend, recorder=recorder, is_meeting_active=lambda: False)


def capture(recorder, pcm, session=None, *, polls=BLOCKS_PER_POLL, rng=None):
    """Feed PCM through the recorder callback, polling the session as its thread would."""
    recorder.is_recording = True
    since_poll = start = 0
    due = polls
    while start < len(pcm):
        size = BLOCK if rng is None else rng.randint(1, 4 * BLOCK)
        chunk = pcm[start:start + size]
        start += size
        recorder._audio_callback(chunk.reshape(-1, 1), len(chunk), None, None)
        since_poll += 1
        if session is not None and since_poll >= due:
            session.poll()
            since_poll = 0
            due = polls if rng is None else rng.randint(5, 3 * polls)


def stop(recorder, session=None):
    """End capture as the recorder thread does, then save the WAV."""
    recorder.is_recording = False
    if session is not None:
        session.poll()
    assert recorder.save_recording()
    return config.RECORDED_AUDIO_FILE


def wait_for(condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.005)
    return condition()


def original_windows(audio_path):
    """windows() as it was before WindowSplitter, kept as the reference."""
    import av

    pending = np.empty(0, dtype=np.float32)
    offset = 0
    converter = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    with av.open(audio_path) as container:
        def frames():
            for frame in container.decode(audio=0):
                frame.pts = None
                yield from converter.resample(frame)
            yield from converter.resample(None)

        for frame in frames():
            pending = np.concatenate((pending, frame.to_ndarray().reshape(-1).astype(np.float32) / 32768.))
            while len(pending) >= MAX_SAMPLES:
                end = split_point(pending)
                yield offset / SAMPLE_RATE, pending[:end]
                offset += end
                pending = pending[end:]
        if pending.size:
            yield offset / SAMPLE_RATE, pending


def identical(a, b):
    return a.dtype == b.dtype and np.array_equal(a.view(np.uint32), b.view(np.uint32))


def long_session(controller, recorder, backend, *, seconds=66, change=None, at=35):
    """Record, decoding early; ``change`` runs at ``at`` seconds. Returns calls made by then."""
    session = DictationSession(controller, backend, recorder)
    pcm = speech_like(seconds, seed=11)
    capture(recorder, pcm[:at * RATE], session)
    assert session.early_windows == 1
    if change:
        change()
    calls_at_change = len(backend._recognize.calls)
    capture(recorder, pcm[at * RATE:], session)
    return session, stop(recorder, session), calls_at_change


# --- the shared splitter and resampler ---------------------------------------


def test_windows_refactor_is_bit_identical_to_the_original(recorder):
    capture(recorder, speech_like(71, seed=1))
    path = stop(recorder)
    new, old = list(windows(path)), list(original_windows(path))
    assert len(new) == len(old) == 3
    for (new_offset, new_audio), (old_offset, old_audio) in zip(new, old):
        assert new_offset == old_offset
        assert identical(new_audio, old_audio)


def test_splitter_windows_do_not_depend_on_how_the_stream_was_cut():
    stream = (np.random.default_rng(2).standard_normal(95 * SAMPLE_RATE) * 3000).astype(np.int16)
    whole = WindowSplitter()
    expected = whole.push([SimpleNamespace(to_ndarray=lambda: stream)]) + whole.finish()
    rng = random.Random(3)
    pieces, got, start = WindowSplitter(), [], 0
    while start < len(stream):
        piece = stream[start:start + rng.randint(1, 50_000)]
        got += pieces.push([SimpleNamespace(to_ndarray=lambda piece=piece: piece)])
        start += len(piece)
    got += pieces.finish()
    assert len(got) == len(expected) == 4
    assert [offset for offset, _ in got] == [offset for offset, _ in expected]
    assert all(identical(a, b) for (_, a), (_, b) in zip(got, expected))
    assert pieces.total == whole.total == len(stream)


def test_incremental_stream_is_bit_identical_to_the_saved_files_windows(controller, recorder, backend):
    """Followed from the first byte in random blocks and polls, the 16 kHz
    windows and their boundaries equal windows() of the file the recorder saves."""
    session = DictationSession(controller, backend, recorder)
    session._resampler = resampler()
    session._language = "en"
    session._decode_ready = lambda: None  # keep every window for comparison
    capture(recorder, speech_like(67, seed=4), session, rng=random.Random(5))
    path = stop(recorder, session)
    during = len(session._ready)
    got = list(session._final_windows(path))
    want = list(windows(path))
    assert during == 2 and len(got) == len(want) == 3
    for (got_offset, got_audio), (want_offset, want_audio) in zip(got, want):
        assert got_offset == want_offset
        assert identical(got_audio, want_audio)


# --- scheduling and join ------------------------------------------------------


def test_long_dictation_decodes_windows_while_recording_and_matches_the_file_path(controller, recorder, backend):
    engine = backend._recognize
    session = DictationSession(controller, backend, recorder)
    pcm = speech_like(80, seed=6)

    capture(recorder, pcm[:29 * RATE], session)
    assert session._resampler is None and session.early_windows == 0
    capture(recorder, pcm[29 * RATE:31 * RATE], session)
    assert session.early_windows == 1
    capture(recorder, pcm[31 * RATE:], session)
    path = stop(recorder, session)
    early = session.early_windows
    assert early >= 2

    background = list(engine.calls)
    assert len(background) == early and not any(busy for _n, busy in background)
    text = session.finish(path)
    at_stop = engine.calls[early:]
    # Only what was left at stop is decoded then, with the engine marked busy.
    assert len(at_stop) == len(list(windows(path))) - early
    assert at_stop and all(busy for _n, busy in at_stop)

    engine.calls.clear()
    assert text == backend.transcribe(path)
    assert len(engine.labels) == len(engine.calls) == early + len(at_stop)  # no new labels: same windows
    assert "  " in text  # an empty window's text still takes its separator


def test_join_keeps_empty_windows_exactly_like_transcribe(controller, recorder, backend):
    backend._recognize.LABELS = ("", "one", "", "")
    session = DictationSession(controller, backend, recorder)
    capture(recorder, speech_like(95, seed=7), session)
    path = stop(recorder, session)
    assert session.early_windows == 3
    text = session.finish(path)
    if text is None:
        # A platform's resampler may cut a saved WAV differently from the
        # live stream; the normal file path must then produce the transcript.
        assert "saved file" in session._invalid
        text = backend.transcribe(path)
    assert text == backend.transcribe(path)
    assert LocalSpeechBackend.join_texts(["", "one", "", ""]) == "one"


def test_silent_windows_are_skipped_the_same_way(controller, recorder, backend):
    pcm = speech_like(90, seed=8)
    pcm[5 * RATE:70 * RATE] = 0  # the second window is digital silence
    session = DictationSession(controller, backend, recorder)
    capture(recorder, pcm, session)
    path = stop(recorder, session)
    assert session.early_windows == 3
    assert session._texts[1] == ""
    slot = IncrementalDictation()
    slot._session = session
    assert slot.transcribe(backend, path) == backend.transcribe(path)


def test_short_dictation_costs_nothing_and_falls_through(controller, recorder, backend):
    slot = IncrementalDictation()
    session = DictationSession(controller, backend, recorder)
    slot._session = session
    with patch.object(recorder, "read_recorded_bytes", wraps=recorder.read_recorded_bytes) as read, \
            patch.object(incremental, "resampler", wraps=incremental.resampler) as make:
        capture(recorder, speech_like(25, seed=9), session)
        path = stop(recorder, session)
        with patch.object(backend, "transcribe", wraps=backend.transcribe) as transcribe:
            text = slot.transcribe(backend, path)
    read.assert_not_called()
    make.assert_not_called()
    transcribe.assert_called_once_with(path)
    assert text == backend.transcribe(path)


def test_nothing_decoded_early_means_the_plain_path(controller, recorder, backend):
    session = DictationSession(controller, backend, recorder)
    capture(recorder, speech_like(31, seed=10), session, polls=10_000)  # no poll before the end
    path = stop(recorder)
    assert session.finish(path) is None


# --- invalidation -------------------------------------------------------------


@pytest.mark.parametrize("change", ["reload", "cancel", "switch", "device", "meeting", "language"])
def test_state_changes_during_recording_fall_back_to_the_file(controller, recorder, backend, change):
    actions = {
        "reload": lambda: setattr(backend, "_generation", backend._generation + 1),
        "cancel": lambda: setattr(backend, "should_cancel", True),
        "switch": lambda: setattr(controller, "current_backend", LocalSpeechBackend("parakeet")),
        "device": lambda: setattr(controller, "recorder", AudioRecorder()),
        "meeting": lambda: setattr(controller, "is_meeting_active", lambda: True),
        "language": lambda: settings_manager.update_settings({"local_asr_language": "de"}),
    }
    session, path, calls_at_change = long_session(controller, recorder, backend, change=actions[change])
    # Window 2 completed after the change; a stale session must not decode it.
    assert len(backend._recognize.calls) == calls_at_change
    assert session._invalid
    assert session.finish(path) is None


def test_early_decode_error_falls_back(controller, recorder, backend):
    backend._recognize.fail = RuntimeError("worker hiccup")
    session = DictationSession(controller, backend, recorder)
    capture(recorder, speech_like(40, seed=12), session)
    path = stop(recorder, session)
    assert session.early_windows == 0 and "worker hiccup" in session._invalid
    backend._recognize.fail = None
    slot = IncrementalDictation()
    slot._session = session
    assert slot.transcribe(backend, path) == backend.transcribe(path)


def test_cleared_capture_invalidates(controller, recorder, backend):
    session, _path, _calls = long_session(controller, recorder, backend, change=recorder.clear_recording_data)
    assert session._invalid == "the capture was cleared"


@pytest.mark.parametrize("damage", ["sample", "truncate", "format"])
def test_saved_file_that_differs_from_the_followed_audio_falls_back(controller, recorder, backend, damage):
    session, path, _calls = long_session(controller, recorder, backend)
    with wave.open(path, "rb") as saved:
        pcm = np.frombuffer(saved.readframes(saved.getnframes()), dtype=np.int16).copy()
    rate = RATE
    if damage == "sample":
        pcm[10 * RATE] ^= 1
    elif damage == "truncate":
        pcm = pcm[:32 * RATE]
    else:
        rate = 48000
    with wave.open(path, "wb") as saved:
        saved.setnchannels(1)
        saved.setsampwidth(2)
        saved.setframerate(rate)
        saved.writeframes(pcm.tobytes())
    calls = len(backend._recognize.calls)
    assert session.finish(path) is None
    assert len(backend._recognize.calls) == calls  # nothing decoded on a mismatch
    assert not backend.is_transcribing


def test_resampled_length_mismatch_falls_back(controller, recorder, backend):
    session, path, _calls = long_session(controller, recorder, backend)
    real_finish = WindowSplitter.finish

    def two_samples_short(self):
        left = real_finish(self)
        self.offset -= 2
        return left

    with patch.object(WindowSplitter, "finish", two_samples_short):
        assert session.finish(path) is None


# --- cancel -------------------------------------------------------------------


def test_discard_never_waits_for_a_window_in_flight(controller, recorder, backend):
    engine = backend._recognize
    engine.gate = threading.Event()
    slot = IncrementalDictation()
    recorder.is_recording = True
    with patch.object(config, "INCREMENTAL_DICTATION_POLL_SEC", 0.01):
        slot.start(controller)
        session = slot._session
        capture(recorder, speech_like(33, seed=13))
        assert wait_for(backend._decode_lock.locked)  # the thread is inside a decode
        started = time.perf_counter()
        slot.discard()
        assert time.perf_counter() - started < 0.1
        engine.gate.set()
        session._thread.join(5)
    assert not session._thread.is_alive()
    assert session.early_windows == 0
    assert session.finish(stop(recorder)) is None


def test_cancel_while_stop_waits_for_an_early_window_ends_the_job(controller, recorder, backend):
    """The engine is marked busy while finish() waits, so Cancel reaches it."""
    engine = backend._recognize
    engine.gate = threading.Event()
    slot = IncrementalDictation()
    recorder.is_recording = True
    with patch.object(config, "INCREMENTAL_DICTATION_POLL_SEC", 0.01):
        slot.start(controller)
        session = slot._session
        capture(recorder, speech_like(33, seed=17))
        assert wait_for(backend._decode_lock.locked)
        path = stop(recorder)
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            job = pool.submit(slot.transcribe, backend, path)
            assert wait_for(lambda: backend.is_transcribing)
            backend.cancel_transcription()  # what TranscriptionRuntime._cancel does now
            engine.gate.set()
            with pytest.raises(RuntimeError, match="Transcription canceled"):
                job.result(5)
    assert not backend.is_transcribing
    assert session._invalid


def test_cancel_at_stop_raises_the_file_paths_cancel(controller, recorder, backend):
    session, path, _calls = long_session(controller, recorder, backend)
    slot = IncrementalDictation()
    slot._session = session
    backend.should_cancel = True
    with pytest.raises(RuntimeError, match="Transcription canceled"):
        slot.transcribe(backend, path)
    assert not backend.is_transcribing


def test_cancel_during_the_final_decode_raises_like_the_file_path(controller, recorder, backend):
    session, path, _calls = long_session(controller, recorder, backend)
    engine = backend._recognize

    def cancel_when_busy(audio, language=None):
        if backend.is_transcribing:
            backend.cancel_transcription()  # the Cancel hotkey during Transcribing
            raise RuntimeError("Transcription canceled")
        return engine(audio, language)

    backend._recognize = cancel_when_busy
    slot = IncrementalDictation()
    slot._session = session
    with pytest.raises(RuntimeError, match="Transcription canceled"):
        slot.transcribe(backend, path)
    assert not backend.is_transcribing


# --- lifecycle and eligibility ------------------------------------------------


def test_thread_follows_capture_and_exits_when_it_ends(controller, recorder, backend):
    slot = IncrementalDictation()
    recorder.is_recording = True
    with patch.object(config, "INCREMENTAL_DICTATION_POLL_SEC", 0.01):
        slot.start(controller)
        session = slot._session
        capture(recorder, speech_like(64, seed=14))
        assert wait_for(lambda: session.early_windows == 2)
        path = stop(recorder)
        session._thread.join(5)
    assert not session._thread.is_alive()
    assert slot.transcribe(backend, path) == backend.transcribe(path)
    assert slot._session is None


def test_thread_exits_when_the_engine_is_cleaned_up(controller, recorder, backend):
    slot = IncrementalDictation()
    recorder.is_recording = True
    with patch.object(config, "INCREMENTAL_DICTATION_POLL_SEC", 0.01):
        slot.start(controller)
        session = slot._session
        capture(recorder, speech_like(31, seed=15))
        backend.cleanup()  # app shutdown, or a meeting releasing the engine
        session._thread.join(5)
    assert not session._thread.is_alive()
    assert session._invalid


def test_uploads_and_retranscribes_never_use_the_session(controller, recorder, backend, tmp_path):
    session, path, _calls = long_session(controller, recorder, backend)
    upload = tmp_path / "upload.wav"
    upload.write_bytes(Path(path).read_bytes())
    slot = IncrementalDictation()
    slot._session = session
    with patch.object(backend, "transcribe", wraps=backend.transcribe) as transcribe:
        text = slot.transcribe(backend, str(upload))
    transcribe.assert_called_once_with(str(upload))
    assert session._invalid and slot._session is None
    assert text == backend.transcribe(path)


def test_a_new_recording_supersedes_a_stale_session(controller, recorder, backend):
    slot = IncrementalDictation()
    stale = DictationSession(controller, backend, recorder)
    slot._session = stale
    with patch.object(config, "INCREMENTAL_DICTATION_BACKENDS", ()):
        slot.start(controller)
    assert stale._invalid and slot._session is None


@pytest.mark.parametrize("case", ["whisper", "moonshine", "qwen_asr", "unavailable", "canceled", "stereo"])
def test_ineligible_engines_and_recorders_get_no_session(controller, backend, recorder, case):
    if case == "whisper":
        controller.current_backend = Mock()
    elif case in ("moonshine", "qwen_asr"):
        other = LocalSpeechBackend(case)
        other.is_available = lambda: True
        controller.current_backend = other
    elif case == "unavailable":
        backend.is_available = lambda: False
    elif case == "canceled":
        backend.should_cancel = True
    else:
        recorder.channels = 2
    assert DictationSession.create(controller) is None


def test_runtime_transcribes_its_own_recording_from_the_session(recorder, backend):
    from services.runtime.transcription import TranscriptionRuntime

    controller = Mock(current_backend=backend, recorder=recorder, _pending_file_size=None)
    controller.is_meeting_active = lambda: False
    runtime = TranscriptionRuntime(controller)
    runtime._maybe_cleanup_transcript = lambda raw: (raw, None, None)
    recorder.is_recording = True
    with patch.object(config, "INCREMENTAL_DICTATION_POLL_SEC", 0.01):
        runtime._incremental.start(controller)
        session = runtime._incremental._session
        capture(recorder, speech_like(64, seed=16))
        assert wait_for(lambda: session.early_windows == 2)
        path = stop(recorder)
        session._thread.join(5)
    with patch.object(backend, "transcribe", wraps=backend.transcribe) as transcribe:
        runtime.transcribe_audio_file(path)
    transcribe.assert_not_called()
    controller.transcription_failed.emit.assert_not_called()
    fixed = controller.transcription_completed.emit.call_args.args[0]
    assert fixed == backend.transcribe(path)


def test_runtime_cancel_discards_the_session(recorder, backend):
    from services.runtime.transcription import TranscriptionRuntime

    controller = Mock(current_backend=backend, recorder=recorder)
    controller.is_meeting_active = lambda: False
    runtime = TranscriptionRuntime(controller)
    recorder.is_recording = True
    with patch.object(config, "INCREMENTAL_DICTATION_POLL_SEC", 0.01):
        runtime._incremental.start(controller)
        session = runtime._incremental._session
        runtime._cancel_recording()
        session._thread.join(5)
    assert session._invalid == "the recording was canceled"
    assert runtime._incremental._session is None


# --- recorder reads -----------------------------------------------------------


def test_read_recorded_bytes_restores_the_write_position(recorder):
    assert recorder.read_recorded_bytes(0) == b""
    first = np.arange(3000, dtype=np.int16)
    second = np.arange(3000, 5000, dtype=np.int16)
    recorder._audio_callback(first.reshape(-1, 1), len(first), None, None)
    assert recorder.read_recorded_bytes(2000) == first[1000:].tobytes()
    recorder._audio_callback(second.reshape(-1, 1), len(second), None, None)
    assert recorder.read_recorded_bytes(0) == np.concatenate((first, second)).tobytes()
    assert recorder.read_recorded_bytes(10_000) == b""
    assert recorder.read_recorded_bytes(10_002) is None
    assert recorder.save_recording()
    with wave.open(config.RECORDED_AUDIO_FILE) as saved:
        pcm = np.frombuffer(saved.readframes(saved.getnframes()), dtype=np.int16)
    assert np.array_equal(pcm[:5000], np.concatenate((first, second)))
    recorder.clear_recording_data()
    assert recorder.read_recorded_bytes(2) is None

"""Qwen on CUDA decodes a file's windows several per request (#48)."""
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from services.local_asr import qwen_batch, qwen_speech
from transcriber.optional_backend import LocalSpeechBackend, SpeechDecoder

MIB = 1024 ** 2


class OutOfMemoryError(RuntimeError):
    pass


def fake_torch(free_mib=8192, reserved_mib=0, allocated_mib=0):
    return SimpleNamespace(cuda=SimpleNamespace(
        mem_get_info=Mock(return_value=(free_mib * MIB, 6144 * MIB)),
        memory_reserved=Mock(return_value=reserved_mib * MIB),
        memory_allocated=Mock(return_value=allocated_mib * MIB),
        empty_cache=Mock(), OutOfMemoryError=OutOfMemoryError,
    ))


class Engine:
    """Qwen3ASRModel.transcribe: a tuple is one window, a list one batch."""

    def __init__(self, fail=lambda values: None):
        self.calls, self.fail = [], fail

    def transcribe(self, audio, language):
        values = [int(audio[0][0])] if isinstance(audio, tuple) else [int(a[0]) for a, _rate in audio]
        self.calls.append(values if isinstance(audio, list) else values[0])
        self.fail(values)
        return [SimpleNamespace(text=f"{language} {value}") for value in values]


def audios(count):
    return [np.full(1600, value, np.float32) for value in range(1, count + 1)]


@pytest.mark.parametrize("free_mib,unused_mib,limit,expected", [
    (8192, 0, 16, 16), (8192, 0, 4, 4),
    (512 + 3 * 256, 0, 16, 4),
    (512, 0, 16, 1), (0, 0, 16, 1),
    # Memory this process reserved but isn't using counts as free.
    (0, 512 + 256, 16, 2),
])
def test_batch_size_follows_free_gpu_memory(free_mib, unused_mib, limit, expected):
    torch = fake_torch(free_mib, reserved_mib=1000 + unused_mib, allocated_mib=1000)
    assert qwen_batch.memory_batch_size(torch, limit) == expected


def test_windows_are_decoded_in_order_as_few_calls_as_memory_allows():
    engine, torch = Engine(), fake_torch(512 + 3 * 256)
    assert qwen_batch.transcribe_batch(engine, audios(10), "English", torch, 16) == (
        [f"English {value}" for value in range(1, 11)], 16)
    assert engine.calls == [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10]]
    torch.cuda.empty_cache.assert_called_once()


def test_out_of_memory_halves_the_limit_without_repeating_decoded_windows():
    def fail(values):
        if len(values) > 2:
            raise OutOfMemoryError("CUDA out of memory")

    engine = Engine(fail)
    texts, limit = qwen_batch.transcribe_batch(engine, audios(6), "English", fake_torch(), 4)
    assert texts == [f"English {value}" for value in range(1, 7)] and limit == 2
    assert engine.calls == [[1, 2, 3, 4], [1, 2], [3, 4], [5, 6]]


def test_any_other_batch_failure_decodes_that_batch_one_window_at_a_time():
    # One window reaching the output limit fails its whole padded batch.
    def fail(values):
        if len(values) > 1 and 3 in values:
            raise RuntimeError("Qwen reached its output limit.")

    engine = Engine(fail)
    texts, limit = qwen_batch.transcribe_batch(engine, audios(6), "English", fake_torch(), 4)
    assert texts == [f"English {value}" for value in range(1, 7)] and limit == 4
    assert engine.calls == [[1, 2, 3, 4], 1, 2, 3, 4, [5, 6]]


def test_a_window_that_fails_alone_raises_its_own_error():
    def fail(values):
        if 3 in values:
            raise RuntimeError("Qwen reached its output limit.")

    engine, torch = Engine(fail), fake_torch()
    with pytest.raises(RuntimeError, match="output limit"):
        qwen_batch.transcribe_batch(engine, audios(4), "English", torch, 4)
    assert engine.calls == [[1, 2, 3, 4], 1, 2, 3]
    torch.cuda.empty_cache.assert_called_once()


def test_out_of_memory_on_a_single_window_raises():
    def fail(values):
        raise OutOfMemoryError("CUDA out of memory")

    with pytest.raises(OutOfMemoryError):
        qwen_batch.transcribe_batch(Engine(fail), audios(1), "English", fake_torch(), 16)


class Worker:
    """SpeechProcess for a loaded Qwen worker; refuses requests once closed."""

    def __init__(self, recovered_limit=None):
        self.process = SimpleNamespace(poll=lambda: None)
        self.batches, self.ops, self.closed = [], [], False
        self.recovered_limit = recovered_limit
        self.on_request = lambda: None

    def close(self):
        self.closed = True

    def request(self, op, *, timeout, **payload):
        if self.closed:
            raise RuntimeError("Transcription canceled")
        self.ops.append(op)
        if op != "transcribe_batch":
            return dict(text="single", segments=[])
        values = [int(np.fromfile(path, dtype=np.float32)[0]) for path in payload["audio_paths"]]
        assert timeout == 300 * len(values) and payload["language"] == "fr"
        self.batches.append(values)
        self.on_request()
        limit = self.recovered_limit or payload["batch_size"]
        return dict(texts=[str(value) for value in values], batch_size=limit)


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setattr(LocalSpeechBackend, "_settings", staticmethod(lambda: {"local_asr_language": "fr"}))
    monkeypatch.setattr(qwen_batch, "MAX_BATCH_SIZE", 4)
    # Negative windows are audible noise without speech; zero is silence.
    monkeypatch.setattr(qwen_speech, "has_speech", lambda audio: audio[0] > 0)
    engine = LocalSpeechBackend("qwen_asr")
    engine.device = "cuda"
    engine.model = object()
    engine._process = Worker()
    return engine


def windows(values, consumed=None):
    for index, value in enumerate(values):
        if consumed is not None:
            consumed.append(value)
        yield index * 30., np.full(1600, value, np.float32)


def test_file_is_read_only_as_far_as_the_next_batch(backend):
    consumed = []
    worker = backend._process
    worker.on_request = lambda: assert_read_ahead(consumed, worker.batches)
    texts = backend.transcribe_windows(windows(range(1, 10), consumed))
    assert texts == [str(value) for value in range(1, 10)]
    assert worker.batches == [[1, 2, 3, 4], [5, 6, 7, 8], [9]]
    assert not backend.is_transcribing


def assert_read_ahead(consumed, batches):
    assert len(consumed) == sum(map(len, batches))


def test_windows_without_speech_keep_their_place_but_not_a_batch_slot(backend, monkeypatch):
    monkeypatch.setattr("services.local_asr.audio.windows",
                        lambda path: windows([1, 0, -1, 2, 3, 4, -2, 5]))
    assert backend.transcribe_windows(windows([1, 0, -1, 2, 3, 4, -2, 5])) == [
        "1", "", "", "2", "3", "4", "", "5"]
    assert backend._process.batches == [[1, 2, 3, 4], [5]]
    assert backend.transcribe("call.mp3") == "1 2 3 4 5"


def test_running_out_of_memory_shrinks_the_rest_of_the_file(backend):
    backend._process.recovered_limit = 2
    backend.transcribe_windows(windows(range(1, 10)))
    assert backend._process.batches == [[1, 2, 3, 4], [5, 6], [7, 8], [9]]


@pytest.mark.parametrize("interruption", ["cancel", "reload"])
def test_a_cancel_or_reload_mid_file_never_mixes_two_workers(backend, interruption):
    old, new = backend._process, Worker()

    def interrupt():
        if interruption == "cancel":
            backend.cancel_transcription()
        else:
            backend.cleanup()
            backend._process = new
            backend.model = object()

    old.on_request = interrupt
    with pytest.raises(RuntimeError, match="canceled"):
        backend.transcribe_windows(windows(range(1, 10)))
    assert old.batches == [[1, 2, 3, 4]] and new.ops == []
    assert not backend.is_transcribing


@pytest.mark.parametrize("family,device,remote", [
    ("qwen_asr", "cpu", False), ("qwen_asr", "mps", False), ("qwen_asr", "cuda", True),
    ("parakeet", "cuda", False), ("nemotron", "cuda", False), ("moonshine", "cpu", False),
])
def test_other_engines_devices_and_remote_hosts_decode_one_window_per_request(backend, family, device, remote):
    backend.backend_id, backend.device, backend.is_remote = family, device, remote
    assert backend.transcribe_windows(windows([1, 2, 3])) == ["single"] * 3
    assert backend._process.ops == ["transcribe"] * 3


@pytest.mark.parametrize("path", ["decode_window", "preview_audio", "recognize", "meeting"])
def test_dictation_preview_meeting_and_host_decodes_stay_single_window(backend, path):
    audio = np.ones(1600, np.float32)
    if path == "meeting":
        SpeechDecoder(backend).transcribe(audio)
    else:
        getattr(backend, path)(audio)
    assert backend._process.ops == ["transcribe"]


def test_transcript_has_no_extra_spaces_where_a_window_had_no_text():
    assert LocalSpeechBackend.join_texts(["Hello.", "", " ", " there. ", ""]) == "Hello. there."

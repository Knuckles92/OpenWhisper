"""Priority and per-device fairness over real loopback TLS WebSockets."""
from contextlib import contextmanager
import threading
import time

import numpy as np
import pytest

from services.remote_asr.admission import WorkAdmission
from services.remote_asr.client import RemoteConnection, RemoteRequestError
from services.remote_asr.engines import HostEngine
from services.remote_asr.host import DeviceRegistry, SpeechHost
from services.remote_asr.tls import ensure_host_identity
from tests.test_remote_engine import no_real_tailscale  # noqa: F401


class _Engine(HostEngine):
    identity = ("priority-test",)

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = []

    def describe(self):
        return {"family": "test", "available": True, "streaming": True}

    def transcribe(self, audio, language):
        self.calls.append(("transcribe", len(audio)))
        if len(self.calls) == 1:
            self.entered.set()
            assert self.release.wait(5)
        return {"text": str(len(audio)), "segments": []}

    def stream(self, session, audio, language, finish):
        self.calls.append(("stream", len(audio)))
        return {"events": [{"text": str(len(audio)), "final": finish}]}


def _wait(predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        time.sleep(.005)
    assert predicate()


@contextmanager
def _host(tmp_path, queued=6):
    entries = []
    registry = DeviceRegistry(lambda: list(entries), lambda values: entries.__setitem__(slice(None), values))
    engine = _Engine()
    identity = ensure_host_identity(str(tmp_path / "identity"))
    host = SpeechHost(engine_provider=lambda: engine, registry=registry, identity=identity,
                      max_active_jobs=1, max_queued_jobs=queued)
    host.start(port=0, bind="127.0.0.1")
    connections = []

    def connect(token=None):
        if token is None:
            _device, token = registry.add(f"device-{len(connections)}")
        connection = RemoteConnection("127.0.0.1", host.port, token, identity.fingerprint)
        connection.connect()
        connections.append(connection)
        return connection, token

    try:
        yield host, engine, connect
    finally:
        engine.release.set()
        for connection in connections:
            connection.close()
        host.stop()


def _request(connection, results, number, op="transcribe", **options):
    try:
        results[number] = connection.request(op, audio=np.ones(number, np.float32),
                                             timeout=5, **options)
    except Exception as exc:
        results[number] = exc


def _start(connection, results, number, op="transcribe", **options):
    thread = threading.Thread(target=_request, args=(connection, results, number, op), kwargs=options)
    thread.start()
    return thread


def test_final_text_overtakes_queued_native_stream_without_dropping_audio(tmp_path):
    with _host(tmp_path) as (host, engine, connect):
        connections = [connect()[0] for _ in range(4)]
        results = {}
        threads = [_start(connections[0], results, 100)]
        assert engine.entered.wait(3)
        threads.append(_start(connections[1], results, 200, "stream", session="native"))
        _wait(lambda: len(host._work_admission._queue) == 1)
        threads.append(_start(connections[2], results, 300))
        _wait(lambda: len(host._work_admission._queue) == 2)
        assert connections[3].request("transcribe", audio=np.ones(400, np.float32),
                                      preview=True) == {"preview_skipped": True}
        assert host._work_admission.snapshot()["queued"] == 2
        engine.release.set()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
        assert engine.calls == [("transcribe", 100), ("transcribe", 300), ("stream", 200)]
        assert results[200]["events"] == [{"text": "200", "final": False}]
        assert results[300]["text"] == "300"


def test_one_device_cannot_fill_queue_ahead_of_five_other_devices(tmp_path):
    with _host(tmp_path) as (host, engine, connect):
        attacker, token = connect()
        waiting_attacker, _ = connect(token)
        extra_attacker, _ = connect(token)
        normal = [connect()[0] for _ in range(5)]
        results = {}
        threads = [_start(attacker, results, 100)]
        assert engine.entered.wait(3)
        threads.append(_start(waiting_attacker, results, 200))
        _wait(lambda: len(host._work_admission._queue) == 1)
        with pytest.raises(RemoteRequestError) as refused:
            extra_attacker.request("transcribe", audio=np.ones(300, np.float32))
        assert refused.value.code == "busy"
        for index, connection in enumerate(normal, start=4):
            threads.append(_start(connection, results, index * 100))
            _wait(lambda: len(host._work_admission._queue) == index - 2)
        engine.release.set()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
        assert all(isinstance(result, dict) for result in results.values())
        assert len(engine.calls) == 7
        assert host._work_admission.snapshot()["active"] == 0
        assert host._work_admission.snapshot()["queued"] == 0


def test_previews_do_not_queue_and_native_stream_finish_is_durable(tmp_path):
    with _host(tmp_path, queued=1) as (host, engine, connect):
        first, _ = connect()
        preview, _ = connect()
        native, _ = connect()
        results = {}
        first_thread = _start(first, results, 100)
        assert engine.entered.wait(3)
        for _ in range(10):
            assert preview.request("transcribe", audio=np.ones(20, np.float32),
                                   preview=True)["preview_skipped"]
        assert not host._work_admission._queue
        native_thread = _start(native, results, 200, "stream", session="native", finish=True)
        _wait(lambda: len(host._work_admission._queue) == 1)
        engine.release.set()
        first_thread.join(5)
        native_thread.join(5)
        assert results[200]["events"][0]["final"] is True
        assert engine.calls == [("transcribe", 100), ("stream", 200)]


def test_one_device_has_at_most_one_active_slot():
    admission = WorkAdmission(2, 0, 1)
    assert admission.acquire(lambda: True, device_id="one")
    assert not admission.acquire(lambda: True, device_id="one")
    assert admission.acquire(lambda: True, device_id="two")
    assert admission.snapshot()["active"] == 2
    admission.release("one")
    admission.release("two")
    assert not admission._devices


def test_disconnected_waiter_cannot_hold_an_admission_slot():
    admission = WorkAdmission(1, 2, 1)
    assert admission.acquire(lambda: True, device_id="one")
    assert not admission.acquire(lambda: False, device_id="two")
    assert admission.snapshot()["queued"] == 0
    admission.release("one")


def test_remote_preview_tags_only_disposable_transcriptions():
    from transcriber.remote_backend import RemoteSpeechBackend

    backend = RemoteSpeechBackend()
    calls = []
    backend._request_audio = lambda *args, **kwargs: calls.append((args, kwargs)) or {
        "text": "preview", "segments": [],
    }
    audio = np.ones(100, np.float32)
    assert backend.preview_audio(audio, "en")["text"] == "preview"
    assert calls[0][0][0] == "transcribe"
    assert calls[0][1] == {"preview": True}
    backend._request_audio = lambda *args, **kwargs: {"preview_skipped": True}
    assert backend.preview_audio(audio, "en") is None
    assert not backend._decode_lock.locked()


def test_window_preview_can_be_shed_but_final_fallback_is_durable():
    from types import SimpleNamespace
    from services.streaming_transcriber import StreamingTranscriber

    calls = []

    class Decoder:
        def preview_transcribe(self, audio, **options):
            calls.append("preview")
            return None

        def transcribe(self, audio, **options):
            calls.append("durable")
            return iter([SimpleNamespace(text="saved audio")]), None

    preview = StreamingTranscriber(SimpleNamespace(model=Decoder()))
    samples = np.ones(100, np.float32)
    assert preview._transcribe_window(preview.backend.model, samples) is None
    preview._finalizing = True
    assert preview._transcribe_window(preview.backend.model, samples) == "saved audio"
    assert calls == ["preview", "durable"]


def test_disposable_preview_preserves_cancel_checks_while_consuming_segments():
    from types import SimpleNamespace
    from services.streaming_transcriber import StreamingTranscriber

    preview = StreamingTranscriber(SimpleNamespace())

    def segments():
        yield SimpleNamespace(text="first")
        preview._discard_results = True
        yield SimpleNamespace(text="late")

    decoder = SimpleNamespace(preview_transcribe=lambda *args, **kwargs: (segments(), None))
    assert preview._transcribe_window(decoder, np.ones(100, np.float32)) is None


def test_local_optional_window_skips_busy_engine_but_durable_decode_remains_available():
    from transcriber.optional_backend import LocalSpeechBackend, SpeechDecoder

    backend = LocalSpeechBackend.__new__(LocalSpeechBackend)
    backend._decode_lock = threading.Lock()
    backend.is_transcribing = False
    calls = []
    backend._transcribe_audio = lambda *args: calls.append(args) or {
        "text": "full text", "segments": [{"text": "full text", "start": 0., "end": 1.}],
    }
    decoder = SpeechDecoder(backend)
    backend._decode_lock.acquire()
    assert decoder.preview_transcribe(np.ones(100, np.float32)) is None
    assert not calls
    backend._decode_lock.release()
    segments, _ = decoder.transcribe(np.ones(100, np.float32))
    assert [segment.text for segment in segments] == ["full text"]
    assert not backend.is_transcribing


def test_disconnected_client_cannot_start_idle_work():
    admission = WorkAdmission(1, 1, 1)
    assert not admission.acquire(lambda: False, device_id="gone")
    assert admission.snapshot()["active"] == 0
    assert admission.snapshot()["queued"] == 0


def test_priority_overtakes_are_bounded_by_actual_grants_not_polling():
    from services.remote_asr.admission import _Ticket

    admission = WorkAdmission(2, 16, 3)
    assert admission.acquire(lambda: True, device_id="active")
    blocked = _Ticket("active", 1, bypasses=2)
    background = _Ticket("background", 1)
    finals = [_Ticket(f"final-{index}", 0) for index in range(10)]
    admission._queue.extend([blocked, background, *finals])
    # The aged same-device ticket is ineligible and cannot block the free slot.
    for _ in range(50):
        assert admission._next() is finals[0]
    assert background.bypasses == 0
    for index in range(2):
        assert admission._next() is finals[index]
        admission._grant(finals[index])
        assert admission._active == 2
        admission.release(finals[index].device_id)
        admission._queue.append(_Ticket(f"new-final-{index}", 0))
    assert background.bypasses == 2
    assert admission._next() is background
    # Once the original active device releases, FIFO ties among aged eligible
    # tickets preserve its earlier position, without resetting accumulated age.
    admission.release("active")
    assert admission._next() is blocked
    admission._grant(blocked)
    assert admission._next() is background
    admission._grant(background)
    admission.release("active")
    admission.release("background")


def test_native_stream_gets_turn_after_two_final_overtakes_over_tls(tmp_path):
    with _host(tmp_path, queued=6) as (host, engine, connect):
        connections = [connect()[0] for _ in range(7)]
        results = {}
        workers = [_start(connections[0], results, 100)]
        assert engine.entered.wait(3)
        workers.append(_start(connections[1], results, 200, "stream", session="native"))
        _wait(lambda: len(host._work_admission._queue) == 1)
        for index in range(2, 7):
            workers.append(_start(connections[index], results, index * 100 + 100))
            _wait(lambda: len(host._work_admission._queue) == index)
        engine.release.set()
        for worker in workers:
            worker.join(5)
            assert not worker.is_alive()
        assert engine.calls == [("transcribe", 100), ("transcribe", 300),
                                ("transcribe", 400), ("stream", 200),
                                ("transcribe", 500), ("transcribe", 600),
                                ("transcribe", 700)]
        assert results[200]["events"] == [{"text": "200", "final": False}]
        assert all(isinstance(result, dict) for result in results.values())

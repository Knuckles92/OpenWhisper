"""Bounded and retryable admission for a paired remote speech host."""

import json
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from services.remote_asr.client import (
    RemoteConnection, RemoteHostBusy, RemoteRequestError, pair_with_host,
)
from services.remote_asr.engines import HostEngine
from services.remote_asr.host import DeviceRegistry, SpeechHost
from services.remote_asr.tls import ensure_host_identity


class _BlockingEngine(HostEngine):
    identity = ("fake", "speech")

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self._lock = threading.Lock()

    def describe(self):
        return {"family": "fake", "label": "Test", "available": True,
                "streaming": True}

    def transcribe(self, audio, language):
        with self._lock:
            self.calls += 1
            number = self.calls
        if number == 1:
            self.entered.set()
            assert self.release.wait(5)
        return {"text": f"job {number}", "segments": []}

    def stream(self, session, audio, language, finish):
        return {"events": []}


def _wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _host(tmp_path, *, connections=5, active=1, queued=1):
    entries = []
    engine = _BlockingEngine()

    def save(updated):
        entries[:] = [dict(item) for item in updated]

    host = SpeechHost(
        engine_provider=lambda: engine,
        registry=DeviceRegistry(lambda: list(entries), save),
        identity=ensure_host_identity(str(tmp_path / "identity")),
        max_connections=connections,
        max_active_jobs=active,
        max_queued_jobs=queued,
    )
    host.start(port=0, bind="127.0.0.1")
    pairing = pair_with_host("127.0.0.1", host.port, host.open_pairing(), "laptop")
    assert _wait_for(lambda: host._connection_slots._value == connections)
    return host, engine, pairing


def _connection(host, pairing):
    connection = RemoteConnection(
        "127.0.0.1", host.port, pairing.token, pairing.fingerprint,
    )
    connection.connect()
    return connection


def _request(connection, results, key):
    try:
        results[key] = connection.request(
            "transcribe", audio=np.zeros(1600, dtype=np.float32), timeout=5,
        )
    except Exception as exc:
        results[key] = exc


def test_connection_cap_is_retryable_and_releases_after_disconnect(tmp_path):
    host, engine, pairing = _host(tmp_path, connections=1)
    first = second = None
    try:
        first = _connection(host, pairing)
        assert _wait_for(lambda: len(host.connected_clients()) == 1)
        second = RemoteConnection(
            "127.0.0.1", host.port, pairing.token, pairing.fingerprint,
        )
        with pytest.raises(RemoteHostBusy) as refused:
            second.connect()
        assert refused.value.retry_after_s == 1
        first.close()
        assert _wait_for(lambda: not host.connected_clients())
        assert _wait_for(lambda: host._connection_slots._value == 1)
        assert second.connect()["type"] == "ready"
    finally:
        if first is not None:
            first.close()
        if second is not None:
            second.close()
        engine.release.set()
        host.stop()


def test_connection_refusal_before_hello_preserves_retry_delay(monkeypatch):
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close
    from services.remote_asr import client, protocol

    closed = []
    refusal = Close(protocol.CLOSE_BUSY, "host busy")

    def closed_before_hello(_message):
        raise ConnectionClosedError(refusal, refusal, True)

    ws = SimpleNamespace(
        socket=object(),
        send=closed_before_hello,
        recv=lambda **_: json.dumps({
            "type": "error", "code": "busy", "message": "Host at capacity",
            "retry_after_ms": 2500,
        }),
        close=lambda: closed.append(True),
    )
    fingerprint = "AB" * 32
    monkeypatch.setattr(client, "_open", lambda *_: ws)
    monkeypatch.setattr(client, "peer_fingerprint", lambda _: fingerprint)
    connection = RemoteConnection("127.0.0.1", 47821, "token", fingerprint)
    with pytest.raises(RemoteHostBusy, match="Host at capacity") as refused:
        connection.connect()
    assert refused.value.retry_after_s == 2.5
    assert closed == [True]


def test_fifo_job_queue_rejects_overload_before_audio_decode_and_recovers(tmp_path,
                                                                          monkeypatch):
    from services.remote_asr import protocol

    host, engine, pairing = _host(tmp_path)
    connections = []
    results = {}
    original_decode = protocol.payload_audio
    decoded = []

    def decode(payload):
        decoded.append(len(payload))
        return original_decode(payload)

    monkeypatch.setattr(protocol, "payload_audio", decode)
    try:
        connections = [_connection(host, pairing) for _ in range(3)]
        first = threading.Thread(target=_request, args=(connections[0], results, "first"))
        first.start()
        assert engine.entered.wait(3)
        second = threading.Thread(target=_request, args=(connections[1], results, "second"))
        second.start()
        assert _wait_for(lambda: len(host._work_admission._queue) == 1)

        with pytest.raises(RemoteRequestError) as refused:
            connections[2].request("transcribe", audio=np.zeros(1600, dtype=np.float32))
        assert refused.value.code == "busy" and refused.value.retryable
        assert refused.value.retry_after_s == 1
        assert len(decoded) == 1 and engine.calls == 1

        engine.release.set()
        first.join(5)
        second.join(5)
        assert not first.is_alive() and not second.is_alive()
        assert results["first"]["text"] == "job 1"
        assert results["second"]["text"] == "job 2"
        assert connections[2].request(
            "transcribe", audio=np.zeros(1600, dtype=np.float32)
        )["text"] == "job 3"
    finally:
        engine.release.set()
        for connection in connections:
            connection.close()
        host.stop()


def test_disconnected_queued_client_releases_its_slot(tmp_path):
    host, engine, pairing = _host(tmp_path)
    connections = []
    results = {}
    try:
        connections = [_connection(host, pairing) for _ in range(3)]
        first = threading.Thread(target=_request, args=(connections[0], results, "first"))
        first.start()
        assert engine.entered.wait(3)
        second = threading.Thread(target=_request, args=(connections[1], results, "second"))
        second.start()
        assert _wait_for(lambda: len(host._work_admission._queue) == 1)
        connections[1].close()
        assert _wait_for(lambda: len(host._work_admission._queue) == 0)
        assert engine.calls == 1
        engine.release.set()
        first.join(5)
        second.join(5)
        assert not first.is_alive() and not second.is_alive()
        assert connections[2].request(
            "transcribe", audio=np.zeros(1600, dtype=np.float32)
        )["text"] == "job 2"
    finally:
        engine.release.set()
        for connection in connections:
            connection.close()
        host.stop()


def test_meeting_connect_busy_keeps_retry_delay(monkeypatch):
    from meeting.asr.remote import MeetingRemoteBackend, RemoteMeetingBusy
    from services.remote_asr import settings
    from transcriber.remote_backend import RemoteSpeechBackend

    pairing = SimpleNamespace(fingerprint="TEST", host_name="host")
    monkeypatch.setattr(settings, "load_client_pairing", lambda: pairing)
    monkeypatch.setattr(settings, "load_client_token", lambda: "test-token")

    def busy_connect(self, pairing, token, generation=None):
        raise RemoteHostBusy("host at capacity", retry_after_ms=2500)

    monkeypatch.setattr(RemoteSpeechBackend, "_connect", busy_connect)
    backend = MeetingRemoteBackend({"fingerprint": "TEST", "host_name": "host"})
    with pytest.raises(RemoteMeetingBusy) as refused:
        backend.ensure_ready()
    assert refused.value.retryable
    assert refused.value.retry_after_s == 2.5

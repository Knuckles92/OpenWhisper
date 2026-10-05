"""Remote engine: protocol, pairing, pinned TLS, and the client backend.

The host and client talk over a real TLS WebSocket on 127.0.0.1; only the
speech engine behind the host is fake.
"""
from __future__ import annotations

import socket
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from services.remote_asr import protocol, tailscale
from services.remote_asr.client import (
    PairingResult,
    RemoteConnection,
    RemoteConnectionLost,
    RemoteEngineError,
    pair_with_host,
    probe_host,
)
from services.remote_asr.engines import (
    HostEngine,
    UnavailableEngine,
    WhisperEngine,
    host_engine_for,
    whisper_language,
)
from services.remote_asr.host import MAX_PAIRING_FAILURES, DeviceRegistry, SpeechHost
from services.remote_asr.tls import ensure_host_identity


class FakeEngine(HostEngine):
    def __init__(self, family="parakeet", model="parakeet-v3", available=True):
        self.family = family
        self.model = model
        self.available = available
        self.calls = []
        self.canceled = []

    @property
    def identity(self):
        return ("fake", self.family, self.model)

    def describe(self):
        return {"family": self.family, "model": self.model, "label": "Fake Parakeet",
                "device": "cuda", "streaming": False, "available": self.available,
                "status": "ready" if self.available else "not loaded"}

    def transcribe(self, audio, language):
        self.calls.append(("transcribe", audio.copy(), language))
        seconds = len(audio) / 16000
        return {"text": f"heard {len(audio)}",
                "segments": [{"text": f"heard {len(audio)}", "start": 0.0, "end": seconds}]}

    def stream(self, session, audio, language, finish):
        self.calls.append(("stream", session, len(audio), finish))
        return {"events": [{"text": f"partial {len(audio)}", "final": finish}]}

    def cancel_stream(self, session):
        self.canceled.append(session)


class ListStore:
    def __init__(self):
        self.devices = []

    def load(self):
        return list(self.devices)

    def save(self, devices):
        self.devices = [dict(d) for d in devices]


_REAL_TAILSCALE_STATUS = tailscale.status


@pytest.fixture(autouse=True)
def no_real_tailscale(monkeypatch):
    """Keep every test off this computer's real tailnet."""
    monkeypatch.setattr(tailscale, "status", lambda timeout=4.0: tailscale.TailscaleStatus("not_installed"))
    monkeypatch.setattr(tailscale, "whois", lambda address, timeout=4.0: None)


@pytest.fixture
def engine():
    return FakeEngine()


@pytest.fixture
def store():
    return ListStore()


@pytest.fixture
def identity(tmp_path):
    return ensure_host_identity(str(tmp_path / "host"))


@pytest.fixture
def host(engine, store, identity):
    speech_host = SpeechHost(
        engine_provider=lambda: engine,
        registry=DeviceRegistry(store.load, store.save),
        identity=identity,
        host_name="devbox",
    )
    speech_host.start(port=0, bind="127.0.0.1")
    yield speech_host
    speech_host.stop()


def _pair(host):
    code = host.open_pairing()
    return pair_with_host("127.0.0.1", host.port, code, "laptop")


def _connect(host, result):
    connection = RemoteConnection("127.0.0.1", host.port, result.token, result.fingerprint)
    connection.connect()
    return connection


def _tone(samples: int) -> np.ndarray:
    """Audible audio: windows quieter than the silence gate never reach an engine."""
    return (0.1 * np.sin(np.arange(samples, dtype=np.float32) / 8)).astype(np.float32)


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# ---- protocol ----

def test_request_frames_round_trip_int16_audio_exactly():
    pcm = np.array([-32768, -1, 0, 1, 12345, 32767], dtype=np.int16)
    audio = pcm.astype(np.float32) / 32768.0
    header, decoded = protocol.unpack_request(
        protocol.pack_request({"id": 7, "op": "transcribe", "language": "en"}, audio)
    )
    assert header == {"id": 7, "op": "transcribe", "language": "en"}
    assert np.array_equal(decoded, audio)


def test_request_frames_reject_garbage():
    with pytest.raises(protocol.ProtocolError):
        protocol.unpack_request(b"\x00")
    with pytest.raises(protocol.ProtocolError):
        protocol.unpack_request(b"\x00\x00\x00\x09nope")
    with pytest.raises(protocol.ProtocolError):
        protocol.unpack_request("text frame")


@pytest.mark.parametrize("text, expected", [
    ("devbox", ("devbox", protocol.DEFAULT_PORT)),
    ("192.168.1.20:5000", ("192.168.1.20", 5000)),
    ("[fe80::1]:6000", ("fe80::1", 6000)),
    ("fe80::1", ("fe80::1", protocol.DEFAULT_PORT)),
    ("https://devbox.local:7000/whatever", ("devbox.local", 7000)),
])
def test_parse_address(text, expected):
    assert protocol.parse_address(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "host:notaport", "host:0", "[fe80::1"])
def test_parse_address_rejects_bad_input(text):
    with pytest.raises(ValueError):
        protocol.parse_address(text)


def test_short_fingerprint_is_five_groups():
    assert protocol.short_fingerprint("ab" * 32) == "ABAB-ABAB-ABAB-ABAB-ABAB"


def test_host_identity_is_created_once_and_reused(tmp_path):
    first = ensure_host_identity(str(tmp_path))
    second = ensure_host_identity(str(tmp_path))
    assert first.fingerprint == second.fingerprint
    assert len(first.fingerprint) == 64


# ---- pairing and authentication ----

def test_pairing_issues_a_token_the_host_stores_only_as_a_digest(host, store, identity):
    result = _pair(host)
    assert result.host_name == "devbox"
    assert result.fingerprint == identity.fingerprint
    assert len(store.devices) == 1
    assert store.devices[0]["name"] == "laptop"
    assert store.devices[0]["token_sha256"] == protocol.token_digest(result.token)
    assert result.token not in repr(store.devices)
    # The code is single use.
    assert host.pairing_status() is None


def test_wrong_codes_are_refused_and_close_pairing(host, store):
    host.open_pairing()
    for _ in range(MAX_PAIRING_FAILURES - 1):
        with pytest.raises(RemoteEngineError, match="doesn't match"):
            pair_with_host("127.0.0.1", host.port, "000000x", "laptop")
    assert host.pairing_status() is not None
    with pytest.raises(RemoteEngineError, match="Too many wrong codes"):
        pair_with_host("127.0.0.1", host.port, "000000x", "laptop")
    assert host.pairing_status() is None
    assert store.devices == []


def test_pairing_is_refused_when_not_open(host):
    with pytest.raises(RemoteEngineError, match="isn't open"):
        pair_with_host("127.0.0.1", host.port, "123456", "laptop")


def test_unknown_token_is_refused(host, identity):
    connection = RemoteConnection("127.0.0.1", host.port, "not-a-token", identity.fingerprint)
    with pytest.raises(RemoteEngineError, match="isn't paired"):
        connection.connect()


def test_a_different_certificate_is_refused_before_the_token_is_sent(host, tmp_path, store):
    result = _pair(host)
    impostor_identity = ensure_host_identity(str(tmp_path / "impostor"))
    seen_tokens = []
    impostor = SpeechHost(
        engine_provider=FakeEngine,
        registry=DeviceRegistry(lambda: seen_tokens.append("hello") or [], lambda _d: None),
        identity=impostor_identity,
    )
    impostor.start(port=0, bind="127.0.0.1")
    try:
        connection = RemoteConnection("127.0.0.1", impostor.port, result.token, result.fingerprint)
        with pytest.raises(RemoteEngineError, match="different certificate"):
            connection.connect()
        assert seen_tokens == []
    finally:
        impostor.stop()


def test_wrong_path_is_not_mistaken_for_a_host(host):
    from websockets.sync.client import connect
    from websockets.exceptions import InvalidStatus
    from services.remote_asr.tls import client_context

    with pytest.raises(InvalidStatus):
        connect(f"wss://127.0.0.1:{host.port}/other", ssl=client_context(), open_timeout=3)


# ---- requests ----

def test_transcribe_round_trip_carries_audio_and_language(host, engine):
    connection = _connect(host, _pair(host))
    try:
        assert connection.ready["engine"]["family"] == "parakeet"
        assert connection.ready["device"]["name"] == "laptop"
        audio = (np.arange(16000, dtype=np.int16) - 8000).astype(np.float32) / 32768.0
        result = connection.request("transcribe", audio=audio, language="en")
        assert result["text"] == "heard 16000"
        op, received, language = engine.calls[-1]
        assert op == "transcribe" and language == "en"
        assert np.array_equal(received, audio)
    finally:
        connection.close()


def test_streams_are_scoped_per_connection_and_canceled_on_disconnect(host, engine):
    result = _pair(host)
    first = _connect(host, result)
    second = _connect(host, result)
    try:
        first.request("stream", audio=np.zeros(800, np.float32), session="preview", finish=False)
        second.request("stream", audio=np.zeros(1600, np.float32), session="preview", finish=False)
        sessions = {call[1] for call in engine.calls if call[0] == "stream"}
        assert len(sessions) == 2
        assert all(s.endswith("-preview") for s in sessions)
    finally:
        first.close()
        second.close()
    assert _wait_for(lambda: len(engine.canceled) == 2)


def test_engine_errors_come_back_as_request_errors(host, engine):
    connection = _connect(host, _pair(host))
    try:
        with pytest.raises(RuntimeError, match="no live stream"):
            HostEngine.stream(engine, "s", np.zeros(1), None, False)
        with pytest.raises(RuntimeError, match="Unknown operation"):
            connection.request("explode")
        # The connection survives an error.
        assert connection.request("describe")["model"] == "parakeet-v3"
    finally:
        connection.close()


def test_switching_the_host_engine_tells_clients_to_reconnect(host, engine):
    connection = _connect(host, _pair(host))
    engine.model = "parakeet-other"
    with pytest.raises(RemoteConnectionLost) as raised:
        connection.request("transcribe", audio=np.zeros(160, np.float32))
    assert raised.value.sent
    assert connection.closed


def test_removing_a_device_drops_its_connection(host):
    result = _pair(host)
    connection = _connect(host, result)
    device_id = host.registry.list()[0]["id"]
    assert host.remove_device(device_id)
    with pytest.raises(RemoteConnectionLost):
        connection.request("transcribe", audio=np.zeros(160, np.float32))
    with pytest.raises(RemoteEngineError, match="isn't paired"):
        _connect(host, result)


def test_close_from_another_thread_cancels_a_waiting_request(host, engine):
    connection = _connect(host, _pair(host))
    started = threading.Event()

    def slow(audio, language):
        started.set()
        time.sleep(1.0)
        return {"text": "late", "segments": []}

    engine.transcribe = slow
    errors = []

    def run():
        try:
            connection.request("transcribe", audio=np.zeros(160, np.float32))
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    assert started.wait(3)
    connection.close()
    worker.join(5)
    assert errors and "canceled" in str(errors[0]).lower()


def test_the_keepalive_times_pongs_and_closes_on_a_missed_one(host, monkeypatch):
    from services.remote_asr import client

    monkeypatch.setattr(client, "KEEPALIVE_INTERVAL_S", 0.05)
    monkeypatch.setattr(client, "KEEPALIVE_TIMEOUT_S", 0.3)
    connection = _connect(host, _pair(host))
    # One ping goes out on connect, timed finely enough for a home network.
    assert _wait_for(lambda: connection.beats >= 2)
    assert connection.latency is not None and 0 < connection.latency < 1
    assert connection.alive
    # A host that stops answering pings is closed, as websockets' own would.
    connection._ws.ping = lambda *args, **kwargs: threading.Event()
    assert _wait_for(lambda: not connection.alive, timeout=3)
    assert connection.closed


def test_unreachable_host_reports_plainly():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    connection = RemoteConnection("127.0.0.1", port, "t", "F" * 64, timeout=2)
    with pytest.raises(RemoteEngineError, match="Couldn't reach"):
        connection.connect()


# ---- host engine adapters ----

def test_host_refuses_to_share_remote_or_cloud_engines():
    assert isinstance(host_engine_for(SimpleNamespace(is_remote=True)), UnavailableEngine)
    assert isinstance(host_engine_for(object()), UnavailableEngine)
    assert isinstance(host_engine_for(None), UnavailableEngine)
    assert not UnavailableEngine("x").describe()["available"]


def test_whisper_engine_decodes_windows_into_segments():
    segments = [SimpleNamespace(text=" Hello", start=0.0, end=0.5),
                SimpleNamespace(text=" world.", start=0.5, end=1.0)]
    calls = []

    class Model:
        def transcribe(self, audio, **options):
            calls.append(options)
            return iter(segments), SimpleNamespace(language="en")

    backend = SimpleNamespace(model=Model(), last_loaded_model="turbo", model_name="auto",
                              device="cuda", device_info="turbo | cuda", is_available=lambda: True)
    engine = WhisperEngine(backend)
    result = engine.transcribe(np.zeros(16000, np.float32), "en-US")
    assert result["text"] == "Hello world."
    assert result["segments"][1] == {"text": "world.", "start": 0.5, "end": 1.0}
    assert calls[0]["language"] == "en"
    assert engine.describe()["family"] == "local_whisper"
    assert whisper_language("auto") is None and whisper_language(None) is None


# ---- the client backend ----

@pytest.fixture
def paired_backend(host, monkeypatch):
    from services.remote_asr import settings as remote_settings
    from transcriber.remote_backend import RemoteSpeechBackend

    result = _pair(host)
    remote_settings.save_client_pairing("127.0.0.1", host.port, result)
    backend = RemoteSpeechBackend()
    yield backend
    backend.cleanup()


def test_remote_backend_adopts_the_host_engine(paired_backend):
    from transcriber.optional_backend import LocalSpeechBackend
    from config import config

    backend = paired_backend
    assert not backend.is_available()
    backend.reload_model()
    assert backend.is_available(), backend.last_error
    assert isinstance(backend, LocalSpeechBackend)
    assert backend.backend_id == "parakeet"
    assert backend.backend_id in config.INCREMENTAL_DICTATION_BACKENDS
    assert backend.model_name == "parakeet-v3"
    assert backend.name == "Fake Parakeet on devbox"
    assert "cuda" in backend.device_info
    assert not backend.is_model_missing
    assert backend.last_loaded_model is None


def test_silent_windows_never_leave_the_client(paired_backend, engine):
    backend = paired_backend
    backend.reload_model()
    assert backend.decode_window(np.zeros(16000, np.float32)) == ""
    assert not [call for call in engine.calls if call[0] == "transcribe"]


def test_remote_backend_decodes_windows_streams_and_segments(paired_backend, engine):
    backend = paired_backend
    backend.reload_model()
    audio = _tone(16000 * 2)
    assert backend.decode_window(audio) == "heard 32000"
    assert backend.transcribe_windows([(0, audio), (32000, audio[:16000])]) == ["heard 32000", "heard 16000"]
    events = backend.stream_audio("preview", audio[:1600], "en", finish=True)
    assert events == [{"text": "partial 1600", "final": True}]
    segments, info = backend.model.transcribe(audio, language="en")
    segments = list(segments)
    assert segments[0].text == "heard 32000" and segments[0].end == 2.0
    # The client's language setting rides along when none is given.
    assert engine.calls[0][2] == backend.request_language()


def test_remote_backend_reconnects_once_after_the_host_restarts(paired_backend, host):
    backend = paired_backend
    backend.reload_model()
    old = backend._process
    port = host.port
    host.stop()
    host.start(port=port, bind="127.0.0.1")
    assert backend.decode_window(_tone(160)) == "heard 160"
    assert backend.is_available()
    assert backend._process is not old and old.closed


def test_turning_sharing_off_cuts_off_connected_clients(host):
    connection = _connect(host, _pair(host))
    assert _wait_for(lambda: len(host.connected_clients()) == 1)
    host.stop()
    with pytest.raises(RemoteConnectionLost):
        connection.request("transcribe", audio=_tone(160))


def test_remote_backend_reports_a_host_without_a_ready_engine(paired_backend, engine):
    engine.available = False
    paired_backend.reload_model()
    assert not paired_backend.is_available()
    assert paired_backend.device_info == "devbox: not loaded"


def test_remote_backend_without_pairing_says_how_to_pair():
    from transcriber.remote_backend import RemoteSpeechBackend

    backend = RemoteSpeechBackend()
    backend.reload_model()
    assert not backend.is_available()
    assert "Pair with a host" in backend.device_info


def test_cancel_stops_waiting_but_keeps_the_connection(paired_backend, engine):
    backend = paired_backend
    backend.reload_model()
    connection = backend._process
    started = threading.Event()

    def slow(audio, language):
        started.set()
        time.sleep(0.6)
        return {"text": "late", "segments": []}

    engine.transcribe = slow
    errors = []

    def run():
        try:
            backend.transcribe_windows([(0, _tone(1600))])
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    assert started.wait(3)
    backend.cancel_transcription()
    worker.join(2)
    assert not worker.is_alive()
    assert errors and "canceled" in str(errors[0]).lower()
    # Nothing to reconnect: the same connection serves the next job.
    assert backend.is_available() and backend._process is connection
    with pytest.raises(RuntimeError, match="canceled"):
        backend.decode_window(_tone(160))
    backend.reset_cancel_flag()
    del engine.transcribe
    # The canceled request's late reply is skipped, not taken for this one's.
    assert backend.decode_window(_tone(320)) == "heard 320"


def test_the_next_job_clears_a_remote_engines_cancel():
    from services.runtime.transcription import TranscriptionRuntime
    from transcriber.optional_backend import LocalSpeechBackend
    from transcriber.remote_backend import RemoteSpeechBackend

    remote = RemoteSpeechBackend()
    remote.cancel_transcription()
    runtime = SimpleNamespace(controller=SimpleNamespace(current_backend=remote))
    TranscriptionRuntime._rearm_remote_engine(runtime)
    assert not remote.should_cancel
    # A local engine is reloaded after a cancel instead, which clears it.
    local = LocalSpeechBackend.__new__(LocalSpeechBackend)
    local.should_cancel = True
    runtime.controller.current_backend = local
    TranscriptionRuntime._rearm_remote_engine(runtime)
    assert local.should_cancel


# ---- the link: what the engine card shows ----

def test_link_without_a_pairing_is_unpaired():
    from transcriber.remote_backend import RemoteSpeechBackend

    backend = RemoteSpeechBackend()
    assert backend.link().state == "unpaired"
    backend.reload_model()
    link = backend.link()
    assert link.state == "unpaired" and "Pair with a host" in link.detail


def test_link_reads_connected_and_the_round_trip(paired_backend):
    backend = paired_backend
    assert backend.link().state == "offline"  # paired, not reached yet
    changes = []
    backend.on_link_changed = lambda: changes.append(backend.link().state)
    backend.reload_model()
    assert "connecting" in changes and changes[-1] == "connected"
    link = backend.link()
    assert link.host == "devbox" and link.route == "local network"
    assert link.engine_label == "Fake Parakeet" and link.device == "cuda"
    # One ping goes out on connect, so the round trip is known at once.
    assert _wait_for(lambda: backend.link().latency_ms is not None)
    assert backend.link().beat > 0


def test_link_is_busy_while_a_request_waits_and_counts_replies(paired_backend, engine):
    backend = paired_backend
    backend.reload_model()
    started, release = threading.Event(), threading.Event()

    def slow(audio, language):
        started.set()
        release.wait(3)
        return {"text": "done", "segments": []}

    engine.transcribe = slow
    worker = threading.Thread(target=lambda: backend.decode_window(_tone(1600)))
    worker.start()
    assert started.wait(3)
    assert backend.link().busy and backend.link().replies == 0
    release.set()
    worker.join(3)
    assert not backend.link().busy and backend.link().replies == 1


def test_check_link_notices_a_host_that_went_away_between_requests(paired_backend, host):
    backend = paired_backend
    backend.reload_model()
    assert not backend.check_link()
    host.stop()
    assert _wait_for(lambda: not backend._process.alive)
    assert backend.check_link()
    assert not backend.is_available()
    link = backend.link()
    assert link.state == "offline" and link.detail == "devbox stopped answering."
    assert not backend.check_link()  # reported once


def test_restore_link_reconnects_quietly_or_adopts_a_new_engine(paired_backend, host, engine):
    backend = paired_backend
    backend.reload_model()
    port = host.port
    host.stop()
    assert _wait_for(lambda: backend.check_link() or not backend.is_available())
    assert backend.restore_link() == "offline"
    assert "Couldn't reach" in backend.last_error
    host.start(port=port, bind="127.0.0.1")
    assert backend.restore_link() == "connected"
    assert backend.is_available() and backend.backend_id == "parakeet"
    assert backend.decode_window(_tone(160)) == "heard 160"
    # Back with another engine: adopted, and the caller told so.
    backend._drop(backend._process, "gone")
    engine.family, engine.model = "nemotron", "nemotron-streaming"
    assert backend.restore_link() == "changed"
    assert backend.backend_id == "nemotron" and backend.is_available()
    assert backend.restore_link() == "connected"  # nothing to do


def test_timing_splits_the_host_from_the_network(paired_backend):
    backend = paired_backend
    backend.reload_model()
    mark = backend.timing_mark()
    backend.transcribe_windows([(0, _tone(1600)), (1600, _tone(1600))])
    timing = backend.timing_since(mark)
    assert timing.host == "devbox" and timing.requests == 2
    assert timing.host_s is not None and 0 <= timing.host_s <= timing.round_trip_s
    assert timing.network_s == pytest.approx(timing.round_trip_s - timing.host_s)
    # Silent windows never leave, so they add nothing.
    mark = backend.timing_mark()
    backend.decode_window(np.zeros(1600, np.float32))
    assert backend.timing_since(mark).requests == 0


def test_a_host_that_doesnt_time_requests_gives_no_split(paired_backend, host, monkeypatch):
    backend = paired_backend
    backend.reload_model()
    original = host._dispatch

    def untimed(*args, **kwargs):
        reply = original(*args, **kwargs)
        reply.pop("host_ms", None)
        return reply

    monkeypatch.setattr(host, "_dispatch", untimed)
    mark = backend.timing_mark()
    backend.decode_window(_tone(1600))
    timing = backend.timing_since(mark)
    assert timing.requests == 1 and timing.host_s is None and timing.network_s is None


def test_the_host_says_who_it_is_decoding_for(host, engine):
    events = []
    host._on_event = lambda kind, detail: events.append((kind, dict(detail)))
    connection = _connect(host, _pair(host))
    seen = []

    def watching(audio, language):
        seen.append(host.connected_clients())
        return {"text": "ok", "segments": []}

    engine.transcribe = watching
    connection.request("transcribe", audio=_tone(160))
    assert seen[0][0]["busy"] and seen[0][0]["name"] == "laptop"
    assert not host.connected_clients()[0]["busy"]
    activity = [detail for kind, detail in events if kind == "activity"]
    assert activity == [{"name": "laptop", "busy": True}, {"name": "laptop", "busy": False}]
    connection.close()


# ---- the service ----

def test_service_starts_sharing_and_pairs_a_client(tmp_path, engine):
    from services.remote_asr.service import RemoteEngineService
    from services.settings import SettingsKey, settings_manager

    service = RemoteEngineService(lambda: None, identity_dir=str(tmp_path / "id"), bind="127.0.0.1")
    service._engine = lambda: engine
    changed = []
    service.on_client_changed = lambda: changed.append(True)
    events = []
    service.add_listener(events.append)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    settings_manager.save_setting(SettingsKey.REMOTE_HOST_PORT, port)
    service.set_host_enabled(True)
    try:
        state = service.host_state()
        assert state["running"] and state["port"] == port and not state["error"]
        code = service.open_pairing()
        pairing = service.pair(f"127.0.0.1:{port}", code)
        assert pairing.host == "127.0.0.1" and pairing.port == port
        assert service.client_pairing() == pairing
        assert changed == [True]
        assert len(service.host_state()["devices"]) == 1
        service.forget_host()
        assert service.client_pairing() is None
        assert "client" in events and "paired" in events
    finally:
        service.set_host_enabled(False)
    assert not service.host_state()["running"]


def test_service_reports_a_busy_port(tmp_path):
    from services.remote_asr.service import RemoteEngineService
    from services.settings import SettingsKey, settings_manager

    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        settings_manager.save_setting(SettingsKey.REMOTE_HOST_PORT, blocker.getsockname()[1])
        service = RemoteEngineService(lambda: None, identity_dir=str(tmp_path / "id"), bind="127.0.0.1")
        service.set_host_enabled(True)
        try:
            state = service.host_state()
            assert not state["running"]
            assert "Couldn't listen on port" in state["error"]
        finally:
            service.set_host_enabled(False)


# ---- controller integration ----

class _Signal:
    def __init__(self, name, events):
        self.name, self.events = name, events

    def emit(self, *args):
        self.events.append((self.name, *args))


def _controller(backend, events):
    """The controller's reload and readiness methods on a minimal stand-in."""
    from unittest.mock import Mock
    from services.application_controller import ApplicationController

    controller = SimpleNamespace(
        _shutting_down=False,
        _engine_lock=threading.RLock(),
        _reload_in_flight=True,
        _pending_streaming_setup=False,
        current_backend=backend,
        _current_model_name="remote",
        transcription_backends={"remote": backend},
        recorder=SimpleNamespace(is_recording=False),
        is_meeting_active=lambda: False,
        is_transcribing=lambda: False,
        ensure_local_model_available=Mock(),
        reload_whisper_model=Mock(),
        _engine_released_for_lease=False,
        _restore_after_reload=False,
        _reload_handoff_lock=threading.Lock(),
        _reload_pending=False,
        executor=Mock(),
        _remote_restoring=False,
        _remote_retry_step=0,
        _remote_retry_at=None,
        _remote_link=None,
        _remote_reported="",
        ui_controller=Mock(),
    )
    from PyQt6.QtCore import QTimer

    controller._remote_watch = QTimer()
    controller._remote_retry_timer = QTimer()
    controller._remote_retry_timer.setSingleShot(True)
    for name in ("device_info_update", "status_update", "engine_busy_changed",
                 "runtime_consent_requested", "streaming_setup_requested",
                 "remote_settled", "remote_retry_requested"):
        setattr(controller, name, _Signal(name, events))
    for name in ("_reload_worker", "_reload_selected_engine", "_finish_speech_reload",
                 "_flush_pending_streaming_setup", "transcription_readiness_message",
                 "local_whisper_loading_message", "_submit_restore_reload",
                 "release_local_engine", "restore_local_engine", "retry_remote_now",
                 "_selected_remote", "_watch_remote", "_on_remote_watch",
                 "_publish_remote_link", "_schedule_remote_retry", "_cancel_remote_retry",
                 "_on_remote_settled", "_on_local_engine_settings_changed"):
        setattr(controller, name, getattr(ApplicationController, name).__get__(controller))
    return controller


def test_an_unreachable_host_never_asks_to_install_a_local_runtime():
    from transcriber.remote_backend import RemoteSpeechBackend

    events = []
    controller = _controller(RemoteSpeechBackend(), events)
    controller._reload_worker()
    names = [event[0] for event in events]
    assert ("device_info_update", "Pair with a host in Settings → Remote engine.", False) in events
    assert "runtime_consent_requested" not in names
    assert ("engine_busy_changed", False) in events


def test_selecting_the_remote_engine_connects_without_a_warmup_decode(paired_backend, engine):
    events = []
    controller = _controller(paired_backend, events)
    controller._reload_worker()
    assert ("device_info_update", "Fake Parakeet on devbox | cuda", True) in events
    # Parakeet warms up locally, but the host warmed its own worker when it
    # loaded, so connecting decodes nothing there.
    assert engine.calls == []
    assert not controller._reload_in_flight
    # The host decides which engine this is, so the preview is set up again.
    assert ("streaming_setup_requested",) in events
    # And the link watch starts on the Qt thread once the reload settles.
    assert ("remote_settled", "reload") in events


def test_a_dropped_host_is_reported_at_once_and_retried_with_backoff(paired_backend, host):
    from services.application_controller import REMOTE_RETRY_DELAYS_S

    events = []
    controller = _controller(paired_backend, events)
    controller._reload_worker()
    controller._on_remote_settled("reload")
    assert controller._remote_watch.isActive()
    published = controller.ui_controller.set_remote_link.call_args[0][0]
    assert published.state == "connected" and published.retry_at is None
    host.stop()
    assert _wait_for(lambda: not paired_backend._process.alive)
    events.clear()
    controller._on_remote_watch()
    # Said now, not at the next recording.
    assert ("device_info_update", "devbox stopped answering.", False) in events
    assert controller._remote_retry_timer.isActive()
    assert controller._remote_retry_timer.interval() == REMOTE_RETRY_DELAYS_S[0] * 1000
    link = controller.ui_controller.set_remote_link.call_args[0][0]
    assert link.state == "offline" and link.retry_at is not None
    # Each failure waits longer, up to the last delay, which repeats.
    controller._remote_retry_timer.stop()
    for delay in (*REMOTE_RETRY_DELAYS_S[1:], REMOTE_RETRY_DELAYS_S[-1]):
        controller._on_remote_settled("offline")
        assert controller._remote_retry_timer.interval() == delay * 1000
        controller._remote_retry_timer.stop()


def test_a_quiet_reconnect_restores_the_engine_card(paired_backend, host):
    events = []
    controller = _controller(paired_backend, events)
    controller._reload_worker()
    port = host.port
    host.stop()
    assert _wait_for(lambda: not paired_backend._process.alive)
    controller._on_remote_watch()
    host.start(port=port, bind="127.0.0.1")
    events.clear()
    controller._on_remote_settled(paired_backend.restore_link())
    assert ("device_info_update", "Fake Parakeet on devbox | cuda", True) in events
    assert not controller._remote_retry_timer.isActive() and controller._remote_retry_step == 0
    # The preview may have been waiting for this connection.
    assert ("streaming_setup_requested",) in events


def test_repeat_failures_dont_clear_the_status_line_every_time():
    from transcriber.remote_backend import RemoteSpeechBackend

    backend = RemoteSpeechBackend()
    backend.host_name = "devbox"
    backend._unpaired = False
    backend.last_error = "Couldn't reach devbox:47821."
    events = []
    controller = _controller(backend, events)
    controller._on_remote_settled("offline")
    controller._remote_retry_timer.stop()
    controller._on_remote_settled("offline")
    reports = [event for event in events if event[0] == "device_info_update"]
    assert reports == [("device_info_update", "Couldn't reach devbox:47821.", False)]


def test_an_unpaired_remote_is_not_retried():
    from transcriber.remote_backend import RemoteSpeechBackend

    backend = RemoteSpeechBackend()
    backend.reload_model()
    controller = _controller(backend, [])
    controller._on_remote_settled("reload")
    assert not controller._remote_retry_timer.isActive()


def test_a_meeting_leaves_the_remote_connection_alone(paired_backend, engine):
    events = []
    controller = _controller(paired_backend, events)
    controller._reload_worker()
    connection = paired_backend._process
    assert controller.release_local_engine() is False
    assert paired_backend._process is connection and paired_backend.is_available()
    assert not any(event[0] == "device_info_update" and "Released" in event[1] for event in events)
    controller.restore_local_engine()
    assert engine.calls == []  # no reconnect, so no decode either


def test_local_engine_settings_leave_a_remote_engine_alone(paired_backend):
    controller = _controller(paired_backend, [])
    controller._on_local_engine_settings_changed()
    controller.reload_whisper_model.assert_not_called()


def _preview_runtime(backend):
    from unittest.mock import Mock
    from services.runtime.streaming import StreamingRuntime

    controller = SimpleNamespace(
        current_backend=backend,
        streaming_transcriber=None,
        _streaming_backend=None,
        _streaming_enabled=False,
        _pending_streaming_setup=False,
        recorder=SimpleNamespace(is_recording=False),
        ui_controller=Mock(),
    )
    return StreamingRuntime(controller), controller


def test_live_preview_waits_for_the_host_then_decodes_there(paired_backend, engine):
    from services.settings import SettingsKey, settings_manager
    from services.streaming_transcriber import StreamingTranscriber

    settings_manager.save_setting(SettingsKey.STREAMING_ENABLED, True)
    runtime, controller = _preview_runtime(paired_backend)
    # Selecting the engine reconfigures the preview before the reload connects.
    runtime.reconfigure_streaming()
    assert controller.streaming_transcriber is None
    assert controller._pending_streaming_setup
    controller.ui_controller.set_status.assert_not_called()

    paired_backend.reload_model()
    controller._pending_streaming_setup = False
    runtime.setup_streaming()
    preview = controller.streaming_transcriber
    assert isinstance(preview, StreamingTranscriber) and preview.backend is paired_backend
    assert controller._streaming_enabled

    preview.sample_rate = 16000
    preview._process_incremental_chunk([_tone(16000 * 3)])
    assert preview.preview_text.startswith("heard ")
    assert [call[0] for call in engine.calls] == ["transcribe"]


def test_a_remote_reload_keeps_a_fitting_preview_and_rebuilds_a_stale_one(paired_backend, engine):
    from services.settings import SettingsKey, settings_manager

    settings_manager.save_setting(SettingsKey.STREAMING_ENABLED, True)
    runtime, controller = _preview_runtime(paired_backend)
    paired_backend.reload_model()
    runtime.setup_streaming()
    first = controller.streaming_transcriber

    paired_backend.reload_model()
    runtime.setup_streaming()
    assert controller.streaming_transcriber is first

    engine.model = "parakeet-v2"
    paired_backend.reload_model()
    controller.recorder.is_recording = True
    runtime.setup_streaming()
    assert controller.streaming_transcriber is first, "a recording keeps its preview"
    controller.recorder.is_recording = False
    runtime.setup_streaming()
    assert controller.streaming_transcriber is not first
    assert controller.streaming_transcriber.backend is paired_backend


def test_a_host_without_a_preview_engine_says_so(paired_backend, engine):
    from services.settings import SettingsKey, settings_manager

    settings_manager.save_setting(SettingsKey.STREAMING_ENABLED, True)
    engine.family = "local_whisper"
    runtime, controller = _preview_runtime(paired_backend)
    paired_backend.reload_model()
    runtime.reconfigure_streaming()
    assert controller.streaming_transcriber is None
    controller.ui_controller.set_status.assert_called_once_with(
        "Live preview needs Parakeet or Nemotron Streaming on devbox"
    )
    # The engine card grays out Live preview with the same words.
    from services.runtime.streaming import preview_unavailable_reason

    assert preview_unavailable_reason("remote", "devbox", "local_whisper") == (
        controller.ui_controller.set_status.call_args.args[0]
    )


# ---- choosing the host's model ----

_HOST_MODELS = [
    {"family": "parakeet", "model": "parakeet-v3", "label": "Parakeet TDT 0.6B v3"},
    {"family": "nemotron", "model": "nemotron-3.5", "label": "Nemotron 3.5 ASR 0.6B"},
    {"family": "local_whisper", "model": "small", "label": "Whisper small"},
]


@pytest.fixture
def switchable(host, engine):
    """The host lists models and switches its fake engine the way the app does."""
    chosen = []

    def select(family, model, device_name):
        chosen.append((family, model, device_name))
        if model == "busy":
            raise RuntimeError("devbox is transcribing right now. Try again in a moment.")
        engine.family, engine.model = family, model
        return engine.describe()

    host._models = lambda: [dict(entry) for entry in _HOST_MODELS]
    host._select_model = select
    return chosen


def test_ready_lists_the_models_the_host_can_switch_to(host, switchable):
    connection = _connect(host, _pair(host))
    try:
        assert connection.ready["models"] == _HOST_MODELS
    finally:
        connection.close()


def test_select_model_switches_the_host_and_answers_with_the_new_engine(host, engine, switchable):
    connection = _connect(host, _pair(host))
    try:
        result = connection.request("select_model", family="nemotron", model="nemotron-3.5")
        assert (result["engine"]["family"], result["engine"]["model"]) == ("nemotron", "nemotron-3.5")
        assert result["models"] == _HOST_MODELS
        assert switchable == [("nemotron", "nemotron-3.5", "laptop")]
        # This connection was opened on the engine the host ran before.
        with pytest.raises(RemoteConnectionLost):
            connection.request("transcribe", audio=_tone(160))
    finally:
        connection.close()


def test_select_model_follows_a_host_side_switch_and_passes_refusals_on(host, engine, switchable):
    connection = _connect(host, _pair(host))
    try:
        engine.model = "switched-on-the-host"
        with pytest.raises(RuntimeError, match="transcribing right now"):
            connection.request("select_model", family="parakeet", model="busy")
        assert not connection.closed
        with pytest.raises(RuntimeError, match="Choose a model"):
            connection.request("select_model", family="parakeet")
    finally:
        connection.close()


def test_a_host_that_cannot_switch_lists_nothing_and_refuses(host):
    connection = _connect(host, _pair(host))
    try:
        assert connection.ready["models"] == []
        with pytest.raises(RuntimeError, match="doesn't let paired computers"):
            connection.request("select_model", family="parakeet", model="parakeet-v3")
    finally:
        connection.close()


def test_host_models_are_the_downloaded_ones_with_a_runtime_here(monkeypatch):
    from services import components, hf_access
    from services.local_asr import cache
    from services.remote_asr.engines import host_models

    monkeypatch.setattr(components, "is_installed", lambda component: component.startswith("asr-nvidia"))
    monkeypatch.setattr(cache, "is_cached", lambda key: key in ("parakeet-v3", "qwen-0.6b"))
    small = hf_access.resolve_model_repo("small")
    monkeypatch.setattr(hf_access, "scan_cached_models", lambda max_age_seconds=0.0: {small: object()})
    # Qwen is downloaded but its runtime isn't installed; Nemotron's runtime
    # is, but its model isn't downloaded.
    assert host_models() == [
        {"family": "parakeet", "model": "parakeet-v3", "label": "Parakeet TDT 0.6B v3"},
        {"family": "local_whisper", "model": "small", "label": "Whisper small"},
    ]


def _switching_service(monkeypatch, engine, load=None):
    """A service whose switch loads on another thread, as the app's reload does."""
    from services.remote_asr import service as service_module
    from services.remote_asr.service import RemoteEngineService

    monkeypatch.setattr(service_module, "host_models", lambda: [dict(e) for e in _HOST_MODELS])
    monkeypatch.setattr(service_module, "_SWITCH_POLL_S", 0.01)
    state = {"settled": True, "switches": [], "refuse": ""}

    def finish(family, model):
        time.sleep(0.05)
        if load is not None:
            load(family, model)
        else:
            engine.family, engine.model = family, model
        state["settled"] = True

    def switch(family, model, device_name):
        state["switches"].append((family, model, device_name))
        if state["refuse"]:
            raise RuntimeError(state["refuse"])
        state["settled"] = False
        threading.Thread(target=finish, args=(family, model), daemon=True).start()

    service = RemoteEngineService(
        lambda: None, switch_engine=switch, engine_settled=lambda: state["settled"],
    )
    service._engine = lambda: engine
    return service, state


def test_service_answers_a_switch_once_the_new_model_has_loaded(monkeypatch, engine):
    service, state = _switching_service(monkeypatch, engine)
    described = service.select_model("nemotron", "nemotron-3.5", "laptop")
    assert (described["family"], described["model"]) == ("nemotron", "nemotron-3.5")
    assert state["switches"] == [("nemotron", "nemotron-3.5", "laptop")]
    assert service.host_models() == _HOST_MODELS


def test_service_refuses_what_isnt_ready_and_skips_what_already_runs(monkeypatch, engine):
    service, state = _switching_service(monkeypatch, engine)
    with pytest.raises(RuntimeError, match="isn't ready on"):
        service.select_model("qwen_asr", "qwen-1.7b", "laptop")
    assert service.select_model("parakeet", "parakeet-v3", "laptop")["model"] == "parakeet-v3"
    assert state["switches"] == []


def test_service_reports_a_refused_or_failed_switch(monkeypatch, engine):
    def fail(family, model):
        engine.family, engine.model, engine.available = family, model, False

    service, state = _switching_service(monkeypatch, engine, load=fail)
    with pytest.raises(RuntimeError, match="not loaded"):
        service.select_model("local_whisper", "small", "laptop")
    state["refuse"] = "devbox is running a meeting."
    with pytest.raises(RuntimeError, match="running a meeting"):
        service.select_model("parakeet", "parakeet-v3", "laptop")


def test_a_service_without_a_switch_offers_no_models():
    from services.remote_asr.service import RemoteEngineService

    assert RemoteEngineService(lambda: None).host_models() == []


def test_model_choices_follow_the_connection(paired_backend, switchable):
    from transcriber.remote_backend import RemoteModels

    assert paired_backend.model_choices() == RemoteModels(host="devbox")
    paired_backend.reload_model()
    choices = paired_backend.model_choices()
    assert choices.host == "devbox"
    assert [m.key for m in choices.models] == [(e["family"], e["model"]) for e in _HOST_MODELS]
    assert choices.current.key == ("parakeet", "parakeet-v3")
    paired_backend.cleanup()
    assert paired_backend.model_choices().current is None


def test_choosing_a_host_model_switches_the_host_and_reconnects_to_it(paired_backend, switchable):
    backend = paired_backend
    backend.reload_model()
    assert backend.request_model("local_whisper", "small").label == "Whisper small"
    backend.reload_model()
    assert switchable == [("local_whisper", "small", "laptop")]
    assert backend.is_available(), backend.last_error
    assert (backend.backend_id, backend.model_name) == ("local_whisper", "small")
    assert backend.current_model.key == ("local_whisper", "small")
    assert backend.switch_error == ""
    # The choice rides on one reload only.
    backend.reload_model()
    assert len(switchable) == 1


def test_a_host_that_keeps_its_model_says_why(paired_backend, switchable):
    backend = paired_backend
    backend.request_model("parakeet", "busy")
    backend.reload_model()
    assert backend.is_available()
    assert backend.model_name == "parakeet-v3"
    assert backend.switch_error == "devbox is transcribing right now. Try again in a moment."


def test_a_host_from_before_model_switching(paired_backend, host, monkeypatch):
    send = SpeechHost._send

    def old_send(ws, message):
        if message.get("type") == "ready":
            message = {key: value for key, value in message.items() if key != "models"}
        send(ws, message)

    monkeypatch.setattr(SpeechHost, "_send", staticmethod(old_send))
    monkeypatch.setattr(host, "_switch_model", lambda request_id, header, device_name: {
        "id": request_id, "error": "Unknown operation: 'select_model'",
    })
    backend = paired_backend
    backend.reload_model()
    choices = backend.model_choices()
    assert choices.models is None and choices.current.key == ("parakeet", "parakeet-v3")
    backend.request_model("nemotron", "nemotron-3.5")
    backend.reload_model()
    assert backend.is_available()
    assert backend.switch_error == "Update OpenWhisper on devbox to choose its model from here."


def test_the_models_are_forgotten_when_the_host_is_out_of_reach(paired_backend, host, switchable):
    paired_backend.reload_model()
    assert paired_backend.host_models
    host.stop()
    paired_backend.reload_model()
    assert paired_backend.host_models is None
    assert paired_backend.model_choices().current is None


def test_cleanup_ends_a_switch_the_host_is_still_loading(paired_backend, host, engine, switchable):
    started = threading.Event()

    def slow(family, model, device_name):
        started.set()
        time.sleep(3)
        return engine.describe()

    host._select_model = slow
    paired_backend.request_model("nemotron", "nemotron-3.5")
    worker = threading.Thread(target=paired_backend.reload_model)
    worker.start()
    assert started.wait(5)
    began = time.monotonic()
    paired_backend.cleanup()
    worker.join(5)
    assert not worker.is_alive()
    assert time.monotonic() - began < 2
    assert not paired_backend.is_available()
    assert paired_backend.last_error == ""


def _client_controller(backend, events):
    from unittest.mock import Mock
    from services.application_controller import ApplicationController

    controller = _controller(backend, events)
    controller._reload_in_flight = False
    controller._reload_note = ""
    controller.ui_controller = Mock()
    for name in ("select_remote_model", "remote_models"):
        setattr(controller, name, getattr(ApplicationController, name).__get__(controller))
    return controller


def test_choosing_a_model_on_the_client_reloads_through_the_host(paired_backend, switchable):
    events = []
    controller = _client_controller(paired_backend, events)
    controller._reload_worker()
    assert controller.remote_models().current.key == ("parakeet", "parakeet-v3")
    controller.select_remote_model("parakeet", "parakeet-v3")
    controller.reload_whisper_model.assert_not_called()

    controller.select_remote_model("local_whisper", "small")
    controller.reload_whisper_model.assert_called_once()
    assert controller._reload_note == "Switching devbox to Whisper small..."
    controller._reload_worker()
    assert switchable == [("local_whisper", "small", "laptop")]
    assert controller.remote_models().current.key == ("local_whisper", "small")

    controller.select_remote_model("parakeet", "busy")
    controller._reload_worker()
    assert ("status_update", "devbox is transcribing right now. Try again in a moment.") in events


def test_the_client_wont_switch_the_host_mid_recording(paired_backend, switchable):
    events = []
    controller = _client_controller(paired_backend, events)
    controller._reload_worker()
    controller.recorder.is_recording = True
    controller.select_remote_model("nemotron", "nemotron-3.5")
    assert ("status_update", "Finish recording before changing the engine") in events
    controller.ui_controller.refresh_remote_models.assert_called_once()
    controller.reload_whisper_model.assert_not_called()
    assert paired_backend._requested is None


def _host_controller(current="parakeet"):
    """The host's switch methods on a stand-in whose main window selects backends."""
    from unittest.mock import Mock
    from config import config
    from services.application_controller import ApplicationController

    controller = SimpleNamespace(
        _current_model_name=current,
        _reload_pending=False,
        _reload_note="",
        recorder=SimpleNamespace(is_recording=False),
        meeting=False,
        transcription_backends={},
        ui_controller=Mock(),
        reload_whisper_model=Mock(),
    )
    controller.is_meeting_active = lambda: controller.meeting
    controller.is_transcribing = lambda: False

    def select(display):
        controller._current_model_name = config.MODEL_VALUE_MAP[display]

    controller.ui_controller.select_transcription_backend.side_effect = select
    for name in ("_switch_engine_to", "_switch_engine_for_client", "_on_client_model_switch"):
        setattr(controller, name, getattr(ApplicationController, name).__get__(controller))
    controller.client_model_switch_requested = SimpleNamespace(emit=controller._on_client_model_switch)
    return controller


def test_the_host_switches_as_if_the_model_were_picked_there():
    from services.settings import SettingsKey, settings_manager

    controller = _host_controller()
    controller._switch_engine_for_client("nemotron", "nemotron-3.5", "laptop")
    assert settings_manager.load_all_settings()[SettingsKey.LOCAL_ASR_MODELS]["nemotron"] == "nemotron-3.5"
    controller.ui_controller.select_transcription_backend.assert_called_once_with("Nemotron Streaming")
    controller.ui_controller.refresh_local_engine_controls.assert_called_once()
    controller.reload_whisper_model.assert_called_once()
    assert controller._reload_note == "Switching to Nemotron 3.5 ASR 0.6B for laptop..."


def test_the_host_changes_whisper_size_without_changing_backend():
    from services.settings import SettingsKey, settings_manager

    controller = _host_controller(current="local_whisper")
    controller._switch_engine_for_client("local_whisper", "small", "laptop")
    assert settings_manager.load_all_settings()[SettingsKey.WHISPER_MODEL] == "small"
    controller.ui_controller.select_transcription_backend.assert_not_called()
    controller.reload_whisper_model.assert_called_once()


def test_the_host_refuses_a_switch_while_busy_or_for_a_model_it_cant_run():
    controller = _host_controller()
    controller.meeting = True
    with pytest.raises(RuntimeError, match="running a meeting"):
        controller._switch_engine_for_client("nemotron", "nemotron-3.5", "laptop")
    controller.meeting = False
    controller.recorder.is_recording = True
    with pytest.raises(RuntimeError, match="transcribing right now"):
        controller._switch_engine_for_client("nemotron", "nemotron-3.5", "laptop")
    controller.recorder.is_recording = False
    with pytest.raises(RuntimeError, match="can't run"):
        controller._switch_engine_for_client("parakeet", "nemotron-3.5", "laptop")
    controller.ui_controller.select_transcription_backend.assert_not_called()
    controller.reload_whisper_model.assert_not_called()


def test_a_queued_reload_is_unsettled_until_it_starts_and_ends():
    from unittest.mock import Mock
    from services.application_controller import ApplicationController

    events = []
    controller = SimpleNamespace(
        _reload_in_flight=False,
        _reload_pending=False,
        _reload_note="Switching to Whisper small for laptop...",
        _reload_timer=Mock(),
        recorder=SimpleNamespace(is_recording=False),
        is_meeting_active=lambda: False,
        is_transcribing=lambda: False,
        executor=Mock(),
        _reload_worker=Mock(),
    )
    for name in ("status_update", "engine_busy_changed", "reload_debounce_requested"):
        setattr(controller, name, _Signal(name, events))
    for name in ("reload_whisper_model", "_do_reload_whisper_model", "_engine_settled"):
        setattr(controller, name, getattr(ApplicationController, name).__get__(controller))
    assert controller._engine_settled()
    controller.reload_whisper_model()
    assert not controller._engine_settled()
    controller._do_reload_whisper_model()
    assert not controller._engine_settled()
    assert ("status_update", "Switching to Whisper small for laptop...") in events
    assert controller._reload_note == ""
    controller._reload_in_flight = False
    assert controller._engine_settled()


def test_readiness_retries_a_remote_engine_that_could_not_connect():
    from transcriber.remote_backend import RemoteSpeechBackend

    backend = RemoteSpeechBackend()
    backend.last_error = "Couldn't reach devbox:47821."
    events = []
    controller = _controller(backend, events)
    controller._reload_in_flight = False
    message = controller.transcription_readiness_message()
    assert message == "Couldn't reach devbox:47821. Trying again..."
    # A quiet reconnect, handed to the Qt thread (hotkeys call this off it),
    # rather than a full engine reload.
    assert ("remote_retry_requested",) in events
    controller.reload_whisper_model.assert_not_called()


def test_remote_is_a_valid_saved_engine():
    from config import config
    from services.settings import settings_manager

    assert config.MODEL_VALUE_MAP["Remote computer"] == "remote"
    settings_manager.save_model_selection("remote")
    assert settings_manager.load_model_selection() == "remote"


# ---- settings page ----

def test_settings_page_pairs_with_a_host_and_lists_the_device(tmp_path, engine):
    from PyQt6.QtWidgets import QApplication
    from services.remote_asr.service import RemoteEngineService
    from services.settings import SettingsKey, settings_manager
    from ui_qt.dialogs.settings_destinations import REMOTE_ENGINE
    from ui_qt.dialogs.settings_dialog import SettingsDialog
    from ui_qt.dialogs.settings_remote import REMOTE_ENGINE_DISPLAY
    from ui_qt.widgets import WrappedLabel

    service = RemoteEngineService(lambda: None, identity_dir=str(tmp_path / "id"), bind="127.0.0.1")
    service._engine = lambda: engine
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    settings_manager.save_setting(SettingsKey.REMOTE_HOST_PORT, port)
    selected = []
    dialog = SettingsDialog(get_loaded_model=lambda: None, background_cache_scan=False)
    try:
        section = dialog.remote_section
        section.bind(service, selected.append)
        dialog.select_destination(REMOTE_ENGINE)
        # Unpaired: the search comes first, typing an address second.
        assert not section.find_tile.isHidden() and section.client_tile.isHidden()
        assert section.pair_row.isHidden()
        section.manual_button.click()
        assert not section.pair_row.isHidden() and section.manual_button.isHidden()
        assert not section.pair_device_button.isEnabled()

        section.share_tile.checkbox.setChecked(True)
        QApplication.processEvents()
        assert service.host_state()["running"]
        assert "Sharing Fake Parakeet on cuda" in section.host_status.text()

        # Nothing is paired yet, so turning sharing on has a code ready.
        code = service.host_state()["pairing"][0]
        assert section.pairing_code_label.text() == f"{code[:3]} {code[3:]}"
        assert section.pair_device_button.isHidden()

        section.address_edit.setText(f"127.0.0.1:{port}")
        section.code_edit.setText(code)
        section.pair_button.click()
        assert _wait_for(lambda: (QApplication.processEvents() or True) and not section._pairing_busy, 10)
        assert section.client_message.text().startswith("Paired with ")
        assert service.client_pairing() is not None
        assert section.paired_row.isVisibleTo(section.client_tile)
        assert section.find_tile.isHidden() and not section.client_tile.isHidden()
        labels = section.devices_list.findChildren(WrappedLabel)
        assert any("paired" in label.text() for label in labels)

        section.use_button.click()
        assert selected == [REMOTE_ENGINE_DISPLAY]
        assert dialog.rail.value(REMOTE_ENGINE).startswith("Sharing · 1 paired")

        section.forget_button.click()
        assert service.client_pairing() is None
        section.share_tile.checkbox.setChecked(False)
        QApplication.processEvents()
        assert not service.host_state()["running"]
    finally:
        service.shutdown()
        dialog.close()
        dialog.deleteLater()


# ---- tailscale ----

OWNER = "owner@example.com"


def _status_json():
    """``tailscale status --json`` trimmed to the fields read, with made-up names."""
    return {
        "BackendState": "Running",
        "Self": {"HostName": "LAPTOP-1", "DNSName": "laptop.tail0000.ts.net.", "UserID": 1,
                 "TailscaleIPs": ["100.64.0.1", "fd7a:115c:a1e0::1"]},
        "CurrentTailnet": {"Name": OWNER, "MagicDNSSuffix": "tail0000.ts.net"},
        "User": {"1": {"LoginName": OWNER}, "2": {"LoginName": "friend@example.com"},
                 "3": {"LoginName": "tagged-devices"}},
        "Peer": {
            "a": {"HostName": "devbox", "DNSName": "devbox.tail0000.ts.net.", "OS": "linux",
                  "Online": True, "UserID": 1, "TailscaleIPs": ["100.64.0.2", "fd7a:115c:a1e0::2"]},
            "b": {"HostName": "friends-pc", "DNSName": "friends-pc.tail0000.ts.net.", "OS": "windows",
                  "Online": True, "UserID": 2, "TailscaleIPs": ["100.64.0.3"]},
            "c": {"HostName": "gpu-server", "DNSName": "gpu.tail0000.ts.net.", "OS": "linux",
                  "Online": True, "UserID": 3, "Tags": ["tag:server"], "TailscaleIPs": ["100.64.0.4"]},
            "d": {"HostName": "localhost", "DNSName": "phone.tail0000.ts.net.", "OS": "iOS",
                  "Online": True, "UserID": 1, "TailscaleIPs": ["100.64.0.5"]},
            "e": {"HostName": "Shared Mac", "DNSName": "mac.other.ts.net.", "OS": "macOS",
                  "Online": False, "UserID": 1, "ShareeNode": True, "TailscaleIPs": ["100.64.0.6"]},
        },
    }


def test_tailscale_status_marks_only_the_owners_untagged_computers_as_mine():
    status = tailscale.parse_status(_status_json())
    assert status.running and status.owner == OWNER
    assert (status.name, status.address) == ("laptop", "100.64.0.1")
    peers = {peer.name: peer for peer in status.peers}
    assert peers["devbox"].mine and peers["devbox"].is_desktop
    assert peers["devbox"].address == "100.64.0.2"  # IPv4, not the ULA
    assert not peers["friends-pc"].mine and peers["friends-pc"].owner == "friend@example.com"
    assert not peers["gpu"].mine and peers["gpu"].tagged and peers["gpu"].owner == ""
    assert not peers["phone"].is_desktop
    assert not peers["mac"].mine  # shared in from someone else's tailnet
    assert status.peers[0].name == "devbox"  # the owner's own computers first


@pytest.mark.parametrize("state, expected", [
    ("Stopped", "stopped"), ("NeedsLogin", "needs_login"), ("Starting", "starting"),
])
def test_tailscale_status_states(state, expected):
    status = tailscale.parse_status({**_status_json(), "BackendState": state})
    assert status.state == expected and not status.running


def test_tailscale_status_never_raises(monkeypatch):
    monkeypatch.setattr(tailscale, "status", _REAL_TAILSCALE_STATUS)
    monkeypatch.setattr(tailscale, "find_cli", lambda: None)
    assert tailscale.status().state == "not_installed"
    monkeypatch.setattr(tailscale, "find_cli", lambda: "tailscale")

    def failing(args, timeout):
        raise tailscale.TailscaleError("failed to connect to local tailscaled")

    monkeypatch.setattr(tailscale, "_run_json", failing)
    status = tailscale.status()
    assert status.state == "unavailable" and "tailscaled" in status.detail


def test_tailscale_cli_is_found_where_the_windows_installer_puts_it(monkeypatch, tmp_path):
    exe = tmp_path / "Tailscale" / "tailscale.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    monkeypatch.setattr(tailscale.shutil, "which", lambda name: None)
    monkeypatch.setattr(tailscale.sys, "platform", "win32")
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    assert tailscale.find_cli() == str(exe)


@pytest.mark.parametrize("address, expected", [
    ("100.64.0.2", True), ("100.127.255.254", True), ("100.128.0.1", False),
    ("192.168.1.20", False), ("fd7a:115c:a1e0::9636:1603", True), ("::ffff:100.64.0.2", True),
    ("fe80::1%eth0", False), ("devbox.local", False), ("", False),
])
def test_is_tailscale_address(address, expected):
    assert tailscale.is_tailscale_address(address) is expected


def test_own_device_needs_tailnet_addresses_the_same_owner_and_no_tags(monkeypatch):
    identities = {
        "100.64.0.2": tailscale.PeerIdentity("devbox", OWNER, False),
        "100.64.0.3": tailscale.PeerIdentity("friends-pc", "friend@example.com", False),
        "100.64.0.4": tailscale.PeerIdentity("gpu", "", True),
    }
    looked_up = []
    monkeypatch.setattr(tailscale, "whois", lambda address, timeout=4.0: (
        looked_up.append(address) or identities.get(address)
    ))
    local = "100.64.0.1"
    assert tailscale.own_device("100.64.0.2", local, OWNER).node == "devbox"
    assert tailscale.own_device("100.64.0.3", local, OWNER) is None
    assert tailscale.own_device("100.64.0.4", local, OWNER) is None
    assert tailscale.own_device("100.64.0.2", local, "") is None
    # Arriving on a LAN interface doesn't count, whatever the source claims.
    assert tailscale.own_device("100.64.0.2", "192.168.1.5", OWNER) is None
    assert tailscale.own_device("192.168.1.9", local, OWNER) is None
    assert looked_up == ["100.64.0.2", "100.64.0.3", "100.64.0.4"]


@pytest.fixture
def tailnet_host(engine, store, identity, monkeypatch):
    """A host whose owner's Tailscale computers pair without a code.

    Connections come from 127.0.0.1 here, so the Tailscale identity check is
    replaced; ``accept`` decides whether it recognizes the caller.
    """
    decision = {"accept": True, "calls": []}

    def own_device(remote, local, owner):
        decision["calls"].append((remote, local, owner))
        return tailscale.PeerIdentity("laptop", owner, False) if decision["accept"] else None

    monkeypatch.setattr(tailscale, "own_device", own_device)
    owner = {"login": OWNER}
    speech_host = SpeechHost(
        engine_provider=lambda: engine,
        registry=DeviceRegistry(store.load, store.save),
        identity=identity,
        host_name="devbox",
        tailscale_owner=lambda: owner["login"],
        addresses=lambda: ["192.0.2.10", "100.64.0.2"],
    )
    speech_host.start(port=0, bind="127.0.0.1")
    speech_host.decision = decision
    speech_host.owner = owner
    yield speech_host
    speech_host.stop()


def test_probe_answers_without_a_token_and_reveals_no_secrets(tailnet_host):
    result = probe_host("127.0.0.1", tailnet_host.port)
    assert result.host_name == "devbox" and result.compatible
    assert result.engine == {"label": "Fake Parakeet", "device": "cuda",
                             "available": True, "family": "parakeet"}
    assert result.tailscale_pairing is True
    assert tailnet_host.connected_clients() == []
    tailnet_host.owner["login"] = ""
    assert probe_host("127.0.0.1", tailnet_host.port).tailscale_pairing is False


def test_owners_tailscale_computer_pairs_without_a_code(tailnet_host, store):
    result = pair_with_host("127.0.0.1", tailnet_host.port, None, "laptop", tailscale=True)
    assert result.via == "tailscale" and result.token
    assert result.alternates == ("192.0.2.10", "100.64.0.2")
    assert tailnet_host.decision["calls"] == [("127.0.0.1", "127.0.0.1", OWNER)]
    assert store.devices[0]["via"] == "tailscale"
    # The token works like any other.
    connection = _connect(tailnet_host, result)
    assert connection.request("transcribe", audio=_tone(160))["text"] == "heard 160"
    connection.close()


def test_tailscale_pairing_is_refused_for_anyone_else(tailnet_host, store):
    tailnet_host.decision["accept"] = False
    with pytest.raises(RemoteEngineError, match="owner's own Tailscale computers"):
        pair_with_host("127.0.0.1", tailnet_host.port, None, "laptop", tailscale=True)
    assert store.devices == []


def test_tailscale_pairing_is_refused_when_the_host_turned_it_off(tailnet_host, store):
    tailnet_host.owner["login"] = ""
    with pytest.raises(RemoteEngineError, match="doesn't pair over Tailscale without a code"):
        pair_with_host("127.0.0.1", tailnet_host.port, None, "laptop", tailscale=True)
    assert tailnet_host.decision["calls"] == []
    assert store.devices == []


def test_code_pairing_still_works_and_carries_the_hosts_addresses(tailnet_host, store):
    code = tailnet_host.open_pairing()
    result = pair_with_host("127.0.0.1", tailnet_host.port, code, "laptop")
    assert result.via == "code" and result.alternates == ("192.0.2.10", "100.64.0.2")
    assert store.devices[0]["via"] == "code"


def test_pairing_keeps_the_hosts_other_addresses():
    from services.remote_asr import settings as remote_settings

    result = PairingResult(token="t", device_id="d", host_name="devbox", fingerprint="AB" * 32,
                           alternates=("192.168.1.20", "100.64.0.2"), via="code")
    pairing = remote_settings.save_client_pairing("192.168.1.20", 47821, result)
    assert pairing.alternates == ("100.64.0.2",)
    loaded = remote_settings.load_client_pairing()
    assert loaded == pairing
    assert not loaded.over_tailscale and loaded.tailscale_fallback == "100.64.0.2"

    over = remote_settings.save_client_pairing(
        "100.64.0.2", 47821,
        PairingResult(token="t", device_id="d", host_name="devbox", fingerprint="AB" * 32,
                      alternates=("192.168.1.20",), via="tailscale"),
    )
    assert over.over_tailscale and over.tailscale_fallback == "" and over.via == "tailscale"


def test_connection_skips_a_stranger_at_the_first_address_without_sending_the_token(
        host, engine, tmp_path, store):
    """A laptop away from home: its LAN address now belongs to someone else."""
    result = _pair(host)
    stranger = SpeechHost(
        engine_provider=lambda: FakeEngine(),
        registry=DeviceRegistry(ListStore().load, ListStore().save),
        identity=ensure_host_identity(str(tmp_path / "stranger")),
        host_name="stranger",
    )
    heard, tokens = [], []
    real_handle = stranger._handle
    stranger._handle = lambda ws: heard.append(ws.remote_address) or real_handle(ws)
    stranger.registry.authenticate = lambda token: tokens.append(token)
    try:
        stranger.start(port=host.port, bind="127.0.0.2")
    except OSError:
        pytest.skip("127.0.0.2 is not usable on this machine")
    try:
        connection = RemoteConnection("127.0.0.2", host.port, result.token, result.fingerprint,
                                      alternates=("127.0.0.1",))
        ready = connection.connect()
        assert ready["host"]["name"] == "devbox"
        assert connection.host == "127.0.0.1"
        assert connection.request("transcribe", audio=_tone(160))["text"] == "heard 160"
        connection.close()
        # The stranger saw a TLS handshake and nothing else: no hello, no token.
        assert _wait_for(lambda: len(heard) == 1)
        time.sleep(0.2)
        assert tokens == [] and stranger.connected_clients() == []

        # The address that worked is tried first from now on.
        attempts = []
        again = RemoteConnection("127.0.0.2", host.port, result.token, result.fingerprint,
                                 alternates=("127.0.0.1",))
        real_connect_to = again._connect_to
        again._connect_to = lambda where, timeout: attempts.append(where) or real_connect_to(where, timeout)
        again.connect()
        assert attempts == ["127.0.0.1"]
        again.close()
    finally:
        stranger.stop()
        RemoteConnection._last_good.clear()


def test_connection_reports_the_paired_address_when_nothing_answers(host):
    result = _pair(host)
    host.stop()
    connection = RemoteConnection("127.0.0.1", host.port, result.token, result.fingerprint,
                                  timeout=1.0)
    with pytest.raises(RemoteEngineError, match=r"127\.0\.0\.1"):
        connection.connect()


def _tailnet_status(host_address="127.0.0.1"):
    peer = tailscale.TailscalePeer(
        name="devbox", dns_name="devbox.tail0000.ts.net", address=host_address, os="linux",
        online=True, owner=OWNER, tagged=False, mine=True,
    )
    phone = tailscale.TailscalePeer(
        name="phone", dns_name="phone.tail0000.ts.net", address="100.64.0.5", os="iOS",
        online=True, owner=OWNER, tagged=False, mine=True,
    )
    offline = tailscale.TailscalePeer(
        name="old-pc", dns_name="old-pc.tail0000.ts.net", address="100.64.0.7", os="windows",
        online=False, owner=OWNER, tagged=False, mine=True,
    )
    quiet = tailscale.TailscalePeer(
        name="nas", dns_name="nas.tail0000.ts.net", address="100.64.0.8", os="linux",
        online=True, owner=OWNER, tagged=False, mine=True,
    )
    return tailscale.TailscaleStatus(
        "running", name="laptop", dns_name="laptop.tail0000.ts.net", address="100.64.0.1",
        owner=OWNER, tailnet=OWNER, peers=(peer, phone, offline, quiet),
    )


def test_scan_tailnet_probes_online_desktops_and_keeps_those_that_answer(tailnet_host, monkeypatch):
    from services.remote_asr import client as client_module
    from services.remote_asr.service import RemoteEngineService

    monkeypatch.setattr(tailscale, "status", lambda timeout=4.0: _tailnet_status())
    probed = []
    real_probe = client_module.probe_host

    def probe(address, port, **kw):
        probed.append(address)
        if address != "127.0.0.1":
            raise RemoteEngineError("nothing listening")
        return real_probe(address, port, **kw)

    monkeypatch.setattr(client_module, "probe_host", probe)
    service = RemoteEngineService(lambda: None, bind="127.0.0.1")
    scan = service.scan_tailnet(port=tailnet_host.port)
    assert scan.status.running
    assert sorted(probed) == ["100.64.0.8", "127.0.0.1"]  # not the phone, not the offline PC
    assert [h.peer.name for h in scan.hosts] == ["devbox"]
    found = scan.hosts[0]
    assert found.host_name == "devbox" and found.engine["label"] == "Fake Parakeet"
    assert found.can_pair_without_code


def test_scan_tailnet_without_tailscale_probes_nothing(monkeypatch):
    from services.remote_asr import client as client_module
    from services.remote_asr.service import RemoteEngineService

    monkeypatch.setattr(client_module, "probe_host", lambda *a, **k: pytest.fail("probed"))
    scan = RemoteEngineService(lambda: None, bind="127.0.0.1").scan_tailnet()
    assert scan.status.state == "not_installed" and scan.hosts == ()


def test_service_trusts_the_tailscale_owner_only_while_the_setting_is_on(monkeypatch):
    from services.remote_asr.service import RemoteEngineService

    monkeypatch.setattr(tailscale, "status", lambda timeout=4.0: _tailnet_status())
    service = RemoteEngineService(lambda: None, bind="127.0.0.1")
    assert service._trusted_tailscale_owner() == OWNER
    service.set_tailscale_trust(False)
    assert service._trusted_tailscale_owner() == ""
    service.set_tailscale_trust(True)
    assert "100.64.0.1" in service._host_addresses()


def test_service_status_check_never_blocks_the_caller(monkeypatch):
    from services.remote_asr.service import RemoteEngineService

    gate = threading.Event()

    def slow_status(timeout=4.0):
        gate.wait(5)
        return _tailnet_status()

    monkeypatch.setattr(tailscale, "status", slow_status)
    service = RemoteEngineService(lambda: None, bind="127.0.0.1")
    events = []
    service.add_listener(events.append)
    started = time.monotonic()
    assert service.tailscale_status() is None
    assert time.monotonic() - started < 0.5
    gate.set()
    assert _wait_for(lambda: "tailscale" in events)
    assert service.tailscale_status().owner == OWNER


def test_settings_page_connects_to_a_tailnet_computer_without_a_code(tmp_path, engine, monkeypatch):
    from PyQt6.QtWidgets import QApplication
    from services.remote_asr.service import RemoteEngineService
    from services.settings import SettingsKey, settings_manager
    from ui_qt.dialogs.settings_destinations import REMOTE_ENGINE
    from ui_qt.dialogs.settings_dialog import SettingsDialog
    from ui_qt.widgets import PrimaryButton, WrappedLabel

    def pump_until(predicate, timeout=10):
        return _wait_for(lambda: (QApplication.processEvents() or True) and predicate(), timeout)

    service = RemoteEngineService(lambda: None, identity_dir=str(tmp_path / "id"), bind="127.0.0.1")
    service._engine = lambda: engine
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    settings_manager.save_setting(SettingsKey.REMOTE_HOST_PORT, port)
    monkeypatch.setattr(tailscale, "status", lambda timeout=4.0: _tailnet_status())
    monkeypatch.setattr(tailscale, "own_device",
                        lambda remote, local, owner: tailscale.PeerIdentity("laptop", owner, False))
    from services.remote_asr import service as service_module
    monkeypatch.setattr(service_module.protocol, "DEFAULT_PORT", port)
    dialog = SettingsDialog(get_loaded_model=lambda: None, background_cache_scan=False)
    try:
        section = dialog.remote_section
        section.bind(service, lambda name: None)
        # This computer shares too, and plays the tailnet peer "devbox".
        section.share_tile.checkbox.setChecked(True)
        assert pump_until(lambda: service.host_state()["running"])
        dialog.show()
        dialog.select_destination(REMOTE_ENGINE)
        assert pump_until(lambda: section._scan is not None and not section._scan_busy)
        assert not section.find_tile.isHidden()
        assert "Signed in to Tailscale as owner@example.com" in section.find_tile.description_label.text()
        rows = [label.text() for label in section.find_list.findChildren(WrappedLabel)]
        assert rows == ["devbox · Fake Parakeet on cuda · your computer"]
        assert not section.tailscale_tile.isHidden()
        assert section.tailscale_tile.checkbox.isChecked()
        assert "and on Tailscale as laptop (100.64.0.1)" in section.host_status.text()

        connect = [b for b in section.find_list.findChildren(PrimaryButton) if b.text() == "Connect"]
        assert len(connect) == 1
        connect[0].click()
        assert pump_until(lambda: not section._pairing_busy)
        pairing = service.client_pairing()
        assert pairing is not None and pairing.via == "tailscale", section.client_message.text()
        assert "through your Tailscale account" in section.client_message.text()
        assert section.find_tile.isHidden()
        # Paired over Tailscale already works away from home.
        assert section.tailscale_hint.isHidden()
        labels = section.devices_list.findChildren(WrappedLabel)
        assert any("over Tailscale" in label.text() for label in labels)

        section.tailscale_tile.checkbox.setChecked(False)
        assert settings_manager.load_all_settings()[SettingsKey.REMOTE_HOST_TAILSCALE_TRUST] is False
    finally:
        service.shutdown()
        dialog.close()
        dialog.deleteLater()


def test_settings_page_explains_how_to_get_tailscale(tmp_path):
    """Found nothing here: Tailscale is offered for a computer on another network."""
    from PyQt6.QtWidgets import QApplication
    from services.remote_asr.service import RemoteEngineService
    from ui_qt.dialogs.settings_destinations import REMOTE_ENGINE
    from ui_qt.dialogs.settings_dialog import SettingsDialog
    from ui_qt.widgets import WrappedLabel

    service = RemoteEngineService(lambda: None, identity_dir=str(tmp_path / "id"), bind="127.0.0.1")
    dialog = SettingsDialog(get_loaded_model=lambda: None, background_cache_scan=False)
    try:
        section = dialog.remote_section
        section.bind(service, lambda name: None)
        dialog.show()
        dialog.select_destination(REMOTE_ENGINE)
        assert _wait_for(lambda: (QApplication.processEvents() or True)
                         and section._scan is not None and not section._scan_busy, 10)
        assert section.find_tile.description_label.text() == (
            "Computers on this network that share their engine."
        )
        notes = [label.text() for label in section.find_list.findChildren(WrappedLabel)]
        assert notes[0].startswith("None found.")
        assert any("On a different network?" in note and "https://tailscale.com/download" in note
                   for note in notes)
        assert not section.sweep_button.isHidden()
        assert section.tailscale_tile.isHidden()
    finally:
        service.shutdown()
        dialog.close()
        dialog.deleteLater()

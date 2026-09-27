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


def test_remote_backend_cancel_closes_the_connection(paired_backend):
    backend = paired_backend
    backend.reload_model()
    backend.cancel_transcription()
    assert not backend.is_available()
    with pytest.raises(RuntimeError, match="canceled"):
        backend.decode_window(_tone(160))


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
        executor=Mock(),
    )
    for name in ("device_info_update", "status_update", "engine_busy_changed",
                 "runtime_consent_requested", "streaming_setup_requested"):
        setattr(controller, name, _Signal(name, events))
    for name in ("_reload_worker", "_reload_selected_engine", "_finish_speech_reload",
                 "_flush_pending_streaming_setup", "transcription_readiness_message",
                 "local_whisper_loading_message", "_submit_restore_reload"):
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


def test_selecting_the_remote_engine_connects_and_warms_the_host(paired_backend, engine):
    events = []
    controller = _controller(paired_backend, events)
    controller._reload_worker()
    assert ("device_info_update", "Fake Parakeet on devbox | cuda", True) in events
    # Parakeet is a warmup engine, so the reload sent one throwaway decode.
    assert [call[0] for call in engine.calls] == ["transcribe"]
    assert not controller._reload_in_flight


def test_readiness_retries_a_remote_engine_that_could_not_connect():
    from transcriber.remote_backend import RemoteSpeechBackend

    backend = RemoteSpeechBackend()
    backend.last_error = "Couldn't reach devbox:47821."
    controller = _controller(backend, [])
    controller._reload_in_flight = False
    message = controller.transcription_readiness_message()
    assert message == "Couldn't reach devbox:47821. Trying again..."
    controller.reload_whisper_model.assert_called_once()


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
        assert not section.pair_row.isHidden()
        assert not section.pair_device_button.isEnabled()

        section.share_tile.checkbox.setChecked(True)
        QApplication.processEvents()
        assert service.host_state()["running"]
        assert section.pair_device_button.isEnabled()
        assert "Sharing Fake Parakeet on cuda" in section.host_status.text()

        section.pair_device_button.click()
        code = service.host_state()["pairing"][0]
        assert section.pairing_code_label.text() == f"{code[:3]} {code[3:]}"

        section.address_edit.setText(f"127.0.0.1:{port}")
        section.code_edit.setText(code)
        section.pair_button.click()
        assert _wait_for(lambda: (QApplication.processEvents() or True) and not section._pairing_busy, 10)
        assert section.client_message.text().startswith("Paired with ")
        assert service.client_pairing() is not None
        assert section.paired_row.isVisibleTo(section.client_tile)
        assert not section.pair_row.isVisibleTo(section.client_tile)
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
        assert not section.tailnet_tile.isHidden()
        assert "Signed in to Tailscale as owner@example.com" in section.tailnet_tile.description_label.text()
        rows = [label.text() for label in section.tailnet_list.findChildren(WrappedLabel)]
        assert rows == ["devbox · Fake Parakeet on cuda · your computer"]
        assert not section.tailscale_tile.isHidden()
        assert section.tailscale_tile.checkbox.isChecked()
        assert "and on Tailscale as laptop (100.64.0.1)" in section.host_status.text()

        connect = [b for b in section.tailnet_list.findChildren(PrimaryButton) if b.text() == "Connect"]
        assert len(connect) == 1
        connect[0].click()
        assert pump_until(lambda: not section._pairing_busy)
        pairing = service.client_pairing()
        assert pairing is not None and pairing.via == "tailscale", section.client_message.text()
        assert "through your Tailscale account" in section.client_message.text()
        assert section.tailnet_tile.isHidden()
        labels = section.devices_list.findChildren(WrappedLabel)
        assert any("over Tailscale" in label.text() for label in labels)

        section.tailscale_tile.checkbox.setChecked(False)
        assert settings_manager.load_all_settings()[SettingsKey.REMOTE_HOST_TAILSCALE_TRUST] is False
    finally:
        service.shutdown()
        dialog.close()
        dialog.deleteLater()


def test_settings_page_explains_how_to_get_tailscale(tmp_path):
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
        assert "With Tailscale on both computers" in section.tailnet_tile.description_label.text()
        notes = [label.text() for label in section.tailnet_list.findChildren(WrappedLabel)]
        assert any("https://tailscale.com/download" in note for note in notes)
        assert section.tailscale_tile.isHidden()
    finally:
        service.shutdown()
        dialog.close()
        dialog.deleteLater()

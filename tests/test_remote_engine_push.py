"""A host tells paired computers when its engine changes, and says what it shares.

On the Arch laptop in the report, DESKTOP-LS3A1VL paired while the host ran
Parakeet; the host moved to Whisper turbo on the GPU and nothing told the
client until its next request. The host now closes idle connections still
on the old engine, which the client's link watch notices and reconnects
from, and the share line names the compute type like the main window.
Real TLS WebSockets on 127.0.0.1; only the engine is fake.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from services.remote_asr import tailscale
from services.remote_asr.client import RemoteConnection, pair_with_host
from services.remote_asr.engines import HostEngine, SpeechWorkerEngine, WhisperEngine
from services.remote_asr.host import DeviceRegistry, SpeechHost
from services.remote_asr.tls import ensure_host_identity


class SwitchableEngine(HostEngine):
    def __init__(self):
        self.model = "parakeet-v3"
        self.release = threading.Event()
        self.release.set()
        self.started = threading.Event()

    @property
    def identity(self):
        return ("fake", self.model)

    def describe(self):
        return {"family": "fake", "model": self.model, "label": self.model,
                "device": "cuda", "streaming": False, "available": True, "status": "ready"}

    def transcribe(self, audio, language):
        self.started.set()
        self.release.wait(5)
        return {"text": "ok", "segments": []}


class ListStore:
    def __init__(self):
        self.devices = []

    def load(self):
        return list(self.devices)

    def save(self, devices):
        self.devices = [dict(d) for d in devices]


@pytest.fixture(autouse=True)
def no_real_tailscale(monkeypatch):
    monkeypatch.setattr(tailscale, "status", lambda timeout=4.0: tailscale.TailscaleStatus("not_installed"))
    monkeypatch.setattr(tailscale, "whois", lambda address, timeout=4.0: None)


@pytest.fixture
def engine():
    return SwitchableEngine()


@pytest.fixture
def host(engine, tmp_path):
    store = ListStore()
    speech_host = SpeechHost(
        engine_provider=lambda: engine,
        registry=DeviceRegistry(store.load, store.save),
        identity=ensure_host_identity(str(tmp_path / "host")),
        host_name="jed",
    )
    speech_host.start(port=0, bind="127.0.0.1")
    yield speech_host
    speech_host.stop()


def _connected(host) -> RemoteConnection:
    result = pair_with_host("127.0.0.1", host.port, host.open_pairing(), "DESKTOP-LS3A1VL")
    connection = RemoteConnection("127.0.0.1", host.port, result.token, result.fingerprint)
    connection.connect()
    assert _wait_for(lambda: len(host.connected_clients()) == 1)
    return connection


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _tone():
    return (0.1 * np.sin(np.arange(16000, dtype=np.float32) / 8)).astype(np.float32)


def test_an_idle_client_is_told_the_engine_changed(host, engine):
    connection = _connected(host)
    try:
        assert host.engine_changed() == 0  # same engine: nothing to say

        engine.model = "turbo"
        assert host.engine_changed() == 1

        assert _wait_for(lambda: not connection.alive)
        assert connection.closed_for_engine_change
    finally:
        connection.close()


def test_a_reconnect_hands_the_client_the_new_engine(host, engine):
    connection = _connected(host)
    engine.model = "turbo"
    host.engine_changed()
    assert _wait_for(lambda: not connection.alive)
    connection.close()

    again = _connected(host)
    try:
        assert again.ready["engine"]["model"] == "turbo"
    finally:
        again.close()


def test_a_client_in_the_middle_of_a_request_keeps_its_connection(host, engine):
    connection = _connected(host)
    engine.release.clear()
    replies = []
    worker = threading.Thread(
        target=lambda: replies.append(connection.request("transcribe", audio=_tone())),
        daemon=True,
    )
    try:
        worker.start()
        assert engine.started.wait(3)
        engine.model = "turbo"

        assert host.engine_changed() == 0

        engine.release.set()
        worker.join(5)
        assert replies and replies[0]["text"] == "ok"
    finally:
        engine.release.set()
        connection.close()


def test_the_client_says_the_host_switched_rather_than_stopped(monkeypatch):
    from transcriber.remote_backend import RemoteSpeechBackend

    backend = RemoteSpeechBackend()
    closed = []
    backend._process = SimpleNamespace(
        alive=False, closed_for_engine_change=True, where="jed:47821",
        close=lambda: closed.append(True),
    )
    backend.host_name = "jed"

    assert backend.check_link()

    assert backend.last_error == "jed switched engines. Reconnecting..."
    assert closed


def test_whisper_identity_counts_the_device_and_compute_type():
    backend = SimpleNamespace(last_loaded_model="turbo", model_name="turbo", device="cpu",
                              compute_type="int8", is_available=lambda: True,
                              device_info="turbo | cpu (int8)")
    engine = WhisperEngine(backend)
    on_cpu = engine.identity

    backend.device, backend.compute_type = "cuda", "int8_float32"

    assert engine.identity != on_cpu
    assert engine.describe()["compute_type"] == "int8_float32"


def test_worker_identity_counts_the_device():
    backend = SimpleNamespace(backend_id="parakeet", model_name="parakeet-v3", device="cpu")
    engine = SpeechWorkerEngine(backend)
    on_cpu = engine.identity

    backend.device = "cuda"

    assert engine.identity != on_cpu


def test_share_line_names_the_compute_type():
    from ui_qt.dialogs.settings_remote import _engine_phrase

    assert _engine_phrase({"label": "Whisper turbo", "device": "cuda",
                           "compute_type": "int8_float32"}) == "Whisper turbo on cuda (int8_float32)"
    # Engines without one (and hosts from before) read as they did.
    assert _engine_phrase({"label": "Parakeet v3", "device": "cpu"}) == "Parakeet v3 on cpu"


def test_service_refreshes_the_page_and_pushes_the_change(tmp_path, monkeypatch):
    from services.remote_asr.service import RemoteEngineService

    service = RemoteEngineService(lambda: None, identity_dir=str(tmp_path / "id"), bind="127.0.0.1")
    heard = []
    service.add_listener(heard.append)
    pushed = threading.Event()
    service._host = SimpleNamespace(running=True, engine_changed=pushed.set)

    service.engine_changed()

    assert heard == ["engine"]
    assert pushed.wait(2)


def test_host_state_labels_a_vpn_address(tmp_path, monkeypatch):
    from services import lan_address
    from services.remote_asr.service import RemoteEngineService

    service = RemoteEngineService(lambda: None, identity_dir=str(tmp_path / "id"), bind="127.0.0.1")
    service._host = SimpleNamespace(
        running=True, port=47821, identity=SimpleNamespace(fingerprint="AB"),
        pairing_status=lambda: None, connected_clients=lambda: [],
        registry=SimpleNamespace(list=lambda: []),
        pending_request=lambda: None, discovery=SimpleNamespace(error=""),
    )
    service._engine = lambda: SimpleNamespace(describe=lambda: {})
    monkeypatch.setattr(
        lan_address, "best_lan_address",
        lambda: lan_address.LocalAddress("10.2.0.2", "proton0", lan_address.VPN),
    )

    state = service.host_state()

    assert (state["address"], state["address_kind"]) == ("10.2.0.2", "vpn")
    # And it isn't handed to clients as a LAN address to fall back on.
    assert "10.2.0.2" not in service._host_addresses()

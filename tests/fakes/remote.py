"""A real remote speech host on 127.0.0.1 with a fake engine behind it.

Shared by the remote engine tests: the fakes, the pairing helpers, and the
fixtures (import the fixtures a module needs; ``no_real_tailscale`` is autouse
wherever it is imported).
"""
from __future__ import annotations

import time

import numpy as np
import pytest

from services.remote_asr import tailscale
from services.remote_asr.client import RemoteConnection, pair_with_host
from services.remote_asr.engines import HostEngine
from services.remote_asr.host import DeviceRegistry, SpeechHost
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
